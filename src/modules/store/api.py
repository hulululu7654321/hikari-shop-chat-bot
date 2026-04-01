# -*- coding: utf-8 -*-
"""
src/modules/store/api.py — 到店场景 API
路由前缀：/api/s/
鉴权：
  屏幕端设备接口：X-Store-Key
  客户端接口：Bearer JWT (require_auth)
  screen_init：无鉴权（短码验证）
"""

import os
import re
import json
import uuid
import hashlib
import secrets
import asyncio
import traceback
import zoneinfo
from datetime import datetime, timezone, timedelta
from functools import wraps

import jwt
from quart import Blueprint, request, jsonify, websocket, g
from quart_cors import cors_exempt

from src.utils.auth import require_auth, verify_access_token
from src.utils.database import (
    get_db_manager,
    COL_USERS, COL_ORDERS, COL_PACKAGES,
)
from src.utils.socket_manager import get_socket_manager
from src.utils.logger import get_logger
from src.services.store_service import (
    load_store_shops,
    invalidate_packages_cache as _invalidate_packages_cache,
    build_packages_prompt_text as _build_packages_prompt_text,
    get_store_packages_prompt,
    push_live2d_motion,
    push_name_learned,
    is_assist_trigger,
    push_assist_request,
    push_danmaku_to_redis,
)

logger = get_logger(__name__)

bp = Blueprint("store_api", __name__, url_prefix="/api/s")

# ─────────────────────────────────────────
# 内部工具
# ─────────────────────────────────────────

async def _rate_limit(key: str, limit: int, window_seconds: int) -> bool:
    try:
        redis = await get_db_manager().get_redis()
        current = await redis.incr(key)
        if current == 1:
            await redis.expire(key, window_seconds)
        return current <= limit
    except Exception as e:
        logger.warning(f"[Store-RateLimit] Redis 异常，放行: {e}")
        return True



def _find_shop_by_key(provided_key: str) -> dict | None:
    for shop in load_store_shops():
        shop_key = shop.get("store_key", "")
        if shop_key and secrets.compare_digest(provided_key, shop_key):
            return shop
    return None


# ─────────────────────────────────────────
# 鉴权装饰器
# ─────────────────────────────────────────

def _require_store_key(f):
    """X-Store-Key 鉴权，注入 g.store_id / g.store_name"""
    @wraps(f)
    async def decorated(*args, **kwargs):
        provided = request.headers.get("X-Store-Key", "")
        if not provided:
            return jsonify({"code": 401, "message": "店铺设备凭证无效", "data": None}), 401
        shop = _find_shop_by_key(provided)
        if not shop:
            logger.warning("[Store-Auth] X-Store-Key 验证失败 | IP:%s", request.remote_addr)
            return jsonify({"code": 401, "message": "店铺设备凭证无效", "data": None}), 401
        g.store_id = shop["store_id"]
        g.store_name = shop.get("name", "")
        return await f(*args, **kwargs)
    return decorated


def _require_store_or_screen(f):
    """接受 X-Store-Key 或 Bearer screen JWT（屏幕端双模鉴权）"""
    @wraps(f)
    async def decorated(*args, **kwargs):
        provided_key = request.headers.get("X-Store-Key", "")
        if provided_key:
            shop = _find_shop_by_key(provided_key)
            if shop:
                g.store_id = shop["store_id"]
                g.store_name = shop.get("name", "")
                return await f(*args, **kwargs)
            return jsonify({"code": 401, "message": "店铺设备凭证无效", "data": None}), 401

        auth_header = request.headers.get("Authorization", "")
        if auth_header.startswith("Bearer "):
            token = auth_header[7:]
            try:
                secret = os.getenv("JWT_SECRET_KEY", "")
                payload = jwt.decode(token, secret, algorithms=["HS256"])
                if payload.get("role") != "store_screen":
                    return jsonify({"code": 401, "message": "凭证类型无效", "data": None}), 401
                g.store_id = payload.get("store_id", "")
                g.store_name = ""
                return await f(*args, **kwargs)
            except jwt.ExpiredSignatureError:
                return jsonify({"code": 401, "message": "凭证已过期", "data": None}), 401
            except Exception:
                return jsonify({"code": 401, "message": "凭证无效", "data": None}), 401

        return jsonify({"code": 401, "message": "缺少鉴权信息", "data": None}), 401
    return decorated


