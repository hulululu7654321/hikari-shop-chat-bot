# -*- coding: utf-8 -*-
"""
src/modules/tools/imagegen/handler.py — 图生图任务处理器（v1.0.0）
=====================================================================
ImageGenWorkflow 的 Activity 实现层。
由 src/workflows/image_gen_workflow.py 中的 Activity 调用，
不直接被 api.py 调用（api.py 只负责触发 Workflow）。

工作流四步（串行）：
  1. analyze_coser_image   — 视觉分析 + 联网搜索角色信息
  2. generate_quad_image   — Seedream 四宫格图生图
  3. save_gen_image        — 下载生成图到本地（防24h URL失效）
  4. push_image_gen_result — WebSocket 推送结果

任何步骤失败均调用 push_image_gen_error 推送错误消息。

【依赖关系】
- services.image_gen_service  → AI 推理（视觉分析 + 图生图）
- utils.vision_helper         → 图片读取 / 本地落盘
- utils.database              → MongoDB 落盘（COL_MESSAGES）
- services.ws_result_push → WS 推送（禁止直接 import socket_manager）
=====================================================================
"""

import os
import re
import traceback
from datetime import datetime, timezone

from src.utils.logger import get_logger
from src.utils.database import get_db_manager, COL_MESSAGES
from src.utils.vision_helper import get_vision_helper
from src.services.image_gen_service import (
    analyze_coser,
    generate_quad_image,
    IMAGE_GEN_TRIGGERS,
)
from src.services.ws_result_push import push_tool_result, push_tool_error

logger = get_logger(__name__)


# ══════════════════════════════════════════════════════════════════
# Activity 实现函数（由 Workflow 逐步调用）
# ══════════════════════════════════════════════════════════════════

async def activity_analyze_coser_image(
    user_id: str,
    image_id: str,
    user_text: str,
) -> str:
    """
    Activity: analyze_coser_image
    超时 60s，重试 3 次（由 Workflow 定义）。
    返回分析文本，空字符串表示失败。
    """
    try:
        vh        = get_vision_helper()
        img_path  = os.path.join(vh.upload_root, user_id, f"{image_id}.jpg")
        image_b64 = vh.get_image_base64(img_path)
        if not image_b64:
            logger.error(f"[ImageGen] 原图不存在 | path={img_path}")
            return ""

        trigger_pattern = "|".join(re.escape(kw) for kw in IMAGE_GEN_TRIGGERS)
        user_hint       = re.sub(trigger_pattern, "", user_text or "").strip()

        analysis = await analyze_coser(image_b64, user_hint=user_hint, image_id=image_id)
        logger.info(
            f"[ImageGen] 视觉分析完成 | image_id={image_id[:8]} | len={len(analysis)}"
        )
        return analysis

    except Exception:
        logger.error(f"[ImageGen] analyze_coser_image 崩溃: {traceback.format_exc()}")
        return ""


async def activity_generate_quad_image(
    user_id: str,
    image_id: str,
    analysis_text: str,
) -> str:
    """
    Activity: generate_quad_image
    超时 120s，重试 2 次（由 Workflow 定义）。
    返回生成图 URL（临时，24h 有效），失败返回空字符串。
    """
    try:
        vh        = get_vision_helper()
        img_path  = os.path.join(vh.upload_root, user_id, f"{image_id}.jpg")
        image_b64 = vh.get_image_base64(img_path)
        if not image_b64:
            logger.error(f"[ImageGen] generate_quad: 原图不存在 | path={img_path}")
            return ""

        gen_url = await generate_quad_image(image_b64, analysis_text)
        if gen_url:
            logger.info("[ImageGen] 四宫格生成完成")
        return gen_url or ""

    except Exception:
        logger.error(f"[ImageGen] generate_quad_image 崩溃: {traceback.format_exc()}")
        return ""


