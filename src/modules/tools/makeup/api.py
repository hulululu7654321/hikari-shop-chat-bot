# -*- coding: utf-8 -*-
"""
src/modules/tools/makeup/api.py — 妆容分析容器入口（v1.0.0）
=====================================================================
hikari-makeup 容器（端口 8003）的 Quart 应用，对齐 MASTER §6.2：

  POST /api/u/images             图片上传（挂载自 shared/image_upload.py）
  GET  /api/u/images/{image_id}  获取图片文件（挂载自 shared/image_upload.py）
  POST /api/u/makeup/analyze     触发妆容分析 Workflow

【VIP 守卫】
  VIP 检查已移至主容器 chat_handler.py，本容器直接执行任务。

【Temporal】
  正常：提交 MakeupWorkflow，结果通过 WS 推送
  降级：直接 asyncio.create_task(run_makeup_task)

【响应格式】全部对齐 MASTER §8
  成功：{"code": 200, "message": "ok", "data": {...}}
  失败：{"code": 4xx/5xx, "message": "错误描述", "data": null}
=====================================================================
"""

import os
import re
import uuid
import asyncio
import traceback

from quart import Quart, jsonify, request, g

from src.utils.auth import require_auth
from src.utils.logger import get_logger, trace_id_var
from src.modules.tools.shared.image_upload import bp as image_upload_bp
from src.modules.tools.makeup.handler import run_makeup_task
from src.services.makeup_service import MAKEUP_TRIGGERS

logger = get_logger(__name__)

app = Quart(__name__)
app.register_blueprint(image_upload_bp)


@app.before_request
async def inject_trace_id():
    tid = request.headers.get("X-Trace-ID") or uuid.uuid4().hex[:16]
    trace_id_var.set(tid)


# ══════════════════════════════════════════════════════════════════
# 内部：Temporal Workflow 提交（含降级）
# ══════════════════════════════════════════════════════════════════

async def _submit_makeup_workflow(
    user_id: str,
    session_id: str,
    image_id: str,
    user_text: str,
    msg_id: str,
) -> str | None:
    """
    提交 MakeupWorkflow 到 Temporal。
    失败时返回 None（调用方降级到直接执行）。
    """
    try:
        from temporalio.client import Client
        from src.workflows.makeup_workflow import MakeupWorkflow, MakeupInput

        client = await Client.connect(
            os.getenv("TEMPORAL_HOST", "hikari-temporal:7233"),
            namespace=os.getenv("TEMPORAL_NAMESPACE", "default"),
        )
        handle = await client.start_workflow(
            MakeupWorkflow.run,
            MakeupInput(
                user_id    = user_id,
                session_id = session_id,
                image_id   = image_id,
                user_text  = user_text,
                msg_id     = msg_id,
            ),
            id         = f"makeup-{msg_id}",
            task_queue = os.getenv("TEMPORAL_TASK_QUEUE", "hikari-main"),
        )
        logger.info(f"[MakeupAPI] Workflow 提交成功 | wf_id={handle.id}")
        return handle.id

    except Exception as e:
        logger.warning(f"[MakeupAPI] Temporal 不可用，降级直接执行 | err={e}")
        return None


# ══════════════════════════════════════════════════════════════════
# POST /api/u/makeup/analyze — 触发妆容分析
# ══════════════════════════════════════════════════════════════════

@app.route("/api/u/makeup/analyze", methods=["POST"])
@require_auth
async def handle_makeup_analyze():
    """
    Body JSON:
    {
        "session_id": "sess_wx_xxx",
        "image_id":   "uuid",
        "message":    "帮我看看妆 蕾姆",
        "msg_id":     "msg_xxx"
    }
    触发词校验在 orchestrator 层已完成，此处直接信任调用方。
    """
    user_id = g.user_id
    try:
        data       = await request.get_json() or {}
        session_id = data.get("session_id", "").strip()
        image_id   = data.get("image_id", "").strip()
        user_text  = data.get("message", "").strip()
        msg_id     = data.get("msg_id", "").strip()

        if not session_id or not image_id or not msg_id:
            return jsonify({
                "code":    400,
                "message": "session_id / image_id / msg_id 均不能为空",
                "data":    None,
            }), 400

        # 提取 user_hint（去掉触发词后的内容）
        trigger_pattern = "|".join(re.escape(kw) for kw in MAKEUP_TRIGGERS)
        user_hint = re.sub(trigger_pattern, "", user_text).strip()
        user_hint = re.sub(r"^[，。,.\s]+", "", user_hint).strip()

        # 提交 Workflow（含降级）
        workflow_id = await _submit_makeup_workflow(
            user_id, session_id, image_id, user_hint, msg_id
        )

        if workflow_id:
            return jsonify({
                "code":    200,
                "message": "ok",
                "data":    {"workflow_id": workflow_id, "status": "pending"},
            })
        else:
            asyncio.create_task(
                run_makeup_task(user_id, session_id, image_id, user_hint, msg_id)
            )
            return jsonify({
                "code":    200,
                "message": "ok",
                "data":    {"workflow_id": msg_id, "status": "pending"},
            })

    except Exception:
        logger.error(f"[MakeupAPI] /analyze 崩溃: {traceback.format_exc()}")
        return jsonify({"code": 500, "message": "服务内部错误", "data": None}), 500


# ══════════════════════════════════════════════════════════════════
# 系统健康检查
# ══════════════════════════════════════════════════════════════════

@app.route("/api/sys/health", methods=["GET"])
async def health():
    return jsonify({
        "code":    200,
        "message": "ok",
        "data":    {"service": "hikari-makeup"},
    }), 200