# ─────────────────────────────────────────
# 内部辅助
# ─────────────────────────────────────────

def _get_display_name(user_doc: dict | None) -> str:
    if not user_doc:
        return "神秘冒险者"
    name = user_doc.get("user_name", "")
    if name and "匿名" not in name:
        return name
    return "神秘冒险者"


def _build_qr_url(token: str) -> str:
    base_url = os.getenv("STORE_QR_BASE_URL", "")
    if base_url:
        return f"{base_url}?store_token={token}"
    server_url = os.getenv("SERVER_BASE_URL", "https://your-domain.com")
    return f"{server_url}/store/checkin?store_token={token}"


def _build_welcome_text(display_name: str, is_new: bool, visit_count: int) -> str:
    if is_new:
        return "欢迎新冒险者光临喵！本喵是希卡莉，请问怎么称呼你呀～"
    if visit_count == 1:
        return f"欢迎回来，{display_name}！第二次来了呢，本喵记得你喵～"
    if visit_count >= 10:
        return f"哇，{display_name}老朋友回来了！本喵等你好久了喵～"
    return f"欢迎回来，{display_name}喵！今天想聊点什么？"


# ─────────────────────────────────────────
# 屏幕端初始化（无鉴权，短码验证）
# ─────────────────────────────────────────

@bp.route("/screen-init", methods=["GET"])
async def screen_init():
    """
    屏幕端启动时调用，无需 X-Store-Key，只需短码 ?s=xxx。
    验证短码 → 签发 screen JWT → 返回店铺配置。
    IP 速率限制：同一 IP 每分钟最多 10 次。
    """
    try:
        client_ip = request.headers.get("X-Real-IP") or request.remote_addr or "unknown"
        redis = await get_db_manager().get_redis()

        rl_key = f"rl:screen_init:{client_ip}"
        count = await redis.incr(rl_key)
        if count == 1:
            await redis.expire(rl_key, 60)
        if count > 10:
            logger.warning("[Screen-Init] IP速率超限 | IP:%s", client_ip)
            return jsonify({"code": 429, "message": "请求过于频繁，请稍后再试", "data": None}), 429

        code = request.args.get("s", "").strip()
        if not code or len(code) > 32:
            return jsonify({"code": 400, "message": "缺少有效的访问码", "data": None}), 400

        redis_key = f"hikari:store:screen:{code}"
        store_id_raw = await redis.get(redis_key)
        if not store_id_raw:
            return jsonify({"code": 401, "message": "访问码无效或已过期", "data": None}), 401
        store_id = store_id_raw.decode() if isinstance(store_id_raw, bytes) else store_id_raw

        shop = next((s for s in load_store_shops() if s["store_id"] == store_id), None)
        store_name = shop.get("name", store_id) if shop else store_id
        live2d_cfg = shop.get("live2d", {}) if shop else {}

        secret = os.getenv("JWT_SECRET_KEY", "")
        if not secret:
            return jsonify({"code": 500, "message": "JWT 密钥未配置", "data": None}), 500

        expire_days = 7
        expires_at = datetime.now(timezone.utc) + timedelta(days=expire_days)
        payload = {
            "sub": f"store_screen_{store_id}",
            "iat": datetime.now(timezone.utc),
            "exp": expires_at,
            "role": "store_screen",
            "store_id": store_id,
        }
        token = jwt.encode(payload, secret, algorithm="HS256")
        ttl = expire_days * 24 * 3600 - 3600
        await redis.setex(f"hikari:store:screen_jwt:{store_id}", ttl, token)

        logger.info("[Screen-Init] 屏幕端初始化 | store=%s | IP:%s", store_id, client_ip)
        return jsonify({"code": 200, "message": "ok", "data": {
            "token": token,
            "ttl_seconds": ttl,
            "store_id": store_id,
            "store_name": store_name,
            "live2d": live2d_cfg,
        }})

    except Exception:
        logger.error("[Screen-Init-Error]: %s", traceback.format_exc())
        return jsonify({"code": 500, "message": "服务内部错误", "data": None}), 500


# ─────────────────────────────────────────
# 屏幕端设备接口（X-Store-Key 或 screen JWT）
# ─────────────────────────────────────────

