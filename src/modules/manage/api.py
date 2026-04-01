# -*- coding: utf-8 -*-
"""
src/modules/manage/api.py — 管理后台 API
路由前缀：/api/m/
鉴权：Bearer JWT + role（admin / store_clerk）
"""

import os
import re
import uuid
import hashlib
import asyncio
import secrets
import traceback
from datetime import datetime, timezone, timedelta

from quart import Blueprint, request, jsonify, g, send_file

from src.utils.auth import require_auth, require_role, verify_access_token
from src.utils.database import (
    get_db_manager, _sanitize_input,
    COL_USERS, COL_ORDERS, COL_PACKAGES, COL_INVITE_CODES, COL_ALERTS, COL_CONFIG, COL_FACTS,
)
from src.utils.logger import get_logger
from src.services.store_service import load_store_shops, invalidate_packages_cache

logger = get_logger(__name__)

bp = Blueprint("manage_api", __name__, url_prefix="/api/m")


# ─────────────────────────────────────────
# 内部工具
# ─────────────────────────────────────────

def _load_store_shops() -> list:
    return load_store_shops()


async def _get_manage_store_id(user_id: str) -> str | None:
    """查询 store_clerk 绑定的店铺 ID。"""
    try:
        db = get_db_manager().db
        user = await db[COL_USERS].find_one({"user_id": user_id}, {"manage_store_id": 1})
        if user:
            return user.get("manage_store_id")
    except Exception as e:
        logger.warning("[Manage] _get_manage_store_id 查询失败: %s", e)
    return None


# ─────────────────────────────────────────
# 身份查询
# ─────────────────────────────────────────

@bp.route("/whoami", methods=["GET"])
@require_auth
async def whoami():
    """查询当前登录用户的管理角色，前端登录后调一次决定显示哪个管理入口。"""
    uid = g.user_id
    try:
        db = get_db_manager().db
        user = await db[COL_USERS].find_one(
            {"user_id": uid},
            {"manage_role": 1, "manage_store_id": 1}
        )
        manage_role = user.get("manage_role") if user else None
        manage_store_id = user.get("manage_store_id") if user else None

        store_name = ""
        if manage_store_id:
            shops = _load_store_shops()
            for shop in shops:
                if shop.get("store_id") == manage_store_id:
                    store_name = shop.get("name", manage_store_id)
                    break

        return jsonify({"code": 200, "message": "ok", "data": {
            "manage_role": manage_role,
            "manage_store_id": manage_store_id,
            "store_name": store_name,
        }})

    except Exception:
        logger.error("[Manage] whoami 异常: %s", traceback.format_exc())
        return jsonify({"code": 500, "message": "服务内部错误", "data": None}), 500


# ─────────────────────────────────────────
# 用户管理（admin）
# ─────────────────────────────────────────

@bp.route("/users", methods=["GET"])
@require_auth
@require_role("admin")
async def search_users():
    """
    按昵称模糊搜索用户。
    Query: q=关键词（空返回最新注册），limit=50，has_access=true|false
    """
    q = _sanitize_input(request.args.get("q", "")).strip()
    limit = min(int(request.args.get("limit", 50)), 50)
    has_access_raw = request.args.get("has_access", "").strip().lower()

    mongo_filter: dict = {}
    if has_access_raw == "true":
        mongo_filter["has_access"] = True
    elif has_access_raw == "false":
        mongo_filter["has_access"] = False

    try:
        db = get_db_manager().db
        now = datetime.now(timezone.utc)
        fields = {
            "user_id": 1, "user_name": 1, "affinity": 1,
            "vip_level": 1, "vip_expires_at": 1,
            "has_access": 1, "store_visit_count": 1, "_id": 0,
        }

        if not q:
            cursor = db[COL_USERS].find(mongo_filter, fields).sort("created_at", -1).limit(limit)
            users = await cursor.to_list(length=limit)
        else:
            name_filter = {"user_name": {"$regex": re.escape(q), "$options": "i"}}
            if mongo_filter:
                name_filter = {"$and": [{**mongo_filter}, name_filter]}
            cursor = db[COL_USERS].find(name_filter, fields).limit(limit)
            users = await cursor.to_list(length=limit)

        for u in users:
            expires = u.get("vip_expires_at")
            if expires:
                expires_aware = expires.replace(tzinfo=timezone.utc) if expires.tzinfo is None else expires
                if expires_aware < now:
                    u["vip_level"] = 0
                u["vip_expires_at"] = expires_aware.isoformat()

        return jsonify({"code": 200, "message": "ok", "data": {"list": users, "count": len(users)}})

    except Exception:
        logger.error("[Manage] search_users 异常: %s", traceback.format_exc())
        return jsonify({"code": 500, "message": "服务内部错误", "data": None}), 500


@bp.route("/users/<user_id>", methods=["GET"])
@require_auth
@require_role("admin")
async def get_user_info(user_id: str):
    """查询单个用户的完整管理信息。"""
    user_id = _sanitize_input(user_id)
    if not user_id:
        return jsonify({"code": 400, "message": "缺少 user_id", "data": None}), 400

    try:
        db = get_db_manager().db
        redis = await get_db_manager().get_redis()

        user = await db[COL_USERS].find_one(
            {"user_id": user_id},
            {
                "user_id": 1, "user_name": 1, "affinity": 1,
                "vip_level": 1, "vip_expires_at": 1, "vip_granted_at": 1,
                "has_access": 1, "invite_code": 1, "access_source": 1,
                "access_granted_at": 1, "store_visit_count": 1,
                "manage_role": 1, "manage_store_id": 1,
                "created_at": 1, "_id": 0,
            }
        )
        if not user:
            return jsonify({"code": 404, "message": "用户不存在", "data": None}), 404

        from src.utils.invite import get_vip_info
        vip_info = await get_vip_info(user_id, db)

        now_bj = datetime.now(timezone.utc) + timedelta(hours=8)
        today = now_bj.strftime("%Y%m%d")
        msg_count_raw = await redis.get(f"hikari:daily_msg:{user_id}:{today}")
        msg_count = int(msg_count_raw) if msg_count_raw else 0

        orders_cursor = db[COL_ORDERS].find(
            {"user_id": user_id},
            {"order_id": 1, "package_name": 1, "amount": 1, "created_at": 1, "_id": 0}
        ).sort("created_at", -1).limit(3)
        recent_orders = await orders_cursor.to_list(length=3)
        for o in recent_orders:
            if o.get("created_at"):
                o["created_at"] = o["created_at"].isoformat()

        for field in ("vip_expires_at", "vip_granted_at", "access_granted_at", "created_at"):
            if user.get(field):
                user[field] = user[field].isoformat()

        return jsonify({"code": 200, "message": "ok", "data": {
            "user": user,
            "vip": vip_info,
            "today_msg_count": msg_count,
            "recent_orders": recent_orders,
        }})

    except Exception:
        logger.error("[Manage] get_user_info 异常: %s", traceback.format_exc())
        return jsonify({"code": 500, "message": "服务内部错误", "data": None}), 500


@bp.route("/users/<user_id>/vip", methods=["POST"])
@require_auth
@require_role("admin")
async def grant_vip(user_id: str):
    """
    授予 VIP（覆盖模式）。
    Body: {"vip_level": 1, "days": 30, "image_gen_quota": 10}
    """
    user_id = _sanitize_input(user_id)
    data = await request.get_json() or {}
    vip_level = int(data.get("vip_level", 1))
    days = int(data.get("days", 0))

    if not user_id:
        return jsonify({"code": 400, "message": "缺少 user_id", "data": None}), 400
    if vip_level not in (0, 1, 2):
        return jsonify({"code": 400, "message": "vip_level 只能是 0（撤销）、1 或 2", "data": None}), 400

    # vip_level=0 表示撤销 VIP
    if vip_level == 0:
        try:
            db = get_db_manager().db
            user = await db[COL_USERS].find_one({"user_id": user_id}, {"user_id": 1})
            if not user:
                return jsonify({"code": 404, "message": "用户不存在", "data": None}), 404
            await db[COL_USERS].update_one(
                {"user_id": user_id},
                {"$set": {"vip_level": 0, "vip_expires_at": None}}
            )
            logger.info("[Manage] 撤销VIP | uid=%s | by=%s", user_id[:8], g.user_id[:8])
            return jsonify({"code": 200, "message": "VIP 已撤销", "data": None})
        except Exception:
            logger.error("[Manage] revoke_vip 异常: %s", traceback.format_exc())
            return jsonify({"code": 500, "message": "服务内部错误", "data": None}), 500

    try:
        db = get_db_manager().db
        user = await db[COL_USERS].find_one({"user_id": user_id}, {"user_id": 1})
        if not user:
            return jsonify({"code": 404, "message": "用户不存在", "data": None}), 404

        now = datetime.now(timezone.utc)
        expires_at = now + timedelta(days=days) if days > 0 else None

        await db[COL_USERS].update_one(
            {"user_id": user_id},
            {"$set": {
                "vip_level": vip_level,
                "vip_expires_at": expires_at,
                "vip_granted_at": now,
                "vip_granted_by": g.user_id,
            }}
        )

        from src.utils.invite import add_image_gen_quota, IMAGE_GEN_QUOTA_VIP, IMAGE_GEN_QUOTA_SVIP
        default_quota = IMAGE_GEN_QUOTA_SVIP if vip_level >= 2 else IMAGE_GEN_QUOTA_VIP
        image_gen_quota = int(data.get("image_gen_quota", default_quota))
        if image_gen_quota > 0:
            await add_image_gen_quota(user_id, image_gen_quota)

        level_label = "SVIP" if vip_level == 2 else "VIP"
        expire_label = f"{days}天" if days > 0 else "永久"
        logger.info("[Manage] 授予VIP | uid=%s | level=%s | expire=%s | quota=+%d | by=%s",
                    user_id[:8], level_label, expire_label, image_gen_quota, g.user_id[:8])
        return jsonify({"code": 200, "message": f"已授予 {level_label}（{expire_label}），图生图配额 +{image_gen_quota}", "data": None})

    except Exception:
        logger.error("[Manage] grant_vip 异常: %s", traceback.format_exc())
        return jsonify({"code": 500, "message": "服务内部错误", "data": None}), 500


