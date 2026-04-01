# -*- coding: utf-8 -*-
"""
图生图 Temporal Workflow

四步串行：
  1. analyze_coser_image   — 视觉分析，识别角色 + 设计4个动作
  2. generate_quad_image   — Seedream 四宫格图生图
  3. save_gen_image        — 下载落盘，防 24h URL 失效
  4. push_image_gen_result — 落盘 + WS 推送结果给前端
"""

from __future__ import annotations

import os
import base64
import io
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

from temporalio import activity, workflow
from temporalio.common import RetryPolicy

IMAGE_GEN_TRIGGERS = ["希卡莉启动pose替身"]


# ════════════════════════════════════════════════════════════════════════════
# Input dataclass（重构文档 §14 锁定）
# ════════════════════════════════════════════════════════════════════════════

@dataclass
class ImageGenInput:
    user_id:    str
    session_id: str
    image_id:   str
    message:    str
    msg_id:     str


# ════════════════════════════════════════════════════════════════════════════
# Activities
# ════════════════════════════════════════════════════════════════════════════

@activity.defn(name="analyze_coser_image")
async def analyze_coser_image(
    user_id: str,
    image_id: str,
    user_hint: str,
) -> str:
    """
    视觉分析 coser 图片。
    返回角色分析 + 4个动作设计的文字描述。失败返回空字符串。
    超时 60s，重试 3 次。
    """
    import io as _io
    from openai import AsyncOpenAI
    from src.utils.vision_helper import get_vision_helper
    from src.utils.api_retry import with_retry
    from src.core.hikari_prompts import build_coser_analysis_prompt
    from src.utils.logger import get_logger
    _log = get_logger(__name__)

    def _make_client() -> AsyncOpenAI:
        return AsyncOpenAI(
            base_url="https://ark.cn-beijing.volces.com/api/v3",
            api_key=os.getenv("DOUBAO_API_KEY"),
        )

    vision_helper = get_vision_helper()
    img_path = os.path.join(vision_helper.upload_root, user_id, f"{image_id}.jpg")
    image_base64 = vision_helper.get_image_base64(img_path)
    if not image_base64:
        _log.error(f"analyze_coser_image: source image not found | path={img_path}")
        return ""

    analysis_prompt = build_coser_analysis_prompt(user_hint)
    model = os.getenv("DOUBAO_MAIN_EP", "")

    # 主路径：responses API + 联网搜索
    try:
        client = _make_client()
        img_bytes = base64.b64decode(image_base64)
        img_file = _io.BytesIO(img_bytes)
        img_file.name = f"{image_id}.jpg"
        uploaded = await with_retry(
            _make_client().files.create,
            file=img_file,
            purpose="user_data",
            task_name="图片上传",
        )
        file_id = uploaded.id
        _log.info(f"analyze_coser_image: uploaded | file_id={file_id[:16]}")

        resp = await with_retry(
            client.responses.create,
            model=model,
            input=[
                {
                    "role": "user",
                    "content": [
                        {"type": "input_image", "file_id": file_id},
                        {"type": "input_text", "text": analysis_prompt},
                    ],
                }
            ],
            temperature=0.7,
            max_output_tokens=1000,
            tools=[{"type": "web_search"}],
            extra_body={"thinking": {"type": "disabled"}},
            task_name="图生图视觉分析",
        )
        result = resp.output_text.strip()
        _log.info(f"analyze_coser_image: done | len={len(result)} hint={repr(user_hint[:20])}")
        return result

    except Exception as e:
        _log.error(f"analyze_coser_image: responses API failed, falling back | {e}")

    # 降级路径：chat.completions（不联网）
    try:
        r = await with_retry(
            _make_client().chat.completions.create,
            model=model,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:image/jpeg;base64,{image_base64}"},
                        },
                        {"type": "text", "text": analysis_prompt},
                    ],
                }
            ],
            temperature=0.7,
            max_tokens=800,
            extra_body={"thinking": {"type": "disabled"}},
            task_name="图生图视觉分析降级",
        )
        result = r.choices[0].message.content.strip()
        _log.info(f"analyze_coser_image: fallback done | len={len(result)}")
        return result

    except Exception as e2:
        _log.error(f"analyze_coser_image: fallback also failed | {e2}")
        return ""


