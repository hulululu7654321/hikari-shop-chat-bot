# -*- coding: utf-8 -*-
"""
src/modules/users/api.py
希卡莉 — 用户域路由层（/api/u/）

路由列表（对齐重构文档 6.2）：
  POST   /api/u/auth/login          微信 code 换 Token
  POST   /api/u/auth/refresh        刷新 Access Token
  POST   /api/u/auth/logout         登出
  POST   /api/u/invites/activate    激活邀请码
  POST   /api/u/invites/quit        退出内测
  GET    /api/u/vip                 VIP 状态查询
  WS     /api/u/ws/<session_id>     主 WebSocket
  GET    /api/u/messages            分页历史消息
  POST   /api/u/images/upload       上传图片（临时，imagegen 容器上线前挂载在此）
  GET    /api/u/images/<uid>/<fn>   图片文件（临时）
  GET    /api/u/voices/<path:path>  语音文件
  POST   /api/u/memoirs/export      触发灵魂报告
  GET    /api/u/memoirs/status      报告冷却状态
  GET    /api/u/memoirs/<path:path> 报告文件下载
  GET    /api/u/voice/library       声线库
  POST   /api/u/voice/preference    设置声线偏好
"""

import os
import io
import json
import asyncio
import traceback
import base64
import re
import httpx
from datetime import datetime, timezone

from quart import Blueprint, request, jsonify, websocket, g, send_file
from quart_cors import cors_exempt

from src.utils.logger import get_logger, trace_id_var
from src.utils.database import get_db_manager, _sanitize_input, COL_USERS, COL_MESSAGES
from src.utils.auth import (
    require_auth,
    verify_access_token,
)
from src.utils.socket_manager import get_socket_manager
from src.utils.vision_helper import get_vision_helper
from src.utils.wx_security import check_image_safe
from src.core.orchestrator import get_bot_orchestrator

from src.modules.users.auth_handler import (
    handle_login,
    handle_refresh,
    handle_logout,
    handle_activate_invite,
    handle_quit,
    get_vip_status,
)
from src.modules.users.chat_handler import process_interaction_pipeline

logger = get_logger(__name__)

bp = Blueprint("user_api", __name__, url_prefix="/api/u")

_COPYWRITING_BASE = os.getenv("COPYWRITING_SERVICE_URL", "http://hikari-copywriting:8004")
_MAKEUP_BASE      = os.getenv("MAKEUP_SERVICE_URL",      "http://hikari-makeup:8003")
_IMAGEGEN_BASE    = os.getenv("IMAGEGEN_SERVICE_URL",    "http://hikari-imagegen:8002")

_TOOL_ROUTES = {
    "copywriting_round1": (_COPYWRITING_BASE, "/api/u/copywriting/round1"),
    "copywriting_round2": (_COPYWRITING_BASE, "/api/u/copywriting/round2"),
    "makeup":             (_MAKEUP_BASE,      "/api/u/makeup/analyze"),
    "imagegen":           (_IMAGEGEN_BASE,    "/api/u/images/generate"),
}


async def _forward_tool(tool: str, payload: dict, token: str) -> bool:
    """
    向专用容器转发工具请求，传播 X-Trace-ID 延续调用链路。
    返回 True 表示转发成功（HTTP 200），False 表示失败（超时/网络错误/非200）。
    """
    base_url, path = _TOOL_ROUTES[tool]
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                f"{base_url}{path}",
                json=payload,
                headers={
                    "Authorization": f"Bearer {token}",
                    "X-Trace-ID":    trace_id_var.get(),
                },
            )
            if resp.status_code != 200:
                logger.warning(f"[ToolForward] {tool} 返回 {resp.status_code}: {resp.text[:200]}")
                return False
            return True
    except Exception as e:
        logger.warning(f"[ToolForward] {tool} 转发失败: {e}")
        return False


# ──────────────────────────────────────────
# 工具：速率限制
# ──────────────────────────────────────────

