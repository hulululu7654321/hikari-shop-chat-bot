# -*- coding: utf-8 -*-
"""
src/modules/tools/copywriting/handler.py — 文案生成任务处理器（v1.0.0）
=====================================================================
CopywritingWorkflow 的 Activity 实现层 + 直接执行降级入口。

工作流（第二轮触发生成）：
  1. generate_copywriting   — 拼网格 → 上传 → LLM 生成文案
  2. push_copywriting_result — 落盘 + WS 推送结果

第一轮（准备阶段）不走 Workflow：
  - 读磁盘 → 拼网格 → 上传 → 存 Redis 状态 → 推送询问气泡
  - 直接 await，因为步骤快（< 5s），无需 Temporal 管理

第二轮取消意图 / 图片参考校验：
  - 纯文字取消判断
  - 参考截图校验（YES/NO）

【状态机 Redis Key】
  hikari:copy:state:{user_id}  →  JSON  TTL=10min

【依赖关系】
- services.copywriting_service → AI 推理函数（拼图 / 上传 / 生成 / 校验）
- utils.vision_helper          → 图片读取
- utils.database               → MongoDB 落盘（COL_MESSAGES / COL_USERS）
- utils.socket_manager         → 中间状态气泡（询问 / 提示）直推
- services.ws_result_push → 最终结果推送
=====================================================================
"""

import os
import re
import json
import asyncio
import traceback
from datetime import datetime, timezone

from src.utils.logger import get_logger
from src.utils.database import get_db_manager, COL_MESSAGES, COL_USERS
from src.utils.vision_helper import get_vision_helper
from src.utils.socket_manager import get_socket_manager
from src.services.copywriting_service import (
    _make_client,
    _make_grid_image,
    _upload_b64,
    _check_is_ref_screenshot,
    _check_is_cancel,
    _generate_copy,
    _get_state,
    _save_state,
    _clear_state,
    COPYWRITING_PLATFORM_TRIGGERS,
    COPYWRITING_GENERIC_TRIGGERS,
    PLATFORM_LABELS,
)
from src.services.ws_result_push import push_tool_result, push_tool_error

logger = get_logger(__name__)

_ALL_TRIGGERS = [
    kw for kws in COPYWRITING_PLATFORM_TRIGGERS.values() for kw in kws
] + COPYWRITING_GENERIC_TRIGGERS


# ══════════════════════════════════════════════════════════════════
# 内部：中间状态气泡推送（不走 tool_result，走普通 text 气泡）
# ══════════════════════════════════════════════════════════════════

async def _send_bubble(session_id: str, user_id: str, text: str) -> str:
    """
    落盘 assistant 气泡并推送 type=text 消息，返回 msg_id。
    用于询问气泡、取消确认、参考图提示等中间状态消息。
    从 DB 读真实好感度，避免前端好感度跳回初始值。
    """
    db  = get_db_manager().db
    sm  = get_socket_manager()
    res = await db[COL_MESSAGES].insert_one({
        "session_id":  session_id,
        "user_id":     user_id,
        "role":        "assistant",
        "content":     text,
        "voice_status": "none",
        "created_at":  datetime.now(timezone.utc),
    })
    mid      = str(res.inserted_id)
    user_doc = await db[COL_USERS].find_one({"user_id": user_id}, {"affinity": 1, "mood": 1})
    affinity = user_doc.get("affinity", 10) if user_doc else 10
    mood     = user_doc.get("mood", "neutral") if user_doc else "neutral"

    await sm.send_to_user(session_id, {
        "type":         "text",
        "msg_id":       mid,
        "content":      text,
        "affinity":     affinity,
        "mood":         mood,
        "voice_status": "none",
    })
    await sm.send_to_user(session_id, {
        "type":         "voice",
        "msg_id":       mid,
        "voice_status": "none",
    })
    return mid