@activity.defn(name="generate_quad_image")
async def generate_quad_image(
    image_id: str,
    user_id: str,
    analysis_text: str,
) -> Optional[str]:
    """
    调 Seedream 生成四宫格动作参考图。
    返回生成图 URL（24小时有效），失败返回 None。
    超时 120s，重试 2 次。
    """
    from openai import AsyncOpenAI
    from src.utils.vision_helper import get_vision_helper
    from src.utils.api_retry import with_retry
    from src.core.hikari_prompts import COSER_IMAGE_GEN_CONSTRAINTS
    from src.utils.logger import get_logger
    _log = get_logger(__name__)

    seedream_ep = os.getenv("SEEDREAM_EP", "")
    if not seedream_ep:
        _log.error("generate_quad_image: SEEDREAM_EP not configured")
        return None

    vision_helper = get_vision_helper()
    img_path = os.path.join(vision_helper.upload_root, user_id, f"{image_id}.jpg")
    image_base64 = vision_helper.get_image_base64(img_path)
    if not image_base64:
        _log.error(f"generate_quad_image: source image not found | image_id={image_id[:8]}")
        return None

    client = AsyncOpenAI(
        base_url="https://ark.cn-beijing.volces.com/api/v3",
        api_key=os.getenv("DOUBAO_API_KEY"),
    )
    final_prompt = f"{analysis_text}\n{COSER_IMAGE_GEN_CONSTRAINTS}"
    _log.info(f"generate_quad_image: prompt len={len(final_prompt)}")

    try:
        resp = await with_retry(
            client.images.generate,
            model=seedream_ep,
            prompt=final_prompt,
            size="2K",
            response_format="url",
            extra_body={
                "image": f"data:image/jpeg;base64,{image_base64}",
                "watermark": False,
                "sequential_image_generation": "disabled",
            },
            task_name="Seedream四宫格生成",
        )
        if resp.data and resp.data[0].url:
            _log.info("generate_quad_image: done")
            return resp.data[0].url
        return None

    except Exception as e:
        _log.error(f"generate_quad_image: failed | {e}")
        return None


@activity.defn(name="save_gen_image")
async def save_gen_image(
    user_id: str,
    session_id: str,
    msg_id: str,
    gen_url: str,
    analysis_text: str,
) -> Optional[str]:
    """
    下载生成图到本地，防止 24h URL 失效。
    返回本地相对 URL（/api/u/images/...），失败返回 None。
    落盘到 MongoDB 由 push_image_gen_result 负责。
    超时 180s，重试 3 次。
    """
    from src.utils.vision_helper import get_vision_helper
    from src.utils.logger import get_logger
    _log = get_logger(__name__)

    vision_helper = get_vision_helper()
    local_url = await vision_helper.save_gen_image(user_id, gen_url)
    if not local_url:
        _log.error(f"save_gen_image: save failed | msg_id={msg_id[:8]}")
        return None

    _log.info(f"save_gen_image: downloaded | url={local_url}")
    return local_url


@activity.defn(name="push_image_gen_result")
async def push_image_gen_result(
    session_id: str,
    user_id: str,
    msg_id: str,
    success: bool,
    payload: dict,
) -> None:
    """
    落盘消息 + 通过 Redis Stream 将图生图结果推送给前端 WebSocket。
    成功路径：insert assistant 消息 → push image_gen_result。
    失败路径：仅 push error。
    超时 15s，重试 3 次。
    """
    from src.services.ws_result_push import push_tool_result, push_tool_error
    from src.utils.database import get_db_manager, COL_MESSAGES
    from src.utils.logger import get_logger
    _log = get_logger(__name__)

    if not success:
        await push_tool_error(
            session_id=session_id,
            msg_id=msg_id,
            error_code="image_gen_failed",
            message=payload.get("error", "图生图失败了喵"),
        )
        return

    local_url     = payload.get("gen_image_url", "")
    analysis_text = payload.get("char_text", "")

    db = get_db_manager().db
    insert_res = await db[COL_MESSAGES].insert_one(
        {
            "session_id":       session_id,
            "user_id":          user_id,
            "role":             "assistant",
            "content":          "",
            "gen_image_url":    local_url,
            "gen_image_status": "ready",
            "char_text":        analysis_text,
            "analysis_text":    analysis_text,
            "voice_status":     "none",
            "created_at":       datetime.now(timezone.utc),
        }
    )
    new_msg_id = str(insert_res.inserted_id)
    _log.info(f"push_image_gen_result: persisted | new_msg={new_msg_id[:8]} url={local_url}")

    await push_tool_result(
        session_id=session_id,
        msg_id=new_msg_id,
        result_type="image_gen_result",
        payload={
            "success":       True,
            "gen_image_url": local_url,
            "char_text":     analysis_text,
        },
    )


