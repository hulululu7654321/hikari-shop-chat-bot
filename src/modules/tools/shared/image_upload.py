# -*- coding: utf-8 -*-
"""
src/modules/tools/shared/image_upload.py — 图片上传与文件服务（v1.0.0）
=====================================================================
供各工具容器挂载的图片上传路由（POST /api/u/images）和图片文件服务
（GET /api/u/images/{image_id}）。

路由路径对齐 MASTER §6.2：
  POST   /api/u/images          上传图片，返回 image_id
  GET    /api/u/images/{image_id} 获取图片文件（Bearer JWT 鉴权）

响应格式对齐 MASTER §8：
  成功：{"code": 200, "message": "ok", "data": {...}}
  失败：{"code": 4xx/5xx, "message": "错误描述", "data": null}

【安全】
- 上传后立即做微信图片安全检测，不通过则删除本地文件并拒绝
- 文件服务路径做严格 prefix 校验，防路径穿越
=====================================================================
"""

import os
import io
import re

from quart import Blueprint, request, jsonify, g, send_file

from src.utils.auth import require_auth
from src.utils.vision_helper import get_vision_helper
from src.utils.wx_security import check_image_safe
from src.utils.logger import get_logger

logger = get_logger(__name__)

bp = Blueprint("image_upload", __name__)


# ══════════════════════════════════════════════════════════════════
# POST /api/u/images — 上传图片
# ══════════════════════════════════════════════════════════════════

@bp.route("/api/u/images", methods=["POST"])
@require_auth
async def handle_upload_image():
    """
    接收 multipart/form-data，字段名 "image"。
    经微信内容安全检测后落盘，返回 image_id 和临时访问 URL。
    """
    user_id = g.user_id
    try:
        files = await request.files
        if "image" not in files:
            return jsonify({"code": 400, "message": "缺少 image 字段", "data": None}), 400

        file = files["image"]
        vh   = get_vision_helper()
        res  = await vh.process_user_image(
            user_id,
            io.BytesIO(file.read()),
            file.filename or "upload.jpg",
        )

        if res["status"] != "success":
            return jsonify({
                "code": 403,
                "message": res.get("message", "图片处理失败"),
                "data": None,
            }), 403

        # 微信内容安全检测
        import base64 as _b64
        raw_bytes = _b64.b64decode(res["base64"])
        is_safe   = await check_image_safe(user_id, raw_bytes)
        if not is_safe:
            bad_path = os.path.join(vh.upload_root, user_id, f"{res['image_id']}.jpg")
            if os.path.exists(bad_path):
                os.remove(bad_path)
            return jsonify({
                "code": 403,
                "message": "这张图的内容有点危险喵，换一张试试？",
                "data": None,
            }), 403

        image_id = res["image_id"]
        return jsonify({
            "code": 200,
            "message": "ok",
            "data": {
                "image_id": image_id,
                "url": f"/api/u/images/{image_id}",
            },
        })

    except Exception as e:
        logger.error(f"[ImageUpload] 上传异常 | uid={user_id[:8]} | err={e}")
        return jsonify({"code": 500, "message": "上传失败了喵，稍后再试", "data": None}), 500


# ══════════════════════════════════════════════════════════════════
# GET /api/u/images/<image_id> — 获取图片文件
# ══════════════════════════════════════════════════════════════════

async def _serve_image_file(user_id: str, image_id: str):
    """公共实现：按 user_id + image_id 返回图片文件。"""
    try:
        # image_id 去掉 .jpg 后缀（前端 URL 带后缀）
        bare_id = image_id[:-4] if image_id.endswith(".jpg") else image_id
        if not re.match(r"^[a-zA-Z0-9_\-]{1,128}$", bare_id):
            return jsonify({"code": 403, "message": "非法 image_id", "data": None}), 403

        vh       = get_vision_helper()
        img_path = os.path.normpath(
            os.path.join(vh.upload_root, user_id, f"{bare_id}.jpg")
        )
        allowed_prefix = os.path.normpath(os.path.join(vh.upload_root, user_id))
        if not img_path.startswith(allowed_prefix):
            return jsonify({"code": 403, "message": "路径非法", "data": None}), 403

        if not os.path.isfile(img_path):
            return jsonify({"code": 404, "message": "图片不存在", "data": None}), 404

        return await send_file(img_path, mimetype="image/jpeg")

    except Exception as e:
        logger.error(f"[ImageUpload] 文件服务异常 | uid={user_id[:8]} | err={e}")
        return jsonify({"code": 500, "message": "获取图片失败", "data": None}), 500


@bp.route("/api/u/images/<image_id>", methods=["GET"])
@require_auth
async def serve_image(image_id: str):
    """GET /api/u/images/<image_id> — 按 image_id 返回图片（token 来自 Bearer 或 ?token=）。"""
    return await _serve_image_file(g.user_id, image_id)


@bp.route("/api/u/images/<path_user_id>/<image_id>", methods=["GET"])
async def serve_image_by_uid(path_user_id: str, image_id: str):
    """
    GET /api/u/images/<user_id>/<image_id>.jpg?token=...
    前端历史消息图片使用此格式（?token= 鉴权，不走 Bearer）。
    只允许 token 对应的 user_id 与 path_user_id 一致。
    """
    from src.utils.auth import decode_token_payload
    token = request.args.get("token", "")
    if not token:
        auth = request.headers.get("Authorization", "")
        token = auth.removeprefix("Bearer ").strip()
    payload = decode_token_payload(token)
    if not payload or payload.get("sub") != path_user_id:
        return jsonify({"code": 401, "message": "无权访问", "data": None}), 401
    return await _serve_image_file(path_user_id, image_id)
