# -*- coding: utf-8 -*-
"""
hikari-worker 容器入口

职责：
- 连接 Temporal，注册所有 Workflow 和 Activity
- 通过 ENABLE_WORKFLOW_* 环境变量控制按需注册（false=关闭注册，不改代码）
"""

import asyncio
import logging
import os

from temporalio.client import Client
from temporalio.worker import Worker

from src.utils.logger import get_logger

logger = get_logger(__name__)

# ── Workflow 开关（重构文档 §9 锁定）────────────────────────────────────────────
ENABLED_WORKFLOWS = {
    "imagegen":    os.getenv("ENABLE_WORKFLOW_IMAGEGEN", "true") == "true",
    "voice":       os.getenv("ENABLE_WORKFLOW_VOICE", "true") == "true",
    "makeup":      os.getenv("ENABLE_WORKFLOW_MAKEUP", "true") == "true",
    "copywriting": os.getenv("ENABLE_WORKFLOW_COPYWRITING", "true") == "true",
    "push":        os.getenv("ENABLE_WORKFLOW_PUSH", "true") == "true",
    "health":      os.getenv("ENABLE_WORKFLOW_HEALTH", "true") == "true",
    "dream":       os.getenv("ENABLE_WORKFLOW_DREAM", "true") == "true",
}


async def main() -> None:
    temporal_host = os.getenv("TEMPORAL_HOST", "hikari-temporal:7233")
    temporal_namespace = os.getenv("TEMPORAL_NAMESPACE", "default")
    task_queue = os.getenv("TEMPORAL_TASK_QUEUE", "hikari-main")
    push_cron = os.getenv("PUSH_CRON_EXPR", "0 2 * * *")

    logger.info(
        f"worker_main: starting | host={temporal_host} ns={temporal_namespace} "
        f"queue={task_queue} enabled={[k for k, v in ENABLED_WORKFLOWS.items() if v]}"
    )

    # 按需 import，未启用的 workflow 不 import，避免 import 错误影响其他 workflow
    workflows = []
    activities = []

    # ── ImageGen ─────────────────────────────────────────────────────────────
    if ENABLED_WORKFLOWS["imagegen"]:
        from src.workflows.image_gen_workflow import (
            ImageGenWorkflow,
            analyze_coser_image,
            generate_quad_image,
            save_gen_image,
            push_image_gen_result,
        )
        workflows.append(ImageGenWorkflow)
        activities.extend([analyze_coser_image, generate_quad_image, save_gen_image, push_image_gen_result])
        logger.info("worker_main: ImageGenWorkflow registered")

    # ── Voice ─────────────────────────────────────────────────────────────────
    if ENABLED_WORKFLOWS["voice"]:
        from src.workflows.voice_gen_workflow import (
            VoiceGenWorkflow,
            generate_tts_audio,
            push_voice_result,
        )
        workflows.append(VoiceGenWorkflow)
        activities.extend([generate_tts_audio, push_voice_result])
        logger.info("worker_main: VoiceGenWorkflow registered")

    # ── Makeup ────────────────────────────────────────────────────────────────
    if ENABLED_WORKFLOWS["makeup"]:
        from src.workflows.makeup_workflow import (
            MakeupWorkflow,
            check_makeup_image,
            analyze_makeup,
            wrap_as_hikari_tone,
            push_makeup_result,
        )
        workflows.append(MakeupWorkflow)
        activities.extend([check_makeup_image, analyze_makeup, wrap_as_hikari_tone, push_makeup_result])
        logger.info("worker_main: MakeupWorkflow registered")

    # ── Copywriting ───────────────────────────────────────────────────────────
    if ENABLED_WORKFLOWS["copywriting"]:
        from src.workflows.copywriting_workflow import (
            CopywritingWorkflow,
            generate_copywriting,
            push_copywriting_result,
        )
        workflows.append(CopywritingWorkflow)
        activities.extend([generate_copywriting, push_copywriting_result])
        logger.info("worker_main: CopywritingWorkflow registered")

    # ── Push ──────────────────────────────────────────────────────────────────
    if ENABLED_WORKFLOWS["push"]:
        from src.workflows.push_workflow import (
            PushSchedulerWorkflow,
            run_daily_push,
        )
        workflows.append(PushSchedulerWorkflow)
        activities.append(run_daily_push)
        logger.info(f"worker_main: PushSchedulerWorkflow registered | cron={push_cron}")

    # ── Health ────────────────────────────────────────────────────────────────
    if ENABLED_WORKFLOWS["health"]:
        from src.workflows.health_workflow import (
            HealthCheckWorkflow,
            check_system_health,
            persist_health_result,
        )
        workflows.append(HealthCheckWorkflow)
        activities.extend([check_system_health, persist_health_result])
        logger.info("worker_main: HealthCheckWorkflow registered | cron=*/5 * * * *")

    # ── Dream（AutoDream 记忆整合）────────────────────────────────────────────
    if ENABLED_WORKFLOWS["dream"]:
        from src.workflows.dream_workflow import (
            DreamWorkflow,
            run_dream_consolidation,
        )
        workflows.append(DreamWorkflow)
        activities.append(run_dream_consolidation)
        logger.info("worker_main: DreamWorkflow registered | cron=0 3 * * *")

    if not workflows:
        logger.warning("worker_main: no workflows enabled, worker will idle")

    # 连接 Temporal
    client = await Client.connect(
        temporal_host,
        namespace=temporal_namespace,
    )

    # 注册 Cron Workflow（Push + Health）
    # Cron workflow 需要通过 client.start_workflow 启动，Worker 只负责执行
    # worker_main 在首次启动时确保 cron workflow 已提交
    await _ensure_cron_workflows(client, task_queue, push_cron)

    # 启动 Worker
    worker = Worker(
        client,
        task_queue=task_queue,
        workflows=workflows,
        activities=activities,
    )

    logger.info("worker_main: worker started, waiting for tasks...")
    await worker.run()
    logger.info("worker_main: worker stopped")


