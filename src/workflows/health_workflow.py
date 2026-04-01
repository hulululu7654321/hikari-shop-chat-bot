# -*- coding: utf-8 -*-
"""
系统健康检查 Temporal Workflow

HealthCheckWorkflow 作为 Cron Workflow 每 5 分钟执行一次（"*/5 * * * *"）。
两步串行：
  1. check_system_health   — 检查 TTS/Redis/MongoDB/ES/Qdrant 健康状态
  2. persist_health_result — 将结果写入 MongoDB core_system_alerts 集合
"""

from __future__ import annotations

import os
import asyncio
import traceback
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

from temporalio import activity, workflow
from temporalio.common import RetryPolicy

from src.utils.logger import get_logger

logger = get_logger(__name__)


# ════════════════════════════════════════════════════════════════════════════
# Result dataclass（重构文档 §14 锁定）
# ════════════════════════════════════════════════════════════════════════════

@dataclass
class HealthCheckResult:
    timestamp:           str
    tts_ok:              bool
    redis_ok:            bool
    mongodb_ok:          bool
    es_ok:               bool
    qdrant_ok:           bool
    tts_available_ports: list[int] = field(default_factory=list)


# ════════════════════════════════════════════════════════════════════════════
# Activities
# ════════════════════════════════════════════════════════════════════════════

@activity.defn(name="check_system_health")
async def check_system_health() -> dict:
    """
    并行探测各基础设施健康状态。
    返回 HealthCheckResult 的 dict 序列化。
    超时 30s，重试 0 次。
    """
    import aiohttp
    from src.utils.database import get_db_manager

    now_str = datetime.now(timezone.utc).isoformat()

    async def _check_tts_port(host: str, port: str) -> tuple[str, bool]:
        """向单个 TTS 端口发 GET /，返回 (port, is_ok)。"""
        url = f"{host}:{port}/"
        try:
            timeout = aiohttp.ClientTimeout(total=5)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(url) as resp:
                    return port, resp.status < 500
        except Exception:
            return port, False

    async def _check_redis() -> bool:
        try:
            redis = await get_db_manager().get_redis()
            return await redis.ping()
        except Exception:
            return False

    async def _check_mongodb() -> bool:
        try:
            db = get_db_manager().db
            await db.command("ping")
            return True
        except Exception:
            return False

    async def _check_es() -> bool:
        es_host = os.getenv("ES_HOST", "http://hikari-es:9200")
        try:
            timeout = aiohttp.ClientTimeout(total=5)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(f"{es_host}/_cluster/health") as resp:
                    if resp.status == 200:
                        data = await resp.json(content_type=None)
                        return data.get("status") in ("green", "yellow")
            return False
        except Exception:
            return False

    async def _check_qdrant() -> bool:
        qdrant_url = os.getenv("QDRANT_URL", "http://hikari-qdrant:6333")
        try:
            timeout = aiohttp.ClientTimeout(total=5)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(f"{qdrant_url}/healthz") as resp:
                    return resp.status == 200
        except Exception:
            return False

    # TTS 端口列表
    tts_host = os.getenv("TTS_ENGINE_HOST", "http://host.docker.internal").rstrip("/")
    ports_env = os.getenv("TTS_ENGINE_PORTS", "9880,9881")
    tts_ports = [p.strip() for p in ports_env.split(",")]

    # 并行探测所有检查项
    tts_checks = [_check_tts_port(tts_host, p) for p in tts_ports]
    all_results = await asyncio.gather(
        asyncio.gather(*tts_checks),
        _check_redis(),
        _check_mongodb(),
        _check_es(),
        _check_qdrant(),
        return_exceptions=True,
    )

    tts_results_raw, redis_ok, mongodb_ok, es_ok, qdrant_ok = all_results

    # 处理 TTS 结果
    tts_available_ports: list[int] = []
    tts_ok = False
    if isinstance(tts_results_raw, list):
        for port_str, ok in tts_results_raw:
            if ok:
                tts_available_ports.append(int(port_str))
        tts_ok = len(tts_available_ports) > 0

    # 处理其他结果（Exception 视为 False）
    redis_ok = redis_ok if isinstance(redis_ok, bool) else False
    mongodb_ok = mongodb_ok if isinstance(mongodb_ok, bool) else False
    es_ok = es_ok if isinstance(es_ok, bool) else False
    qdrant_ok = qdrant_ok if isinstance(qdrant_ok, bool) else False

    result = HealthCheckResult(
        timestamp=now_str,
        tts_ok=tts_ok,
        redis_ok=redis_ok,
        mongodb_ok=mongodb_ok,
        es_ok=es_ok,
        qdrant_ok=qdrant_ok,
        tts_available_ports=tts_available_ports,
    )

    logger.info(
        f"check_system_health: tts={tts_ok}({len(tts_available_ports)}ports) "
        f"redis={redis_ok} mongo={mongodb_ok} es={es_ok} qdrant={qdrant_ok}"
    )
    return result.__dict__


