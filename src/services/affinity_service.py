# -*- coding: utf-8 -*-
"""
好感度逻辑服务
"""

import traceback
from datetime import datetime, timezone

from pymongo import ReturnDocument

from src.utils.database import get_db_manager, COL_USERS, COL_SESSIONS
from src.utils.logger import get_logger

logger = get_logger(__name__)


class AffinityService:
    def __init__(self):
        self.db_manager = get_db_manager()

        logger.info("affinity_service initialized")

    @property
    def users_col(self):
        return get_db_manager().db[COL_USERS]

    @property
    def sessions_col(self):
        return get_db_manager().db[COL_SESSIONS]

    async def process_daily_chat(
        self, session_id: str, message: str, bot_emotion: str = "neutral"
    ) -> dict | None:
        """
        由每次对话驱动的好感度与情绪步进。
        返回 {"user_id": str, "old": int, "new": int}，失败返回 None。
        """
        try:
            toxic_keywords = ["骂你", "垃圾", "删库", "白痴", "死猫", "弱智"]
            is_toxic = any(k in message for k in toxic_keywords)
            increment = -5 if is_toxic else 1

            update_pipeline = [
                {
                    "$set": {
                        "affinity": {
                            "$max": [
                                0,
                                {
                                    "$min": [
                                        100,
                                        {
                                            "$add": [
                                                {"$ifNull": ["$affinity", 10]},
                                                increment,
                                            ]
                                        },
                                    ]
                                },
                            ]
                        },
                        "mood": bot_emotion,
                        "updated_at": datetime.now(timezone.utc),
                    }
                }
            ]

            # 更新 session，拿更新前的 affinity 用于里程碑判定
            session_res = await self.sessions_col.find_one_and_update(
                {"session_id": session_id},
                update_pipeline,
                projection={"user_id": 1, "affinity": 1},
                return_document=ReturnDocument.BEFORE,
            )

            if not session_res or not session_res.get("user_id"):
                logger.warning(f"affinity pulse aborted: session not found | session_id={session_id[:8]}")
                return None

            user_id = session_res["user_id"]
            old_affinity = session_res.get("affinity", 10)

            # 同步更新用户永久档案
            updated = await self.users_col.find_one_and_update(
                {"user_id": user_id},
                update_pipeline,
                return_document=ReturnDocument.AFTER,
                projection={"affinity": 1},
            )
            new_affinity = updated.get("affinity", old_affinity) if updated else old_affinity

            # 击穿 session 缓存防幻读
            redis = await self.db_manager.get_redis()
            await redis.delete(f"hikari:session:{session_id}")

            logger.debug(
                f"affinity pulse | session={session_id[:8]} user={user_id[:8]} "
                f"delta={increment} mood={bot_emotion} {old_affinity}->{new_affinity}"
            )
            return {"user_id": user_id, "old": old_affinity, "new": new_affinity}

        except Exception:
            logger.error(f"affinity process_daily_chat error: {traceback.format_exc()}")
            return None

    async def update_affinity(
        self, session_id: str, delta: int, reason: str = "identity_discovery"
    ) -> None:
        """
        专项跳变加分出口，支持正负任意增量（边界锁定 0~100）。
        """
        try:
            update_pipeline = [
                {
                    "$set": {
                        "affinity": {
                            "$max": [
                                0,
                                {
                                    "$min": [
                                        100,
                                        {
                                            "$add": [
                                                {"$ifNull": ["$affinity", 10]},
                                                delta,
                                            ]
                                        },
                                    ]
                                },
                            ]
                        },
                        "updated_at": datetime.now(timezone.utc),
                    }
                }
            ]

            session_res = await self.sessions_col.find_one_and_update(
                {"session_id": session_id},
                update_pipeline,
                projection={"user_id": 1},
            )

            if not session_res or not session_res.get("user_id"):
                logger.warning(f"update_affinity aborted: session not found | session_id={session_id[:8]}")
                return

            user_id = session_res["user_id"]
            await self.users_col.update_one({"user_id": user_id}, update_pipeline)

            redis = await self.db_manager.get_redis()
            await redis.delete(f"hikari:session:{session_id}")

            logger.info(f"affinity updated | user={user_id[:8]} delta={delta} reason={reason}")

        except Exception:
            logger.error(f"affinity update_affinity error: {traceback.format_exc()}")


# ── 单例工厂 ──────────────────────────────────────────────────────────────────

_affinity_service: AffinityService | None = None


def get_affinity_service() -> AffinityService:
    global _affinity_service
    if _affinity_service is None:
        _affinity_service = AffinityService()
    return _affinity_service