async def _rate_limit(key: str, limit: int, window_seconds: int) -> bool:
    try:
        redis   = await get_db_manager().get_redis()
        current = await redis.incr(key)
        if current == 1:
            await redis.expire(key, window_seconds)
        return current <= limit
    except Exception as e:
        logger.warning(f"[RateLimit-Degrade] Redis 异常: {e}")
        return True


def _client_ip() -> str:
    return (
        request.headers.get("X-Real-IP") or
        request.headers.get("X-Forwarded-For", "").split(",")[0].strip() or
        "unknown"
    )


# ══════════════════════════════════════════
# 1. 鉴权接口
# ══════════════════════════════════════════

@bp.route("/auth/login", methods=["POST"])
async def api_login():
    """POST /api/u/auth/login — 微信 code 换双 Token。"""
    ip = _client_ip()
    if not await _rate_limit(f"hikari:rl:login:{ip}", 10, 60):
        return jsonify({"code": 429, "message": "请求过于频繁，请稍后再试", "data": None}), 429

    try:
        data       = await request.get_json() or {}
        js_code    = _sanitize_input(data.get("code"))
        cached_uid = _sanitize_input(data.get("user_id"))

        result = await handle_login(js_code, cached_uid)
        return jsonify({"code": 200, "message": "ok", "data": result}), 200

    except Exception:
        logger.error(f"[Login-Error] {traceback.format_exc()}")
        return jsonify({"code": 500, "message": "登录失败", "data": None}), 500


@bp.route("/auth/refresh", methods=["POST"])
async def api_refresh():
    """POST /api/u/auth/refresh — 刷新 Access Token。"""
    try:
        data          = await request.get_json() or {}
        user_id       = _sanitize_input(data.get("user_id", ""))
        refresh_token = _sanitize_input(data.get("refresh_token", ""))
        jti           = _sanitize_input(data.get("jti", ""))

        if not user_id or not refresh_token or not jti:
            return jsonify({"code": 400, "message": "缺少必要参数", "data": None}), 400

        result = await handle_refresh(user_id, refresh_token, jti)
        if not result:
            return jsonify({"code": 401, "message": "Refresh Token 无效或已过期", "data": None}), 401

        return jsonify({"code": 200, "message": "ok", "data": result}), 200

    except Exception:
        logger.error(f"[Refresh-Error] {traceback.format_exc()}")
        return jsonify({"code": 500, "message": "刷新失败", "data": None}), 500


@bp.route("/auth/logout", methods=["POST"])
@require_auth
async def api_logout():
    """POST /api/u/auth/logout — 登出，撤销 Refresh Token。"""
    try:
        await handle_logout(g.user_id, g.jti)
        return jsonify({"code": 200, "message": "ok", "data": None}), 200
    except Exception:
        logger.error(f"[Logout-Error] {traceback.format_exc()}")
        return jsonify({"code": 500, "message": "登出失败", "data": None}), 500


# ══════════════════════════════════════════
# 2. 邀请码
# ══════════════════════════════════════════

@bp.route("/invites/activate", methods=["POST"])
@require_auth
async def api_activate_invite():
    """POST /api/u/invites/activate — 激活邀请码。"""
    data = await request.get_json() or {}
    code = _sanitize_input(data.get("code", "")).strip()
    if not code:
        return jsonify({"code": 400, "message": "请输入邀请码喵", "data": None}), 400

    try:
        success, msg = await handle_activate_invite(g.user_id, code)
        if success:
            return jsonify({"code": 200, "message": msg, "data": None}), 200
        return jsonify({"code": 400, "message": msg, "data": None}), 400
    except Exception:
        logger.error(f"[activate_invite] {traceback.format_exc()}")
        return jsonify({"code": 500, "message": "激活失败了喵，稍后再试", "data": None}), 500


@bp.route("/invites/quit", methods=["POST"])
@require_auth
async def api_quit():
    """POST /api/u/invites/quit — 退出内测。"""
    try:
        success = await handle_quit(g.user_id)
        if success:
            return jsonify({"code": 200, "message": "已退出，期待你下次回来喵～", "data": None}), 200
        return jsonify({"code": 500, "message": "退出失败了喵", "data": None}), 500
    except Exception:
        logger.error(f"[quit] {traceback.format_exc()}")
        return jsonify({"code": 500, "message": "操作失败了喵", "data": None}), 500


