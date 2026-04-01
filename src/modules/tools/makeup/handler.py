# -*- coding: utf-8 -*-
"""
src/modules/tools/makeup/handler.py — 妆容分析任务处理器（v1.0.0）
=====================================================================
MakeupWorkflow 的 Activity 实现层。
由 src/workflows/makeup_workflow.py 中的 Activity 调用。

工作流四步（串行）：
  1. check_makeup_image   — 轻量校验图片是否为 cosplay 妆容相关
  2. analyze_makeup       — 视觉分析 + 联网搜索角色妆容参考
  3. wrap_as_hikari_tone  — 转成希卡莉口吻
  4. push_makeup_result   — 落盘 + WebSocket 推送

校验不通过时直接推送拒绝文字气泡（type=text），不走后续步骤。

【依赖关系】
- services.makeup_service              → AI 推理（校验 / 分析 / 口吻转换）
- utils.vision_helper                  → 图片读取 + file_id 上传
- utils.database                       → MongoDB 落盘（COL_MESSAGES）
- services.ws_result_push  → WS 推送（禁止直接 import socket_manager）
=====================================================================
"""

import os
import io
import base64
import traceback
from datetime import datetime, timezone

from src.utils.logger import get_logger
from src.utils.database import get_db_manager, COL_MESSAGES
from src.utils.vision_helper import get_vision_helper
from src.utils.api_retry import with_retry
from src.services.makeup_service import (
    _make_client,
    _check_is_makeup_image,
    _analyze_makeup,
    _wrap_as_hikari,
)
from src.services.ws_result_push import push_tool_result, push_tool_error

logger = get_logger(__name__)


# ══════════════════════════════════════════════════════════════════
# 内部：上传图片拿 file_id（两个 activity 共用）
# ══════════════════════════════════════════════════════════════════

async def _upload_image(user_id: str, image_id: str) -> tuple[str, str]:
    """
    从磁盘读取图片，上传到豆包拿 file_id。
    返回 (image_base64, file_id)，失败时均返回空字符串。
    """
    try:
        vh        = get_vision_helper()
        img_path  = os.path.join(vh.upload_root, user_id, f"{image_id}.jpg")
        image_b64 = vh.get_image_base64(img_path)
        if not image_b64:
            logger.error(f"[Makeup] 原图不存在 | path={img_path}")
            return "", ""

        client = _make_client()
        buf    = io.BytesIO(base64.b64decode(image_b64))
        buf.name = f"{image_id}.jpg"
        uploaded = await with_retry(
            client.files.create,
            file    = buf,
            purpose = "user_data",
            task_name = "妆容图上传",
        )
        file_id = uploaded.id
        logger.info(f"[Makeup] 图片上传完成 | file_id={file_id[:16]}")
        return image_b64, file_id

    except Exception:
        logger.error(f"[Makeup] _upload_image 崩溃: {traceback.format_exc()}")
        return "", ""


# ══════════════════════════════════════════════════════════════════
# Activity 实现函数
# ══════════════════════════════════════════════════════════════════

async def activity_check_makeup_image(
    user_id: str,
    image_id: str,
    session_id: str,
    msg_id: str,
) -> tuple[bool, str, str]:
    """
    Activity: check_makeup_image
    超时 30s，重试 2 次（由 Workflow 定义）。

    返回 (is_makeup, image_base64, file_id)
    is_makeup=False 时调用方应推送拒绝气泡并终止 Workflow。
    """
    try:
        image_b64, file_id = await _upload_image(user_id, image_id)
        if not image_b64 or not file_id:
            # 图片读取 / 上传失败，推送错误后终止
            await push_tool_error(
                session_id = session_id,
                msg_id     = msg_id,
                error_code = "makeup_error",
                message    = "找不到图片了喵，重新发一下？",
            )
            return False, "", ""

        client    = _make_client()
        is_makeup = await _check_is_makeup_image(client, file_id)
        logger.info(f"[Makeup] 图片校验 | image_id={image_id[:8]} | is_makeup={is_makeup}")
        return is_makeup, image_b64, file_id

    except Exception:
        logger.error(f"[Makeup] check_makeup_image 崩溃: {traceback.format_exc()}")
        return False, "", ""


async def activity_analyze_makeup(
    image_base64: str,
    file_id: str,
    user_hint: str,
) -> str:
    """
    Activity: analyze_makeup
    超时 60s，重试 2 次（由 Workflow 定义）。
    返回分析文本，空字符串表示失败。
    """
    try:
        client  = _make_client()
        result  = await _analyze_makeup(client, file_id, image_base64, user_hint)
        logger.info(f"[Makeup] 分析完成 | len={len(result)}")
        return result

    except Exception:
        logger.error(f"[Makeup] analyze_makeup 崩溃: {traceback.format_exc()}")
        return ""