@bp.route("/token", methods=["GET"])
@_require_store_or_screen
async def get_store_token():
    """获取当前有效的 store_token，屏幕端用于生成店内二维码。"""
    try:
        store_id = g.store_id
        db = get_db_manager()
        redis = await db.get_redis()

        existing = await redis.get(f"hikari:store:current_token:{store_id}")
        if existing:
            token = existing.decode() if isinstance(existing, bytes) else existing
            ttl = await redis.ttl(f"hikari:store:token:{token}")
            if ttl > 0:
                usable_ttl = max(0, ttl - 900)
                return jsonify({"code": 200, "message": "ok", "data": {
                    "token": token,
                    "expires_in": usable_ttl,
                    "qr_url": _build_qr_url(token),
                }})

        # 原子写：SET NX 避免并发重复生成
        token = secrets.token_urlsafe(32)
        expire_minutes = int(os.getenv("STORE_TOKEN_EXPIRE", 75))
        expire_seconds = expire_minutes * 60

        acquired = await redis.set(
            f"hikari:store:current_token:{store_id}", token,
            ex=65 * 60, nx=True,
        )
        if not acquired:
            # 其他并发请求已写入，直接读取它
            existing2 = await redis.get(f"hikari:store:current_token:{store_id}")
            token = (existing2.decode() if isinstance(existing2, bytes) else existing2) or token

        await redis.setex(f"hikari:store:token:{token}", expire_seconds, store_id)

        logger.info("[Store-Token] 新 token 生成 | store=%s | expires=%dmin", store_id, expire_minutes)
        return jsonify({"code": 200, "message": "ok", "data": {
            "token": token,
            "expires_in": expire_seconds,
            "qr_url": _build_qr_url(token),
        }})

    except Exception:
        logger.error("[Store-Token-Error]: %s", traceback.format_exc())
        return jsonify({"code": 500, "message": "token 生成失败", "data": None}), 500


@bp.route("/tts-config", methods=["GET"])
@_require_store_or_screen
async def get_tts_config():
    """屏幕端启动时获取 TTS 参考音频路径和文字。"""
    try:
        character = os.getenv("DEFAULT_VOICE_CHARACTER", "女性1_zh")
        from src.services.asset_service import get_asset_service
        svc = get_asset_service()
        internal_wav, ref_text, raw_win_path = svc.helper.get_reference_pair(character, "neutral")
        lang = svc._detect_lang(internal_wav or "")

        return jsonify({"code": 200, "message": "ok", "data": {
            "ref_audio_path": raw_win_path or "",
            "prompt_text": ref_text or "",
            "prompt_lang": lang or "zh",
            "text_lang": lang or "zh",
            "character": character,
        }})
    except Exception:
        logger.error("[Store-TTS-Config-Error]: %s", traceback.format_exc())
        return jsonify({"code": 200, "message": "ok", "data": {
            "ref_audio_path": "",
            "prompt_text": "",
            "prompt_lang": "zh",
            "text_lang": "zh",
            "character": os.getenv("DEFAULT_VOICE_CHARACTER", "女性1_zh"),
        }})


# ─────────────────────────────────────────
# 客户端接口（Bearer JWT）
# ─────────────────────────────────────────

