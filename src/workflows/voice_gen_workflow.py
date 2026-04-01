# -*- coding: utf-8 -*-
"""
语音生成 Temporal Workflow

两步串行：
  1. generate_tts_audio — TTS 推理 + 落盘 + WS 推流给手机端 + 同步屏幕端
  2. push_voice_result  — 失败时推送错误 WS（成功路径在 activity 内部推送）
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import timedelta
from typing import Optional

from temporalio import activity, workflow
from temporalio.common import RetryPolicy

from src.utils.logger import get_logger

logger = get_logger(__name__)


# ════════════════════════════════════════════════════════════════════════════
# Input dataclass（重构文档 §14 锁定）
# ════════════════════════════════════════════════════════════════════════════

@dataclass
class VoiceGenInput:
    user_id:    str
    session_id: str
    msg_id:     str
    text:       str
    audio_text: str   # 实际送入 TTS 的文本（可与 text 不同，如情感标注后版本）


# ════════════════════════════════════════════════════════════════════════════
# Activities
# ════════════════════════════════════════════════════════════════════════════

@activity.defn(name="generate_tts_audio")
async def generate_tts_audio(
    user_id: str,
    session_id: str,
    msg_id: str,
    audio_text: str,
) -> bool:
    """
    TTS 推理 → 落盘 → WS 推流（手机端 + 屏幕端）。
    超时 TTS_TIMEOUT(300s)，不重试（maximum_attempts=1）。
    成功返回 True，失败返回 False。
    """
    from src.services.asset_service import get_asset_service

    service = get_asset_service()
    try:
        await service.push_voice_generation(
            user_id=user_id,
            session_id=session_id,
            text=audio_text,
            msg_id=msg_id,
        )
        return True
    except Exception as e:
        logger.error(f"generate_tts_audio: failed | msg_id={msg_id[:8]} error={e}")
        return False


@activity.defn(name="push_voice_result")
async def push_voice_result(
    session_id: str,
    msg_id: str,
    success: bool,
    error_message: str = "",
) -> None:
    """
    推送语音生成结果到 WS。
    成功路径由 generate_tts_audio 内部的 asset_service 直接推送，
    此 activity 仅负责推送失败错误通知。
    超时 10s，重试 3 次。
    """
    if success:
        # 成功已在 asset_service.push_voice_generation 内部推送，此处无需重复
        return

    from src.services.ws_result_push import push_tool_error
    await push_tool_error(
        session_id=session_id,
        msg_id=msg_id,
        error_code="voice_gen_failed",
        message=error_message or "语音生成失败了喵",
    )


# ════════════════════════════════════════════════════════════════════════════
# Workflow
# ════════════════════════════════════════════════════════════════════════════

@workflow.defn(name="VoiceGenWorkflow")
class VoiceGenWorkflow:
    """
    两步串行：TTS 推理 → 失败时推送错误 WS。
    """

    @workflow.run
    async def run(self, inp: VoiceGenInput) -> None:
        task_queue = "hikari-main"
        tts_timeout = 300

        # Step 1: TTS 推理 + 推流（超时 TTS_TIMEOUT，重试 1 次）
        success: bool = await workflow.execute_activity(
            generate_tts_audio,
            args=[inp.user_id, inp.session_id, inp.msg_id, inp.audio_text],
            task_queue=task_queue,
            start_to_close_timeout=timedelta(seconds=tts_timeout),
            retry_policy=RetryPolicy(maximum_attempts=1),
        )

        # Step 2: 失败时推送错误通知（超时 10s，重试 3 次）
        if not success:
            await workflow.execute_activity(
                push_voice_result,
                args=[inp.session_id, inp.msg_id, False, "语音生成失败了喵，稍后再试？"],
                task_queue=task_queue,
                start_to_close_timeout=timedelta(seconds=10),
                retry_policy=RetryPolicy(maximum_attempts=3),
            )
