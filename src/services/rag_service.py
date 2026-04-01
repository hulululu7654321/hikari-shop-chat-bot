# -*- coding: utf-8 -*-
"""
B 端 RAG 服务

职责：
1. 知识库管理：添加/更新/删除条目（支持飞书同步、本地文件、手动填写）
2. 向量化存储：豆包 Embedding → Qdrant（对齐重构架构，废弃 Python 层余弦）
3. 语义检索：用户消息 → Qdrant 语义检索 → 注入对话 context
4. 触发判断：关键词命中才检索，平时不耗成本

数据隔离：每条知识条目带 tenant_id，不同品牌方的知识库物理隔离。
集合：COL_KB（core_kb）
"""

import asyncio
import hashlib
import io
import os
import traceback
from datetime import datetime, timezone
from typing import Any, Optional

from openai import AsyncOpenAI

from src.utils.database import get_db_manager, COL_KB, COL_FACTS
from src.utils.qdrant_client import get_qdrant, QDRANT_COLLECTION, _fact_id_to_int
from src.utils.logger import get_logger
from src.utils.api_retry import with_retry

logger = get_logger(__name__)

# ── 触发关键词 ────────────────────────────────────────────────────────────────
RAG_TRIGGERS = [
    # 产品推荐类
    "推荐", "买", "多少钱", "价格", "哪里买", "有没有", "活动", "优惠", "库存",
    "哪款", "好用吗", "值得买", "入手", "下单",
    # 知识问答类
    "怎么", "如何", "什么是", "哪个好", "区别", "对比", "适合",
    # 课程/排班类
    "课程", "排班", "几点", "上课", "时间", "报名", "预约",
    # 主动种草（画像匹配注入，不依赖用户说）
    "__rag_proactive__",
]

DEFAULT_TENANT = "hikari_default"


def _make_ark_client():
    """火山 Ark SDK，用于 multimodal_embeddings（与 fact_extractor 完全一致）。"""
    from volcenginesdkarkruntime import Ark
    return Ark(
        base_url="https://ark.cn-beijing.volces.com/api/v3",
        api_key=os.getenv("DOUBAO_API_KEY"),
    )