# ══════════════════════════════════════════
# 3. VIP 状态
# ══════════════════════════════════════════

@bp.route("/vip", methods=["GET"])
@require_auth
async def api_vip_status():
    """GET /api/u/vip — VIP 状态 + 配额 + 今日消息数。"""
    try:
        data = await get_vip_status(g.user_id)
        return jsonify({"code": 200, "message": "ok", "data": data}), 200
    except Exception:
        logger.error(f"[vip_status] {traceback.format_exc()}")
        return jsonify({"code": 500, "message": "查询失败", "data": None}), 500


# ══════════════════════════════════════════
# 4. WebSocket（主链路）
# ══════════════════════════════════════════

@bp.websocket("/ws/<session_id>")
@cors_exempt
async def handle_websocket(session_id):
    """
    WS /api/u/ws/<session_id>
    首行立刻 accept()，物理绕过 ASGI 握手拦截。
    Token 通过 query param 传入：?token=<access_token>
    """
    await websocket.accept()

    token = _sanitize_input(websocket.args.get("token", ""))
    payload = verify_access_token(token)
    if not payload:
        await websocket.send(json.dumps({"type": "error", "content": "凭证过期了喵，重新登录一下~"}))
        await websocket.close(1008)
        return

    ws_user_id = payload["user_id"]

    # 屏幕端 Token 校验
    if ws_user_id.startswith("store_screen_"):
        expected_sid = f"store_screen_{payload.get('store_id', '')}"
        if session_id != expected_sid:
            logger.warning(f"[WS-ScreenToken] store_id 不匹配 | session={session_id} | expected={expected_sid}")
            await websocket.send(json.dumps({"type": "error", "content": "屏幕端凭证与连接目标不匹配"}))
            await websocket.close(1008)
            return

    sm         = get_socket_manager()
    current_ws = websocket._get_current_object()
    queue      = await sm.connect(session_id, current_ws, user_id=ws_user_id)
    logger.info(f"[WS-Open] UID:{ws_user_id[:8]} | SID:{session_id}")

    async def recv_loop():
        while True:
            raw = await websocket.receive()
            if not raw:
                return
            payload_msg = json.loads(raw)

            if payload_msg.get("action") == "ping":
                await queue.put({"action": "pong"})

            elif payload_msg.get("action") == "chat":
                msg          = _sanitize_input(payload_msg.get("message", ""))
                raw_ids      = payload_msg.get("image_ids") or []
                single       = _sanitize_input(payload_msg.get("image_id") or "")
                if not raw_ids and single:
                    raw_ids = [single]
                image_ids    = [_sanitize_input(i) for i in raw_ids if i]
                client_mid   = _sanitize_input(payload_msg.get("client_msg_id", "")) or None

                # 文案状态机第二轮拦截 → 转发到专用容器
                from src.services.copywriting_service import is_waiting_ref
                if await is_waiting_ref(ws_user_id):
                    ref_id  = image_ids[0] if image_ids else ""
                    tmp_mid = f"msg_{int(datetime.now().timestamp())}"
                    success = await _forward_tool(
                        "copywriting_round2",
                        {"session_id": session_id, "image_ids": image_ids,
                         "ref_image_id": ref_id, "message": msg,
                         "msg_id": tmp_mid, "client_msg_id": client_mid or ""},
                        token,
                    )
                    if not success:
                        await queue.put({
                            "type": "text", "msg_id": tmp_mid,
                            "content": "文案服务暂时开小差了喵，稍后再发一次试试？",
                            "affinity": 10, "mood": "neutral", "voice_status": "none",
                        })
                    continue

                asyncio.create_task(_handle_chat(
                    ws_user_id, session_id, msg, image_ids,
                    bool(payload_msg.get("enable_search", False)),
                    client_mid, token,
                ))

    async def send_loop():
        while True:
            msg = await queue.get()
            if msg is None:
                return
            await websocket.send(json.dumps(msg))
            logger.info(f"[WS-Send] Type:{msg.get('type')}")

    try:
        recv_task = asyncio.ensure_future(recv_loop())
        send_task = asyncio.ensure_future(send_loop())
        done, pending = await asyncio.wait(
            [recv_task, send_task],
            return_when=asyncio.FIRST_COMPLETED,
        )
        for t in pending:
            t.cancel()
    except Exception:
        logger.warning("[WS-Warning] 物理链路发生波动")
    finally:
        await queue.put(None)
        await sm.disconnect(session_id, current_ws)


