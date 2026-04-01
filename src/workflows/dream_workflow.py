# -*- coding: utf-8 -*-
"""
AutoDream 记忆整合 Temporal Workflow

DreamWorkflow 作为 Cron Workflow，每天凌晨 3 点扫描记忆膨胀的用户并触发整合。
整合逻辑实现在 fact_extractor.consolidate_facts()。

触发策略：
1. 每天只跑一次（cron_schedule 控制）
2. 每用户 core_facts 活跃条目 > AUTODREAM_THRESHOLD（默认 30）才整合
3. 串行处理用户，防止同时发起大量 LLM 调用
4. 单用户整合失败不影响其他用户
"""

from __future__ import annotations

import os
from datetime import timedelta

from temporalio import activity, workflow
from temporalio.common import RetryPolicy

from src.utils.logger import get_logger

logger = get_logger(__name__)


# ════════════════════════════════════════════════════════════════════════════
# Activity
# ════════════════════════════════════════════════════════════════════════════

@activity.defn(name="run_dream_consolidation")
async def run_dream_consolidation() -> dict:
    """
    扫描所有满足条件的用户，逐一执行记忆整合。
    超时 3600s，重试 0 次（失败等下一次 Cron）。
    返回：{"processed": int, "total_merged": int, "total_pruned": int}
    """
    from src.utils.database import get_db_manager, COL_FACTS
    from src.core.fact_extractor import get_fact_extractor

    threshold = int(os.getenv("AUTODREAM_THRESHOLD", 30))
    db_manager = get_db_manager()
    db = db_manager.db

    # 找出活跃 fact 数超过阈值的用户
    pipeline = [
        {"$match": {"is_active": True}},
        {"$group": {"_id": "$user_id", "count": {"$sum": 1}}},
        {"$match": {"count": {"$gt": threshold}}},
    ]
    candidates = await db[COL_FACTS].aggregate(pipeline).to_list(length=1000)
    logger.info(f"[AutoDream] 找到 {len(candidates)} 个待整合用户 | threshold={threshold}")

    extractor = get_fact_extractor()
    total_merged = 0
    total_pruned = 0
    processed = 0

    for c in candidates:
        user_id = c["_id"]
        if not user_id:
            continue
        try:
            result = await extractor.consolidate_facts(user_id)
            total_merged += result.get("merged", 0)
            total_pruned += result.get("pruned", 0)
            processed += 1
        except Exception as e:
            logger.warning(f"[AutoDream] 用户整合失败，跳过 | uid={user_id[:8]} | {e}")

    logger.info(
        f"[AutoDream] 批次完成 | processed={processed} | "
        f"total_merged={total_merged} | total_pruned={total_pruned}"
    )
    return {"processed": processed, "total_merged": total_merged, "total_pruned": total_pruned}


# ════════════════════════════════════════════════════════════════════════════
# Workflow
# ════════════════════════════════════════════════════════════════════════════

@workflow.defn(name="DreamWorkflow")
class DreamWorkflow:
    """AutoDream 记忆整合 Cron Workflow，每天凌晨执行一次。"""

    @workflow.run
    async def run(self) -> None:
        try:
            result = await workflow.execute_activity(
                run_dream_consolidation,
                schedule_to_close_timeout=timedelta(seconds=3600),
                retry_policy=RetryPolicy(maximum_attempts=1),
            )
            workflow.logger.info(f"[AutoDream] Workflow 完成 | {result}")
        except Exception as e:
            workflow.logger.error(f"[AutoDream] Workflow 异常: {e}")
