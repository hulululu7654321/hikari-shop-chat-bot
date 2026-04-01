# -*- coding: utf-8 -*-
"""
数据库工具模块 — 希卡莉 (v5.0.0)
=====================================================================
重构对齐 [2026-03-29]：
1. 集合名全部改为模块级 COL_* 常量，禁止在其他文件写字符串字面量。
2. 环境变量对齐 MASTER v3.0：MONGO_URI（非旧版 MONGODB_URI）。
3. vector_memory 集合索引已废弃（向量数据迁移至 Qdrant），删除对应索引。
4. 新增 COL_KB / COL_CONFIG / COL_ALERTS 集合名常量及对应索引。
5. Redis 连接改为直接读取 REDIS_URL（统一 DSN 格式）。
6. 长连接（XREAD BLOCK / blpop 专用）保持独立，不复用业务连接。
7. _sanitize_input 逻辑不变（保留 . 去除 $）。
=====================================================================
"""

import os
from datetime import datetime
from typing import Any, Optional

from bson import ObjectId
from motor.motor_asyncio import AsyncIOMotorClient
from redis import asyncio as aioredis
from pymongo import ASCENDING, DESCENDING, TEXT
from pymongo.errors import OperationFailure
from elasticsearch import AsyncElasticsearch

from src.utils.logger import get_logger

logger = get_logger(__name__)


# ══════════════════════════════════════════════════════════════════
# 集合名常量（唯一真相来源，禁止在其他文件写字符串字面量）
# ══════════════════════════════════════════════════════════════════

COL_USERS        = "core_users"
COL_SESSIONS     = "core_sessions"
COL_MESSAGES     = "core_messages"
COL_MEMORIES     = "core_memories"
COL_FACTS        = "core_facts"
COL_KB           = "core_kb"
COL_CONFIG       = "core_config"
COL_ALERTS       = "core_system_alerts"
COL_PACKAGES     = "shop_packages"
COL_ORDERS       = "store_orders"
COL_INVITE_CODES = "auth_invite_codes"
COL_SEARCH_CACHE = "search_cache"

# 已废弃：core_vector_memory（向量数据迁移至 Qdrant，不再使用）


# ══════════════════════════════════════════════════════════════════
# 序列化工具
# ══════════════════════════════════════════════════════════════════

def nya_json_serializer(obj):
    """MongoDB 文档 JSON 序列化补丁（ObjectId → str，datetime → isoformat）"""
    if isinstance(obj, ObjectId):
        return str(obj)
    if isinstance(obj, datetime):
        return obj.isoformat()
    raise TypeError(f"Object of type {obj.__class__.__name__} is not JSON serializable")


# ══════════════════════════════════════════════════════════════════
# DatabaseManager（单例）
# ══════════════════════════════════════════════════════════════════