@bp.route("/users/<user_id>/kick", methods=["POST"])
@require_auth
@require_role("admin")
async def kick_user(user_id: str):
    """踢出用户：撤销访问权限，邀请码永久作废。Body: {"reason": "违规"}"""
    user_id = _sanitize_input(user_id)
    data = await request.get_json() or {}
    reason = _sanitize_input(data.get("reason", "admin_kick"))[:100]

    if not user_id:
        return jsonify({"code": 400, "message": "缺少 user_id", "data": None}), 400

    try:
        from src.utils.invite import admin_kick
        db = get_db_manager().db
        success = await admin_kick(user_id, db, reason=reason)
        if success:
            logger.info("[Manage] 踢出用户 | uid=%s | by=%s | reason=%s",
                        user_id[:8], g.user_id[:8], reason)
            return jsonify({"code": 200, "message": "用户已踢出", "data": None})
        return jsonify({"code": 500, "message": "踢出失败", "data": None}), 500

    except Exception:
        logger.error("[Manage] kick_user 异常: %s", traceback.format_exc())
        return jsonify({"code": 500, "message": "服务内部错误", "data": None}), 500


# ─────────────────���───────────────────────
# 邀请码管理（admin）
# ─────────────────────────────────────────

@bp.route("/invites", methods=["GET"])
@require_auth
@require_role("admin")
async def list_invites():
    """查询邀请码列表。Query: status=unused|used|revoked|all，limit=50"""
    status_filter = request.args.get("status", "all")
    limit = min(int(request.args.get("limit", 50)), 200)

    try:
        db = get_db_manager().db
        query = {} if status_filter == "all" else {"status": status_filter}
        cursor = db[COL_INVITE_CODES].find(query, {"_id": 0}).sort("created_at", -1).limit(limit)
        docs = await cursor.to_list(length=limit)

        for d in docs:
            for field in ("created_at", "expires_at", "used_at", "revoked_at"):
                if d.get(field):
                    d[field] = d[field].isoformat()

        return jsonify({"code": 200, "message": "ok", "data": {"list": docs, "count": len(docs)}})

    except Exception:
        logger.error("[Manage] list_invites 异常: %s", traceback.format_exc())
        return jsonify({"code": 500, "message": "服务内部错误", "data": None}), 500


@bp.route("/invites", methods=["POST"])
@require_auth
@require_role("admin")
async def create_invites():
    """生成邀请码。Body: {"count": 1, "expire_days": 7, "note": "备注"}"""
    data = await request.get_json() or {}
    count = min(int(data.get("count", 1)), 50)
    expire_days = int(data.get("expire_days", 0)) or None
    note = _sanitize_input(data.get("note", ""))[:50]

    try:
        from src.utils.invite import create_invite_code
        db = get_db_manager().db
        codes = []
        for _ in range(count):
            doc = await create_invite_code(db, created_by=g.user_id, expire_days=expire_days, note=note)
            codes.append({
                "code": doc["code"],
                "expires_at": doc["expires_at"].isoformat() if doc.get("expires_at") else None,
                "note": doc.get("note", ""),
            })

        logger.info("[Manage] 生成邀请码 | count=%d | by=%s | note=%r", count, g.user_id[:8], note)
        return jsonify({"code": 200, "message": "ok", "data": {"codes": codes, "count": len(codes)}})

    except Exception:
        logger.error("[Manage] create_invites 异常: %s", traceback.format_exc())
        return jsonify({"code": 500, "message": "服务内部错误", "data": None}), 500


@bp.route("/invites/<code>/revoke", methods=["POST"])
@require_auth
@require_role("admin")
async def revoke_invite(code: str):
    """作废一个未使用的邀请码。"""
    code = _sanitize_input(code)
    if not code:
        return jsonify({"code": 400, "message": "缺少 code", "data": None}), 400

    try:
        db = get_db_manager().db
        invite = await db[COL_INVITE_CODES].find_one({"code": code})
        if not invite:
            return jsonify({"code": 404, "message": "邀请码不存在", "data": None}), 404
        if invite["status"] != "unused":
            return jsonify({"code": 400, "message": f"邀请码状态为 {invite['status']}，无法作废", "data": None}), 400

        await db[COL_INVITE_CODES].update_one(
            {"code": code},
            {"$set": {
                "status": "revoked",
                "revoked_at": datetime.now(timezone.utc),
                "revoke_reason": f"manage_admin_{g.user_id[:8]}",
            }}
        )
        logger.info("[Manage] 作废邀请码 | code=%s | by=%s", code, g.user_id[:8])
        return jsonify({"code": 200, "message": "邀请码已作废", "data": None})

    except Exception:
        logger.error("[Manage] revoke_invite 异常: %s", traceback.format_exc())
        return jsonify({"code": 500, "message": "服务内部错误", "data": None}), 500


# ─────────────────────────────────────────
# 角色管理（admin）
# ─────────────────────────────────────────

@bp.route("/roles", methods=["GET"])
@require_auth
@require_role("admin")
async def list_roles():
    """查询所有有管理角色的用户列表。"""
    try:
        db = get_db_manager().db
        cursor = db[COL_USERS].find(
            {"manage_role": {"$ne": None}},
            {
                "user_id": 1, "user_name": 1,
                "manage_role": 1, "manage_store_id": 1,
                "manage_granted_at": 1, "_id": 0,
            }
        ).sort("manage_granted_at", -1)
        users = await cursor.to_list(length=200)

        store_name_map = {}
        for shop in _load_store_shops():
            store_name_map[shop.get("store_id", "")] = shop.get("name", "")

        for u in users:
            sid = u.get("manage_store_id")
            u["store_name"] = store_name_map.get(sid, sid or "")
            if u.get("manage_granted_at"):
                u["manage_granted_at"] = u["manage_granted_at"].isoformat()

        return jsonify({"code": 200, "message": "ok", "data": {"list": users, "count": len(users)}})

    except Exception:
        logger.error("[Manage] list_roles 异常: %s", traceback.format_exc())
        return jsonify({"code": 500, "message": "服务内部错误", "data": None}), 500


@bp.route("/roles/grant", methods=["POST"])
@require_auth
@require_role("admin")
async def grant_role():
    """
    授予管理角色。
    Body: {"user_id": "wx_xxx", "role": "store_clerk"|"admin", "store_id": "..."}
    """
    data = await request.get_json() or {}
    target_uid = _sanitize_input(data.get("user_id", ""))
    role = _sanitize_input(data.get("role", ""))
    store_id = _sanitize_input(data.get("store_id", ""))

    if not target_uid:
        return jsonify({"code": 400, "message": "缺少 user_id", "data": None}), 400
    if role not in ("store_clerk", "admin"):
        return jsonify({"code": 400, "message": "role 只能是 store_clerk 或 admin", "data": None}), 400
    if role == "store_clerk" and not store_id:
        return jsonify({"code": 400, "message": "store_clerk 必须指定 store_id", "data": None}), 400

    try:
        db = get_db_manager().db
        user = await db[COL_USERS].find_one({"user_id": target_uid}, {"user_id": 1, "user_name": 1})
        if not user:
            return jsonify({"code": 404, "message": "用户不存在，请先让对方登录一次小程序", "data": None}), 404

        if role == "store_clerk":
            valid_ids = [s.get("store_id") for s in _load_store_shops()]
            if store_id not in valid_ids:
                return jsonify({"code": 400, "message": f"store_id 无效，有效店铺：{valid_ids}", "data": None}), 400

        now = datetime.now(timezone.utc)
        await db[COL_USERS].update_one(
            {"user_id": target_uid},
            {"$set": {
                "manage_role": role,
                "manage_store_id": store_id if role == "store_clerk" else None,
                "manage_granted_by": g.user_id,
                "manage_granted_at": now,
            }}
        )

        logger.info("[Manage] 角色授权 | target=%s | role=%s | store=%s | by=%s",
                    target_uid[:8], role, store_id or "N/A", g.user_id[:8])
        return jsonify({"code": 200, "message": f"已授权 {user.get('user_name', target_uid[:8])} 为 {role}"
                        + (f"（{store_id}）" if role == "store_clerk" else ""), "data": None})

    except Exception:
        logger.error("[Manage] grant_role 异常: %s", traceback.format_exc())
        return jsonify({"code": 500, "message": "服务内部错误", "data": None}), 500