# ══════════════════════════════════════════
# 5. 历史消息
# ══════════════════════════════════════════

@bp.route("/messages", methods=["GET"])
@require_auth
async def api_messages():
    """
    GET /api/u/messages?before_id=<id>
    分页历史消息，图片 URL 注入 access_token。
    """
    uid = g.user_id
    auth_header = request.headers.get("Authorization", "")
    token = auth_header.split(" ")[1] if auth_header.startswith("Bearer ") else ""

    try:
        before_id = request.args.get("before_id", None)
        data = await get_bot_orchestrator().get_user_history(user_id=uid, before_id=before_id)

        if "list" in data:
            for msg in data["list"]:
                img_id = msg.get("image_id")
                if img_id:
                    msg["image_url"] = f"/api/u/images/{uid}/{img_id}.jpg?token={token}"
                gen_url = msg.get("gen_image_url", "")
                if gen_url and gen_url.startswith("/api/u/images/") and "?token=" not in gen_url:
                    msg["gen_image_url"] = f"{gen_url}?token={token}"

        db       = get_db_manager().db
        user_doc = await db[COL_USERS].find_one({"user_id": uid})
        if user_doc:
            if "profile" not in data:
                data["profile"] = {}
            data["profile"]["voice_character"] = user_doc.get("voice_character", "女性1_zh")

        return jsonify({"code": 200, "message": "ok", "data": data}), 200

    except Exception:
        logger.error(f"[Messages-Error]: {traceback.format_exc()}")
        return jsonify({"code": 500, "message": "查询失败", "data": None}), 500


# ══════════════════════════════════════════
# 6. 图片（临时挂载；imagegen 容器上线后由 Nginx 转发至 hikari-imagegen）
# ══════════════════════════════════════════

@bp.route("/images/upload", methods=["POST"])
@require_auth
async def api_upload_image():
    """POST /api/u/images/upload — 上传图片，返回 image_id。"""
    uid = g.user_id
    try:
        files = await request.files
        if "image" not in files:
            return jsonify({"code": 400, "message": "缺失图片", "data": None}), 400

        file = files["image"]
        vh   = get_vision_helper()
        res  = await vh.process_user_image(uid, io.BytesIO(file.read()), _sanitize_input(file.filename))

        if res["status"] == "success":
            raw_bytes = base64.b64decode(res["base64"])
            if not await check_image_safe(uid, raw_bytes):
                if os.path.exists(res["local_path"]):
                    os.remove(res["local_path"])
                return jsonify({"code": 403, "message": "这张图的能量波动有点危险喵，换一张行不行~", "data": None}), 403

            return jsonify({
                "code": 200, "message": "ok",
                "data": {
                    "image_id": res["image_id"],
                    "temp_url": f"/api/u/images/{uid}/{res['image_id']}.jpg",
                },
            }), 200
        else:
            return jsonify({"code": 403, "message": res.get("message", "识别失败"), "data": None}), 403

    except Exception:
        logger.error(f"[upload_image] {traceback.format_exc()}")
        return jsonify({"code": 500, "message": "上传失败", "data": None}), 500


