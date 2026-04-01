# -*- coding: utf-8 -*-
"""
用户会话管理模块 — 希卡莉 (v5.0.0)
=====================================================================
重构对齐 [MASTER v3.0 / 2026-03-29]：
1. 集合名全部改用 COL_SESSIONS / COL_USERS 常量，禁止字符串字面量。
2. 移除旧版 db["sessions"] / db["users"] 字符串引用。
3. get_session_by_user_id：新增通过 user_id 直接打捞会话（auth_handler 用）。
4. invalidate_session_cache：暴露缓存失效接口（踢人 / logout 时调用）。
5. 其余逻辑（原子播种、Redis 缓存、fallback）保持 v4.4.0 行为不变。

设计说明：
- 对话历史由 dialogue_engine._save_to_history 写入 Redis（Fernet 加密，TTL 24h）。
- 审计记录由 orchestrator 写入独立的 core_messages 集合，可分页查询。
- sessions 文档只存 session 状态（affinity / mood / platform / 时间戳），
  不内嵌任何消息数组，文档体积保持恒定，无 BSON 16MB 上限风险。
=====================================================================
"""

import json
import asyncio
import os
from typing import Dict, Any
from datetime import datetime, timezone

from pymongo import ReturnDocument

from src.utils.database import get_db_manager, COL_SESSIONS, COL_USERS
from src.utils.logger import get_logger

logger = get_logger(__name__)


