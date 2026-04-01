# -*- coding: utf-8 -*-
"""
妆容分析 Temporal Workflow

四步串行：
  1. check_makeup_image  — 轻量校验图片是否是 cosplay 妆容照
  2. analyze_makeup      — 视觉分析 + 联网搜索角色妆容参考
  3. wrap_as_hikari_tone — 把分析报告转成希卡莉口吻
  4. push_makeup_result  — 落盘 + WS 推送结果
"""

from __future__ import annotations

import os
import io
import base64
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

from temporalio import activity, workflow
from temporalio.common import RetryPolicy


# ════════════════════════════════════════════════════════════════════════════
# Input dataclass（重构文档 §14 锁定）
# ════════════════════════════════════════════════════════════════════════════

@dataclass
class MakeupInput:
    user_id:    str
    session_id: str
    image_id:   str
    user_text:  str
    msg_id:     str


# ════════════════════════════════════════════════════════════════════════════
# Activities
# ════════════════════════════════════════════════════════════════════════════

@activity.defn(name="check_makeup_image")
async def check_makeup_image(
    user_id: str,
    image_id: str,
) -> dict:
    """
    上传图片 + 轻量校验是否是 cosplay 妆容照。
    返回 {"is_makeup": bool, "file_id": str, "image_base64": str}
    file_id / image_base64 供后续 activity 复用，失败时为空字符串。
    超时 30s，重试 2 次。
    """
    from openai import AsyncOpenAI
    from src.utils.vision_helper import get_vision_helper
    from src.utils.api_retry import with_retry
    from src.core.hikari_prompts import MAKEUP_CHECK_PROMPT
    from src.utils.logger import get_logger
    _log = get_logger(__name__)

    vh = get_vision_helper()
    img_path = os.path.join(vh.upload_root, user_id, f"{image_id}.jpg")
    image_base64 = vh.get_image_base64(img_path)
    if not image_base64:
        _log.error(f"check_makeup_image: image not found | path={img_path}")
        return {"is_makeup": False, "file_id": "", "image_base64": ""}

    client = AsyncOpenAI(
        base_url="https://ark.cn-beijing.volces.com/api/v3",
        api_key=os.getenv("DOUBAO_API_KEY"),
    )

    # 上传图片拿 file_id
    try:
        buf = io.BytesIO(base64.b64decode(image_base64))
        buf.name = f"{image_id}.jpg"
        uploaded = await with_retry(
            client.files.create,
            file=buf,
            purpose="user_data",
            task_name="妆容图上传",
        )
        file_id = uploaded.id
        _log.info(f"check_makeup_image: uploaded | file_id={file_id[:16]}")
    except Exception as e:
        _log.error(f"check_makeup_image: upload failed | {e}")
        return {"is_makeup": False, "file_id": "", "image_base64": ""}

    # 轻量校验（max_output_tokens=10，只需 YES/NO）
    try:
        resp = await with_retry(
            client.responses.create,
            model=os.getenv("DOUBAO_MAIN_EP", ""),
            input=[
                {
                    "role": "user",
                    "content": [
                        {"type": "input_image", "file_id": file_id},
                        {"type": "input_text", "text": MAKEUP_CHECK_PROMPT},
                    ],
                }
            ],
            temperature=0.0,
            max_output_tokens=10,
            extra_body={"thinking": {"type": "disabled"}},
            task_name="妆容图校验",
        )
        answer = resp.output_text.strip().upper()
        is_makeup = answer.startswith("Y")
        _log.info(f"check_makeup_image: answer={answer!r} is_makeup={is_makeup}")
        return {"is_makeup": is_makeup, "file_id": file_id, "image_base64": image_base64}
    except Exception as e:
        # 校验失败放行，不影响用户体验
        _log.warning(f"check_makeup_image: check failed, pass through | {e}")
        return {"is_makeup": True, "file_id": file_id, "image_base64": image_base64}