async def _push_result(
    session_id: str,
    user_id: str,
    platform: str,
    copy_text: str,
) -> None:
    """
    落盘文案气泡并推送 copywriting_result 事件。
    空文本时推送错误。
    """
    label = PLATFORM_LABELS.get(platform, "小红书")

    if not copy_text:
        await push_tool_error(
            session_id = session_id,
            msg_id     = "err",
            error_code = "copywriting_error",
            message    = "文案生成失败了喵，稍后再试？",
        )
        return

    db  = get_db_manager().db
    res = await db[COL_MESSAGES].insert_one({
        "session_id":          session_id,
        "user_id":             user_id,
        "role":                "assistant",
        "content":             copy_text,
        "copywriting_platform": platform,
        "voice_status":        "none",
        "created_at":          datetime.now(timezone.utc),
    })
    new_mid = str(res.inserted_id)

    await push_tool_result(
        session_id  = session_id,
        msg_id      = new_mid,
        result_type = "copywriting_result",
        payload     = {
            "platform":       platform,
            "platform_label": label,
            "content":        copy_text,
        },
    )
    logger.info(f"[Copywriting] 结果推送 | len={len(copy_text)} msg_id={new_mid[:8]}")


# ══════════════════════════════════════════════════════════════════
# 第一轮：准备阶段（不走 Workflow，直接 await）
# ══════════════════════════════════════════════════════════════════

async def run_copywriting_round1(
    user_id: str,
    session_id: str,
    image_ids: list[str],
    platform: str,
    user_hint: str,
    msg_id: str,
) -> None:
    """
    第一轮：读磁盘 → 拼网格 → 上传豆包 → 存 Redis 状态 → 推送询问气泡。
    耗时 < 5s，直接 await，不走 Temporal。
    """
    try:
        vh     = get_vision_helper()

        # 1. 读磁盘（优先 _small.jpg，省 token）
        b64_list: list[str] = []
        for iid in image_ids:
            small_path = os.path.join(vh.upload_root, user_id, f"{iid}_small.jpg")
            full_path  = os.path.join(vh.upload_root, user_id, f"{iid}.jpg")
            path       = small_path if os.path.exists(small_path) else full_path
            b64        = vh.get_image_base64(path)
            if b64:
                b64_list.append(b64)

        if not b64_list:
            await push_tool_error(
                session_id = session_id,
                msg_id     = msg_id,
                error_code = "copywriting_error",
                message    = "找不到图片了喵，重新发一下？",
            )
            return

        # 2. 拼网格（CPU 密集，扔线程池）
        grid_b64 = await asyncio.get_event_loop().run_in_executor(
            None, _make_grid_image, b64_list
        )

        # 3. 上传网格图 → file_id
        client   = _make_client()
        grid_fid = await _upload_b64(client, grid_b64, "grid.jpg")
        if not grid_fid:
            await push_tool_error(
                session_id = session_id,
                msg_id     = msg_id,
                error_code = "copywriting_error",
                message    = "图片上传失败了喵，稍后再试？",
            )
            return

        # 4. 存 Redis 状态
        await _save_state(user_id, {
            "state":      "waiting_ref",
            "grid_fid":   grid_fid,
            "platform":   platform,
            "user_hint":  user_hint,
            "session_id": session_id,
            "count":      len(b64_list),
        })

        # 5. 推送询问气泡
        label = PLATFORM_LABELS.get(platform, "小红书")
        await _send_bubble(
            session_id, user_id,
            f"本喵收到 {len(b64_list)} 张图了，要写{label}文案对吧喵。"
            f"告诉本喵这组图的主题是什么？"
            f"比如「银狼cos」「今天漫展集邮」「和朋友吃了顿好的」，随便说喵。"
            f"有参考文案截图的话一起发过来，没有就直接说主题就好喵～",
        )
        logger.info(
            f"[Copywriting] Round1 完成 | {len(b64_list)}张图"
        )

    except Exception:
        logger.error(f"[Copywriting] run_copywriting_round1 崩溃: {traceback.format_exc()}")
        try:
            await push_tool_error(
                session_id = session_id,
                msg_id     = msg_id,
                error_code = "copywriting_error",
                message    = "本喵的文案系统抽风了喵",
            )
        except Exception:
            pass


# ══════════════════════════════════════════════════════════════════
# Activity 实现函数（第二轮，由 Workflow 调用）
# ══════════════════════════════════════════════════════════════════