@activity.defn(name="persist_health_result")
async def persist_health_result(result: dict) -> None:
    """
    将健康检查结果写入 MongoDB core_system_alerts 集合。
    若有服务不健康，额外写入告警记录。
    超时 10s，重试 0 次。
    """
    from src.utils.database import get_db_manager, COL_ALERTS

    db = get_db_manager().db
    now = datetime.now(timezone.utc)

    # 写入健康检查快照
    await db[COL_ALERTS].insert_one(
        {
            "type": "health_check",
            "timestamp": result.get("timestamp", now.isoformat()),
            "tts_ok": result.get("tts_ok", False),
            "redis_ok": result.get("redis_ok", False),
            "mongodb_ok": result.get("mongodb_ok", False),
            "es_ok": result.get("es_ok", False),
            "qdrant_ok": result.get("qdrant_ok", False),
            "tts_available_ports": result.get("tts_available_ports", []),
            "created_at": now,
        }
    )

    # 告警：列出所有不健康的服务
    unhealthy = []
    checks = {
        "tts": result.get("tts_ok", False),
        "redis": result.get("redis_ok", False),
        "mongodb": result.get("mongodb_ok", False),
        "es": result.get("es_ok", False),
        "qdrant": result.get("qdrant_ok", False),
    }
    for svc, ok in checks.items():
        if not ok:
            unhealthy.append(svc)

    if unhealthy:
        await db[COL_ALERTS].insert_one(
            {
                "type": "alert",
                "level": "error",
                "services": unhealthy,
                "message": f"以下服务健康检查失败：{', '.join(unhealthy)}",
                "timestamp": result.get("timestamp", now.isoformat()),
                "created_at": now,
            }
        )
        logger.warning(f"persist_health_result: unhealthy services={unhealthy}")
    else:
        logger.debug("persist_health_result: all services healthy")


# ════════════════════════════════════════════════════════════════════════════
# Workflow
# ════════════════════════════════════════════════════════════════════════════

@workflow.defn(name="HealthCheckWorkflow")
class HealthCheckWorkflow:
    """
    Cron Workflow，每 5 分钟执行一次（"*/5 * * * *"）。
    两步串行：健康检查 → 落盘告警。
    两个 activity 均重试 0 次（探测失败不重试）。
    """

    @workflow.run
    async def run(self) -> None:
        task_queue = "hikari-main"

        # Step 1: 健康检查（超时30s，重试0次）
        result: dict = await workflow.execute_activity(
            check_system_health,
            task_queue=task_queue,
            start_to_close_timeout=timedelta(seconds=30),
            retry_policy=RetryPolicy(maximum_attempts=1),  # 不重试
        )

        # Step 2: 持久化结果（超时10s，重试0次）
        await workflow.execute_activity(
            persist_health_result,
            args=[result],
            task_queue=task_queue,
            start_to_close_timeout=timedelta(seconds=10),
            retry_policy=RetryPolicy(maximum_attempts=1),  # 不重试
        )
