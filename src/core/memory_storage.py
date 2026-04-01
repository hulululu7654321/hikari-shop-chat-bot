# -*- coding: utf-8 -*-
"""
记忆存储与检索核心模块 — 希卡莉 (v5.0.0)
=====================================================================
重构对齐 [MASTER v3.0 / 2026-03-29]：
1. 集合名全部改用 COL_MEMORIES / COL_FACTS 常量，禁止字符串字面量。
2. 移除 config.config_loader 依赖，配置全走环境变量（与整体架构对齐）。
3. DatabaseManager() 直接实例化 → get_db_manager() 单例工厂。
4. 初始化时不再缓存 collection 引用（避免连接未就绪时拿到 None），
   每次操作通过 self.db_manager.db[COL_*] 动态获取。
5. 其余业务逻辑（ES 双写、IK 分词、2-Gram 降级）保持 v4.0.0 行为不变。

设计说明：
- core_memories 集合：存储对话记忆条目（MongoDB 主存储 + ES 检索索引）
- core_facts 集合：存储结构化画像事实（由 fact_extractor 写入）
- ES 不可用时自动降级到 2-Gram 内存检索，零感知切换
=====================================================================
"""

import re
import uuid
import asyncio
import traceback
from datetime import datetime, timezone
from typing import List, Dict, Any, Optional

from src.utils.database import get_db_manager, COL_MEMORIES, COL_FACTS
from src.utils.logger import get_logger

logger = get_logger(__name__)

ES_INDEX = "hikari_memories"

# ES 索引 Mapping：ik_max_word 分词器处理中文，keyword 子字段支持精确过滤
ES_INDEX_MAPPING = {
    "settings": {
        "analysis": {
            "analyzer": {
                "ik_analyzer": {
                    "type": "custom",
                    "tokenizer": "ik_max_word",
                    "filter": ["lowercase"],
                }
            }
        },
        "number_of_shards": 1,
        "number_of_replicas": 0,
    },
    "mappings": {
        "properties": {
            "memory_id":  {"type": "keyword"},
            "user_id":    {"type": "keyword"},
            "session_id": {"type": "keyword"},
            "role":       {"type": "keyword"},
            "content": {
                "type": "text",
                "analyzer": "ik_analyzer",
                "search_analyzer": "ik_max_word",
                "fields": {
                    "keyword": {"type": "keyword", "ignore_above": 512}
                },
            },
            "created_at": {"type": "date"},
        }
    },
}