class RAGService:
    """知识库 RAG 服务，支持多租户隔离。向量存储使用 Qdrant。"""

    def __init__(self) -> None:
        self.db = get_db_manager().db
        self.col = self.db[COL_KB]
        self.embed_model = os.getenv("LLM_EMBEDDING_MODEL", "")
        self._index_ensured = False
        logger.info("rag_service initialized")

    # ── 索引保障 ──────────────────────────────────────────────────────────────

    async def ensure_indexes(self) -> None:
        """确保 MongoDB 索引存在，启动时由 main.py 调用一次。"""
        if self._index_ensured:
            return
        try:
            await self.col.create_index([("tenant_id", 1), ("is_active", 1)])
            await self.col.create_index([("tenant_id", 1), ("category", 1)])
            await self.col.create_index([("source_id", 1)], sparse=True)
            self._index_ensured = True
            logger.info("rag_service: indexes ensured")
        except Exception as e:
            logger.warning(f"rag_service: index creation failed (non-fatal): {e}")

    # ── Embedding ─────────────────────────────────────────────────────────────

    async def _embed(self, text: str) -> list[float]:
        """
        用火山 Ark SDK multimodal_embeddings 向量化文本。
        与 fact_extractor._get_multimodal_embedding 完全对齐，保证向量空间一致。
        """
        ark = _make_ark_client()
        res = await asyncio.to_thread(
            ark.multimodal_embeddings.create,
            model=self.embed_model,
            input=[{"type": "text", "text": text[:2000]}],
        )
        if hasattr(res, "data"):
            if isinstance(res.data, list):
                return res.data[0].embedding
            elif hasattr(res.data, "embedding"):
                return res.data.embedding
        if hasattr(res, "embedding"):
            return res.embedding
        raise RuntimeError("embedding structure parse failed")

    # ── 写入 ──────────────────────────────────────────────────────────────────

    async def upsert_entry(
        self,
        content: str,
        title: str = "",
        category: str = "custom",
        metadata: Optional[dict] = None,
        source: str = "manual",
        source_id: str = "",
        tenant_id: str = DEFAULT_TENANT,
    ) -> str:
        """
        写入或更新一条知识条目。
        source_id 相同时 upsert（飞书增量同步用）。
        同时写入 Qdrant 向量（point ID = _fact_id_to_int(str(_id))，uint64）。
        返回条目的 MongoDB _id 字符串。
        """
        try:
            embedding = await self._embed(content)
            now = datetime.now(timezone.utc)

            doc = {
                "tenant_id": tenant_id,
                "category": category,
                "title": title or content[:30],
                "content": content,
                "metadata": metadata or {},
                "source": source,
                "source_id": source_id,
                "is_active": True,
                "updated_at": now,
            }

            if source_id:
                result = await self.col.update_one(
                    {"tenant_id": tenant_id, "source_id": source_id},
                    {"$set": doc, "$setOnInsert": {"created_at": now}},
                    upsert=True,
                )
                existing = await self.col.find_one(
                    {"tenant_id": tenant_id, "source_id": source_id}, {"_id": 1}
                )
                oid = result.upserted_id or existing["_id"]
            else:
                doc["created_at"] = now
                result = await self.col.insert_one(doc)
                oid = result.inserted_id

            str_oid = str(oid)

            # 同步写入 Qdrant（payload 保留 tenant 和 category 供过滤）
            qdrant = get_qdrant()
            from qdrant_client.models import PointStruct
            point_id = _fact_id_to_int(str_oid)

            def _do_upsert():
                qdrant.upsert(
                    collection_name=QDRANT_COLLECTION,
                    points=[
                        PointStruct(
                            id=point_id,
                            vector=embedding,
                            payload={
                                "mongo_id": str_oid,
                                "tenant_id": tenant_id,
                                "category": category,
                                "title": title or content[:30],
                                "content": content,
                                "metadata": metadata or {},
                                "is_active": True,
                                "updated_at": now.isoformat(),
                            },
                        )
                    ],
                )

            await asyncio.to_thread(_do_upsert)

            logger.info(
                f"rag_service: entry upserted | tenant={tenant_id} category={category} id={str_oid[:8]}"
            )
            return str_oid

        except Exception:
            logger.error(f"rag_service: upsert_entry failed: {traceback.format_exc()}")
            raise

    async def upsert_batch(
        self, entries: list[dict], tenant_id: str = DEFAULT_TENANT
    ) -> int:
        """
        批量写入，并发最多 8 个（防限流）。
        返回成功写入条数。
        """
        semaphore = asyncio.Semaphore(8)

        async def _one(entry: dict) -> bool:
            async with semaphore:
                try:
                    await self.upsert_entry(tenant_id=tenant_id, **entry)
                    return True
                except Exception as e:
                    logger.warning(f"rag_service: batch single entry failed: {e}")
                    return False

        results = await asyncio.gather(*[_one(e) for e in entries])
        success = sum(1 for r in results if r)
        logger.info(f"rag_service: batch done | {success}/{len(entries)} succeeded")
        return success

    # ── 飞书同步 ──────────────────────────────────────────────────────────────

    async def sync_from_feishu_bitable(
        self,
        app_token: str,
        table_id: str,
        category: str,
        field_template: Optional[str] = None,
        title_field: str = "",
        metadata_fields: Optional[list[str]] = None,
        tenant_id: str = DEFAULT_TENANT,
    ) -> int:
        """从飞书多维表格同步知识条目。"""
        from src.utils.feishu_helper import get_feishu_helper

        helper = get_feishu_helper()
        records = await helper.fetch_bitable(app_token, table_id)
        if not records:
            logger.warning(f"rag_service: feishu bitable empty | table={table_id[:8]}")
            return 0

        entries = []
        for record in records:
            content = helper.bitable_record_to_text(record, field_template)
            if not content.strip():
                continue
            if title_field and title_field in record:
                title = str(record[title_field])
            else:
                title = next(
                    (str(v) for k, v in record.items() if not k.startswith("_") and v),
                    content[:20],
                )
            meta = {}
            if metadata_fields:
                for f in metadata_fields:
                    if f in record:
                        meta[f] = record[f]
            entries.append(
                {
                    "content": content,
                    "title": title,
                    "category": category,
                    "metadata": meta,
                    "source": "feishu_bitable",
                    "source_id": f"{table_id}_{record.get('_record_id', '')}",
                }
            )

        return await self.upsert_batch(entries, tenant_id=tenant_id)

    async def sync_from_feishu_doc(
        self,
        doc_token: str,
        category: str = "faq",
        chunk_size: int = 300,
        tenant_id: str = DEFAULT_TENANT,
    ) -> int:
        """从飞书文档同步知识条目，按段落切片后向量化。"""
        from src.utils.feishu_helper import get_feishu_helper

        helper = get_feishu_helper()
        full_text = await helper.fetch_doc(doc_token)
        if not full_text.strip():
            return 0

        paragraphs = [p.strip() for p in full_text.split("\n") if p.strip()]
        chunks, current = [], ""
        for para in paragraphs:
            if len(current) + len(para) < chunk_size:
                current = f"{current}\n{para}" if current else para
            else:
                if current:
                    chunks.append(current)
                current = para
        if current:
            chunks.append(current)

        entries = [
            {
                "content": chunk,
                "title": chunk[:30],
                "category": category,
                "metadata": {"doc_token": doc_token, "chunk_index": i},
                "source": "feishu_doc",
                "source_id": f"{doc_token}_chunk_{i}_{hashlib.md5(chunk.encode()).hexdigest()[:8]}",
            }
            for i, chunk in enumerate(chunks)
        ]
        return await self.upsert_batch(entries, tenant_id=tenant_id)

    async def sync_from_file(
        self,
        file_bytes: bytes,
        filename: str,
        category: str = "custom",
        chunk_size: int = 300,
        tenant_id: str = DEFAULT_TENANT,
    ) -> int:
        """从上传的本地文件同步（支持 .md / .txt / .pdf）。"""
        text = ""
        if filename.endswith(".pdf"):
            try:
                import pdfplumber

                with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
                    text = "\n".join(page.extract_text() or "" for page in pdf.pages)
            except ImportError:
                logger.error("rag_service: pdfplumber not installed, cannot parse PDF")
                return 0
        else:
            text = file_bytes.decode("utf-8", errors="ignore")

        if not text.strip():
            return 0

        file_hash = hashlib.md5(file_bytes).hexdigest()[:12]
        paragraphs = [p.strip() for p in text.split("\n") if p.strip()]
        chunks, current = [], ""
        for para in paragraphs:
            if len(current) + len(para) < chunk_size:
                current = f"{current}\n{para}" if current else para
            else:
                if current:
                    chunks.append(current)
                current = para
        if current:
            chunks.append(current)

        entries = [
            {
                "content": chunk,
                "title": chunk[:30],
                "category": category,
                "metadata": {"filename": filename},
                "source": "file",
                "source_id": f"{file_hash}_chunk_{i}",
            }
            for i, chunk in enumerate(chunks)
        ]
        return await self.upsert_batch(entries, tenant_id=tenant_id)

    # ── 检索 ──────────────────────────────────────────────────────────────────

    def should_trigger(self, text: str) -> bool:
        """判断本条消息是否需要走 RAG 检索。"""
        return any(kw in text for kw in RAG_TRIGGERS)

    async def retrieve(
        self,
        query: str,
        tenant_id: str = DEFAULT_TENANT,
        top_k: int = 3,
        category: Optional[str] = None,
    ) -> list[dict]:
        """
        Qdrant 语义检索最相关的知识条目。
        按 tenant_id 过滤，score 阈值 0.6，返回 top_k 条。
        """
        try:
            query_vec = await self._embed(query)
            qdrant = get_qdrant()

            from qdrant_client.models import Filter, FieldCondition, MatchValue, Must

            must_conditions = [
                FieldCondition(key="tenant_id", match=MatchValue(value=tenant_id)),
                FieldCondition(key="is_active", match=MatchValue(value=True)),
            ]
            if category:
                must_conditions.append(
                    FieldCondition(key="category", match=MatchValue(value=category))
                )

            hits = await asyncio.to_thread(
                qdrant.search,
                collection_name=QDRANT_COLLECTION,
                query_vector=query_vec,
                query_filter=Filter(must=must_conditions),
                limit=top_k,
                score_threshold=0.6,
                with_payload=True,
            )

            results = []
            for hit in hits:
                p = hit.payload or {}
                results.append(
                    {
                        "entry_id": str(hit.id),
                        "title": p.get("title", ""),
                        "content": p.get("content", ""),
                        "category": p.get("category", ""),
                        "metadata": p.get("metadata", {}),
                        "score": round(hit.score, 3),
                    }
                )

            logger.info(
                f"rag_service: retrieve | tenant={tenant_id} query={query[:20]!r} hits={len(results)}"
            )
            return results

        except Exception:
            logger.error(f"rag_service: retrieve failed: {traceback.format_exc()}")
            return []

    def format_context(self, results: list[dict]) -> str:
        """把检索结果格式化成注入 Prompt 的 context 文本。"""
        if not results:
            return ""
        lines = ["【知识库参考信息】（希卡莉可据此回答，风格保持希卡莉口吻）"]
        for i, r in enumerate(results, 1):
            meta_str = ""
            for key in ["价格", "链接", "时间", "地点", "库存", "price", "url"]:
                if key in r.get("metadata", {}) and r["metadata"][key]:
                    meta_str += f"，{key}：{r['metadata'][key]}"
            lines.append(f"{i}. {r.get('title', '')}：{r.get('content', '')}{meta_str}")
        return "\n".join(lines)

    # ── 主动推送 ──────────────────────────────────────────────────────────────

    async def match_for_user(
        self,
        user_id: str,
        tenant_id: str = DEFAULT_TENANT,
        category: Optional[str] = None,
        top_k: int = 2,
    ) -> list[dict]:
        """
        根据用户画像主动匹配知识条目，用于定时推送或软植入。
        从 COL_FACTS 捞用户偏好和计划维度的最新碎片作为检索 query。
        """
        try:
            cursor = self.db[COL_FACTS].find(
                {"user_id": user_id, "category": {"$in": ["pref", "plan", "exp"]}},
                {"content": 1},
            ).sort("updated_at", -1).limit(5)
            facts = await cursor.to_list(length=5)

            if not facts:
                return []

            query = "；".join(f.get("content", "") for f in facts if f.get("content"))
            if not query.strip():
                return []

            return await self.retrieve(
                query=query, tenant_id=tenant_id, top_k=top_k, category=category
            )

        except Exception:
            logger.error(f"rag_service: match_for_user failed: {traceback.format_exc()}")
            return []

    # ── 管理 ──────────────────────────────────────────────────────────────────

    async def deactivate(self, entry_id: str, tenant_id: str = DEFAULT_TENANT) -> bool:
        """软删除一条知识条目（is_active → False，同步更新 Qdrant payload）。"""
        from bson import ObjectId

        try:
            result = await self.col.update_one(
                {"_id": ObjectId(entry_id), "tenant_id": tenant_id},
                {"$set": {"is_active": False, "updated_at": datetime.now(timezone.utc)}},
            )
            if result.modified_count > 0:
                # 同步更新 Qdrant payload（point ID 必须与写入时一致：_fact_id_to_int 转 uint64）
                qdrant = get_qdrant()
                await asyncio.to_thread(
                    qdrant.set_payload,
                    collection_name=QDRANT_COLLECTION,
                    payload={"is_active": False},
                    points=[_fact_id_to_int(entry_id)],
                )
            return result.modified_count > 0
        except Exception as e:
            logger.error(f"rag_service: deactivate failed: {e}")
            return False

    async def list_entries(
        self,
        tenant_id: str = DEFAULT_TENANT,
        category: Optional[str] = None,
        page: int = 1,
        page_size: int = 20,
    ) -> dict:
        """分页列出知识条目（不返回 embedding）。"""
        filter_q: dict = {"tenant_id": tenant_id, "is_active": True}
        if category:
            filter_q["category"] = category

        total = await self.col.count_documents(filter_q)
        cursor = (
            self.col.find(filter_q)
            .sort("updated_at", -1)
            .skip((page - 1) * page_size)
            .limit(page_size)
        )
        items = await cursor.to_list(length=page_size)
        for item in items:
            item["_id"] = str(item["_id"])

        return {"total": total, "page": page, "page_size": page_size, "items": items}


# ── 单例工厂 ──────────────────────────────────────────────────────────────────

_rag_service: RAGService | None = None


def get_rag_service() -> RAGService:
    global _rag_service
    if _rag_service is None:
        _rag_service = RAGService()
    return _rag_service