@activity.defn(name="analyze_makeup")
async def analyze_makeup(
    file_id: str,
    image_base64: str,
    user_text: str,
) -> str:
    """
    视觉分析 + 联网搜索角色妆容参考。
    返回分析报告文字，失败返回空字符串。
    超时 60s，重试 2 次。
    """
    import re
    from openai import AsyncOpenAI
    from src.utils.api_retry import with_retry
    from src.core.hikari_prompts import build_makeup_analysis_prompt
    from src.services.makeup_service import MAKEUP_TRIGGERS
    from src.utils.logger import get_logger
    _log = get_logger(__name__)

    # 提取 user_hint（去除触发词）
    trigger_pattern = "|".join(re.escape(kw) for kw in MAKEUP_TRIGGERS)
    user_hint = re.sub(trigger_pattern, "", user_text or "").strip()
    user_hint = re.sub(r"^[，。,.\s]+", "", user_hint).strip()

    model = os.getenv("DOUBAO_MAIN_EP", "")
    prompt = build_makeup_analysis_prompt(user_hint)
    client = AsyncOpenAI(
        base_url="https://ark.cn-beijing.volces.com/api/v3",
        api_key=os.getenv("DOUBAO_API_KEY"),
    )

    # 主路径：responses API + web_search + file_id
    try:
        resp = await with_retry(
            client.responses.create,
            model=model,
            input=[
                {
                    "role": "user",
                    "content": [
                        {"type": "input_image", "file_id": file_id},
                        {"type": "input_text", "text": prompt},
                    ],
                }
            ],
            temperature=0.7,
            max_output_tokens=1000,
            tools=[{"type": "web_search"}],
            extra_body={"thinking": {"type": "disabled"}},
            task_name="妆容分析",
        )
        result = resp.output_text.strip()
        _log.info(f"analyze_makeup: done | len={len(result)} hint={repr(user_hint[:20])}")
        return result
    except Exception as e:
        _log.error(f"analyze_makeup: responses API failed, falling back | {e}")

    # 降级路径：chat.completions + base64（不联网）
    try:
        r = await with_retry(
            AsyncOpenAI(
                base_url="https://ark.cn-beijing.volces.com/api/v3",
                api_key=os.getenv("DOUBAO_API_KEY"),
            ).chat.completions.create,
            model=model,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:image/jpeg;base64,{image_base64}"},
                        },
                        {"type": "text", "text": prompt},
                    ],
                }
            ],
            temperature=0.7,
            max_tokens=800,
            extra_body={"thinking": {"type": "disabled"}},
            task_name="妆容分析降级",
        )
        result = r.choices[0].message.content.strip()
        _log.info(f"analyze_makeup: fallback done | len={len(result)}")
        return result
    except Exception as e2:
        _log.error(f"analyze_makeup: fallback also failed | {e2}")
        return ""


@activity.defn(name="wrap_as_hikari_tone")
async def wrap_as_hikari_tone(analysis_text: str) -> str:
    """
    把分析报告转成希卡莉口吻输出。
    失败时返回原始分析文本，不影响主流程。
    超时 30s，重试 2 次。
    """
    from openai import AsyncOpenAI
    from src.utils.api_retry import with_retry
    from src.utils.logger import get_logger
    _log = get_logger(__name__)

    model = os.getenv("DOUBAO_MAIN_EP", "")
    prompt = (
        "你是希卡莉喵，一只傲娇护短的白猫娘。"
        "用你的语气重新表达下面这段妆容分析，要求：\n"
        "- 保留所有分析内容和结论，不能删减或改变意思\n"
        "- 自称「本喵」，语气傲娇但真心在帮忙，嘴硬心软\n"
        "- 结构保持【角色识别】【整体还原度】【问题和改法】三段，不要加其他段落\n"
        "- 每段开头可以带一句希卡莉风格的吐槽或点评，但分析内容必须完整保留\n"
        "- 结尾加一句傲娇的鼓励\n"
        "- 严禁 Markdown 符号\n\n"
        f"原始分析：\n{analysis_text}"
    )
    try:
        r = await with_retry(
            AsyncOpenAI(
                base_url="https://ark.cn-beijing.volces.com/api/v3",
                api_key=os.getenv("DOUBAO_API_KEY"),
            ).chat.completions.create,
            model=model,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.8,
            max_tokens=800,
            extra_body={"thinking": {"type": "disabled"}},
            task_name="妆容口吻转换",
        )
        result = r.choices[0].message.content.strip()
        _log.info(f"wrap_as_hikari_tone: done | len={len(result)}")
        return result
    except Exception as e:
        _log.warning(f"wrap_as_hikari_tone: failed, using raw analysis | {e}")
        return analysis_text


