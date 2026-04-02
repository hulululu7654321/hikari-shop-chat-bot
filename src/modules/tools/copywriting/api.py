# -*- coding: utf-8 -*-
"""
src/modules/tools/copywriting/api.py — 文案生成容器入口（v1.0.0）
=====================================================================
hikari-copywriting 容器（端口 8004）的 Quart 应用，对齐 MASTER §6.2：

  POST /api/u/images              图片上传（挂载自 shared/image_upload.py）
  GET  /api/u/images/{image_id}   获取图片文件（挂载自 shared/image_upload.py）
  POST /api/u/copywriting/round1  第一轮：接收图片，存状态，推送询问气泡
  POST /api/u/copywriting/round2  第二轮：接收主题/参考图，生成并推送文案

【VIP 守卫】
  VIP 检查已移至主容器 chat_handler.py，本容器直接执行任务。

【状态机说明】
  Round1 → 存 Redis hikari:copy:state:{user_id}（TTL=10min）
  Round2 → 读 Redis 状态，走生成 → 清 Redis

【响应格式】全部对齐 MASTER §8
  成功：{"code": 200, "message": "ok", "data": {...}}
  失败：{"code": 4xx/5xx, "message": "错误描述", "data": null}
=====================================================================
"""

import uuid
import asyncio
import traceback

from quart import Quart, jsonify, request, g

from src.utils.auth import require_auth
from src.utils.logger import get_logger, trace_id_var
from src.modules.tools.shared.image_upload import bp as image_upload_bp
from src.modules.tools.copywriting.handler import (
    run_copywriting_round1,
    run_copywriting_round2,
    is_waiting_ref,
)
from src.services.copywriting_service import is_copywriting_trigger

logger = get_logger(__name__)

app = Quart(__name__)
app.register_blueprint(image_upload_bp)


@app.before_request
async def inject_trace_id():
    tid = request.headers.get("X-Trace-ID") or uuid.uuid4().hex[:16]
    trace_id_var.set(tid)


# ══════════════════════════════════════════════════════════════════
# POST /api/u/copywriting/round1 — 第一轮
# ══════════════════════════════════════════════════════════════════

@app.route("/api/u/copywriting/round1", methods=["POST"])
@require_auth
async def handle_round1():
    """
    接收图片列表 + 触发词，准备文案生成状态。
    结果通过 WS 推送询问气泡，HTTP 立即返回 202 Accepted。

    Body JSON:
    {
        "session_id": "sess_wx_xxx",
        "image_ids":  ["uuid1", "uuid2"],
        "message":    "小红书文案 漫展日记",
        "msg_id":     "msg_xxx"
    }
    """
    user_id = g.user_id
    try:
        data       = await request.get_json() or {}
        session_id = data.get("session_id", "").strip()
        image_ids  = [i for i in data.get("image_ids", []) if isinstance(i, str) and i.strip()]
        user_text  = data.get("message", "").strip()
        msg_id     = data.get("msg_id", "").strip()

        if not session_id or not msg_id:
            return jsonify({
                "code":    400,
                "message": "session_id / msg_id 不能为空",
                "data":    None,
            }), 400

        if not image_ids:
            return jsonify({
                "code":    400,
                "message": "至少需要一张图片才能写文案喵",
                "data":    None,
            }), 400

        MAX_IMAGES = 9
        if len(image_ids) > MAX_IMAGES:
            return jsonify({
                "code":    400,
                "message": f"最多支持 {MAX_IMAGES} 张图片喵，选最好看的几张就行～",
                "data":    None,
            }), 400

        # 解析平台和 user_hint
        triggered, platform, user_hint = is_copywriting_trigger(user_text, image_ids)
        if not triggered:
            # 前端调用时应已确认触发词，此处兜底
            platform  = "xiaohongshu"
            user_hint = user_text

        # 异步执行（快速返回，结果通过 WS 推送）
        asyncio.create_task(
            run_copywriting_round1(
                user_id    = user_id,
                session_id = session_id,
                image_ids  = image_ids,
                platform   = platform,
                user_hint  = user_hint,
                msg_id     = msg_id,
            )
        )

        return jsonify({
            "code":    200,
            "message": "ok",
            "data":    {"status": "processing"},
        })

    except Exception:
        logger.error(f"[CopywritingAPI] /round1 崩溃: {traceback.format_exc()}")
        return jsonify({"code": 500, "message": "服务内部错误", "data": None}), 500


# ══════════════════════════════════════════════════════════════════
# POST /api/u/copywriting/round2 — 第二轮
# ══════════════════════════════════════════════════════════════════

@app.route("/api/u/copywriting/round2", methods=["POST"])
@require_auth
async def handle_round2():
    """
    接收主题描述 / 参考截图，触发文案生成。
    结果通过 WS 推送 copywriting_result 事件。

    Body JSON:
    {
        "session_id":    "sess_wx_xxx",
        "ref_image_id":  "uuid_or_empty",
        "message":       "银狼cos漫展记录",
        "msg_id":        "msg_xxx",
        "client_msg_id": "client_xxx",
        "image_ids":     ["uuid1"]   // 可选，用于检测是否重新触发 Round1
    }
    """
    user_id = g.user_id
    try:
        data           = await request.get_json() or {}
        session_id     = data.get("session_id", "").strip()
        ref_image_id   = data.get("ref_image_id", "").strip()
        user_text      = data.get("message", "").strip()
        msg_id         = data.get("msg_id", "").strip()
        client_msg_id  = data.get("client_msg_id", "").strip()
        image_ids_hint = [
            i for i in data.get("image_ids", [])
            if isinstance(i, str) and i.strip()
        ]

        if not session_id or not msg_id:
            return jsonify({
                "code":    400,
                "message": "session_id / msg_id 不能为空",
                "data":    None,
            }), 400

        # 检查是否处于等待状态（防止前端误调）
        if not await is_waiting_ref(user_id):
            return jsonify({
                "code":    400,
                "message": "没有待处理的文案任务喵，请先发图片触发第一轮",
                "data":    None,
            }), 400

        # 异步执行状态机（快速返回，结果通过 WS 推送）
        asyncio.create_task(
            run_copywriting_round2(
                user_id        = user_id,
                session_id     = session_id,
                ref_image_id   = ref_image_id,
                user_text      = user_text,
                msg_id         = msg_id,
                client_msg_id  = client_msg_id,
                image_ids_hint = image_ids_hint,
            )
        )

        return jsonify({
            "code":    200,
            "message": "ok",
            "data":    {"status": "processing"},
        })

    except Exception:
        logger.error(f"[CopywritingAPI] /round2 崩溃: {traceback.format_exc()}")
        return jsonify({"code": 500, "message": "服务内部错误", "data": None}), 500


# ══════════════════════════════════════════════════════════════════
# 系统健康检查
# ══════════════════════════════════════════════════════════════════

@app.route("/api/sys/health", methods=["GET"])
async def health():
    return jsonify({
        "code":    200,
        "message": "ok",
        "data":    {"service": "hikari-copywriting"},
    }), 200