async def activity_wrap_as_hikari_tone(analysis_text: str) -> str:
    """
    Activity: wrap_as_hikari_tone
    超时 30s，重试 2 次（由 Workflow 定义）。
    失败时原样返回 analysis_text，保证主流程不中断。
    """
    try:
        client      = _make_client()
        hikari_text = await _wrap_as_hikari(client, analysis_text)
        logger.info(f"[Makeup] 口吻转换完成 | len={len(hikari_text)}")
        return hikari_text

    except Exception:
        logger.error(f"[Makeup] wrap_as_hikari_tone 崩溃，使用原始分析: {traceback.format_exc()}")
        return analysis_text


async def activity_push_makeup_result(
    user_id: str,
    session_id: str,
    hikari_text: str,
    msg_id: str,
) -> None:
    """
    Activity: push_makeup_result
    超时 10s，重试 3 次（由 Workflow 定义）。
    落盘新消息气泡，并通过 WS 推送结果。
    """
    try:
        db  = get_db_manager().db
        res = await db[COL_MESSAGES].insert_one({
            "session_id":  session_id,
            "user_id":     user_id,
            "role":        "assistant",
            "content":     hikari_text,
            "voice_status": "none",
            "created_at":  datetime.now(timezone.utc),
        })
        new_msg_id = str(res.inserted_id)

        await push_tool_result(
            session_id  = session_id,
            msg_id      = new_msg_id,
            result_type = "makeup_result",
            payload     = {"content": hikari_text},
        )
        logger.info(f"[Makeup] 推送完成 | new_msg_id={new_msg_id[:8]}")

    except Exception:
        logger.error(f"[Makeup] push_makeup_result 崩溃: {traceback.format_exc()}")
        raise  # 让 Workflow 捕获并触发重试


async def _push_makeup_reject(session_id: str, msg_id: str, user_id: str) -> None:
    """
    校验不通过时推送拒绝文字气泡（type=text，与普通对话消息一致）。
    """
    try:
        reject_text = "这张图好像和妆容没什么关系喵，换一张 coser 的妆容照片试试？"
        db  = get_db_manager().db
        res = await db[COL_MESSAGES].insert_one({
            "session_id":  session_id,
            "user_id":     user_id,
            "role":        "assistant",
            "content":     reject_text,
            "voice_status": "none",
            "created_at":  datetime.now(timezone.utc),
        })
        new_msg_id = str(res.inserted_id)

        from src.services.ws_result_push import push_chat_bubble
        await push_chat_bubble(session_id, new_msg_id, reject_text)
    except Exception as e:
        logger.error(f"[Makeup] _push_makeup_reject 失败: {e}")


# ══════════════════════════════════════════════════════════════════
# 直接执行入口（Temporal 降级路径）
# ══════════════════════════════════════════════════════════════════

async def run_makeup_task(
    user_id: str,
    session_id: str,
    image_id: str,
    user_text: str,
    msg_id: str,
) -> None:
    """
    完整串行执行（Temporal 降级时使用）。
    正常情况下由 MakeupWorkflow 驱动各 Activity。
    """
    import re
    from src.services.makeup_service import MAKEUP_TRIGGERS

    try:
        # Step 1: 校验
        is_makeup, image_b64, file_id = await activity_check_makeup_image(
            user_id, image_id, session_id, msg_id
        )
        if not image_b64:
            # _upload_image 失败，错误已在 activity 内推送
            return
        if not is_makeup:
            await _push_makeup_reject(session_id, msg_id, user_id)
            return

        # 提取 user_hint
        trigger_pattern = "|".join(re.escape(kw) for kw in MAKEUP_TRIGGERS)
        user_hint = re.sub(trigger_pattern, "", user_text or "").strip()
        user_hint = re.sub(r"^[，。,.\s]+", "", user_hint).strip()

        # Step 2: 视觉分析
        analysis = await activity_analyze_makeup(image_b64, file_id, user_hint)
        if not analysis:
            await push_tool_error(session_id, msg_id, "makeup_error", "妆容分析失败了喵，稍后再试？")
            return

        # Step 3: 口吻转换
        hikari_text = await activity_wrap_as_hikari_tone(analysis)

        # Step 4: 落盘 + 推送
        await activity_push_makeup_result(user_id, session_id, hikari_text, msg_id)

    except Exception:
        logger.error(f"[Makeup] run_makeup_task 崩溃: {traceback.format_exc()}")
        await push_tool_error(session_id, msg_id, "makeup_error", "本喵的妆容分析系统抽风了喵")