@activity.defn(name="push_makeup_result")
async def push_makeup_result(
    session_id: str,
    msg_id: str,
    user_id: str,
    success: bool,
    payload: dict,
) -> None:
    """
    落盘 + WS 推送妆容分析结果。
    成功路径：insert assistant 消息 → push makeup_result。
    失败路径：仅 push error。
    超时 10s，重试 3 次。
    """
    from src.services.ws_result_push import push_tool_result, push_tool_error
    from src.utils.database import get_db_manager, COL_MESSAGES
    from src.utils.logger import get_logger
    _log = get_logger(__name__)

    if not success:
        await push_tool_error(
            session_id=session_id,
            msg_id=msg_id,
            error_code="makeup_failed",
            message=payload.get("error", "本喵的妆容分析系统抽风了喵"),
        )
        return

    hikari_text = payload.get("hikari_text", "")

    # 非妆容图：用普通 text 气泡回复
    if payload.get("not_makeup"):
        reject_text = "这张图好像和妆容没什么关系喵，换一张 coser 的妆容照片试试？"
        db = get_db_manager().db
        res = await db[COL_MESSAGES].insert_one(
            {
                "session_id": session_id,
                "user_id": user_id,
                "role": "assistant",
                "content": reject_text,
                "voice_status": "none",
                "created_at": datetime.now(timezone.utc),
            }
        )
        await push_tool_result(
            session_id=session_id,
            msg_id=str(res.inserted_id),
            result_type="makeup_result",
            payload={"success": False, "not_makeup": True, "content": reject_text},
        )
        return

    # 正常妆容分析：落盘 + push
    db = get_db_manager().db
    res = await db[COL_MESSAGES].insert_one(
        {
            "session_id": session_id,
            "user_id": user_id,
            "role": "assistant",
            "content": hikari_text,
            "voice_status": "none",
            "created_at": datetime.now(timezone.utc),
        }
    )
    new_msg_id = str(res.inserted_id)
    await push_tool_result(
        session_id=session_id,
        msg_id=new_msg_id,
        result_type="makeup_result",
        payload={"success": True, "content": hikari_text},
    )
    _log.info(f"push_makeup_result: done | msg_id={new_msg_id[:8]}")


# ════════════════════════════════════════════════════════════════════════════
# Workflow
# ════════════════════════════════════════════════════════════════════════════

@workflow.defn(name="MakeupWorkflow")
class MakeupWorkflow:
    """
    四步串行，每步独立重试，最终失败推 error WS。
    """

    @workflow.run
    async def run(self, inp: MakeupInput) -> None:
        task_queue = "hikari-main"

        # Step 1: 校验图片（超时30s，重试2次）
        check_result: dict = await workflow.execute_activity(
            check_makeup_image,
            args=[inp.user_id, inp.image_id],
            task_queue=task_queue,
            start_to_close_timeout=timedelta(seconds=30),
            retry_policy=RetryPolicy(maximum_attempts=2),
        )

        if not check_result.get("file_id"):
            # 图片读取失败
            await workflow.execute_activity(
                push_makeup_result,
                args=[inp.session_id, inp.msg_id, inp.user_id, False,
                      {"error": "找不到图片了喵，重新发一下？"}],
                task_queue=task_queue,
                start_to_close_timeout=timedelta(seconds=10),
                retry_policy=RetryPolicy(maximum_attempts=3),
            )
            return

        if not check_result.get("is_makeup"):
            # 非妆容图，推拒绝气泡
            await workflow.execute_activity(
                push_makeup_result,
                args=[inp.session_id, inp.msg_id, inp.user_id, True,
                      {"not_makeup": True}],
                task_queue=task_queue,
                start_to_close_timeout=timedelta(seconds=10),
                retry_policy=RetryPolicy(maximum_attempts=3),
            )
            return

        # Step 2: 视觉分析（超时60s，重试2次）
        analysis_text: str = await workflow.execute_activity(
            analyze_makeup,
            args=[
                check_result["file_id"],
                check_result["image_base64"],
                inp.user_text,
            ],
            task_queue=task_queue,
            start_to_close_timeout=timedelta(seconds=60),
            retry_policy=RetryPolicy(maximum_attempts=2),
        )
        if not analysis_text:
            await workflow.execute_activity(
                push_makeup_result,
                args=[inp.session_id, inp.msg_id, inp.user_id, False,
                      {"error": "妆容分析失败了喵，稍后再试？"}],
                task_queue=task_queue,
                start_to_close_timeout=timedelta(seconds=10),
                retry_policy=RetryPolicy(maximum_attempts=3),
            )
            return

        # Step 3: 希卡莉口吻转换（超时30s，重试2次）
        hikari_text: str = await workflow.execute_activity(
            wrap_as_hikari_tone,
            args=[analysis_text],
            task_queue=task_queue,
            start_to_close_timeout=timedelta(seconds=30),
            retry_policy=RetryPolicy(maximum_attempts=2),
        )

        # Step 4: 落盘 + 推送（超时10s，重试3次）
        await workflow.execute_activity(
            push_makeup_result,
            args=[inp.session_id, inp.msg_id, inp.user_id, True,
                  {"hikari_text": hikari_text}],
            task_queue=task_queue,
            start_to_close_timeout=timedelta(seconds=10),
            retry_policy=RetryPolicy(maximum_attempts=3),
        )