async def _ensure_cron_workflows(client: "Client", task_queue: str, push_cron: str) -> None:
    """
    确保 Cron Workflow 已在 Temporal 中提交。
    使用 workflow_id 幂等提交，重复提交不会创建新实例。
    """

    # Push Scheduler
    if ENABLED_WORKFLOWS["push"] and os.getenv("PUSH_ENABLED", "false").lower() == "true":
        try:
            from src.workflows.push_workflow import PushSchedulerWorkflow
            await client.start_workflow(
                PushSchedulerWorkflow.run,
                id="hikari-push-scheduler",
                task_queue=task_queue,
                cron_schedule=push_cron,
            )
            logger.info(f"worker_main: PushSchedulerWorkflow cron submitted | expr={push_cron}")
        except Exception as e:
            # AlreadyStarted 是正常情况（重启后幂等）
            if "already" in str(e).lower():
                logger.debug("worker_main: PushSchedulerWorkflow already running")
            else:
                logger.warning(f"worker_main: PushSchedulerWorkflow submit failed | {e}")

    # Health Check
    if ENABLED_WORKFLOWS["health"]:
        try:
            from src.workflows.health_workflow import HealthCheckWorkflow
            await client.start_workflow(
                HealthCheckWorkflow.run,
                id="hikari-health-check",
                task_queue=task_queue,
                cron_schedule="*/5 * * * *",
            )
            logger.info("worker_main: HealthCheckWorkflow cron submitted")
        except Exception as e:
            if "already" in str(e).lower():
                logger.debug("worker_main: HealthCheckWorkflow already running")
            else:
                logger.warning(f"worker_main: HealthCheckWorkflow submit failed | {e}")

    # AutoDream 记忆整合
    if ENABLED_WORKFLOWS["dream"] and os.getenv("DREAM_ENABLED", "false").lower() == "true":
        try:
            from src.workflows.dream_workflow import DreamWorkflow
            dream_cron = os.getenv("DREAM_CRON_EXPR", "0 3 * * *")
            await client.start_workflow(
                DreamWorkflow.run,
                id="hikari-dream-consolidation",
                task_queue=task_queue,
                cron_schedule=dream_cron,
            )
            logger.info(f"worker_main: DreamWorkflow cron submitted | expr={dream_cron}")
        except Exception as e:
            if "already" in str(e).lower():
                logger.debug("worker_main: DreamWorkflow already running")
            else:
                logger.warning(f"worker_main: DreamWorkflow submit failed | {e}")


if __name__ == "__main__":
    asyncio.run(main())
