# -*- coding: utf-8 -*-
"""
src/modules/tools/imagegen/api.py — 图生图容器入口（v1.0.0）
=====================================================================
hikari-imagegen 容器（端口 8002）的 Quart 应用，对齐 MASTER §6.2：

  POST   /api/u/images             图片上传（来自 shared/image_upload.py）
  GET    /api/u/images/{image_id}  获取图片文件（来自 shared/image_upload.py）
  POST   /api/u/images/generate    触发图生图 Workflow
  GET    /api/u/images/{workflow_id}/status  查询 Workflow 进度

【VIP 守卫】
  VIP/配额检查已移至主容器 chat_handler.py，本容器直接执行任务。
  consume_quota 由 worker 在任务完成后调用 /api/u/images/quota/consume。

【Temporal】
  正常：提交 ImageGenWorkflow，返回 workflow_id
  降级：TEMPORAL_HOST 不可用时直接 asyncio.create_task(run_image_gen_task)

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
from src.modules.tools.shared.vip_guard import consume_quota
from src.modules.tools.shared.image_upload import bp as image_upload_bp
from src.modules.tools.imagegen.handler import run_image_gen_task
from src.workflows.image_gen_workflow import IMAGE_GEN_TRIGGERS

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

async def _submit_image_gen_workflow(
    user_id: str,
    session_id: str,
    image_id: str,
    user_text: str,
    msg_id: str,
) -> str | None:
    """
    提交 ImageGenWorkflow 到 Temporal。
    失败时返回 None（调用方降级到直接执行）。
    返回 workflow_id（= msg_id，便于前端轮询状态）。
    """
    try:
        from temporalio.client import Client
        from src.workflows.image_gen_workflow import ImageGenWorkflow, ImageGenInput

        client = await Client.connect(
            os.getenv("TEMPORAL_HOST", "hikari-temporal:7233"),
            namespace=os.getenv("TEMPORAL_NAMESPACE", "default"),
        )
        handle = await client.start_workflow(
            ImageGenWorkflow.run,
            ImageGenInput(
                user_id    = user_id,
                session_id = session_id,
                image_id   = image_id,
                message    = user_text,
                msg_id     = msg_id,
            ),
            id          = f"image-gen-{msg_id}",
            task_queue  = os.getenv("TEMPORAL_TASK_QUEUE", "hikari-main"),
        )
        logger.info(f"[ImageGenAPI] Workflow 提交成功 | wf_id={handle.id}")
        return handle.id

    except Exception as e:
        logger.warning(f"[ImageGenAPI] Temporal 不可用，降级直接执行 | err={e}")
        return None


# ══════════════════════════════════════════════════════════════════
# POST /api/u/images/generate — 触发图生图
# ══════════════════════════════════════════════════════════════════

@app.route("/api/u/images/generate", methods=["POST"])
@require_auth
async def handle_image_gen():
    """
    Body JSON:
    {
        "session_id": "sess_wx_xxx",
        "image_id":   "uuid",
        "message":    "希卡莉启动pose替身 蕾姆",
        "msg_id":     "msg_xxx"
    }
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
                "code": 400,
                "message": "session_id / image_id / msg_id 均不能为空",
                "data": None,
            }), 400

        # 去掉触发词，只保留 user_hint（对齐 makeup 的处理方式）
        trigger_pattern = "|".join(re.escape(kw) for kw in IMAGE_GEN_TRIGGERS)
        user_hint = re.sub(trigger_pattern, "", user_text).strip()

        # 提交 Workflow
        workflow_id = await _submit_image_gen_workflow(
            user_id, session_id, image_id, user_hint, msg_id
        )

        if workflow_id:
            # 正常路径：Workflow 已提交，结果通过 WS 推送
            return jsonify({
                "code":    200,
                "message": "ok",
                "data":    {"workflow_id": workflow_id, "status": "pending"},
            })
        else:
            # 降级路径：直接异步执行，结果同样通过 WS 推送
            asyncio.create_task(
                run_image_gen_task(user_id, session_id, image_id, user_hint, msg_id)
            )
            return jsonify({
                "code":    200,
                "message": "ok",
                "data":    {"workflow_id": msg_id, "status": "pending"},
            })

    except Exception:
        logger.error(f"[ImageGenAPI] /generate 崩溃: {traceback.format_exc()}")
        return jsonify({"code": 500, "message": "服务内部错误", "data": None}), 500


# ══════════════════════════════════════════════════════════════════
# GET /api/u/images/<workflow_id>/status — 查询进度
# ══════════════════════════════════════════════════════════════════

@app.route("/api/u/images/<workflow_id>/status", methods=["GET"])
@require_auth
async def handle_image_gen_status(workflow_id: str):
    """
    查询图生图 Workflow 执行状态。
    正常情况下结果通过 WS 推送，此接口供前端轮询兜底使用。
    """
    try:
        from temporalio.client import Client
        from temporalio.service import RPCError

        client = await Client.connect(
            os.getenv("TEMPORAL_HOST", "hikari-temporal:7233"),
            namespace=os.getenv("TEMPORAL_NAMESPACE", "default"),
        )
        handle = client.get_workflow_handle(f"image-gen-{workflow_id}")
        desc   = await handle.describe()
        status = desc.status.name.lower() if desc.status else "unknown"

        return jsonify({
            "code":    200,
            "message": "ok",
            "data":    {"workflow_id": workflow_id, "status": status},
        })

    except Exception as e:
        logger.warning(f"[ImageGenAPI] 查询 Workflow 状态失败 | wf={workflow_id} | err={e}")
        # Temporal 不可用或 workflow 不存在时返回 unknown，前端以 WS 推送为准
        return jsonify({
            "code":    200,
            "message": "ok",
            "data":    {"workflow_id": workflow_id, "status": "unknown"},
        })


# ══════════════════════════════════════════════════════════════════
# 系统健康检查
# ══════════════════════════════════════════════════════════════════

@app.route("/api/sys/health", methods=["GET"])
async def health():
    return jsonify({"code": 200, "message": "ok", "data": {"service": "hikari-imagegen"}}), 200


# ══════════════════════════════════════════════════════════════════
# 配额消费 Hook（供 ImageGenWorkflow 最终成功后回调）
# ══════════════════════════════════════════════════════════════════

@app.route("/api/u/images/quota/consume", methods=["POST"])
@require_auth
async def handle_quota_consume():
    """
    内部接口：Workflow 成功完成后由 worker 调用扣减配额。
    正常情况下 worker 直接调用 consume_quota()，此接口为兜底。
    """
    user_id = g.user_id
    try:
        ok = await consume_quota(user_id, "image_gen")
        return jsonify({
            "code":    200 if ok else 500,
            "message": "ok" if ok else "配额扣减失败",
            "data":    None,
        })
    except Exception as e:
        logger.error(f"[ImageGenAPI] quota/consume 失败 | uid={user_id[:8]} | err={e}")
        return jsonify({"code": 500, "message": "服务内部错误", "data": None}), 500