async def activity_save_gen_image(
    user_id: str,
    gen_url: str,
) -> str:
    """
    Activity: save_gen_image
    超时 30s，重试 3 次（由 Workflow 定义）。
    下载生成图到本地，返回本地访问 URL，失败返回空字符串。
    """
    try:
        vh        = get_vision_helper()
        local_url = await vh.save_gen_image(user_id, gen_url)
        if local_url:
            logger.info(f"[ImageGen] 图片落盘完成 | url={local_url}")
        return local_url or ""

    except Exception:
        logger.error(f"[ImageGen] save_gen_image 崩溃: {traceback.format_exc()}")
        return ""


async def activity_push_image_gen_result(
    user_id: str,
    session_id: str,
    original_msg_id: str,
    local_url: str,
    analysis_text: str,
) -> None:
    """
    Activity: push_image_gen_result
    超时 10s，重试 3 次（由 Workflow 定义）。
    落盘新消息气泡，并通过 WS 推送结果。
    """
    try:
        db = get_db_manager().db
        res = await db[COL_MESSAGES].insert_one({
            "session_id":        session_id,
            "user_id":           user_id,
            "role":              "assistant",
            "content":           "",
            "gen_image_url":     local_url,
            "gen_image_status":  "ready",
            "char_text":         analysis_text,
            "analysis_text":     analysis_text,
            "voice_status":      "none",
            "created_at":        datetime.now(timezone.utc),
        })
        new_msg_id = str(res.inserted_id)

        await push_tool_result(
            session_id  = session_id,
            msg_id      = new_msg_id,
            result_type = "image_gen_result",
            payload     = {
                "gen_image_url": local_url,
                "char_text":     analysis_text,
            },
        )
        logger.info(
            f"[ImageGen] 推送完成 | original={original_msg_id[:8]} | new={new_msg_id[:8]}"
        )

    except Exception:
        logger.error(f"[ImageGen] push_image_gen_result 崩溃: {traceback.format_exc()}")
        raise  # 让 Workflow 捕获并触发重试


async def activity_push_image_gen_error(
    session_id: str,
    msg_id: str,
    error_message: str,
) -> None:
    """失败时推送错误消息（由 Workflow 在最终失败时调用）。"""
    try:
        await push_tool_error(
            session_id = session_id,
            msg_id     = msg_id,
            error_code = "image_gen_error",
            message    = error_message,
        )
    except Exception as e:
        logger.error(f"[ImageGen] push_image_gen_error 失败: {e}")


# ══════════════════════════════════════════════════════════════════
# 直接执行入口（Temporal 不可用时的降级路径，由 api.py 调用）
# ══════════════════════════════════════════════════════════════════

async def run_image_gen_task(
    user_id: str,
    session_id: str,
    image_id: str,
    user_text: str,
    msg_id: str,
) -> None:
    """
    完整串行执行四步工作流（Temporal 降级时使用）。
    正常情况下由 ImageGenWorkflow 驱动各 Activity。
    """
    try:
        # Step 1
        analysis = await activity_analyze_coser_image(user_id, image_id, user_text)
        if not analysis:
            await activity_push_image_gen_error(session_id, msg_id, "角色分析失败了喵，稍后再试？")
            return

        # Step 2
        gen_url = await activity_generate_quad_image(user_id, image_id, analysis)
        if not gen_url:
            await activity_push_image_gen_error(session_id, msg_id, "图片生成失败了喵，稍后再试？")
            return

        # Step 3
        local_url = await activity_save_gen_image(user_id, gen_url)
        if not local_url:
            await activity_push_image_gen_error(session_id, msg_id, "图片保存失败了喵，稍后再试？")
            return

        # Step 4
        await activity_push_image_gen_result(
            user_id, session_id, msg_id, local_url, analysis
        )

    except Exception:
        logger.error(f"[ImageGen] run_image_gen_task 崩溃: {traceback.format_exc()}")
        await activity_push_image_gen_error(session_id, msg_id, "本喵的画图系统抽风了喵")