@bp.route("/checkin", methods=["POST"])
@require_auth
async def store_checkin():
    """
    客户扫码签到。
    验证 store_token → 打在店标记 → 推屏幕端欢迎指令。
    速率限制：同一用户 60 秒内最多 5 次。
    """
    try:
        user_id = g.user_id
        data = await request.get_json() or {}
        store_token = data.get("store_token", "").strip()

        if not store_token:
            return jsonify({"code": 400, "message": "缺少 store_token", "data": None}), 400

        if not await _rate_limit(f"rl:store_checkin:{user_id}", 5, 60):
            logger.warning("[Store-RateLimit] checkin 超频 | UID:%s", user_id[:8])
            return jsonify({"code": 429, "message": "操作太频繁了喵，稍等一下～", "data": None}), 429

        db = get_db_manager()
        redis = await db.get_redis()

        store_id_raw = await redis.get(f"hikari:store:token:{store_token}")
        if not store_id_raw:
            logger.info("[Store-Checkin] token 无效或已过期 | UID:%s", user_id[:8])
            return jsonify({"code": 403, "message": "二维码已过期，请扫店内最新的二维码喵～", "data": None}), 403

        store_id = store_id_raw.decode() if isinstance(store_id_raw, bytes) else store_id_raw

        # token 使用次数监控（不拒绝，只告警）
        use_count = await redis.incr(f"hikari:store:token_use:{store_token}")
        if use_count == 1:
            token_ttl = await redis.ttl(f"hikari:store:token:{store_token}")
            if token_ttl > 0:
                await redis.expire(f"hikari:store:token_use:{store_token}", token_ttl)
        max_use = int(os.getenv("STORE_TOKEN_MAX_USE", 50))
        if use_count > max_use:
            logger.warning("[Store-Token-Abuse] token 使用次数异常 | count=%d | store=%s | UID:%s",
                           use_count, store_id, user_id[:8])

        instore_ttl = int(os.getenv("STORE_INSTORE_TTL", 240)) * 60
        await redis.setex(f"hikari:store:instore:{user_id}", instore_ttl, store_id)
        await redis.setex(f"hikari:store:active_user:{store_id}", instore_ttl, user_id)
        await redis.setex(f"hikari:store:visited:{user_id}", 3 * 24 * 3600, store_id)

        user_doc = await db.db[COL_USERS].find_one({"user_id": user_id})
        is_new = not user_doc or user_doc.get("total_interactions", 0) == 0
        visit_count = user_doc.get("store_visit_count", 0) if user_doc else 0
        display_name = _get_display_name(user_doc)

        await db.db[COL_USERS].update_one(
            {"user_id": user_id},
            {
                "$inc": {"store_visit_count": 1},
                "$set": {"last_store_visit": datetime.now(timezone.utc)},
            }
        )

        if display_name != "神秘冒险者":
            try:
                packages_text = await _build_packages_prompt_text(store_id)
                if packages_text:
                    await redis.setex(
                        f"hikari:store:packages_injected:{user_id}",
                        instore_ttl,
                        packages_text,
                    )
            except Exception as _e:
                logger.debug("[Store-Checkin] 套餐预写失败，跳过: %s", _e)

        welcome_text = _build_welcome_text(display_name, is_new, visit_count)

        affinity_val = 10
        try:
            u_doc = await db.db[COL_USERS].find_one({"user_id": user_id}, {"affinity": 1})
            if u_doc:
                affinity_val = int(u_doc.get("affinity", 10))
        except Exception:
            pass

        screen_session = f"store_screen_{store_id}"
        await get_socket_manager().send_to_user(screen_session, {
            "type": "store_event",
            "event": "customer_checkin",
            "user_id_masked": user_id[:8] + "****",
            "display_name": display_name,
            "affinity": affinity_val,
            "tts_text": welcome_text,
            "live2d_motion": "happy",
            "live2d_expression": "smile",
        })

        logger.info("[Store-Checkin] UID:%s | store=%s | new=%s | visit=%d",
                    user_id[:8], store_id, is_new, visit_count + 1)
        return jsonify({"code": 200, "message": "ok", "data": {
            "is_new_customer": is_new,
            "display_name": display_name,
            "welcome_text": welcome_text,
            "store_mode": True,
        }})

    except Exception:
        logger.error("[Store-Checkin-Error]: %s", traceback.format_exc())
        return jsonify({"code": 500, "message": "签到失败喵，稍后再试", "data": None}), 500


@bp.route("/checkout", methods=["POST"])
@require_auth
async def store_checkout():
    """客户主动离店，立即清除在店状态，屏幕端恢复待机。"""
    try:
        user_id = g.user_id
        db = get_db_manager()
        redis = await db.get_redis()

        instore_raw = await redis.get(f"hikari:store:instore:{user_id}")
        await redis.delete(f"hikari:store:instore:{user_id}")

        if instore_raw:
            store_id = instore_raw.decode() if isinstance(instore_raw, bytes) else instore_raw
            active_raw = await redis.get(f"hikari:store:active_user:{store_id}")
            if active_raw:
                active_uid = active_raw.decode() if isinstance(active_raw, bytes) else active_raw
                if active_uid == user_id:
                    await redis.delete(f"hikari:store:active_user:{store_id}")
            await get_socket_manager().send_to_user(f"store_screen_{store_id}", {
                "type": "store_event",
                "event": "customer_checkout",
                "user_id_masked": user_id[:8] + "****",
                "tts_text": "再见喵～欢迎下次再来！",
                "live2d_motion": "wave",
                "live2d_expression": "neutral",
            })

        logger.info("[Store-Checkout] UID:%s 已离店", user_id[:8])
        return jsonify({"code": 200, "message": "ok", "data": None})

    except Exception:
        logger.error("[Store-Checkout-Error]: %s", traceback.format_exc())
        return jsonify({"code": 500, "message": "服务内部错误", "data": None}), 500