@bp.route("/roles/revoke", methods=["POST"])
@require_auth
@require_role("admin")
async def revoke_role():
    """撤销用户管理角色。Body: {"user_id": "wx_xxx"}"""
    data = await request.get_json() or {}
    target_uid = _sanitize_input(data.get("user_id", ""))
    if not target_uid:
        return jsonify({"code": 400, "message": "缺少 user_id", "data": None}), 400
    if target_uid == g.user_id:
        return jsonify({"code": 400, "message": "不能撤销自己的管理权限", "data": None}), 400

    try:
        db = get_db_manager().db
        user = await db[COL_USERS].find_one({"user_id": target_uid}, {"user_id": 1, "user_name": 1, "manage_role": 1})
        if not user:
            return jsonify({"code": 404, "message": "用户不存在", "data": None}), 404
        if not user.get("manage_role"):
            return jsonify({"code": 400, "message": "该用户本来就没有管理权限", "data": None}), 400

        await db[COL_USERS].update_one(
            {"user_id": target_uid},
            {"$set": {"manage_role": None, "manage_store_id": None,
                      "manage_granted_by": None, "manage_granted_at": None}}
        )
        logger.info("[Manage] 撤销角色 | target=%s | by=%s", target_uid[:8], g.user_id[:8])
        return jsonify({"code": 200, "message": f"已撤销 {user.get('user_name', target_uid[:8])} 的管理权限", "data": None})

    except Exception:
        logger.error("[Manage] revoke_role 异常: %s", traceback.format_exc())
        return jsonify({"code": 500, "message": "服务内部错误", "data": None}), 500


# ─────────────────────────────────────────
# 套餐管理（admin）
# ─────────────────────────────────────────

@bp.route("/packages", methods=["GET"])
@require_auth
async def list_packages():
    """
    查询套餐列表。
    store_clerk：只返回自己店的 type=store 套餐。
    admin：可按 store_id / type 筛选，分组返回。
    Query: store_id=xxx, type=online|store|all, active_only=1
    """
    if g.role not in ("admin", "store_clerk"):
        return jsonify({"code": 403, "message": "权限不足", "data": None}), 403
    active_only = request.args.get("active_only", "0") == "1"

    try:
        db = get_db_manager().db

        if g.role == "store_clerk":
            manage_store_id = await _get_manage_store_id(g.user_id)
            query = {"store_id": manage_store_id, "type": "store"}
            if active_only:
                query["is_active"] = True
            cursor = db[COL_PACKAGES].find(query, {"_id": 0}).sort("sort_order", 1)
            packages = await cursor.to_list(length=200)
            return jsonify({"code": 200, "message": "ok", "data": {"list": packages}})

        # admin 视图
        filter_store = _sanitize_input(request.args.get("store_id", ""))
        filter_type = _sanitize_input(request.args.get("type", "all"))

        query = {}
        if filter_store:
            query["store_id"] = filter_store
        if filter_type != "all":
            query["type"] = filter_type
        if active_only:
            query["is_active"] = True

        cursor = db[COL_PACKAGES].find(query, {"_id": 0}).sort(
            [("store_id", 1), ("sort_order", 1)]
        )
        packages = await cursor.to_list(length=500)

        store_name_map = {"hikari_global": "线上VIP套餐（全局）"}
        for shop in _load_store_shops():
            store_name_map[shop["store_id"]] = shop.get("name", shop["store_id"])

        grouped: dict = {}
        for pkg in packages:
            sid = pkg.get("store_id", "unknown")
            if sid not in grouped:
                grouped[sid] = {"store_id": sid, "store_name": store_name_map.get(sid, sid), "packages": []}
            for f in ("created_at", "updated_at"):
                if isinstance(pkg.get(f), datetime):
                    pkg[f] = pkg[f].isoformat()
            grouped[sid]["packages"].append(pkg)

        return jsonify({"code": 200, "message": "ok", "data": {
            "groups": list(grouped.values()),
            "total": len(packages),
        }})

    except Exception:
        logger.error("[Manage] list_packages 异常: %s", traceback.format_exc())
        return jsonify({"code": 500, "message": "服务内部错误", "data": None}), 500


@bp.route("/packages", methods=["POST"])
@require_auth
@require_role("admin")
async def create_package():
    """
    新增套餐（admin）。
    Body: type / store_id / name / price / description / sort_order
          到店专用：screen_minutes / affinity_delta
          线上专用：vip_level / vip_days / image_gen_quota
    """
    data = await request.get_json() or {}

    pkg_type = _sanitize_input(data.get("type", ""))
    if pkg_type not in ("online", "store"):
        return jsonify({"code": 400, "message": "type 必须是 online 或 store", "data": None}), 400

    name = _sanitize_input(data.get("name", "")).strip()[:50]
    if not name:
        return jsonify({"code": 400, "message": "name 必填", "data": None}), 400

    try:
        price = float(data.get("price"))
        if price < 0:
            raise ValueError
    except (TypeError, ValueError):
        return jsonify({"code": 400, "message": "price 必须为非负数字", "data": None}), 400

    description = _sanitize_input(data.get("description", "")).strip()[:200]
    sort_order = int(data.get("sort_order", 99))
    affinity_delta = int(data.get("affinity_delta", int(os.getenv("STORE_ORDER_AFFINITY_DELTA", 30))))

    category = _sanitize_input(data.get("category", "other")).strip()[:32]
    valid_categories = {"photography", "costume", "makeup", "studio", "activity", "online", "other"}
    if category not in valid_categories:
        category = "other"

    if pkg_type == "store":
        store_id = _sanitize_input(data.get("store_id", ""))
        if not store_id:
            return jsonify({"code": 400, "message": "到店套餐必须指定 store_id", "data": None}), 400
        valid_ids = [s["store_id"] for s in _load_store_shops()]
        if store_id not in valid_ids:
            return jsonify({"code": 400, "message": f"store_id 无效，有效店铺：{valid_ids}", "data": None}), 400
        screen_minutes = max(0, int(data.get("screen_minutes", 0)))
        extra = {"screen_minutes": screen_minutes}
    else:
        store_id = "hikari_global"
        vip_level = int(data.get("vip_level", 1))
        vip_days = int(data.get("vip_days", 0))
        image_gen_quota = int(data.get("image_gen_quota", 0))
        if vip_level not in (0, 1, 2):
            return jsonify({"code": 400, "message": "vip_level 只能是 0、1 或 2", "data": None}), 400
        if vip_level == 0 and vip_days > 0:
            return jsonify({"code": 400, "message": "纯配额包 vip_days 必须为 0", "data": None}), 400
        if vip_level > 0 and vip_days == 0:
            return jsonify({"code": 400, "message": "有 vip_level 时 vip_days 必须 > 0", "data": None}), 400
        if image_gen_quota < 0:
            return jsonify({"code": 400, "message": "image_gen_quota 不能为负数", "data": None}), 400
        extra = {"vip_level": vip_level, "vip_days": vip_days, "image_gen_quota": image_gen_quota}

    try:
        db = get_db_manager().db
        package_id = f"pkg_{uuid.uuid4().hex[:12]}"
        now = datetime.now(timezone.utc)

        doc = {
            "package_id": package_id,
            "store_id": store_id,
            "type": pkg_type,
            "name": name,
            "price": price,
            "description": description,
            "category": category,
            "images": [],
            "is_active": True,
            "sort_order": sort_order,
            "affinity_delta": affinity_delta,
            "created_by": g.user_id,
            "created_at": now,
            "updated_at": now,
            **extra,
        }
        await db[COL_PACKAGES].insert_one(doc)

        await invalidate_packages_cache()

        logger.info("[Manage] 新增套餐 | id=%s | type=%s | name=%s | by=%s",
                    package_id, pkg_type, name, g.user_id[:8])
        return jsonify({"code": 200, "message": "ok", "data": {"package_id": package_id}})

    except Exception:
        logger.error("[Manage] create_package 异常: %s", traceback.format_exc())
        return jsonify({"code": 500, "message": "服务内部错误", "data": None}), 500


