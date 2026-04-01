# -*- coding: utf-8 -*-
"""
src/services/ws_result_push.py — 工具结果 WebSocket 推送（v1.0.0）
=====================================================================
供所有工具容器（imagegen / makeup / copywriting）调用。
封装 socket_manager.send_to_user，提供统一的工具结果推送接口。

严格对齐 MASTER §13.4 函数签名，禁止修改签名。

【函数说明】
- push_tool_result(session_id, msg_id, result_type, payload)
    result_type: "image_gen_result" | "makeup_result" | "copywriting_result"
    payload 将展开合并进消息体

- push_tool_error(session_id, msg_id, error_code, message)
    推送工具执行失败消息，type 字段和 result_type 保持一致

【底层】
- 通过 Redis Stream 定向推送（hikari:ws_stream:{instance_id}）
- 跨实例路由由 socket_manager 通过 ws_owner 路由表处理
- 断线时消息进入 hikari:pending:{session_id}，重连后补发
=====================================================================
"""

from src.utils.socket_manager import get_socket_manager
from src.utils.logger import get_logger

logger = get_logger(__name__)

_VALID_RESULT_TYPES = frozenset({
    "image_gen_result",
    "makeup_result",
    "copywriting_result",
})


async def push_tool_result(
    session_id: str,
    msg_id: str,
    result_type: str,   # "image_gen_result" | "makeup_result" | "copywriting_result"
    payload: dict,
) -> None:
    """
    通过 Redis Stream 定向推送工具结果到用户 WS 连接。

    发出消息体格式：
    {
        "type": result_type,
        "msg_id": msg_id,
        "success": True,
        ...payload 字段展开...
    }
    """
    if result_type not in _VALID_RESULT_TYPES:
        logger.warning(f"[WsResultPush] 非法 result_type={result_type!r}，跳过推送")
        return

    message = {
        "type":    result_type,
        "msg_id":  msg_id,
        "success": True,
        **payload,
    }
    try:
        await get_socket_manager().send_to_user(session_id, message)
        logger.info(
            f"[WsResultPush] 推送成功 | type={result_type} | session={session_id[:16]} | msg={msg_id[:8]}"
        )
    except Exception as e:
        logger.error(
            f"[WsResultPush] 推送失败 | type={result_type} | session={session_id[:16]} | err={e}"
        )


async def push_tool_error(
    session_id: str,
    msg_id: str,
    error_code: str,
    message: str,
) -> None:
    """
    推送工具执行失败消息。

    发出消息体格式：
    {
        "type": "{error_code}_result"（尽量对应 result_type），
        "msg_id": msg_id,
        "success": False,
        "error_code": error_code,
        "error": message,
    }

    error_code 约定：
      "image_gen_failed"   → type="image_gen_result"
      "makeup_failed"      → type="makeup_result"
      "copywriting_failed" → type="copywriting_result"
      "voice_gen_failed"   → type="voice_gen_result"
      其他               → type="tool_error"
    """
    # 推断 result_type 以便前端统一处理
    _code_to_type = {
        "image_gen_failed":   "image_gen_result",
        "makeup_failed":      "makeup_result",
        "copywriting_failed": "copywriting_result",
        "voice_gen_failed":   "voice_gen_result",
    }
    result_type = _code_to_type.get(error_code, "tool_error")

    msg_body = {
        "type":       result_type,
        "msg_id":     msg_id,
        "success":    False,
        "error_code": error_code,
        "error":      message,
    }
    try:
        await get_socket_manager().send_to_user(session_id, msg_body)
        logger.info(
            f"[WsResultPush] 错误推送 | error_code={error_code} | session={session_id[:16]} | msg={msg_id[:8]}"
        )
    except Exception as e:
        logger.error(
            f"[WsResultPush] 错误推送失败 | error_code={error_code} | session={session_id[:16]} | err={e}"
        )


async def push_chat_bubble(
    session_id: str,
    msg_id: str,
    content: str,
    affinity: int = 10,
    mood: str = "neutral",
) -> None:
    """
    推送普通对话气泡（type=text + type=voice）到用户 WS 连接。
    供工具层推送拒绝/提示类文字气泡使用，禁止直接 import socket_manager。
    """
    sm = get_socket_manager()
    try:
        await sm.send_to_user(session_id, {
            "type":         "text",
            "msg_id":       msg_id,
            "content":      content,
            "affinity":     affinity,
            "mood":         mood,
            "voice_status": "none",
        })
        await sm.send_to_user(session_id, {
            "type":         "voice",
            "msg_id":       msg_id,
            "voice_status": "none",
        })
    except Exception as e:
        logger.error(f"[WsResultPush] push_chat_bubble 失败 | session={session_id[:16]} | err={e}")
