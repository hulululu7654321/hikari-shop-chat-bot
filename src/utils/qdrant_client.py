# -*- coding: utf-8 -*-
"""
Qdrant 连接管理 — 希卡莉 (v1.0.0)
=====================================================================
新增 [2026-03-29]：替代旧版 Python 层余弦相似度检索。
- 全局单例 QdrantClient，连接地址来自 QDRANT_URL 环境变量
- QDRANT_COLLECTION 常量禁止修改，其他文件不得硬编码集合名
- 集合规格：2048 维向量（豆包多模态 Embedding），余弦距离
- 所有操作均为异步包装，底层 qdrant-client 同步调用走 asyncio.to_thread
=====================================================================
"""

import os
import asyncio
from datetime import datetime, timezone

from qdrant_client import QdrantClient
from qdrant_client.http import models as qmodels

from src.utils.logger import get_logger

logger = get_logger(__name__)

# ──────────────────────────────────────────────────────────────────
# 常量（禁止在其他文件硬编码）
# ──────────────────────────────────────────────────────────────────

QDRANT_COLLECTION = "hikari_memories"   # 唯一向量集合名
_VECTOR_SIZE      = 2048                # 豆包 multimodal_embeddings 维度
_DISTANCE         = qmodels.Distance.COSINE

# ──────────────────────────────────────────────────────────────────
# 单例客户端
# ──────────────────────────────────────────────────────────────────

_client: QdrantClient | None = None


def get_qdrant() -> QdrantClient:
    """
    全局单例，连接地址来自 QDRANT_URL 环境变量。
    首次调用时初始化，后续复用同一实例。
    """
    global _client
    if _client is None:
        qdrant_url = os.getenv("QDRANT_URL", "http://hikari-qdrant:6333")
        _client = QdrantClient(url=qdrant_url, timeout=10)
        logger.info(f"🔵 [Qdrant] 客户端就绪 | url={qdrant_url}")
    return _client


# ──────────────────────────────────────────────────────────────────
# 集合自愈（启动时调用）
# ──────────────────────────────────────────────────────────────────

async def ensure_collection() -> None:
    """
    启动时调用，集合不存在时自动创建。
    已存在时幂等跳过，不会重建或清空数据。
    """
    try:
        client = get_qdrant()

        def _check_or_create():
            existing = [c.name for c in client.get_collections().collections]
            if QDRANT_COLLECTION not in existing:
                client.create_collection(
                    collection_name=QDRANT_COLLECTION,
                    vectors_config=qmodels.VectorParams(
                        size=_VECTOR_SIZE,
                        distance=_DISTANCE,
                    ),
                )
                logger.info(f"🔵 [Qdrant] 集合 {QDRANT_COLLECTION!r} 已创建 | dim={_VECTOR_SIZE}")
            else:
                logger.info(f"🔵 [Qdrant] 集合 {QDRANT_COLLECTION!r} 已存在，跳过创建")

        await asyncio.to_thread(_check_or_create)
    except Exception as e:
        logger.error(f"❌ [Qdrant] ensure_collection 失败: {e}")


# ──────────────────────────────────────────────────────────────────
# 核心操作
# ──────────────────────────────────────────────────────────────────

async def upsert_vector(
    fact_id: str,
    vector: list[float],
    payload: dict,
) -> bool:
    """
    写入或更新一条向量记录。

    参数：
        fact_id : MD5("{user_id}_{cat}_{subject}_{topic}")，唯一标识
        vector  : 2048 维 float 列表
        payload : 必含字段 user_id / content / category / importance /
                  image_id / is_active / updated_at

    返回：True=成功，False=失败（已记 error 日志）
    """
    try:
        # 补充 updated_at，保证字段存在
        payload.setdefault("updated_at", datetime.now(timezone.utc).isoformat())

        def _upsert():
            get_qdrant().upsert(
                collection_name=QDRANT_COLLECTION,
                points=[
                    qmodels.PointStruct(
                        id=_fact_id_to_int(fact_id),
                        vector=vector,
                        payload={**payload, "fact_id": fact_id},
                    )
                ],
            )

        await asyncio.to_thread(_upsert)
        return True
    except Exception as e:
        logger.error(f"❌ [Qdrant] upsert_vector 失败 | fact_id={fact_id[:8]} | err={e}")
        return False