@bp.route("/images/<uid>/<filename>", methods=["GET"])
async def api_serve_image(uid, filename):
    """GET /api/u/images/<uid>/<filename> — 图片文件，支持 Bearer 或 ?token= 两种方式鉴权。"""
    auth_header = request.headers.get("Authorization", "")
    token = (
        auth_header.split(" ")[1] if auth_header.startswith("Bearer ")
        else request.args.get("token", "")
    )
    payload = verify_access_token(token)
    if not payload or payload["user_id"] != uid:
        return jsonify({"code": 401, "message": "凭证失效", "data": None}), 401

    try:
        if not re.match(r"^[a-zA-Z0-9_\-]{1,128}$", uid):
            return jsonify({"code": 403, "message": "非法 UID", "data": None}), 403

        vh       = get_vision_helper()
        img_path = os.path.normpath(os.path.join(vh.upload_root, uid, _sanitize_input(filename)))
        if not img_path.startswith(os.path.join(vh.upload_root, uid)):
            return jsonify({"code": 403, "message": "路径越界", "data": None}), 403

        if os.path.isfile(img_path):
            return await send_file(img_path, mimetype="image/jpeg")
        return jsonify({"code": 404, "message": "图片不存在", "data": None}), 404

    except Exception:
        logger.error(f"[serve_image] {traceback.format_exc()}")
        return jsonify({"code": 500, "message": "服务异常", "data": None}), 500


# ══════════════════════════════════════════
# 7. 语音文件
# ══════════════════════════════════════════

@bp.route("/voices/<path:path>", methods=["GET"])
@require_auth
async def api_serve_voice(path):
    """GET /api/u/voices/<path> — 语音文件下载。"""
    try:
        voice_root   = os.getenv("VOICE_OUTPUT_ROOT", "/app/assets/voices")
        safe_path    = os.path.normpath(os.path.join(voice_root, path))
        if not safe_path.startswith(voice_root):
            return jsonify({"code": 403, "message": "路径越界", "data": None}), 403
        if os.path.isfile(safe_path):
            return await send_file(safe_path, mimetype="audio/wav")
        return jsonify({"code": 404, "message": "文件不存在", "data": None}), 404
    except Exception:
        logger.error(f"[serve_voice] {traceback.format_exc()}")
        return jsonify({"code": 500, "message": "服务异常", "data": None}), 500


# ══════════════════════════════════════════
# 8. 灵魂报告
# ══════════════════════════════════════════

@bp.route("/memoirs/export", methods=["POST"])
@require_auth
async def api_export_memoir():
    """POST /api/u/memoirs/export — 触发灵魂报告生成。"""
    uid = g.user_id
    if not await _rate_limit(f"hikari:rl:memoir:{uid}", 1, 300):
        return jsonify({"code": 429, "message": "画像炼制冷却中", "data": None}), 429

    session_id = await get_socket_manager().get_session_id_for_user(uid)
    asyncio.create_task(_background_memoir_task(uid, session_id))
    return jsonify({"code": 200, "message": "Started", "data": None}), 200


@bp.route("/memoirs/status", methods=["GET"])
@require_auth
async def api_memoir_status():
    """GET /api/u/memoirs/status — 报告生成冷却剩余秒数。"""
    uid = g.user_id
    try:
        redis = await get_db_manager().get_redis()
        ttl   = await redis.ttl(f"hikari:rl:memoir:{uid}")
        return jsonify({"code": 200, "message": "ok", "data": {"cooldown": max(0, ttl) if ttl >= 0 else 0}}), 200
    except Exception as e:
        logger.warning(f"[memoir-status] Redis 查询失败: {e}")
        return jsonify({"code": 200, "message": "ok", "data": {"cooldown": 0}}), 200


@bp.route("/memoirs/<path:path>", methods=["GET"])
@require_auth
async def api_serve_memoir(path):
    """GET /api/u/memoirs/<path> — 报告文件下载。"""
    uid = g.user_id
    try:
        memoir_dir   = os.getenv("MEMOIR_EXPORT_DIR", "/app/exports")
        safe_path    = os.path.normpath(os.path.join(memoir_dir, path))
        if not safe_path.startswith(memoir_dir):
            return jsonify({"code": 403, "message": "路径越界", "data": None}), 403

        db = get_db_manager().db
        if not await db[COL_MESSAGES].find_one({"user_id": uid, "report_url": path}):
            return jsonify({"code": 403, "message": "无权访问", "data": None}), 403

        if os.path.isfile(safe_path):
            return await send_file(safe_path, mimetype="text/markdown")
        return jsonify({"code": 404, "message": "文件不存在", "data": None}), 404

    except Exception:
        logger.error(f"[serve_memoir] {traceback.format_exc()}")
        return jsonify({"code": 500, "message": "服务异常", "data": None}), 500


