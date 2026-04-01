# -*- coding: utf-8 -*-
"""
文案生成 Temporal Workflow

设计说明：
  文案流程分两轮。Round1（拼网格、上传、存 Redis、推询问气泡）是快速同步操作，
  不进 Temporal，由 copywriting_service.handle_copywriting_round1() 直接处理。

  CopywritingWorkflow 只负责 Round2 的核心步骤（重构文档 §14 锁定 Input 字段）：
    1. generate_copywriting  — 调用豆包生成文案文字
    2. push_copywriting_result — 落盘 + WS 推送结果

  Redis 状态机、取消检测、re-trigger 检测等逻辑由 service 层在触发 Workflow 前完成。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

from temporalio import activity, workflow
from temporalio.common import RetryPolicy

PLATFORM_LABELS: dict[str, str] = {
    "xiaohongshu": "小红书",
    "weibo":       "微博",
    "bilibili":    "B站",
}


# ════════════════════════════════════════════════════════════════════════════
# Input dataclass（重构文档 §14 锁定）
# ════════════════════════════════════════════════════════════════════════════

@dataclass
class CopywritingInput:
    user_id:   str
    session_id: str
    msg_id:    str
    grid_fid:  str
    ref_fid:   Optional[str]
    platform:  str
    user_hint: str


# ════════════════════════════════════════════════════════════════════════════
# Activities
# ════════════════════════════════════════════════════════════════════════════

@activity.defn(name="generate_copywriting")
async def generate_copywriting(
    grid_fid: str,
    ref_fid: Optional[str],
    platform: str,
    user_hint: str,
) -> str:
    """
    调豆包 responses.create + file_id 生成文案。
    失败返回空字符串。超时 60s，重试 2 次。
    """
    from openai import AsyncOpenAI
    from src.utils.api_retry import with_retry
    from src.core.hikari_prompts import build_copywriting_prompt
    from src.utils.logger import get_logger
    _log = get_logger(__name__)

    model  = os.getenv("DOUBAO_MAIN_EP", "")
    prompt = build_copywriting_prompt(platform=platform, user_hint=user_hint, has_ref=bool(ref_fid))
    client = AsyncOpenAI(
        base_url="https://ark.cn-beijing.volces.com/api/v3",
        api_key=os.getenv("DOUBAO_API_KEY"),
    )

    content: list = [{"type": "input_image", "file_id": grid_fid}]
    if ref_fid:
        content.append({"type": "input_image", "file_id": ref_fid})
    content.append({"type": "input_text", "text": prompt})

    try:
        resp = await with_retry(
            client.responses.create,
            model=model,
            input=[{"role": "user", "content": content}],
            temperature=0.8,
            max_output_tokens=600,
            extra_body={"thinking": {"type": "disabled"}},
            task_name="文案生成",
        )
        result = resp.output_text.strip()
        _log.info(f"generate_copywriting: done | platform={platform} len={len(result)}")
        return result
    except Exception as e:
        _log.error(f"generate_copywriting: failed | {e}")
        if not ref_fid:
            return ""
        # 降级：去掉参考图
        try:
            resp2 = await with_retry(
                client.responses.create,
                model=model,
                input=[{"role": "user", "content": [
                    {"type": "input_image", "file_id": grid_fid},
                    {"type": "input_text", "text": prompt},
                ]}],
                temperature=0.8,
                max_output_tokens=600,
                extra_body={"thinking": {"type": "disabled"}},
                task_name="文案生成降级",
            )
            result2 = resp2.output_text.strip()
            _log.info(f"generate_copywriting: fallback done | len={len(result2)}")
            return result2
        except Exception as e2:
            _log.error(f"generate_copywriting: fallback also failed | {e2}")
            # 双重失败写告警，供 health_workflow 可观测
            try:
                from src.utils.database import get_db_manager, COL_ALERTS
                from datetime import datetime, timezone
                await get_db_manager().db[COL_ALERTS].insert_one({
                    "type":       "copywriting_failure",
                    "platform":   platform,
                    "message":    f"文案生成双重失败: {e2}",
                    "created_at": datetime.now(timezone.utc),
                })
            except Exception:
                pass  # 告警写失败不影响主流程
            return ""


@activity.defn(name="push_copywriting_result")
async def push_copywriting_result(
    session_id: str,
    msg_id: str,
    user_id: str,
    copy_text: str,
    platform: str,
) -> None:
    """
    落盘文案消息 + WS 推送 copywriting_result 事件。
    copy_text 为空时推送错误通知。
    超时 10s，重试 3 次。
    """
    from src.services.ws_result_push import push_tool_result, push_tool_error
    from src.utils.database import get_db_manager, COL_MESSAGES
    from src.utils.logger import get_logger
    _log = get_logger(__name__)

    if not copy_text:
        await push_tool_error(
            session_id=session_id,
            msg_id=msg_id,
            error_code="copywriting_failed",
            message="文案生成失败了喵，稍后再试？",
        )
        return

    label = PLATFORM_LABELS.get(platform, "小红书")
    db = get_db_manager().db
    res = await db[COL_MESSAGES].insert_one(
        {
            "session_id": session_id,
            "user_id": user_id,
            "role": "assistant",
            "content": copy_text,
            "copywriting_platform": platform,
            "voice_status": "none",
            "created_at": datetime.now(timezone.utc),
        }
    )
    new_msg_id = str(res.inserted_id)
    await push_tool_result(
        session_id=session_id,
        msg_id=new_msg_id,
        result_type="copywriting_result",
        payload={
            "success": True,
            "platform": platform,
            "platform_label": label,
            "content": copy_text,
        },
    )
    _log.info(f"push_copywriting_result: done | msg_id={new_msg_id[:8]} len={len(copy_text)}")


# ════════════════════════════════════════════════════════════════════════════
# Workflow
# ════════════════════════════════════════════════════════════════════════════

@workflow.defn(name="CopywritingWorkflow")
class CopywritingWorkflow:
    """
    两步串行：文案生成 → 落盘推送。
    Round1 预处理（拼网格、上传、Redis 状态机）在 service 层完成，不进 Workflow。
    """

    @workflow.run
    async def run(self, inp: CopywritingInput) -> None:
        task_queue = "hikari-main"

        # Step 1: 文案生成（超时60s，重试2次）
        copy_text: str = await workflow.execute_activity(
            generate_copywriting,
            args=[inp.grid_fid, inp.ref_fid, inp.platform, inp.user_hint],
            task_queue=task_queue,
            start_to_close_timeout=timedelta(seconds=60),
            retry_policy=RetryPolicy(maximum_attempts=2),
        )

        # Step 2: 落盘 + 推送（超时10s，重试3次）
        await workflow.execute_activity(
            push_copywriting_result,
            args=[inp.session_id, inp.msg_id, inp.user_id, copy_text, inp.platform],
            task_queue=task_queue,
            start_to_close_timeout=timedelta(seconds=10),
            retry_policy=RetryPolicy(maximum_attempts=3),
        )