@bp.route("/instore", methods=["GET"])
@require_auth
async def check_instore():
    """查询自己当前是否处于在店状态，小程序每次 onShow 时调用。"""
    try:
        user_id = g.user_id
        redis = await get_db_manager().get_redis()

        instore_raw = await redis.get(f"hikari:store:instore:{user_id}")
        is_instore = bool(instore_raw)
        ttl = await redis.ttl(f"hikari:store:instore:{user_id}") if is_instore else 0

        return jsonify({"code": 200, "message": "ok", "data": {
            "is_instore": is_instore,
            "remaining_seconds": max(0, ttl),
        }})

    except Exception:
        logger.error("[Store-Instore-Error]: %s", traceback.format_exc())
        return jsonify({"code": 500, "message": "服务内部错误", "data": None}), 500


# ─────────────────────────────────────────
# 店员接口（X-Store-Key）
# ─────────────────────────────────────────

@bp.route("/customers", methods=["GET"])
@_require_store_key
async def list_customers():
    """查询当前在店客户状态，屏幕端刷新后重建客户卡片。"""
    try:
        db = get_db_manager()
        redis = await db.get_redis()
        store_id = g.store_id

        instore_uids = []
        cursor = 0
        while True:
            cursor, keys = await redis.scan(cursor, match="hikari:store:instore:*", count=100)
            for key in keys:
                key_str = key.decode() if isinstance(key, bytes) else key
                uid = key_str.replace("hikari:store:instore:", "")
                raw = await redis.get(key_str)
                val = raw.decode() if isinstance(raw, bytes) else raw
                if val == store_id:
                    instore_uids.append(uid)
            if cursor == 0:
                break

        customers = []
        for uid in instore_uids:
            user_doc = await db.db[COL_USERS].find_one(
                {"user_id": uid},
                {"user_name": 1, "affinity": 1, "vip_level": 1}
            )
            display_name = _get_display_name(user_doc)
            affinity = int(user_doc.get("affinity", 10)) if user_doc else 10
            vip_level = int(user_doc.get("vip_level", 0)) if user_doc else 0

            paid_ttl = await redis.ttl(f"hikari:store:paid:{uid}")
            paid_expires_at = int(datetime.now(timezone.utc).timestamp()) + paid_ttl if paid_ttl > 0 else 0

            package_name = ""
            if paid_expires_at:
                last_order = await db.db[COL_ORDERS].find_one(
                    {"user_id": uid, "store_id": store_id},
                    sort=[("created_at", -1)]
                )
                if last_order:
                    package_name = last_order.get("package_name", "")

            customers.append({
                "user_id": uid,
                "user_id_masked": uid[:8] + "****",
                "display_name": display_name,
                "affinity": affinity,
                "vip_level": vip_level,
                "paid_expires_at": paid_expires_at,
                "package_name": package_name,
            })

        return jsonify({"code": 200, "message": "ok", "data": {
            "list": customers,
            "count": len(customers),
        }})

    except Exception:
        logger.error("[Store-Customers-Error]: %s", traceback.format_exc())
        return jsonify({"code": 500, "message": "服务内部错误", "data": None}), 500


@bp.route("/packages", methods=["GET"])
@_require_store_key
async def list_packages():
    """查询当前店铺上架套餐。"""
    try:
        store_id = g.store_id
        db = get_db_manager()
        cursor = db.db[COL_PACKAGES].find(
            {"store_id": store_id, "is_active": True},
            {"_id": 0, "package_id": 1, "name": 1, "price": 1,
             "description": 1, "sort_order": 1, "screen_minutes": 1}
        ).sort("sort_order", 1)
        packages = await cursor.to_list(length=200)
        return jsonify({"code": 200, "message": "ok", "data": packages})
    except Exception:
        logger.error("[Store-Packages-Error]: %s", traceback.format_exc())
        return jsonify({"code": 500, "message": "服务内部错误", "data": None}), 500