async def activity_generate_copywriting(
    user_id: str,
    session_id: str,
    msg_id: str,
    grid_fid: str,
    ref_fid: str | None,
    platform: str,
    user_hint: str,
) -> str:
    """Activity: generate_copywriting"""
    try:
        client    = _make_client()
        copy_text = await _generate_copy(
            client, grid_fid, platform, user_hint, ref_fid or ""
        )
        logger.info(
            f"[Copywriting] generate_copywriting | platform={platform} len={len(copy_text)}"
        )
        return copy_text
    except Exception:
        logger.error(f"[Copywriting] generate_copywriting 崩溃: {traceback.format_exc()}")
        return ""


async def activity_push_copywriting_result(
    user_id: str,
    session_id: str,
    platform: str,
    copy_text: str,
    msg_id: str,
) -> None:
    """
    Activity: push_copywriting_result
    超时 10s，重试 3 次（由 Workflow 定义）。
    落盘 + 推送 copywriting_result 事件。
    """
    try:
        await _push_result(session_id, user_id, platform, copy_text)
    except Exception:
        logger.error(f"[Copywriting] push_copywriting_result 崩溃: {traceback.format_exc()}")
        raise  # 让 Workflow 捕获并触发重试


# ══════════════════════════════════════════════════════════════════
# 第二轮：状态机主入口
# ══════════════════════════════════════════════════════════════════

async def run_copywriting_round2(
    user_id: str,
    session_id: str,
    ref_image_id: str,
    user_text: str,
    msg_id: str,
    client_msg_id: str = "",
    image_ids_hint: list[str] | None = None,
) -> bool:
    """
    第二轮状态机处理。
    返回 True  = 本函数已处理，pipeline 不再继续。
    返回 False = 普通聊天，pipeline 继续走正常对话流。

    三种情况：
    A. 用户发了图 → 校验是否参考截图 → 是则附参考生成，否则自由发挥
    B. 用户发了纯文字 → 语义判断是否取消 → 是则清 Redis，否则继续
    C. 10 分钟超时 → Redis 自动过期，降级提示重新发
    """
    if image_ids_hint is None:
        image_ids_hint = []

    _should_clear = True
    try:
        vh     = get_vision_helper()
        client = _make_client()
        db     = get_db_manager().db

        # 用户在等待状态下又发了多图 + 触发词 → 重新触发 Round1
        if len(image_ids_hint) >= 2 and user_text:
            if any(kw in user_text for kw in _ALL_TRIGGERS):
                logger.info(
                    f"[Copywriting] 重新触发 Round1，清除旧状态 | uid={user_id[:8]}"
                )
                await _clear_state(user_id)
                _should_clear = False
                return False

        # 读状态
        state = await _get_state(user_id)
        if not state or not state.get("grid_fid"):
            await _send_bubble(
                session_id, user_id,
                "等太久状态过期了喵，重新发图片和触发词吧～",
            )
            return True

        grid_fid = state["grid_fid"]
        platform = state.get("platform", "xiaohongshu")
        label    = PLATFORM_LABELS.get(platform, "小红书")

        # 落盘用户第二轮消息（带 client_msg_id 供前端乐观气泡匹配）
        if user_text or ref_image_id:
            await db[COL_MESSAGES].insert_one({
                "session_id":    session_id,
                "user_id":       user_id,
                "role":          "user",
                "content":       user_text or "",
                "image_id":      ref_image_id or "",
                "client_msg_id": client_msg_id,
                "created_at":    datetime.now(timezone.utc),
            })

        # 取消意图检测（纯文字才判断）
        if not ref_image_id and user_text:
            is_cancel = await _check_is_cancel(client, user_text)
            if is_cancel:
                await _clear_state(user_id)
                _should_clear = False
                await _send_bubble(
                    session_id, user_id,
                    "好的，取消了喵。要重新写的话，把图片都发过来再说触发词就行～",
                )
                return True

        # 合并主题（本轮文字优先，否则沿用 Round1 的 hint）
        user_hint = user_text.strip() if user_text.strip() else state.get("user_hint", "")

        # 处理参考图（可选）
        ref_fid   = ""
        ref_valid = False
        if ref_image_id:
            small_path = os.path.join(vh.upload_root, user_id, f"{ref_image_id}_small.jpg")
            full_path  = os.path.join(vh.upload_root, user_id, f"{ref_image_id}.jpg")
            path       = small_path if os.path.exists(small_path) else full_path
            ref_b64    = vh.get_image_base64(path) or ""
            if ref_b64:
                ref_valid = await _check_is_ref_screenshot(client, ref_b64)
                if ref_valid:
                    ref_fid = await _upload_b64(client, ref_b64, "ref.jpg")
                else:
                    await _send_bubble(
                        session_id, user_id,
                        "这张好像不是文案截图喵，本喵照你说的主题自由发挥了～",
                    )

        # 没有主题也没有参考图 → 放行走普通聊天
        if not user_hint and not ref_image_id:
            _should_clear = False
            return False

        # 推送"生成中"提示气泡
        _hint_display = user_hint[:30] + "…" if len(user_hint) > 30 else user_hint
        if user_hint and ref_valid:
            hint_text = f"收到主题「{_hint_display}」，本喵照着参考风格给你写{label}文案，稍等喵～"
        elif user_hint:
            hint_text = f"收到主题「{_hint_display}」，本喵给你写{label}文案，稍等喵～"
        elif ref_valid:
            hint_text = f"本喵照着参考风格给你写{label}文案，稍等喵～"
        else:
            hint_text = f"好，本喵给你写{label}文案，稍等喵～"
        await _send_bubble(session_id, user_id, hint_text)

        # 提交 Workflow 或降级直接生成
        workflow_id = await _submit_copywriting_workflow(
            user_id    = user_id,
            session_id = session_id,
            msg_id     = msg_id,
            grid_fid   = grid_fid,
            ref_fid    = ref_fid if ref_valid else None,
            platform   = platform,
            user_hint  = user_hint,
        )
        if not workflow_id:
            # 降级直接执行
            copy_text = await activity_generate_copywriting(
                user_id, session_id, msg_id, grid_fid,
                ref_fid if ref_valid else None, platform, user_hint,
            )
            await _push_result(session_id, user_id, platform, copy_text)

        return True

    except Exception:
        logger.error(f"[Copywriting] run_copywriting_round2 崩溃: {traceback.format_exc()}")
        try:
            await push_tool_error(
                session_id = session_id,
                msg_id     = msg_id,
                error_code = "copywriting_error",
                message    = "本喵的文案系统抽风了喵",
            )
        except Exception:
            pass
        return True
    finally:
        if _should_clear:
            await _clear_state(user_id)