class MemoryStorage:
    _instance = None
    _lock = asyncio.Lock()

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super(MemoryStorage, cls).__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if self._initialized:
            return
        # get_db_manager() 单例工厂，不缓存 collection 引用
        self.db_manager = get_db_manager()
        self._es_ready = False  # ES 索引是否已就绪
        self._initialized = True
        logger.info("💾 [Memory-Storage] v5.0.0 就绪 | COL_MEMORIES + ES 双写")

    # ──────────────────────────────────────────────────────────────
    # ES 索引自愈
    # ──────────────────────────────────────────────────────────────

    async def _ensure_es_index(self) -> bool:
        """启动时确保 ES 索引存在（幂等，已存在直接跳过）"""
        if self._es_ready:
            return True
        es = self.db_manager.get_es()
        if not es:
            return False
        try:
            await es.indices.create(
                index=ES_INDEX,
                settings=ES_INDEX_MAPPING["settings"],
                mappings=ES_INDEX_MAPPING["mappings"],
            )
            logger.info(f"🔍 [ES] 索引 {ES_INDEX} 创建完成（IK 中文分词）")
            self._es_ready = True
            return True
        except Exception as e:
            err_str = str(e)
            if "resource_already_exists_exception" in err_str:
                logger.info(f"🔍 [ES] 索引 {ES_INDEX} 已存在，跳过创建")
                self._es_ready = True
                return True
            logger.warning(f"⚠️ [ES] 索引初始化失败，将降级到 2-Gram: {e}\n{traceback.format_exc()}")
            return False

    # ──────────────────────────────────────────────────────────────
    # 写入
    # ──────────────────────────────────────────────────────────────

    async def save_memory(
        self,
        user_id: str,
        session_id: str,
        role: str,
        content: str,
        metadata: Optional[Dict] = None,
    ) -> bool:
        """
        [原子化落盘] MongoDB 持久化 + ES 全文索引双写。

        content 去重：同一用户同一 role 相同内容不重复写入。
        ES 写入为异步任务，允许短暂延迟，不阻塞主流程。
        """
        try:
            if not content or not content.strip():
                return False

            db = self.db_manager.db
            memory_col = db[COL_MEMORIES]

            # content 去重（Q1 已知：无联合索引，高频写入有全表扫描风险，后续可加索引优化）
            existing = await memory_col.find_one(
                {"user_id": user_id, "role": role, "content": content},
                {"_id": 1},
            )
            if existing:
                return True

            memory_id = str(uuid.uuid4())
            created_at = datetime.now(timezone.utc)

            # 1. MongoDB 持久化（主存储，不可丢）
            await memory_col.insert_one({
                "memory_id":  memory_id,
                "user_id":    user_id,
                "session_id": session_id,
                "role":       role,
                "content":    content,
                "created_at": created_at,
                "metadata":   metadata or {},
            })

            # 2. ES 异步写入（检索索引，允许短暂延迟）
            asyncio.create_task(self._es_index_doc(
                memory_id=memory_id,
                user_id=user_id,
                session_id=session_id,
                role=role,
                content=content,
                created_at=created_at,
            ))

            return True
        except Exception as e:
            logger.error(f"❌ [Memory-Save] 持久化失败 | uid={user_id[:8]} | {e}")
            return False

    async def _es_index_doc(
        self,
        memory_id: str,
        user_id: str,
        session_id: str,
        role: str,
        content: str,
        created_at: datetime,
    ) -> None:
        """[ES 异步写入] 索引单条记忆，任何异常不影响主流程"""
        try:
            await self._ensure_es_index()
            es = self.db_manager.get_es()
            if not es:
                return
            await es.index(
                index=ES_INDEX,
                id=memory_id,
                document={
                    "memory_id":  memory_id,
                    "user_id":    user_id,
                    "session_id": session_id,
                    "role":       role,
                    "content":    content,
                    "created_at": created_at.isoformat(),
                },
            )
        except Exception as e:
            logger.debug(f"[ES] 写入失败（不影响主流程）: {e}")

    # ──────────────────────────────────────────────────────────────
    # 检索
    # ──────────────────────────────────────────────────────────────

    async def search_memories(
        self,
        user_id: str,
        query: str,
        limit: int = 3,
    ) -> List[Dict]:
        """
        [混合 RAG 检索] 优先走 ES IK 分词全文检索，ES 不可用时降级到 2-Gram 内存检索。
        """
        if not user_id or not query:
            return []

        es_results = await self._search_by_es(user_id, query, limit)
        if es_results is not None:
            return es_results

        logger.debug(f"[Memory] ES 不可用，降级到 2-Gram | uid={user_id[:8]}")
        return await self._search_by_ngram(user_id, query, limit)

    async def _search_by_es(
        self,
        user_id: str,
        query: str,
        limit: int,
    ) -> Optional[List[Dict]]:
        """ES IK 分词全文检索，失败返回 None 触发降级"""
        try:
            await self._ensure_es_index()
            es = self.db_manager.get_es()
            if not es or not self._es_ready:
                return None

            resp = await es.search(
                index=ES_INDEX,
                query={
                    "bool": {
                        "must": [
                            {"term": {"user_id": user_id}},
                            {
                                "match": {
                                    "content": {
                                        "query": query,
                                        "analyzer": "ik_max_word",
                                        "minimum_should_match": "60%",
                                    }
                                }
                            },
                        ]
                    }
                },
                sort=[
                    {"_score":      {"order": "desc"}},
                    {"created_at":  {"order": "desc"}},
                ],
                size=limit,
                source=["memory_id", "user_id", "role", "content", "created_at"],
            )

            hits = resp.get("hits", {}).get("hits", [])
            if not hits:
                return []

            results = []
            for hit in hits:
                src = hit["_source"]
                results.append({
                    "memory_id":  src.get("memory_id", ""),
                    "user_id":    src.get("user_id", ""),
                    "role":       src.get("role", ""),
                    "content":    src.get("content", ""),
                    "created_at": src.get("created_at", ""),
                    "_id":        hit["_id"],
                    "_score":     hit.get("_score", 0),
                })
            return results

        except Exception as e:
            logger.warning(f"⚠️ [ES] 检索异常，触发降级: {e}")
            return None

    async def _search_by_ngram(
        self,
        user_id: str,
        query: str,
        limit: int,
    ) -> List[Dict]:
        """[降级] 2-Gram 内存检索逻辑"""
        try:
            clean_q = re.sub(r'[^\w\u4e00-\u9fa5]', '', query)
            stopwords = r'[的了是我你他她它啊呢吗喵吧哦在就跟也还哈嗯哎]'
            clean_q = re.sub(stopwords, '', clean_q)
            if len(clean_q) < 2:
                return []

            query_grams = set(clean_q[i:i + 2] for i in range(len(clean_q) - 1))
            if not query_grams:
                return []

            db = self.db_manager.db
            memory_col = db[COL_MEMORIES]
            cursor = memory_col.find({"user_id": user_id}).sort("created_at", -1).limit(100)
            recent_memories = [doc async for doc in cursor]

            scored_memories = []
            for mem in recent_memories:
                content = mem.get("content", "")
                if not content:
                    continue
                score = sum(1 for gram in query_grams if gram in content)
                if score > 0:
                    mem["_id"] = str(mem["_id"])
                    scored_memories.append((score, mem))

            scored_memories.sort(
                key=lambda x: (
                    x[0],
                    x[1].get("created_at", datetime.min.replace(tzinfo=timezone.utc)),
                ),
                reverse=True,
            )
            return [m[1] for m in scored_memories[:limit]]

        except Exception as e:
            logger.warning(f"⚠️ [Memory-NGram] 2-Gram 降级失败 | uid={user_id[:8]}: {traceback.format_exc()}")
            return []

    # ──────────────────────────────────────────────────────────────
    # 画像聚合
    # ──────────────────────────────────────────────────────────────

    async def get_user_all_fragments(self, user_id: str) -> Dict[str, List[str]]:
        """
        [画像重组] 聚合 core_facts 中用户的结构化事实碎片。

        按 category 分组，每组取置信度最高的前 5 条。
        memoir_exporter 和 dialogue_engine 调用此方法构建用户画像上下文。
        """
        try:
            db = self.db_manager.db
            pipeline = [
                {"$match": {"user_id": user_id, "is_active": True}},
                {"$sort": {"confidence": -1, "updated_at": -1}},
                {"$group": {
                    "_id":       "$category",
                    "fragments": {"$push": "$content"},
                }},
            ]
            cursor = db[COL_FACTS].aggregate(pipeline)
            result: Dict[str, List[str]] = {}
            async for doc in cursor:
                category = doc["_id"]
                result[category] = doc["fragments"][:5]
            return result
        except Exception as e:
            logger.error(f"❌ [Memory-Fragments] 画像聚合失败 | uid={user_id[:8]}: {e}")
            return {}


# ══════════════════════════════════════════════════════════════════
# 单例导出
# ══════════════════════════════════════════════════════════════════

_memory_storage_instance: MemoryStorage | None = None
_memory_storage_lock = asyncio.Lock()


def get_memory_storage() -> MemoryStorage:
    global _memory_storage_instance
    if _memory_storage_instance is None:
        _memory_storage_instance = MemoryStorage()
    return _memory_storage_instance


async def get_memory_storage_safe() -> MemoryStorage:
    """并发安全的懒初始化（多协程同时首次调用时使用）"""
    global _memory_storage_instance
    if _memory_storage_instance is not None:
        return _memory_storage_instance
    async with _memory_storage_lock:
        if _memory_storage_instance is None:
            _memory_storage_instance = MemoryStorage()
    return _memory_storage_instance