class SessionManager:
    """
    [原子守卫] 负责 UID → 物理 Session 的唯一映射。

    Session 文档结构（core_sessions，恒定体积）：
        session_id   str       "sess_{user_id}"
        user_id      str       用户唯一标识（wx_{unionid}）
        platform     str       接入平台，仅首次创建时写入（$setOnInsert）
        affinity     int       好感度镜像，由 affinity_service 原子更新
        mood         str       情绪标签镜像，由 affinity_service 原子更新
        created_at   datetime  首次创建时间
        last_active  datetime  每次请求更新

    对话历史和消息审计由各自独立链路负责，本类不参与。
    """

    _instance = None
    _lock = asyncio.Lock()

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super(SessionManager, cls).__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if self._initialized:
            return
        self.db_manager = get_db_manager()
        self.session_timeout = int(os.getenv("SESSION_TIMEOUT_SECONDS", 604800))
        self._initialized = True
        logger.info("🧠 [Session-Manager] v5.0.0 原子化中枢就绪")

    # ──────────────────────────────────────────────────────────────
    # 主接口
    # ──────────────────────────────────────────────────────────────

    async def get_or_create_session(self, platform: str, user_id: str) -> Dict[str, Any]:
        """
        [原子化播种]：find_one_and_update + upsert 实现"查询即创建"。

        关键约束：
        - platform 只能出现在 $setOnInsert，不能同时在 $set，
          否则 MongoDB 报 Code 40（路径冲突）。
        - $setOnInsert 仅在文档首次创建时执行，后续 upsert 完全跳过。

        返回：
            {"session_id": str, "user_id": str, "affinity": int}
        降级返回（MongoDB 不可用时）：
            {"session_id": "sess_{user_id}", "user_id": user_id, "affinity": 10}
        """
        try:
            db = self.db_manager.db
            session_id = f"sess_{user_id}"

            session_doc = await db[COL_SESSIONS].find_one_and_update(
                {"user_id": user_id},
                {
                    "$setOnInsert": {
                        "session_id": session_id,
                        "platform":   platform,
                        "affinity":   10,
                        "mood":       "neutral",
                        "created_at": datetime.now(timezone.utc),
                    },
                    "$set": {
                        "last_active": datetime.now(timezone.utc),
                    },
                },
                upsert=True,
                return_document=ReturnDocument.AFTER,
            )

            # 异步缓存，不阻塞主流程
            asyncio.create_task(self._cache_session(session_doc))

            return {
                "session_id": session_doc["session_id"],
                "user_id":    session_doc["user_id"],
                "affinity":   session_doc.get("affinity", 10),
            }

        except Exception as e:
            logger.error(f"❌ [Session-Manager] 原子播种失败 | user_id={user_id} | {e}")
            return {
                "session_id": f"sess_{user_id}",
                "user_id":    user_id,
                "affinity":   10,
            }

    async def get_session(self, session_id: str) -> Dict[str, Any] | None:
        """
        [会话打捞] 通过 session_id 获取完整 session 文档。

        缓存策略：先读 Redis（TTL = SESSION_TIMEOUT_SECONDS），未命中回源 MongoDB。
        """
        # 1. 尝试 Redis 缓存
        try:
            redis = await self.db_manager.get_redis()
            cached = await redis.get(f"hikari:session:{session_id}")
            if cached:
                raw = cached if isinstance(cached, str) else cached.decode()
                return json.loads(raw)
        except Exception as e:
            logger.warning(f"⚠️ [Session-Get] Redis 读取失败，回源 MongoDB: {e}")

        # 2. 回源 MongoDB
        try:
            db = self.db_manager.db
            doc = await db[COL_SESSIONS].find_one({"session_id": session_id})
            if doc:
                asyncio.create_task(self._cache_session(doc))
            return doc
        except Exception as e:
            logger.error(f"❌ [Session-Get] MongoDB 打捞失败 | session_id={session_id} | {e}")
            return None

    async def get_session_by_user_id(self, user_id: str) -> Dict[str, Any] | None:
        """
        [会话打捞] 通过 user_id 直接打捞 session 文档。

        使用场景：auth_handler login / refresh 时无 session_id，只有 user_id。
        不读 Redis（Redis key 按 session_id 存储），直接打 MongoDB。
        """
        try:
            db = self.db_manager.db
            return await db[COL_SESSIONS].find_one({"user_id": user_id})
        except Exception as e:
            logger.error(f"❌ [Session-ByUID] MongoDB 打捞失败 | user_id={user_id} | {e}")
            return None

    async def invalidate_session_cache(self, session_id: str) -> None:
        """
        强制清除 Redis 中该 session 的缓存条目。

        调用时机：
        - 踢人（kick）操作后，确保下次请求强制回源
        - 用户登出（logout）后
        - 管理后台修改 session 数据后
        """
        try:
            redis = await self.db_manager.get_redis()
            await redis.delete(f"hikari:session:{session_id}")
            logger.debug(f"🗑️ [Session-Cache] 缓存已失效 | session_id={session_id}")
        except Exception as e:
            logger.warning(f"⚠️ [Session-Cache] 缓存失效操作失败（非致命）: {e}")

    # ──────────────────────────────────────────────────────────────
    # 内部工具
    # ──────────────────────────────────────────────────────────────

    async def _cache_session(self, session: Dict[str, Any]) -> None:
        """
        序列化 session 文档并推送到 Redis 高速层。

        TTL = SESSION_TIMEOUT_SECONDS（默认 7 天），与 session 过期对齐。
        失败时记录 warning 但不抛出（不影响主流程）。
        """
        try:
            redis = await self.db_manager.get_redis()
            session_id = session.get("session_id")
            if not session_id:
                return

            def _json_fix(obj: Any) -> str:
                if isinstance(obj, datetime):
                    return obj.isoformat()
                return str(obj)

            serialized = json.dumps(session, default=_json_fix, ensure_ascii=False)
            await redis.setex(
                f"hikari:session:{session_id}",
                self.session_timeout,
                serialized,
            )
        except Exception as e:
            logger.warning(f"⚠️ [Session-Cache] 写入跳过（非致命）: {e}")


# ══════════════════════════════════════════════════════════════════
# 单例导出
# ══════════════════════════════════════════════════════════════════

_session_manager: SessionManager | None = None


def get_session_manager() -> SessionManager:
    global _session_manager
    if _session_manager is None:
        _session_manager = SessionManager()
    return _session_manager