@bp.route("/orders", methods=["POST"])
@_require_store_key
async def confirm_order():
    """
    店员确认客户下单。
    触发：好感度 +affinity_delta，订单存档，屏幕端播报。
    每日操作次数上限（STORE_ORDER_DAILY_LIMIT）防滥用。
    """
    try:
        store_id = g.store_id
        db = get_db_manager()
        redis = await db.get_redis()
        data = await request.get_json() or {}

        user_id = data.get("user_id", "").strip()
        package_id = data.get("package_id", "").strip()
        payment_method = data.get("payment_method", "other").strip()
        if payment_method not in ("wechat", "alipay", "cash", "other"):
            payment_method = "other"
        note = data.get("note", "").strip()[:200]

        if not user_id or not package_id:
            return jsonify({"code": 400, "message": "user_id 和 package_id 为必填项", "data": None}), 400
        if not re.match(r'^[a-zA-Z0-9_\-]{1,128}$', user_id):
            return jsonify({"code": 400, "message": "user_id 格式无效", "data": None}), 400
        if not re.match(r'^pkg_[a-zA-Z0-9]{1,32}$', package_id):
            return jsonify({"code": 400, "message": "package_id 格式无效", "data": None}), 400

        daily_limit = int(os.getenv("STORE_ORDER_DAILY_LIMIT", 200))
        _now_cst = datetime.now(zoneinfo.ZoneInfo("Asia/Shanghai"))
        today = _now_cst.strftime("%Y%m%d")
        key_hash = hashlib.sha256(store_id.encode()).hexdigest()[:16]
        daily_key = f"rl:store_order:{key_hash}:{today}"
        daily_count = await redis.incr(daily_key)
        if daily_count == 1:
            _midnight_cst = (_now_cst + timedelta(days=1)).replace(
                hour=0, minute=0, second=0, microsecond=0
            )
            _ttl = int((_midnight_cst - _now_cst).total_seconds()) + 60
            await redis.expire(daily_key, _ttl)
        if daily_count > daily_limit:
            logger.warning("[Store-Order-Abuse] 今日下单操作超限 | count=%d", daily_count)
            return jsonify({"code": 429, "message": "今日操作次数已达上限", "data": None}), 429

        package = await db.db[COL_PACKAGES].find_one(
            {"package_id": package_id, "store_id": store_id, "is_active": True},
            {"_id": 0}
        )
        if not package:
            return jsonify({"code": 404, "message": "套餐不存在或已下架", "data": None}), 404

        user_doc = await db.db[COL_USERS].find_one({"user_id": user_id})
        if not user_doc:
            return jsonify({"code": 404, "message": "客户不存在", "data": None}), 404

        display_name = _get_display_name(user_doc)
        now = datetime.now(timezone.utc)
        order_id = f"ord_{uuid.uuid4().hex[:12]}"
        affinity_delta = int(package.get("affinity_delta",
                             int(os.getenv("STORE_ORDER_AFFINITY_DELTA", 30))))

        await db.db[COL_ORDERS].insert_one({
            "order_id": order_id,
            "user_id": user_id,
            "store_id": store_id,
            "package_id": package_id,
            "package_name": package["name"],
            "package_type": "store",
            "amount": package["price"],
            "payment_method": payment_method,
            "status": "confirmed",
            "note": note,
            "confirmed_by": f"store_key_{key_hash}",
            "affinity_delta": affinity_delta,
            "created_at": now,
            "confirmed_at": now,
        })

        from src.services.affinity_service import get_affinity_service
        session_id = f"sess_{user_id}"
        await get_affinity_service().update_affinity(session_id, affinity_delta, "store_order")

        screen_minutes = int(package.get("screen_minutes", 0))
        paid_expires_at = None
        if screen_minutes > 0:
            await redis.setex(f"hikari:store:paid:{user_id}", screen_minutes * 60, "1")
            paid_expires_at = int(now.timestamp()) + screen_minutes * 60
            logger.info("[Store-Paid] 屏幕服务已开启 | UID:%s | 时效=%dmin | pkg=%s",
                        user_id[:8], screen_minutes, package["name"])

        tts_text = f"哇！{display_name}下单了{package['name']}，本喵超级开心喵！谢谢支持～"
        await get_socket_manager().send_to_user(f"store_screen_{store_id}", {
            "type": "store_event",
            "event": "order_confirmed",
            "display_name": display_name,
            "user_id_masked": user_id[:8] + "****",
            "package_name": package["name"],
            "amount": package["price"],
            "affinity_delta": affinity_delta,
            "screen_minutes": screen_minutes,
            "paid_expires_at": paid_expires_at,
            "tts_text": tts_text,
            "live2d_motion": "broadcast",
            "live2d_expression": "smile",
        })

        logger.info("[Store-Order] 下单确认 | UID:%s | pkg=%s | ¥%s | affinity+%d",
                    user_id[:8], package["name"], package["price"], affinity_delta)
        return jsonify({"code": 200, "message": "ok", "data": {
            "order_id": order_id,
            "package_name": package["name"],
            "amount": package["price"],
            "affinity_delta": affinity_delta,
        }})

    except Exception:
        logger.error("[Order-Confirm-Error]: %s", traceback.format_exc())
        return jsonify({"code": 500, "message": "服务内部错误", "data": None}), 500