@bp.route("/packages/<package_id>", methods=["PATCH"])
@require_auth
@require_role("admin")
async def update_package(package_id: str):
    """修改套餐（admin），只传需要改的字段。"""
    if not re.match(r"^pkg_[a-zA-Z0-9]{1,32}$", package_id):
        return jsonify({"code": 400, "message": "package_id 格式无效", "data": None}), 400

    data = await request.get_json() or {}
    allowed_fields = {
        "name", "price", "description", "is_active",
        "sort_order", "screen_minutes", "affinity_delta",
        "vip_level", "vip_days", "image_gen_quota", "category",
    }
    updates = {}
    for field in allowed_fields:
        if field not in data:
            continue
        val = data[field]
        if field in ("name", "description", "category"):
            val = _sanitize_input(val).strip()[:{"name": 50, "description": 200, "category": 32}[field]]
        elif field == "price":
            val = float(val)
        elif field in ("sort_order", "screen_minutes", "affinity_delta", "vip_days", "image_gen_quota", "vip_level"):
            val = int(val)
            if field == "image_gen_quota" and val < 0:
                return jsonify({"code": 400, "message": "image_gen_quota 不能为负数", "data": None}), 400
        updates[field] = val

    if not updates:
        return jsonify({"code": 400, "message": "没有提供需要修改的字段", "data": None}), 400

    try:
        db = get_db_manager().db
        updates["updated_at"] = datetime.now(timezone.utc)
        result = await db[COL_PACKAGES].update_one({"package_id": package_id}, {"$set": updates})
        if result.matched_count == 0:
            return jsonify({"code": 404, "message": "套餐不存在", "data": None}), 404

        await invalidate_packages_cache()

        logger.info("[Manage] 修改套餐 | id=%s | fields=%s | by=%s",
                    package_id, list(updates.keys()), g.user_id[:8])
        return jsonify({"code": 200, "message": "套餐已更新", "data": None})

    except Exception:
        logger.error("[Manage] update_package 异常: %s", traceback.format_exc())
        return jsonify({"code": 500, "message": "服务内部错误", "data": None}), 500