# ════════════════════════════════════════════════════════════════════════════
# Workflow
# ════════════════════════════════════════════════════════════════════════════

@workflow.defn(name="ImageGenWorkflow")
class ImageGenWorkflow:
    """
    四步串行，每步独立重试，最终失败推 error WS。
    """

    @workflow.run
    async def run(self, inp: ImageGenInput) -> None:
        task_queue = "hikari-main"

        # Step 1: 视觉分析（超时60s，重试3次）
        analysis_text: str = await workflow.execute_activity(
            analyze_coser_image,
            args=[inp.user_id, inp.image_id, inp.message],
            task_queue=task_queue,
            start_to_close_timeout=timedelta(seconds=60),
            retry_policy=RetryPolicy(maximum_attempts=3),
        )
        if not analysis_text:
            await workflow.execute_activity(
                push_image_gen_result,
                args=[inp.session_id, inp.user_id, inp.msg_id, False, {"error": "角色分析失败了喵"}],
                task_queue=task_queue,
                start_to_close_timeout=timedelta(seconds=15),
                retry_policy=RetryPolicy(maximum_attempts=3),
            )
            return

        # Step 2: 图生图（超时120s，重试2次）
        gen_url: Optional[str] = await workflow.execute_activity(
            generate_quad_image,
            args=[inp.image_id, inp.user_id, analysis_text],
            task_queue=task_queue,
            start_to_close_timeout=timedelta(seconds=120),
            retry_policy=RetryPolicy(maximum_attempts=2),
        )
        if not gen_url:
            await workflow.execute_activity(
                push_image_gen_result,
                args=[inp.session_id, inp.user_id, inp.msg_id, False, {"error": "图片生成失败了喵，稍后再试？"}],
                task_queue=task_queue,
                start_to_close_timeout=timedelta(seconds=15),
                retry_policy=RetryPolicy(maximum_attempts=3),
            )
            return

        # Step 3: 下载落盘（超时180s，重试3次；2K图下载最长需120s）
        local_url: Optional[str] = await workflow.execute_activity(
            save_gen_image,
            args=[inp.user_id, inp.session_id, inp.msg_id, gen_url, analysis_text],
            task_queue=task_queue,
            start_to_close_timeout=timedelta(seconds=180),
            retry_policy=RetryPolicy(maximum_attempts=3),
        )
        if not local_url:
            await workflow.execute_activity(
                push_image_gen_result,
                args=[inp.session_id, inp.user_id, inp.msg_id, False, {"error": "图片保存失败了喵，稍后再试？"}],
                task_queue=task_queue,
                start_to_close_timeout=timedelta(seconds=15),
                retry_policy=RetryPolicy(maximum_attempts=3),
            )
            return

        # Step 4: 落盘 + WS 推送（超时15s，重试3次）
        await workflow.execute_activity(
            push_image_gen_result,
            args=[
                inp.session_id,
                inp.user_id,
                inp.msg_id,
                True,
                {
                    "gen_image_url": local_url,
                    "char_text":     analysis_text,
                },
            ],
            task_queue=task_queue,
            start_to_close_timeout=timedelta(seconds=15),
            retry_policy=RetryPolicy(maximum_attempts=3),
        )