@bp.route("/orders", methods=["GET"])
@_require_store_key
async def list_orders():
    """查询订单列表，支持 ?user_id=xxx&date=20260320&limit=50。"""
    try:
        store_id = g.store_id
        db = get_db_manager()

        query: dict = {"store_id": store_id}
        filter_uid = request.args.get("user_id", "").strip()
        if filter_uid:
            query["user_id"] = filter_uid

        filter_date = request.args.get("date", "").strip()
        if filter_date and len(filter_date) == 8:
            try:
                _cst = zoneinfo.ZoneInfo("Asia/Shanghai")
                _day = datetime.strptime(filter_date, "%Y%m%d").replace(tzinfo=_cst)
                day_start = _day.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)
                day_end = _day.replace(hour=23, minute=59, second=59, microsecond=999999).astimezone(timezone.utc)
                query["created_at"] = {"$gte": day_start, "$lte": day_end}
            except ValueError:
                pass

        limit = min(int(request.args.get("limit", 50)), 200)
        cursor = db.db[COL_ORDERS].find(query, {"_id": 0}).sort("created_at", -1).limit(limit)
        orders = await cursor.to_list(length=limit)

        for o in orders:
            for k in ("created_at", "confirmed_at"):
                if isinstance(o.get(k), datetime):
                    o[k] = o[k].isoformat()

        return jsonify({"code": 200, "message": "ok", "data": orders})

    except Exception:
        logger.error("[Order-List-Error]: %s", traceback.format_exc())
        return jsonify({"code": 500, "message": "服务内部错误", "data": None}), 500


@bp.route("/danmaku", methods=["POST"])
@_require_store_or_screen
async def post_danmaku():
    """弹幕触发，写入 Redis 滑动窗口，检测群体感知并异步回复。"""
    try:
        data = await request.get_json() or {}
        user_name = data.get("user_name", "观众").strip()[:20]
        text = data.get("text", "").strip()[:100]
        if not text:
            return jsonify({"code": 400, "message": "弹幕内容不能为空", "data": None}), 400

        store_id = g.store_id
        await push_danmaku_to_redis(store_id, user_name, text)
        return jsonify({"code": 200, "message": "ok", "data": None})

    except Exception:
        logger.error("[Danmaku-Error]: %s", traceback.format_exc())
        return jsonify({"code": 500, "message": "服务内部错误", "data": None}), 500



# ─────────────────────────────────────────
# 屏幕端 WebSocket（Bearer screen JWT）
# ─────────────────────────────────────────