@bp.route("/packages/<package_id>/images", methods=["POST"])
@require_auth
@require_role("admin")
async def upload_package_image(package_id: str):
    """上传套餐图片（admin），multipart/form-data，字段名 image，最多 9 张。"""
    if not re.match(r"^pkg_[a-zA-Z0-9]{1,32}$", package_id):
        return jsonify({"code": 400, "message": "package_id 格式无效", "data": None}), 400

    try:
        db = get_db_manager().db
        pkg = await db[COL_PACKAGES].find_one({"package_id": package_id}, {"images": 1})
        if not pkg:
            return jsonify({"code": 404, "message": "套餐不存在", "data": None}), 404
        if len(pkg.get("images", [])) >= 9:
            return jsonify({"code": 400, "message": "最多上传 9 张图片", "data": None}), 400

        files = await request.files
        file = files.get("image")
        if not file:
            return jsonify({"code": 400, "message": "请上传 image 文件", "data": None}), 400

        allowed_mime = {"image/jpeg", "image/png", "image/webp", "image/gif"}
        ext_map = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp", "image/gif": "gif"}
        ext_fallback = {".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                        ".png": "image/png", ".webp": "image/webp", ".gif": "image/gif"}

        mime = (file.content_type or "").split(";")[0].strip().lower()
        if mime not in allowed_mime:
            import posixpath
            ext = posixpath.splitext((file.filename or "").lower())[1]
            mime = ext_fallback.get(ext, "")
        if mime not in allowed_mime:
            return jsonify({"code": 400, "message": "仅支持 jpg/png/webp/gif 格式", "data": None}), 400

        pkg_dir = os.path.join(os.getenv("ASSETS_ROOT", "/app/assets"), "packages", package_id)
        os.makedirs(pkg_dir, exist_ok=True)
        img_name = f"{uuid.uuid4().hex[:12]}.{ext_map[mime]}"
        img_path = os.path.join(pkg_dir, img_name)

        file_data = file.read()
        if len(file_data) == 0:
            return jsonify({"code": 400, "message": "文件数据为空，请重新上传", "data": None}), 400
        if len(file_data) > 10 * 1024 * 1024:
            return jsonify({"code": 400, "message": "图片不能超过 10MB", "data": None}), 400

        await asyncio.to_thread(lambda: open(img_path, "wb").write(file_data))

        rel_path = f"{package_id}/{img_name}"
        await db[COL_PACKAGES].update_one(
            {"package_id": package_id},
            {"$push": {"images": rel_path}, "$set": {"updated_at": datetime.now(timezone.utc)}}
        )

        logger.info("[Manage] 套餐图片上传 | pkg=%s | img=%s | by=%s", package_id, img_name, g.user_id[:8])
        return jsonify({"code": 200, "message": "ok", "data": {
            "path": rel_path,
            "url": f"/api/p/images/{rel_path}",
        }})

    except Exception:
        logger.error("[Manage] upload_package_image 异常: %s", traceback.format_exc())
        return jsonify({"code": 500, "message": "服务内部错误", "data": None}), 500


@bp.route("/packages/<package_id>/images", methods=["DELETE"])
@require_auth
@require_role("admin")
async def delete_package_image(package_id: str):
    """删除套餐图片（admin），body: { "path": "pkg_xxx/abc.jpg" }"""
    if not re.match(r"^pkg_[a-zA-Z0-9]{1,32}$", package_id):
        return jsonify({"code": 400, "message": "package_id 格式无效", "data": None}), 400

    try:
        data = await request.get_json() or {}
        img_path = (data.get("path") or "").strip()
        if not img_path:
            return jsonify({"code": 400, "message": "path 不能为空", "data": None}), 400

        # 安全校验：path 必须以当前 package_id/ 开头，防止越权删除
        expected_prefix = f"{package_id}/"
        if not img_path.startswith(expected_prefix):
            return jsonify({"code": 403, "message": "无权删除其他套餐的图片", "data": None}), 403

        db = get_db_manager().db
        result = await db[COL_PACKAGES].update_one(
            {"package_id": package_id},
            {"$pull": {"images": img_path}, "$set": {"updated_at": datetime.now(timezone.utc)}}
        )
        if result.matched_count == 0:
            return jsonify({"code": 404, "message": "套餐不存在", "data": None}), 404

        # 删除磁盘文件（不存在时静默忽略）
        disk_path = os.path.join(os.getenv("ASSETS_ROOT", "/app/assets"), "packages", img_path)
        try:
            os.remove(disk_path)
        except FileNotFoundError:
            pass

        logger.info("[Manage] 套餐图片删除 | pkg=%s | img=%s | by=%s", package_id, img_path, g.user_id[:8])
        return jsonify({"code": 200, "message": "ok", "data": None})

    except Exception:
        logger.error("[Manage] delete_package_image 异常: %s", traceback.format_exc())
        return jsonify({"code": 500, "message": "服务内部错误", "data": None}), 500


# ─────────────────────────────────────────
# 订单与在店客户（store_clerk + admin）
# ─────────────────────────────────────────

@bp.route("/store/customers", methods=["GET"])
@require_auth
async def list_customers():
    """
    查询当前在店客户。
    store_clerk 只看自己店，admin 可传 store_id 筛选或查全部。
    """
    if g.role not in ("admin", "store_clerk"):
        return jsonify({"code": 403, "message": "权限不足", "data": None}), 403
    try:
        db = get_db_manager().db
        redis = await get_db_manager().get_redis()

        if g.role == "store_clerk":
            manage_store_id = await _get_manage_store_id(g.user_id)
            target_store_ids = [manage_store_id] if manage_store_id else []
        else:
            filter_store = _sanitize_input(request.args.get("store_id", ""))
            if filter_store:
                target_store_ids = [filter_store]
            else:
                target_store_ids = [s["store_id"] for s in _load_store_shops()]

        customers = []
        for store_id in target_store_ids:
            cur = 0
            while True:
                cur, keys = await redis.scan(cur, match="hikari:store:instore:*", count=100)
                for key in keys:
                    key_str = key.decode() if isinstance(key, bytes) else key
                    uid = key_str.replace("hikari:store:instore:", "")
                    val_raw = await redis.get(key_str)
                    val = val_raw.decode() if isinstance(val_raw, bytes) else val_raw
                    if val != store_id:
                        continue

                    user_doc = await db[COL_USERS].find_one(
                        {"user_id": uid},
                        {"user_name": 1, "affinity": 1, "vip_level": 1, "_id": 0}
                    )
                    name = (user_doc or {}).get("user_name", "")
                    display_name = name if name and "匿名" not in name else "神秘冒险者"
                    ttl = await redis.ttl(key_str)
                    customers.append({
                        "user_id": uid,
                        "display_name": display_name,
                        "affinity": int((user_doc or {}).get("affinity", 10)),
                        "vip_level": int((user_doc or {}).get("vip_level", 0)),
                        "store_id": store_id,
                        "remaining_seconds": max(0, ttl),
                    })
                if cur == 0:
                    break

        return jsonify({"code": 200, "message": "ok", "data": {"list": customers, "count": len(customers)}})

    except Exception:
        logger.error("[Manage] list_customers 异常: %s", traceback.format_exc())
        return jsonify({"code": 500, "message": "服务内部错误", "data": None}), 500


@bp.route("/orders", methods=["POST"])
@require_auth
async def create_order():
    """
    确认下单（store_clerk + admin）。
    到店套餐：强制验证客户在店；线上套餐：仅 admin。
    Body: user_id / package_id / payment_method / note
    """
    if g.role not in ("admin", "store_clerk"):
        return jsonify({"code": 403, "message": "权限不足", "data": None}), 403
    import zoneinfo as _zi

    data = await request.get_json() or {}
    user_id = _sanitize_input(data.get("user_id", ""))
    package_id = _sanitize_input(data.get("package_id", ""))
    payment_method = _sanitize_input(data.get("payment_method", "other"))
    note = _sanitize_input(data.get("note", ""))[:200]

    if not user_id or not package_id:
        return jsonify({"code": 400, "message": "user_id 和 package_id 必填", "data": None}), 400
    if not re.match(r"^[a-zA-Z0-9_\-]{1,128}$", user_id):
        return jsonify({"code": 400, "message": "user_id 格式无效", "data": None}), 400
    if not re.match(r"^pkg_[a-zA-Z0-9]{1,32}$", package_id):
        return jsonify({"code": 400, "message": "package_id 格式无效", "data": None}), 400
    if payment_method not in ("wechat", "alipay", "cash", "other"):
        payment_method = "other"

    try:
        db = get_db_manager().db
        redis = await get_db_manager().get_redis()

        package = await db[COL_PACKAGES].find_one({"package_id": package_id, "is_active": True}, {"_id": 0})
        if not package:
            return jsonify({"code": 404, "message": "套餐不存在或已下架", "data": None}), 404

        pkg_type = package.get("type", "store")
        pkg_store_id = package.get("store_id", "")

        if pkg_type == "online" and g.role != "admin":
            return jsonify({"code": 403, "message": "线上套餐只有管理员可以下单", "data": None}), 403

        if g.role == "store_clerk":
            manage_store_id = await _get_manage_store_id(g.user_id)
            if manage_store_id != pkg_store_id:
                return jsonify({"code": 403, "message": "无权操作其他店铺的套餐", "data": None}), 403

        if pkg_type == "store":
            instore_raw = await redis.get(f"hikari:store:instore:{user_id}")
            if not instore_raw:
                return jsonify({"code": 400, "message": "客户当前不在店，无法下到店套餐", "data": None}), 400
            instore_store_id = instore_raw.decode() if isinstance(instore_raw, bytes) else instore_raw
            if instore_store_id != pkg_store_id:
                return jsonify({"code": 400, "message": "客户所在店铺与套餐不符", "data": None}), 400

        user_doc = await db[COL_USERS].find_one({"user_id": user_id})
        if not user_doc:
            return jsonify({"code": 404, "message": "客户不存在", "data": None}), 404

        daily_limit = int(os.getenv("STORE_ORDER_DAILY_LIMIT", 200))
        _now_cst = datetime.now(_zi.ZoneInfo("Asia/Shanghai"))
        today = _now_cst.strftime("%Y%m%d")
        op_hash = hashlib.sha256(g.user_id.encode()).hexdigest()[:16]
        daily_key = f"rl:manage_order:{op_hash}:{today}"
        daily_count = await redis.incr(daily_key)
        if daily_count == 1:
            _midnight = (_now_cst + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
            _ttl = int((_midnight - _now_cst).total_seconds()) + 60
            await redis.expire(daily_key, _ttl)
        if daily_count > daily_limit:
            return jsonify({"code": 429, "message": "今日下单操作次数已达上限", "data": None}), 429

        name = user_doc.get("user_name", "")
        display_name = name if name and "匿名" not in name else "神秘冒险者"
        now = datetime.now(timezone.utc)
        order_id = f"ord_{uuid.uuid4().hex[:12]}"
        affinity_delta = int(package.get("affinity_delta", int(os.getenv("STORE_ORDER_AFFINITY_DELTA", 30))))

        await db[COL_ORDERS].insert_one({
            "order_id": order_id,
            "user_id": user_id,
            "store_id": pkg_store_id,
            "package_id": package_id,
            "package_name": package["name"],
            "package_type": pkg_type,
            "amount": package["price"],
            "payment_method": payment_method,
            "status": "confirmed",
            "note": note,
            "confirmed_by": g.user_id,
            "affinity_delta": affinity_delta,
            "created_at": now,
            "confirmed_at": now,
        })

        from src.services.affinity_service import get_affinity_service
        await get_affinity_service().update_affinity(f"sess_{user_id}", affinity_delta, "store_order")

        screen_minutes = int(package.get("screen_minutes", 0))
        paid_expires_at = None
        if pkg_type == "store":
            if screen_minutes > 0:
                await redis.setex(f"hikari:store:paid:{user_id}", screen_minutes * 60, "1")
                paid_expires_at = int(now.timestamp()) + screen_minutes * 60

            from src.utils.socket_manager import get_socket_manager
            await get_socket_manager().send_to_user(f"store_screen_{pkg_store_id}", {
                "type": "store_event",
                "event": "order_confirmed",
                "display_name": display_name,
                "user_id_masked": user_id[:8] + "****",
                "package_name": package["name"],
                "amount": package["price"],
                "affinity_delta": affinity_delta,
                "screen_minutes": screen_minutes,
                "paid_expires_at": paid_expires_at,
                "tts_text": f"哇！{display_name}下单了{package['name']}，本喵超级开心喵！谢谢支持～",
                "live2d_motion": "broadcast",
                "live2d_expression": "smile",
            })

        if pkg_type == "online":
            from src.utils.invite import grant_vip_by_package, add_image_gen_quota
            _pkg_vip_level = int(package.get("vip_level", 0))
            _pkg_vip_days = int(package.get("vip_days", 0))
            _pkg_quota = int(package.get("image_gen_quota", 0))

            if _pkg_vip_level > 0 and _pkg_vip_days > 0:
                await grant_vip_by_package(
                    user_id=user_id, vip_level=_pkg_vip_level,
                    vip_days=_pkg_vip_days, db=db, granted_by=g.user_id
                )
                logger.info("[Manage] VIP叠加 | uid=%s | level=%d | days=%d",
                            user_id[:8], _pkg_vip_level, _pkg_vip_days)
            if _pkg_quota > 0:
                await add_image_gen_quota(user_id, _pkg_quota)

        logger.info("[Manage] 下单确认 | uid=%s | pkg=%s | type=%s | ¥%s | by=%s",
                    user_id[:8], package["name"], pkg_type, package["price"], g.user_id[:8])
        return jsonify({"code": 200, "message": "ok", "data": {
            "order_id": order_id,
            "package_name": package["name"],
            "amount": package["price"],
            "affinity_delta": affinity_delta,
        }})

    except Exception:
        logger.error("[Manage] create_order 异常: %s", traceback.format_exc())
        return jsonify({"code": 500, "message": "服务内部错误", "data": None}), 500


@bp.route("/orders", methods=["GET"])
@require_auth
async def list_orders():
    """查询订单列表。store_clerk 只能查自己店，admin 可全查。Query: store_id, date, limit"""
    if g.role not in ("admin", "store_clerk"):
        return jsonify({"code": 403, "message": "权限不足", "data": None}), 403
    try:
        import zoneinfo as _zi
        db = get_db_manager().db
        query: dict = {}

        if g.role == "store_clerk":
            manage_store_id = await _get_manage_store_id(g.user_id)
            query["store_id"] = manage_store_id
        else:
            filter_store = _sanitize_input(request.args.get("store_id", ""))
            if filter_store:
                query["store_id"] = filter_store

        filter_date = request.args.get("date", "").strip()
        if filter_date and len(filter_date) == 8:
            try:
                _cst = _zi.ZoneInfo("Asia/Shanghai")
                _day = datetime.strptime(filter_date, "%Y%m%d").replace(tzinfo=_cst)
                day_start = _day.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)
                day_end = _day.replace(hour=23, minute=59, second=59, microsecond=999999).astimezone(timezone.utc)
                query["created_at"] = {"$gte": day_start, "$lte": day_end}
            except ValueError:
                pass

        limit = min(int(request.args.get("limit", 50)), 200)
        cursor = db[COL_ORDERS].find(query, {"_id": 0}).sort("created_at", -1).limit(limit)
        orders = await cursor.to_list(length=limit)

        user_ids = list({o["user_id"] for o in orders if o.get("user_id")})
        user_name_map = {}
        if user_ids:
            u_cursor = db[COL_USERS].find(
                {"user_id": {"$in": user_ids}},
                {"_id": 0, "user_id": 1, "user_name": 1}
            )
            for u in await u_cursor.to_list(length=len(user_ids)):
                user_name_map[u["user_id"]] = u.get("user_name") or u["user_id"][:8]

        for o in orders:
            for k in ("created_at", "confirmed_at"):
                if isinstance(o.get(k), datetime):
                    o[k] = o[k].isoformat()
            o["user_name"] = user_name_map.get(o.get("user_id", ""), o.get("user_id", "")[:8])

        return jsonify({"code": 200, "message": "ok", "data": {"list": orders, "count": len(orders)}})

    except Exception:
        logger.error("[Manage] list_orders 异常: %s", traceback.format_exc())
        return jsonify({"code": 500, "message": "服务内部错误", "data": None}), 500


# ─────────────────────────��───────────────
# 屏幕端链接管理（admin）
# ─────────────────────────────────────────

@bp.route("/screen-codes", methods=["GET"])
@require_auth
@require_role("admin")
async def get_screen_codes():
    """查询所有店铺屏幕端访问链接。"""
    try:
        redis = await get_db_manager().get_redis()
        shops = _load_store_shops()
        server = os.getenv("SERVER_BASE_URL", "http://localhost").rstrip("/")

        result = []
        for shop in shops:
            store_id = shop.get("store_id", "")
            store_name = shop.get("name", store_id)
            if not store_id:
                continue

            matches = []
            cur = 0
            while True:
                cur, keys = await redis.scan(cur, match="hikari:store:screen:*", count=100)
                for key in keys:
                    val_raw = await redis.get(key)
                    val = val_raw.decode() if isinstance(val_raw, bytes) else val_raw
                    if val == store_id:
                        key_str = key.decode() if isinstance(key, bytes) else key
                        matches.append(key_str)
                if cur == 0:
                    break

            if len(matches) > 1:
                for k in matches[:-1]:
                    await redis.delete(k)
                matches = matches[-1:]

            code = matches[0].replace("hikari:store:screen:", "") if matches else None
            ttl = await redis.ttl(f"hikari:store:screen:{code}") if code else 0

            result.append({
                "store_id": store_id,
                "store_name": store_name,
                "screen_code": code or "",
                "url": f"{server}/store_screen.html?s={code}" if code else "",
                "ttl_days": round(ttl / 86400) if ttl > 0 else 0,
            })

        return jsonify({"code": 200, "message": "ok", "data": result})

    except Exception:
        logger.error("[Manage] get_screen_codes 异常: %s", traceback.format_exc())
        return jsonify({"code": 500, "message": "服务内部错误", "data": None}), 500


@bp.route("/screen-codes/reset", methods=["POST"])
@require_auth
@require_role("admin")
async def reset_screen_code():
    """重置指定店铺的屏幕短码。Body: {"store_id": "hikari_store_001"}"""
    data = await request.get_json() or {}
    store_id = _sanitize_input(data.get("store_id", ""))
    if not store_id:
        return jsonify({"code": 400, "message": "缺少 store_id", "data": None}), 400

    try:
        valid_ids = [s["store_id"] for s in _load_store_shops()]
        if store_id not in valid_ids:
            return jsonify({"code": 400, "message": f"store_id 无效，有效店铺：{valid_ids}", "data": None}), 400

        redis = await get_db_manager().get_redis()

        cur = 0
        while True:
            cur, keys = await redis.scan(cur, match="hikari:store:screen:*", count=100)
            for key in keys:
                val_raw = await redis.get(key)
                val = val_raw.decode() if isinstance(val_raw, bytes) else val_raw
                if val == store_id:
                    await redis.delete(key)
            if cur == 0:
                break

        await redis.delete(f"hikari:store:screen_jwt:{store_id}")

        new_code = secrets.token_urlsafe(12)
        await redis.setex(f"hikari:store:screen:{new_code}", 365 * 24 * 3600, store_id)

        from src.utils.socket_manager import get_socket_manager
        await get_socket_manager().send_to_user(
            f"store_screen_{store_id}",
            {"type": "force_reload", "msg": "短码已重置，页面自动刷新"}
        )

        server = os.getenv("SERVER_BASE_URL", "http://localhost").rstrip("/")
        new_url = f"{server}/store_screen.html?s={new_code}"

        logger.info("[Manage] 重置短码 | store=%s | by=%s", store_id, g.user_id[:8])
        return jsonify({"code": 200, "message": "ok", "data": {
            "store_id": store_id,
            "screen_code": new_code,
            "url": new_url,
        }})

    except Exception:
        logger.error("[Manage] reset_screen_code 异常: %s", traceback.format_exc())
        return jsonify({"code": 500, "message": "服务内部错误", "data": None}), 500


# ─────────────────────────────────────────
# SOUL 热配置（admin）
# ─────────────────────────────────────────

@bp.route("/config/soul", methods=["GET"])
@require_auth
@require_role("admin")
async def get_soul_config():
    """读取当前 SOUL 配置。"""
    try:
        from src.core.soul_config import get_soul_config as _get_soul
        config = await _get_soul()
        return jsonify({"code": 200, "message": "ok", "data": config})
    except Exception:
        logger.error("[Manage] get_soul_config 异常: %s", traceback.format_exc())
        return jsonify({"code": 500, "message": "服务内部错误", "data": None}), 500


@bp.route("/config/soul", methods=["PUT"])
@require_auth
@require_role("admin")
async def update_soul_config():
    """更新 SOUL 配置，立即生效。Body: {persona_template, affinity_levels, ...}"""
    try:
        data = await request.get_json() or {}
        if not data:
            return jsonify({"code": 400, "message": "请求体为空", "data": None}), 400

        from src.core.soul_config import update_soul_config as _update_soul
        success = await _update_soul(data, updated_by=g.user_id)
        if success:
            logger.info("[Manage] SOUL 配置已更新 | by=%s", g.user_id[:8])
            return jsonify({"code": 200, "message": "配置已更新并立即生效", "data": None})
        return jsonify({"code": 500, "message": "配置更新失败", "data": None}), 500

    except Exception:
        logger.error("[Manage] update_soul_config 异常: %s", traceback.format_exc())
        return jsonify({"code": 500, "message": "服务内部错误", "data": None}), 500


# ─────────────────────────────────────────
# 系统健康与统计（admin）
# ─────────────────────────────────────────

@bp.route("/system/alerts", methods=["GET"])
@require_auth
@require_role("admin")
async def get_system_alerts():
    """查询系统健康告警（由 health_workflow 写入）。"""
    try:
        db = get_db_manager().db
        limit = min(int(request.args.get("limit", 20)), 100)
        cursor = db[COL_ALERTS].find({}, {"_id": 0}).sort("timestamp", -1).limit(limit)
        alerts = await cursor.to_list(length=limit)
        for a in alerts:
            if isinstance(a.get("timestamp"), datetime):
                a["timestamp"] = a["timestamp"].isoformat()
        return jsonify({"code": 200, "message": "ok", "data": alerts})
    except Exception:
        logger.error("[Manage] get_system_alerts 异常: %s", traceback.format_exc())
        return jsonify({"code": 500, "message": "服务内部错误", "data": None}), 500


@bp.route("/system/stats", methods=["GET"])
@require_auth
@require_role("admin")
async def get_stats():
    """统计概览：用户数、VIP、邀请码、今日订单。"""
    try:
        db = get_db_manager().db
        total_users = await db[COL_USERS].count_documents({})
        access_users = await db[COL_USERS].count_documents({"has_access": True})
        vip_users = await db[COL_USERS].count_documents({"vip_level": {"$gt": 0}})
        invite_unused = await db[COL_INVITE_CODES].count_documents({"status": "unused"})
        invite_used = await db[COL_INVITE_CODES].count_documents({"status": "used"})
        invite_revoked = await db[COL_INVITE_CODES].count_documents({"status": "revoked"})

        now_bj = datetime.now(timezone.utc) + timedelta(hours=8)
        today_start_utc = now_bj.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(hours=8)
        today_orders = await db[COL_ORDERS].count_documents({"created_at": {"$gte": today_start_utc}})

        return jsonify({"code": 200, "message": "ok", "data": {
            "users": {"total": total_users, "with_access": access_users, "vip": vip_users},
            "invite_codes": {"unused": invite_unused, "used": invite_used, "revoked": invite_revoked},
            "orders": {"today": today_orders},
        }})

    except Exception:
        logger.error("[Manage] get_stats 异常: %s", traceback.format_exc())
        return jsonify({"code": 500, "message": "服务内部错误", "data": None}), 500


# ─────────────────────────────────────────
# 买家画像（admin）
# ─────────────────────────────────────────

@bp.route("/users/by-package", methods=["GET"])
@require_auth
@require_role("admin")
async def get_buyers_by_package():
    """查询购买指定套餐的买家列表（分页）。Query: package_id, page, limit"""
    package_id = _sanitize_input(request.args.get("package_id", ""))
    if not package_id:
        return jsonify({"code": 400, "message": "缺少 package_id", "data": None}), 400

    try:
        page = max(1, int(request.args.get("page", 1)))
        limit = min(max(1, int(request.args.get("limit", 50))), 100)
    except ValueError:
        return jsonify({"code": 400, "message": "page / limit 参数格式错误", "data": None}), 400

    try:
        db = get_db_manager().db
        now = datetime.now(timezone.utc)

        package = await db[COL_PACKAGES].find_one(
            {"package_id": package_id},
            {"_id": 0, "package_id": 1, "name": 1, "type": 1,
             "price": 1, "store_id": 1, "is_active": 1, "category": 1}
        )
        if not package:
            return jsonify({"code": 404, "message": "套餐不存在", "data": None}), 404

        pipeline = [
            {"$match": {"package_id": package_id}},
            {"$group": {
                "_id": "$user_id",
                "order_count": {"$sum": 1},
                "total_spent": {"$sum": "$amount"},
                "last_order_at": {"$max": "$created_at"},
                "first_order_at": {"$min": "$created_at"},
            }},
            {"$sort": {"last_order_at": -1}},
        ]
        all_buyers_raw = await db[COL_ORDERS].aggregate(pipeline).to_list(length=10000)
        total = len(all_buyers_raw)
        skip = (page - 1) * limit
        page_buyers_raw = all_buyers_raw[skip: skip + limit]

        if not page_buyers_raw:
            return jsonify({"code": 200, "message": "ok", "data": {
                "package": package, "list": [], "total": total,
                "page": page, "limit": limit, "has_more": False,
            }})

        buyer_ids = [b["_id"] for b in page_buyers_raw]
        users_cursor = db[COL_USERS].find(
            {"user_id": {"$in": buyer_ids}},
            {"_id": 0, "user_id": 1, "user_name": 1, "vip_level": 1, "vip_expires_at": 1,
             "has_access": 1, "access_source": 1, "total_interactions": 1,
             "store_visit_count": 1, "last_active_at": 1, "created_at": 1,
             "image_gen_quota": 1, "voice_character": 1}
        )
        users_map = {u["user_id"]: u for u in await users_cursor.to_list(length=len(buyer_ids))}

        result_list = []
        for b in page_buyers_raw:
            uid = b["_id"]
            user = users_map.get(uid, {})
            vip_level = user.get("vip_level", 0)
            vip_expires = user.get("vip_expires_at")
            if vip_expires:
                vip_expires_aware = vip_expires.replace(tzinfo=timezone.utc) if vip_expires.tzinfo is None else vip_expires
                if vip_expires_aware < now:
                    vip_level = 0
            result_list.append({
                "user_id": uid,
                "user_name": user.get("user_name", uid[:8]),
                "vip_level": vip_level,
                "vip_expires_at": vip_expires.isoformat() if vip_expires else None,
                "has_access": user.get("has_access", False),
                "access_source": user.get("access_source", ""),
                "total_interactions": user.get("total_interactions", 0),
                "store_visit_count": user.get("store_visit_count", 0),
                "image_gen_quota": user.get("image_gen_quota", 0),
                "voice_character": user.get("voice_character", ""),
                "last_active_at": user.get("last_active_at").isoformat() if user.get("last_active_at") else None,
                "created_at": user.get("created_at").isoformat() if user.get("created_at") else None,
                "order_count": b["order_count"],
                "total_spent": round(b["total_spent"], 2),
                "last_order_at": b["last_order_at"].isoformat() if b.get("last_order_at") else None,
                "first_order_at": b["first_order_at"].isoformat() if b.get("first_order_at") else None,
            })

        return jsonify({"code": 200, "message": "ok", "data": {
            "package": package, "list": result_list, "total": total,
            "page": page, "limit": limit, "has_more": (skip + limit) < total,
        }})

    except Exception:
        logger.error("[Manage] get_buyers_by_package 异常: %s", traceback.format_exc())
        return jsonify({"code": 500, "message": "服务内部错误", "data": None}), 500


@bp.route("/users/by-package/export", methods=["POST"])
@require_auth
@require_role("admin")
async def export_buyers_portrait():
    """生成指定套餐所有买家的完整画像 MD 文件。Body: {"package_id": "pkg_xxx"}"""
    data = await request.get_json() or {}
    package_id = _sanitize_input(data.get("package_id", ""))
    if not package_id:
        return jsonify({"code": 400, "message": "缺少 package_id", "data": None}), 400

    redis = None
    lock_key = f"hikari:lock:buyer_export:{package_id}"
    try:
        db = get_db_manager().db
        redis = await get_db_manager().get_redis()
        now = datetime.now(timezone.utc)

        acquired = await redis.set(lock_key, g.user_id, nx=True, ex=60)
        if not acquired:
            return jsonify({"code": 429, "message": "该套餐正在导出中，请稍后再试", "data": None}), 429

        package = await db[COL_PACKAGES].find_one(
            {"package_id": package_id},
            {"_id": 0, "package_id": 1, "name": 1, "type": 1, "price": 1,
             "description": 1, "store_id": 1, "vip_level": 1, "vip_days": 1,
             "image_gen_quota": 1, "category": 1, "is_active": 1}
        )
        if not package:
            return jsonify({"code": 404, "message": "套餐不存在", "data": None}), 404

        pipeline = [
            {"$match": {"package_id": package_id}},
            {"$group": {
                "_id": "$user_id",
                "order_count": {"$sum": 1},
                "total_spent": {"$sum": "$amount"},
                "last_order_at": {"$max": "$created_at"},
            }},
            {"$sort": {"last_order_at": -1}},
        ]
        all_buyers_agg = await db[COL_ORDERS].aggregate(pipeline).to_list(length=10000)
        if not all_buyers_agg:
            return jsonify({"code": 404, "message": "该套餐暂无购买记录", "data": None}), 404

        buyer_ids = [b["_id"] for b in all_buyers_agg]
        buyer_spent_map = {b["_id"]: b for b in all_buyers_agg}

        users_list = await db[COL_USERS].find(
            {"user_id": {"$in": buyer_ids}},
            {"_id": 0, "user_id": 1, "user_name": 1, "vip_level": 1, "vip_expires_at": 1,
             "has_access": 1, "access_source": 1, "total_interactions": 1,
             "store_visit_count": 1, "last_active_at": 1, "created_at": 1,
             "image_gen_quota": 1, "voice_character": 1}
        ).to_list(length=len(buyer_ids))
        users_map = {u["user_id"]: u for u in users_list}

        all_facts = await db[COL_FACTS].find(
            {"user_id": {"$in": buyer_ids}},
            {"_id": 0, "user_id": 1, "category": 1, "topic": 1,
             "content": 1, "confidence": 1, "mention_count": 1, "created_at": 1}
        ).sort([("user_id", 1), ("category", 1), ("confidence", -1)]).to_list(length=100000)
        facts_map: dict = {}
        for f in all_facts:
            facts_map.setdefault(f["user_id"], []).append(f)

        all_orders = await db[COL_ORDERS].find(
            {"package_id": package_id, "user_id": {"$in": buyer_ids}},
            {"_id": 0, "user_id": 1, "package_name": 1, "package_type": 1,
             "amount": 1, "payment_method": 1, "status": 1, "created_at": 1, "note": 1}
        ).sort("created_at", -1).to_list(length=100000)
        orders_map: dict = {}
        for o in all_orders:
            orders_map.setdefault(o["user_id"], []).append(o)

        md = _render_buyer_portrait_md(package, buyer_ids, users_map, facts_map, orders_map, buyer_spent_map, now)

        export_dir = os.getenv("MEMOIR_EXPORT_DIR", "/app/exports")
        portrait_dir = os.path.realpath(os.path.join(export_dir, "buyer_portraits"))
        os.makedirs(portrait_dir, exist_ok=True)

        safe_pkg_name = re.sub(r'[^\w\u4e00-\u9fff]', '_', package.get("name", package_id))
        timestamp = now.strftime("%Y%m%d_%H%M%S")
        filename = f"buyers_{safe_pkg_name}_{timestamp}.md"
        filepath = os.path.realpath(os.path.join(portrait_dir, filename))

        if not filepath.startswith(portrait_dir + os.sep):
            return jsonify({"code": 403, "message": "非法路径", "data": None}), 403

        with open(filepath, "w", encoding="utf-8") as fh:
            fh.write(md)
            fh.flush()
            os.fsync(fh.fileno())

        logger.info("[Manage] 导出买家画像 | pkg=%s | buyers=%d | file=%s | by=%s",
                    package["name"], len(buyer_ids), filename, g.user_id[:8])
        return jsonify({"code": 200, "message": "ok", "data": {
            "filename": filename,
            "rel_path": f"buyer_portraits/{filename}",
            "buyer_count": len(buyer_ids),
            "generated_at": now.isoformat(),
        }})

    except Exception:
        logger.error("[Manage] export_buyers_portrait 异常: %s", traceback.format_exc())
        return jsonify({"code": 500, "message": "服务内部错误", "data": None}), 500
    finally:
        if redis is not None:
            try:
                await redis.delete(lock_key)
            except Exception:
                pass


@bp.route("/files/buyer-portraits/<filename>", methods=["GET"])
async def download_buyer_portrait(filename: str):
    """
    下载买家画像 MD 文件（admin）。
    鉴权：Bearer header 或 ?token= query string（浏览器直接下载场景）。
    """
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        token = auth_header[len("Bearer "):]
    else:
        token = request.args.get("token", "")
    if not token:
        return jsonify({"code": 401, "message": "未提供认证 Token", "data": None}), 401

    payload = verify_access_token(token)
    if not payload:
        return jsonify({"code": 401, "message": "Token 无效或已过期", "data": None}), 401
    if payload.get("role") != "admin":
        return jsonify({"code": 403, "message": "无管理员权限", "data": None}), 403

    if not re.match(r'^buyers_[\w\u4e00-\u9fff]+_\d{8}_\d{6}\.md$', filename):
        return jsonify({"code": 400, "message": "非法文件名", "data": None}), 400

    export_dir = os.getenv("MEMOIR_EXPORT_DIR", "/app/exports")
    portrait_dir = os.path.realpath(os.path.join(export_dir, "buyer_portraits"))
    target = os.path.realpath(os.path.join(portrait_dir, filename))

    if not target.startswith(portrait_dir + os.sep) and target != portrait_dir:
        return jsonify({"code": 403, "message": "非法路径", "data": None}), 403
    if not os.path.isfile(target):
        return jsonify({"code": 404, "message": "文件不存在", "data": None}), 404

    logger.info("[Manage] 下载买家画像 | file=%s | by=%s", filename, payload["user_id"][:8])
    return await send_file(target, mimetype="text/markdown; charset=utf-8",
                           as_attachment=True, attachment_filename=filename)


# ─────────────────────────────────────────
# MD 渲染工具（纯数据，不调 LLM）
# ─────────────────────────────────────────

_CAT_LABELS = {
    "bio": "📋 身份信息", "pref": "❤️ 喜好偏好", "exp": "📅 经历事件",
    "rel": "👥 关系信息", "trait": "🧠 性格特征", "plan": "🎯 计划打算", "vision": "🖼️ 图片知识",
}
_VIP_LABELS = {0: "普通用户", 1: "VIP", 2: "SVIP"}
_PKG_TYPE_LABELS = {"online": "线上VIP套餐", "store": "到店套餐"}
_ACCESS_LABELS = {"invite_code": "邀请码", "admin_direct": "管理员直授"}


def _fmt_dt(dt_val) -> str:
    if not dt_val:
        return "N/A"
    if isinstance(dt_val, str):
        try:
            dt_val = datetime.fromisoformat(dt_val.replace("Z", "+00:00"))
        except Exception:
            return dt_val
    try:
        bj = dt_val.astimezone(timezone(timedelta(hours=8)))
        return bj.strftime("%Y-%m-%d %H:%M")
    except Exception:
        return str(dt_val)


def _render_buyer_portrait_md(package, buyer_ids, users_map, facts_map, orders_map, buyer_spent_map, now) -> str:
    lines = []
    pkg_name = package.get("name", "未知套餐")
    pkg_type = _PKG_TYPE_LABELS.get(package.get("type", ""), package.get("type", ""))
    bj_now = now.astimezone(timezone(timedelta(hours=8)))

    lines += [
        f"# 套餐买家画像报告 — {pkg_name}", "",
        f"- **生成时间**：{bj_now.strftime('%Y-%m-%d %H:%M')} (北京时间)",
        f"- **买家总数**：{len(buyer_ids)} 人",
        f"- **套餐类型**：{pkg_type}",
        f"- **套餐价格**：¥{package.get('price', 'N/A')}",
    ]
    if package.get("description"):
        lines.append(f"- **套餐描述**：{package['description']}")
    lines += ["", "---", "", "## 买家总览", "",
              "| # | 用户名 | VIP | 总对话数 | 到店次数 | 本套餐购买次数 | 累计消费(¥) | 最后活跃 |",
              "|---|--------|-----|---------|---------|--------------|------------|---------|"]

    for idx, uid in enumerate(buyer_ids, 1):
        user = users_map.get(uid, {})
        spent = buyer_spent_map.get(uid, {})
        vip_lv = user.get("vip_level", 0)
        vip_exp = user.get("vip_expires_at")
        if vip_exp:
            vip_exp_aware = vip_exp.replace(tzinfo=timezone.utc) if vip_exp.tzinfo is None else vip_exp
            if vip_exp_aware < now:
                vip_lv = 0
        lines.append(
            f"| {idx} | {user.get('user_name') or uid[:8]} | {_VIP_LABELS.get(vip_lv, str(vip_lv))} "
            f"| {user.get('total_interactions', 0)} | {user.get('store_visit_count', 0)} "
            f"| {spent.get('order_count', 0)} | {round(spent.get('total_spent', 0), 2)} "
            f"| {_fmt_dt(user.get('last_active_at'))} |"
        )

    lines += ["", "---", "", "## 买家详细画像", ""]

    for idx, uid in enumerate(buyer_ids, 1):
        user = users_map.get(uid, {})
        uname = user.get("user_name") or uid[:8]
        lines += [f"### {idx}. {uname}", "", "#### 基础信息", ""]
        vip_lv = user.get("vip_level", 0)
        vip_exp = user.get("vip_expires_at")
        if vip_exp:
            vip_exp_aware = vip_exp.replace(tzinfo=timezone.utc) if vip_exp.tzinfo is None else vip_exp
            if vip_exp_aware < now:
                vip_lv = 0
        access_src = user.get("access_source", "")
        lines += [
            f"- **注册时间**：{_fmt_dt(user.get('created_at'))}",
            f"- **最后活跃**：{_fmt_dt(user.get('last_active_at'))}",
            f"- **访问来源**：{_ACCESS_LABELS.get(access_src, access_src or 'N/A')}",
            f"- **账号状态**：{'已激活' if user.get('has_access') else '未激活'}",
            f"- **VIP 状态**：{_VIP_LABELS.get(vip_lv, str(vip_lv))}",
            f"- **图生图配额**：{user.get('image_gen_quota', 0)} 次",
            f"- **声线偏好**：{user.get('voice_character') or '未设置'}",
            f"- **总对话数**：{user.get('total_interactions', 0)} 次",
            f"- **到店次数**：{user.get('store_visit_count', 0)} 次", "",
        ]

        user_orders = orders_map.get(uid, [])
        lines += ["#### 消费记录（本套餐）", ""]
        if user_orders:
            lines += ["| 时间 | 套餐名 | 类型 | 金额(¥) | 支付方式 | 状态 | 备注 |",
                      "|------|--------|------|---------|---------|------|------|"]
            for o in user_orders:
                lines.append(
                    f"| {_fmt_dt(o.get('created_at'))} | {o.get('package_name', 'N/A')} "
                    f"| {_PKG_TYPE_LABELS.get(o.get('package_type', ''), 'N/A')} "
                    f"| {o.get('amount', 0)} | {o.get('payment_method', 'N/A')} "
                    f"| {o.get('status', 'N/A')} | {o.get('note') or '-'} |"
                )
        else:
            lines.append("_暂无订单记录_")
        lines.append("")

        lines += ["#### 用户画像", ""]
        user_facts = facts_map.get(uid, [])
        if user_facts:
            cat_groups: dict = {}
            for f in user_facts:
                cat_groups.setdefault(f.get("category", "other"), []).append(f)
            for cat in ["bio", "pref", "exp", "rel", "trait", "plan", "vision"]:
                facts_in_cat = cat_groups.get(cat, [])
                if not facts_in_cat:
                    continue
                lines += [f"**{_CAT_LABELS.get(cat, cat)}**", ""]
                for f in facts_in_cat:
                    content = f.get("content", "").strip()
                    confidence = f.get("confidence", 0)
                    conf_str = f"{confidence:.2f}" if isinstance(confidence, float) else str(confidence)
                    topic_str = f"[{f.get('topic')}] " if f.get("topic") else ""
                    lines.append(
                        f"- {topic_str}{content}"
                        f"  _(置信度 {conf_str} / 提及 {f.get('mention_count', 1)} 次 / 首记录 {_fmt_dt(f.get('created_at'))})_"
                    )
                lines.append("")
            for cat, facts_in_cat in cat_groups.items():
                if cat in _CAT_LABELS:
                    continue
                lines += [f"**{cat}**", ""]
                for f in facts_in_cat:
                    content = f.get("content", "").strip()
                    confidence = f.get("confidence", 0)
                    conf_str = f"{confidence:.2f}" if isinstance(confidence, float) else str(confidence)
                    lines.append(f"- {content}  _(置信度 {conf_str} / 提及 {f.get('mention_count', 1)} 次)_")
                lines.append("")
        else:
            lines += ["_暂无画像数据_", ""]

        lines += ["---", ""]

    return "\n".join(lines)

def init_manage_api():
    return bp
