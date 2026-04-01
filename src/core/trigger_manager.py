# -*- coding: utf-8 -*-
"""
导出与状态调度中枢 — 希卡莉 (v4.0.0)
=====================================================================
重构对齐 [MASTER v3.0 / 2026-03-29]：
1. 移除 config.config_loader 依赖，参数全走 os.getenv（PERSONA_ARCHIVE_THRESHOLD）。
2. DatabaseManager() 直接实例化 → get_db_manager() 单例工厂。
3. Redis Key 对齐 MASTER 第五节：
   - "lock:archival:{uid}"   → "hikari:lock:archival:{uid}"
   - "limit:engagement:{sid}" → "hikari:limit:engagement:{sid}"
   所有其他 key（milestone:last / archive:count）已符合规范，保持不变。
4. 其余业务逻辑（里程碑幂等锁、归档原子自愈、冷场整活冷却）完全保留。
=====================================================================
"""

import os
import asyncio
import traceback
from typing import Optional, List, Dict

from src.core.memory_storage import get_memory_storage
from src.core.session_manager import get_session_manager
from src.utils.database import get_db_manager
from src.utils.logger import get_logger

logger = get_logger(__name__)


class TriggerManager:
    """
    希卡莉喵的"调度防线"：负责在异步环境下的幂等性状态管理。

    职责：
    - 好感度里程碑检测（Redis 幂等锁，单 Session 内仅触发一次）
    - 画像归档调度（NX 锁防并发，增量阈值判定）
    - 冷场整活触发器（30 秒冷却防过度响应）
    """

    def __init__(self):
        self.memory_storage  = get_memory_storage()
        self.session_manager = get_session_manager()
        self.db_manager      = get_db_manager()

        self.milestones        = [30, 70, 90]
        self.archive_threshold = int(os.getenv("PERSONA_ARCHIVE_THRESHOLD", 5))
        self.engagement_cooldown = 30   # 整活冷却秒数

        logger.info("🐾 [Trigger-Manager] v4.0.0 异步调度防线就绪")

    # ──────────────────────────────────────────────────────────────
    # 1. 好感度里程碑监测（原子幂等）
    # ──────────────────────────────────────────────────────────────

    async def check_affinity_milestone(
        self,
        session_id: str,
        old_affinity: int,
        new_affinity: int,
    ) -> Optional[int]:
        """
        [隔离检测 + 幂等锁] 确保每个里程碑在单次 Session 内仅触发一次。

        逻辑：跨越阈值 且 该阈值大于本 Session 历史触发记录 → 触发。
        Redis key: hikari:milestone:last:{session_id}（MASTER 第五节，永久 TTL）
        """
        redis = await self.db_manager.get_redis()
        last_milestone_key = f"hikari:milestone:last:{session_id}"

        last_m_raw = await redis.get(last_milestone_key)
        last_m = int(last_m_raw) if last_m_raw else 0

        for m in self.milestones:
            if old_affinity < m <= new_affinity and m > last_m:
                await redis.set(last_milestone_key, str(m))
                logger.info(
                    f"🎊 [Milestone] 关系进阶 | sid={session_id[:8]} | "
                    f"阈值={m} | 历史最高={last_m}"
                )
                return m
        return None

    # ──────────────────────────────────────────────────────────────
    # 2. 画像归档调度（NX 锁 + 增量阈值）
    # ──────────────────────────────────────────────────────────────

    async def check_and_run_persona_archival(
        self,
        session_id: str,
        force: bool = False,
    ) -> bool:
        """
        [原子自愈] 判断是否触发画像归档，防止多协程并发重复归档。

        流程：
        1. 通过 session_id 打捞 user_id
        2. NX 锁（hikari:lock:archival:{uid}，TTL=10s）防并发
        3. 打捞全量事实碎片，计算增量
        4. 增量 >= PERSONA_ARCHIVE_THRESHOLD 时锁定进度锚点并返回 True

        Redis key: hikari:lock:archival:{uid}（MASTER 第五节，TTL=10s）
        """
        try:
            session = await self.session_manager.get_session(session_id)
            if not session:
                return False

            user_id = session.get("user_id")
            if not user_id:
                return False

            redis = await self.db_manager.get_redis()

            # NX 锁：防止多个异步任务同时触发归档
            archive_lock_key = f"hikari:lock:archival:{user_id}"
            lock_success = await redis.set(archive_lock_key, "locked", nx=True, ex=10)
            if not lock_success:
                return False

            try:
                all_fragments = await self.memory_storage.get_user_all_fragments(user_id)
                total_count   = sum(len(v) for v in all_fragments.values())
                last_count    = await self._get_last_archive_count(user_id)
                increment     = total_count - last_count

                if force or (total_count > 0 and increment >= self.archive_threshold):
                    logger.info(
                        f"📂 [Archival] 归档触发 | uid={user_id[:8]} | "
                        f"碎片增量={increment} | 总计={total_count}"
                    )
                    return await self._lock_persona_sync_state(user_id, total_count)
            finally:
                await redis.delete(archive_lock_key)

            return False

        except Exception:
            logger.error(f"❌ [Archival] 归档异常: {traceback.format_exc()}")
            return False

    # ──────────────────────────────────────────────────────────────
    # 3. 归档进度锚点（Redis 持久 key）
    # ──────────────────────────────────────────────────────────────

    async def _lock_persona_sync_state(self, user_id: str, current_count: int) -> bool:
        """物理锁定归档进度锚点"""
        try:
            await self._set_last_archive_count(user_id, current_count)
            return True
        except Exception as e:
            logger.error(f"❌ [Archival] 进度锚点写入失败: {e}")
            return False

    async def _get_last_archive_count(self, user_id: str) -> int:
        """读取归档进度锚点（hikari:archive:count:{uid}）"""
        redis = await self.db_manager.get_redis()
        val = await redis.get(f"hikari:archive:count:{user_id}")
        return int(val) if val else 0

    async def _set_last_archive_count(self, user_id: str, count: int) -> None:
        """写入归档进度锚点（永久 TTL，MASTER 第五节）"""
        redis = await self.db_manager.get_redis()
        await redis.set(f"hikari:archive:count:{user_id}", str(count))

    # ──────────────────────────────────────────────────────────────
    # 4. 冷场整活触发器（30 秒冷却防过度响应）
    # ──────────────────────────────────────────────────────────────

    async def check_engagement_need(
        self,
        session_id: str,
        history: List[Dict],
    ) -> bool:
        """
        [降噪检测] 判断是否触发冷场整活。

        条件：最近 3 条 user 消息平均长度 < 5 字，且不在 30 秒冷却期内。
        Redis key: hikari:limit:engagement:{session_id}（MASTER 第五节，TTL=30s）
        """
        redis = await self.db_manager.get_redis()

        cooldown_key = f"hikari:limit:engagement:{session_id}"
        in_cooldown  = await redis.get(cooldown_key)
        if in_cooldown:
            return False

        user_msgs = [m for m in history if m.get("role") == "user"][-3:]
        if len(user_msgs) < 3:
            return False

        total_len = sum(len(str(m.get("content", ""))) for m in user_msgs)
        avg_len   = total_len / 3

        if avg_len < 5:
            await redis.setex(cooldown_key, self.engagement_cooldown, "1")
            logger.info(
                f"🐾 [Engagement] 冷场整活触发 | avg={avg_len:.1f} | "
                f"sid={session_id[:8]}"
            )
            return True
        return False


# ══════════════════════════════════════════════════════════════════
# 单例导出
# ══════════════════════════════════════════════════════════════════

_trigger_manager: TriggerManager | None = None


def get_trigger_manager() -> TriggerManager:
    global _trigger_manager
    if _trigger_manager is None:
        _trigger_manager = TriggerManager()
    return _trigger_manager