@bp.websocket("/ws/<session_id>")
@cors_exempt
async def store_screen_ws(session_id):
    """
    WS /api/s/ws/<session_id>
    屏幕端 WebSocket 连接，Token 通过 query param 传入：?token=<screen_jwt>
    session_id 格式：store_screen_{store_id}
    """
    await websocket.accept()

    token = websocket.args.get("token", "")
    payload = verify_access_token(token)
    if not payload or payload.get("role") != "store_screen":
        await websocket.send(json.dumps({"type": "error", "content": "凭证无效或已过期"}))
        await websocket.close(1008)
        return

    ws_user_id = payload["user_id"]
    # store_id 从 sub 字段解析（sub = "store_screen_{store_id}"）
    store_id_from_token = ws_user_id.removeprefix("store_screen_")
    expected_sid = f"store_screen_{store_id_from_token}"
    if session_id != expected_sid:
        logger.warning("[WS-Store] session_id 不匹配 | session=%s | expected=%s", session_id, expected_sid)
        await websocket.send(json.dumps({"type": "error", "content": "屏幕端凭证与连接目标不匹配"}))
        await websocket.close(1008)
        return

    sm = get_socket_manager()
    current_ws = websocket._get_current_object()
    queue = await sm.connect(session_id, current_ws, user_id=ws_user_id)
    logger.info("[WS-Store-Open] store_id:%s | SID:%s", store_id_from_token, session_id)

    async def recv_loop():
        while True:
            raw = await websocket.receive()
            if not raw:
                return
            try:
                msg = json.loads(raw)
                if msg.get("action") == "ping":
                    await queue.put({"action": "pong"})
            except Exception:
                pass

    async def send_loop():
        while True:
            msg = await queue.get()
            if msg is None:
                return
            await websocket.send(json.dumps(msg))

    try:
        recv_task = asyncio.ensure_future(recv_loop())
        send_task = asyncio.ensure_future(send_loop())
        _, pending = await asyncio.wait(
            [recv_task, send_task],
            return_when=asyncio.FIRST_COMPLETED,
        )
        for t in pending:
            t.cancel()
    except Exception:
        logger.warning("[WS-Store-Warning] 屏幕端链路波动")
    finally:
        await queue.put(None)
        await sm.disconnect(session_id, current_ws)
        logger.info("[WS-Store-Close] SID:%s", session_id)


# ─────────────────────────────────────────
# 启动初始化
# ─────────────────────────────────────────

async def _auto_init_screen_codes():
    """服务启动时自动为所有店铺生成短码（若 Redis 中不存在）。"""
    try:
        shops = load_store_shops()
        if not shops:
            return

        redis = await get_db_manager().get_redis()
        server_url = os.getenv("SERVER_BASE_URL", "http://localhost").rstrip("/")
        now_str = datetime.now(zoneinfo.ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d %H:%M:%S")

        lines = ["=" * 50, "  希卡莉店铺屏幕端访问链接", f"  生成时间：{now_str}", "=" * 50, ""]

        for shop in shops:
            store_id = shop.get("store_id", "")
            store_name = shop.get("name", store_id)
            if not store_id:
                continue

            all_matches = []
            cur = 0
            while True:
                cur, keys = await redis.scan(cur, match="hikari:store:screen:*", count=100)
                for key in keys:
                    val_raw = await redis.get(key)
                    val = val_raw.decode() if isinstance(val_raw, bytes) else val_raw
                    if val == store_id:
                        key_str = key.decode() if isinstance(key, bytes) else key
                        all_matches.append(key_str)
                if cur == 0:
                    break

            if len(all_matches) > 1:
                logger.warning("[Screen-Code-Auto] 发现重复短码 %d 条，清理中 | store=%s",
                               len(all_matches), store_id)
                for key_str in all_matches[:-1]:
                    await redis.delete(key_str)
                all_matches = all_matches[-1:]

            if all_matches:
                redis_key = all_matches[0]
                existing_code = redis_key.replace("hikari:store:screen:", "")
                await redis.expire(redis_key, 365 * 24 * 3600)
            else:
                existing_code = secrets.token_urlsafe(12)
                redis_key = f"hikari:store:screen:{existing_code}"
                await redis.setex(redis_key, 365 * 24 * 3600, store_id)
                logger.info("[Screen-Code-Auto] 自动生成短码 | store=%s | code=%s...",
                            store_id, existing_code[:6])

            url = f"{server_url}/store_screen.html?s={existing_code}"
            lines += [f"【{store_name}】", f"  访问链接：{url}", f"  短    码：{existing_code}", ""]

        lines += ["=" * 50, "  通过 GET /api/m/screen-codes 接口查询完整链接", "=" * 50]
        logger.info("[Screen-Code-Auto] 所有店铺访问链接已就绪：\n%s", "\n".join(lines))

    except Exception:
        logger.error("[Screen-Code-Auto-Error]: %s", traceback.format_exc())


@bp.before_app_serving
async def _on_startup():
    if os.getenv("STORE_ENABLED", "false").lower() == "true":
        await _auto_init_screen_codes()


def init_store_api():
    return bp