async def _background_memoir_task(user_id: str, session_id: str) -> None:
    try:
        from src.services.memoir_exporter import get_memoir_exporter
        res = await get_memoir_exporter().export_user_memoir_llm(user_id, session_id=session_id)
        if res and res.get("success"):
            db     = get_db_manager().db
            bubble = "🐾 报告出炉了！本喵的观察结果，自己去看喵~"
            await db[COL_MESSAGES].insert_one({
                "user_id": user_id, "session_id": session_id, "role": "assistant",
                "type": "report", "content": bubble, "is_report": True,
                "report_url": res["file_url"], "voice_status": "none",
                "created_at": datetime.now(timezone.utc),
            })
            await get_socket_manager().send_to_user(session_id, {
                "type": "memoir_ready", "content": bubble, "file_url": res["file_url"],
            })
    except Exception:
        logger.error(f"[Memoir-Collapse]: {traceback.format_exc()}")


# ══════════════════════════════════════════
# 9. 声线
# ══════════════════════════════════════════

@bp.route("/voice/library", methods=["GET"])
@require_auth
async def api_voice_library():
    """GET /api/u/voice/library — 可用声线列表。"""
    try:
        from src.services.asset_service import get_asset_service
        data = await get_asset_service().get_voice_library()
        return jsonify({"code": 200, "message": "ok", "data": data}), 200
    except Exception:
        logger.error(f"[voice_library] {traceback.format_exc()}")
        return jsonify({"code": 500, "message": "查询失败", "data": None}), 500


@bp.route("/voice/preference", methods=["POST"])
@require_auth
async def api_voice_preference():
    """POST /api/u/voice/preference — 设置声线偏好。"""
    uid  = g.user_id
    data = await request.get_json() or {}
    try:
        from src.services.asset_service import get_asset_service
        success = await get_asset_service().set_user_voice_preference(
            uid, _sanitize_input(data.get("character"))
        )
        return jsonify({"code": 200 if success else 400, "message": "ok" if success else "设置失败", "data": None}), 200 if success else 400
    except Exception:
        logger.error(f"[voice_preference] {traceback.format_exc()}")
        return jsonify({"code": 500, "message": "设置失败", "data": None}), 500


# ──────────────────────────────────────────
# 内部：WS chat 处理（pipeline + 工具转发）
# ──────────────────────────────────────────

async def _handle_chat(
    user_id: str,
    session_id: str,
    msg: str,
    image_ids: list,
    enable_search: bool,
    client_mid: str | None,
    token: str,
) -> None:
    """
    执行 process_interaction_pipeline，若返回 forward_tool 则转发到对应专用容器。
    """
    # 立即推 typing 事件，让前端知道后端已收到消息、正在处理
    await get_socket_manager().send_to_user(session_id, {"type": "typing"})

    result = await process_interaction_pipeline(
        user_id, session_id, msg,
        image_ids=image_ids,
        is_ws=True,
        enable_search=enable_search,
        client_msg_id=client_mid,
    )
    if result and result.get("forward_tool"):
        tool    = result["forward_tool"]
        payload = result.get("forward_payload", {})
        if tool in _TOOL_ROUTES:
            success = await _forward_tool(tool, payload, token)
            if not success:
                msg_id = payload.get("msg_id", "")
                await get_socket_manager().send_to_user(session_id, {
                    "type": "text", "msg_id": msg_id,
                    "content": "工具服务暂时开小差了喵，稍后再试试？",
                    "affinity": 10, "mood": "neutral", "voice_status": "none",
                })


# ──────────────────────────────────────────
# 蓝图导出
# ──────────────────────────────────────────

def init_user_api():
    return bp