# ══════════════════════════════════════════════════════════════════
# 内部：Temporal Workflow 提交
# ══════════════════════════════════════════════════════════════════

async def _submit_copywriting_workflow(
    user_id: str,
    session_id: str,
    msg_id: str,
    grid_fid: str,
    ref_fid: str | None,
    platform: str,
    user_hint: str,
) -> str | None:
    try:
        from temporalio.client import Client
        from src.workflows.copywriting_workflow import CopywritingWorkflow, CopywritingInput

        client = await Client.connect(
            os.getenv("TEMPORAL_HOST", "hikari-temporal:7233"),
            namespace=os.getenv("TEMPORAL_NAMESPACE", "default"),
        )
        handle = await client.start_workflow(
            CopywritingWorkflow.run,
            CopywritingInput(
                user_id    = user_id,
                session_id = session_id,
                msg_id     = msg_id,
                grid_fid   = grid_fid,
                ref_fid    = ref_fid,
                platform   = platform,
                user_hint  = user_hint,
            ),
            id         = f"copywriting-{msg_id}",
            task_queue = os.getenv("TEMPORAL_TASK_QUEUE", "hikari-main"),
        )
        logger.info(f"[Copywriting] Workflow 提交成功 | wf_id={handle.id}")
        return handle.id

    except Exception as e:
        logger.warning(f"[Copywriting] Temporal 不可用，降级直接执行 | err={e}")
        return None


# ══════════════════════════════════════════════════════════════════
# 公开：状态机判断（供 api.py 调用）
# ══════════════════════════════════════════════════════════════════

async def is_waiting_ref(user_id: str) -> bool:
    """当前用户是否处于等待参考图阶段。"""
    state = await _get_state(user_id)
    return state.get("state") == "waiting_ref"