async def search_vectors(
    user_id: str,
    query_vector: list[float],
    top_k: int = 5,
    score_threshold: float = 0.6,
) -> list[dict]:
    """
    按 user_id 过滤后语义检索，按 score 降序返回。

    返回列表每项包含原始 payload + "_score" 字段。
    """
    try:
        def _search():
            return get_qdrant().search(
                collection_name=QDRANT_COLLECTION,
                query_vector=query_vector,
                query_filter=qmodels.Filter(
                    must=[
                        qmodels.FieldCondition(
                            key="user_id",
                            match=qmodels.MatchValue(value=user_id),
                        ),
                        qmodels.FieldCondition(
                            key="is_active",
                            match=qmodels.MatchValue(value=True),
                        ),
                    ]
                ),
                limit=top_k,
                score_threshold=score_threshold,
                with_payload=True,
            )

        hits = await asyncio.to_thread(_search)
        return [{**h.payload, "_score": h.score} for h in hits]
    except Exception as e:
        logger.error(f"❌ [Qdrant] search_vectors 失败 | uid={user_id[:8]} | err={e}")
        return []


async def deactivate_vector(fact_id: str) -> bool:
    """
    标记 is_active=False（软删除）。
    不物理删除，保留向量供冲突检测参考。
    """
    try:
        def _deactivate():
            get_qdrant().set_payload(
                collection_name=QDRANT_COLLECTION,
                payload={"is_active": False},
                points=[_fact_id_to_int(fact_id)],
            )

        await asyncio.to_thread(_deactivate)
        return True
    except Exception as e:
        logger.error(f"❌ [Qdrant] deactivate_vector 失败 | fact_id={fact_id[:8]} | err={e}")
        return False


async def get_user_vectors(user_id: str, with_vectors: bool = False, category: str | None = None) -> list[dict]:
    """
    全量读取用户的所有活跃向量（memoir_exporter / fact_extractor 冲突检测用）。
    不分页，适合单用户数据量（通常 < 200 条）。

    with_vectors=False（默认）：只拉 payload，不拉向量数据，约节省 4MB/次。
    with_vectors=True：同时拉向量，用于本地余弦冲突检测。
    category：不为 None 时只拉该分类的向量，减少 Qdrant 传输量。
    """
    try:
        must_conditions = [
            qmodels.FieldCondition(
                key="user_id",
                match=qmodels.MatchValue(value=user_id),
            ),
            qmodels.FieldCondition(
                key="is_active",
                match=qmodels.MatchValue(value=True),
            ),
        ]
        if category is not None:
            must_conditions.append(
                qmodels.FieldCondition(
                    key="category",
                    match=qmodels.MatchValue(value=category),
                )
            )

        def _scroll():
            results, _ = get_qdrant().scroll(
                collection_name=QDRANT_COLLECTION,
                scroll_filter=qmodels.Filter(must=must_conditions),
                limit=500,   # 单用户上限，超过时取最近的
                with_payload=True,
                with_vectors=with_vectors,
            )
            return results

        records = await asyncio.to_thread(_scroll)
        return [
            {
                "fact_id": r.payload.get("fact_id", ""),
                **r.payload,
                **({"vector": r.vector} if with_vectors else {}),
            }
            for r in records
        ]
    except Exception as e:
        logger.error(f"❌ [Qdrant] get_user_vectors 失败 | uid={user_id[:8]} | err={e}")
        return []


# ──────────────────────────────────────────────────────────────────
# 内部工具
# ──────────────────────────────────────────────────────────────────

def _fact_id_to_int(fact_id: str) -> int:
    """
    Qdrant point ID 需要是 uint64。
    将 MD5 hex 字符串的前 15 位转为 int（约 60 bit，安全无碰撞风险）。
    """
    return int(fact_id[:15], 16)