class DatabaseManager:
    """
    负责 MongoDB / Redis 连接池维护与索引自愈。
    单例模式：全局唯一实例，所有模块共用。
    """
    _instance = None
    _client   = None
    _db       = None
    _redis    = None
    _es       = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self):
        if self._client:
            return

        # MASTER v3.0 环境变量：MONGO_URI
        mongo_uri = os.getenv("MONGO_URI")
        if not mongo_uri:
            logger.warning("⚠️ [Config] MONGO_URI 未配置，降级到容器内网默认地址")
            mongo_uri = "mongodb://hikari-mongodb:27017/hikari"

        # 数据库名从 URI 路径段提取，兜底用 "hikari"
        db_name = mongo_uri.rstrip("/").rsplit("/", 1)[-1].split("?")[0] or "hikari"

        self._client = AsyncIOMotorClient(
            mongo_uri,
            maxPoolSize=100,
            minPoolSize=10,
            serverSelectionTimeoutMS=5000,
            socketTimeoutMS=10000,
        )
        self._db = self._client[db_name]
        logger.info(f"🍃 [MongoDB] 连接就绪 | db={db_name}")

    @property
    def db(self):
        return self._db

    def get_es(self) -> Optional[AsyncElasticsearch]:
        """获取 ES 客户端（懒初始化，不可用时返回 None 降级 2-Gram）"""
        if self._es is not None:
            return self._es
        es_host = os.getenv("ES_HOST", "")
        if not es_host:
            return None
        try:
            clean  = es_host.replace("http://", "").replace("https://", "")
            scheme = "https" if "https://" in es_host else "http"
            if ":" in clean:
                host_part, port_str = clean.rsplit(":", 1)
                port = int(port_str)
            else:
                host_part, port = clean, 9200
            self._es = AsyncElasticsearch(
                hosts=[{"host": host_part, "port": port, "scheme": scheme}],
                request_timeout=5,
                retry_on_timeout=True,
                max_retries=2,
            )
            logger.info(f"🔍 [ES] 客户端就绪 | host={es_host}")
        except Exception as e:
            logger.warning(f"⚠️ [ES] 初始化失败，降级 2-Gram: {e}")
            self._es = None
        return self._es

    async def get_redis(self):
        """业务用 Redis 连接（decode_responses=True，返回 str）"""
        if not self._redis:
            redis_url = os.getenv("REDIS_URL", "redis://hikari-redis:6379/0")
            self._redis = aioredis.from_url(
                redis_url,
                decode_responses=True,
                socket_timeout=5.0,
            )
            logger.info(f"🔥 [Redis] 连接就绪")
        return self._redis

    async def get_redis_pubsub(self):
        """
        长连接专用（XREAD BLOCK / blpop），每次调用新建，不复用业务连接。
        无 socket_timeout 限制（需要长时间阻塞等待消息）。
        """
        redis_url = os.getenv("REDIS_URL", "redis://hikari-redis:6379/0")
        return aioredis.from_url(redis_url, decode_responses=True)

    # ──────────────────────────────────────────────────────────────
    # 索引自愈（启动时调用，幂等）
    # ──────────────────────────────────────────────────────────────

    async def ensure_indexes(self):
        """
        强制自愈所有集合索引。
        - 爆破旧冲突索引名后重建标准索引
        - 集合名全部使用 COL_* 常量
        - vector_memory 相关索引已移除（迁 Qdrant）
        """
        try:
            # ── 1. core_users ──────────────────────────────────────
            col_users = self._db[COL_USERS]
            u_info    = await col_users.index_information()

            # 爆破历史冲突索引名
            for old in ["idx_uuid_user_unique", "user_id_1"]:
                if old in u_info:
                    logger.warning(f"🗑️ [Index] 爆破冲突索引 {old} ...")
                    await col_users.drop_index(old)

            await col_users.create_index(
                [("user_id", ASCENDING)],
                unique=True,
                name="hikari_uid_unique_v1",
            )
            await col_users.create_index(
                [("manage_role", ASCENDING)],
                name="hikari_users_manage_role_v1",
                sparse=True,
            )
            # TEXT 索引：仅在不存在时创建（重建会短暂锁表）
            if "hikari_users_name_text_v1" not in u_info:
                await col_users.create_index(
                    [("user_name", TEXT)],
                    name="hikari_users_name_text_v1",
                )
            await col_users.create_index(
                [("has_access", ASCENDING)],
                name="hikari_users_access_v1",
                sparse=True,
            )

            # ── 2. core_memories ──────────────────────────────────
            col_mem = self._db[COL_MEMORIES]
            m_info  = await col_mem.index_information()

            # 清理废弃全文索引
            for old in ["idx_interaction_text_search_isolated", "hikari_rag_search_v1"]:
                if old in m_info:
                    logger.warning(f"🗑️ [Index] 清理废弃索引 {old} ...")
                    await col_mem.drop_index(old)

            # search_memories 查询方向：user_id 等值 + created_at 倒序
            await col_mem.create_index(
                [("user_id", ASCENDING), ("created_at", DESCENDING)],
                name="hikari_memories_query_v1",
            )
            # save_memory() 去重查询：find_one({user_id, role, content})
            await col_mem.create_index(
                [("user_id", ASCENDING), ("role", ASCENDING), ("content", ASCENDING)],
                name="hikari_memories_dedup_v1",
            )

            # ── 3. core_sessions ──────────────────────────────────
            await self._db[COL_SESSIONS].create_index(
                [("user_id", ASCENDING)],
                unique=True,
                name="hikari_sessions_uid_v1",
            )

            # ── 4. core_facts ─────────────────────────────────────
            # fact_extractor 查询：user_id 等值过滤
            await self._db[COL_FACTS].create_index(
                [("user_id", ASCENDING)],
                name="hikari_facts_uid_v1",
            )
            # 画像聚合：fact_extractor / memoir_exporter 按 category 分组
            await self._db[COL_FACTS].create_index(
                [("user_id", ASCENDING), ("category", ASCENDING)],
                name="hikari_facts_category_v1",
            )
            # 置信度排序：memoir_exporter 聚合、AutoDream Gather 阶段
            await self._db[COL_FACTS].create_index(
                [("user_id", ASCENDING), ("confidence", DESCENDING), ("updated_at", DESCENDING)],
                name="hikari_facts_confidence_v1",
            )
            # 有效事实过滤：fact_extractor 查 is_active=True
            await self._db[COL_FACTS].create_index(
                [("user_id", ASCENDING), ("is_active", ASCENDING)],
                name="hikari_facts_active_v1",
            )

            # ── 5. shop_packages ──────────────────────────────────
            await self._db[COL_PACKAGES].create_index(
                [("store_id", ASCENDING), ("type", ASCENDING), ("is_active", ASCENDING)],
                name="hikari_packages_query_v1",
            )

            # ── 6. auth_invite_codes ──────────────────────────────
            await self._db[COL_INVITE_CODES].create_index(
                [("code", ASCENDING)],
                unique=True,
                name="hikari_invite_code_unique_v1",
            )
            await self._db[COL_INVITE_CODES].create_index(
                [("status", ASCENDING), ("created_at", DESCENDING)],
                name="hikari_invite_status_v1",
            )

            # ── 7. store_orders ───────────────────────────────────
            await self._db[COL_ORDERS].create_index(
                [("store_id", ASCENDING), ("created_at", DESCENDING)],
                name="hikari_orders_store_date_v1",
            )
            await self._db[COL_ORDERS].create_index(
                [("user_id", ASCENDING), ("created_at", DESCENDING)],
                name="hikari_orders_user_date_v1",
            )

            # ── 8. core_config（SOUL 热配置，updated_at 索引）─────
            await self._db[COL_CONFIG].create_index(
                [("updated_at", DESCENDING)],
                name="hikari_config_updated_v1",
            )

            # ── 9. core_system_alerts（健康巡检告警）─────────────
            await self._db[COL_ALERTS].create_index(
                [("created_at", DESCENDING)],
                name="hikari_alerts_date_v1",
            )
            # 30天 TTL 自动清理（与 init-mongo.js idx_alert_ttl 对齐）
            await self._db[COL_ALERTS].create_index(
                [("created_at", ASCENDING)],
                expireAfterSeconds=2592000,
                name="hikari_alerts_ttl_v1",
            )

            # ── 10. search_cache（搜索结果时效知识库，7天 TTL）─────────
            await self._db[COL_SEARCH_CACHE].create_index(
                [("created_at", ASCENDING)],
                expireAfterSeconds=604800,
                name="hikari_search_cache_ttl_v1",
            )
            await self._db[COL_SEARCH_CACHE].create_index(
                [("user_id", ASCENDING), ("created_at", DESCENDING)],
                name="hikari_search_cache_query_v1",
            )

            logger.info("🛠️ [Index] 全量索引自愈完毕")

        except Exception as e:
            logger.error(f"❌ [Index] 索引自愈失败: {e}")


# ══════════════════════════════════════════════════════════════════
# NoSQL 注入防御
# ══════════════════════════════════════════════════════════════════

def _sanitize_input(val: Any) -> str:
    """
    NoSQL 注入防御：
    - 非 str/int/float 类型直接拒绝（字典/列表注入拦截）
    - 移除 $ 操作符前缀
    - 保留 .（JWT Token 三段式结构依赖点号）
    """
    if val is None:
        return ""
    if not isinstance(val, (str, int, float)):
        raise ValueError(f"[Security] 非法参数类型注入: {type(val)}")
    return str(val).strip().replace("$", "")


# ══════════════════════════════════════════════════════════════════
# 单例导出
# ══════════════════════════════════════════════════════════════════

def get_db_manager() -> DatabaseManager:
    return DatabaseManager()
