# -*- coding: utf-8 -*-
"""
src/modules/manage/admin_api.py — 系统紧急操作接口
路由前缀：/api/sys/
鉴权：X-Admin-Key（require_admin_key）或无鉴权（健康检查）
用途：curl / 紧急操作，不对外暴露（Nginx 层 IP 白名单）
"""

import os
import traceback
from datetime import datetime, timezone, timedelta

from quart import Blueprint, request, jsonify

from src.utils.auth import require_admin_key
from src.utils.database import (
    get_db_manager, _sanitize_input,
    COL_USERS, COL_ORDERS, COL_INVITE_CODES,
)
from src.utils.logger import get_logger

logger = get_logger(__name__)

bp = Blueprint("admin_api", __name__, url_prefix="/api/sys")


# ─────────────────────────────────────────
# 健康检查（无鉴权，供 Nginx upstream 检测）
# ─────────────────────────────────────────

@bp.route("/health", methods=["GET"])
async def health_check():
    """Nginx upstream 健康检查，无鉴权。"""
    return jsonify({"code": 200, "message": "ok", "data": {"status": "healthy"}})


# ─────────────────────────────────────────
# 邀请码管理
# ─────────────────────────────────────────

@bp.route("/admin/invites", methods=["POST"])
@require_admin_key
async def admin_create_invites():
    """
    紧急生成邀请码。
    Body: {"count": 1, "expire_days": 7, "note": "备注"}
    """
    data = await request.get_json() or {}
    count = min(int(data.get("count", 1)), 50)
    expire_days = int(data.get("expire_days", 0)) or None
    note = _sanitize_input(data.get("note", ""))[:50]

    try:
        from src.utils.invite import create_invite_code
        db = get_db_manager().db
        codes = []
        for _ in range(count):
            doc = await create_invite_code(db, created_by="sys_admin", expire_days=expire_days, note=note)
            codes.append({
                "code": doc["code"],
                "expires_at": doc["expires_at"].isoformat() if doc.get("expires_at") else None,
                "note": doc.get("note", ""),
            })

        logger.info("[SysAdmin] 生成邀请码 | count=%d | note=%r", count, note)
        return jsonify({"code": 200, "message": "ok", "data": {"codes": codes, "count": len(codes)}})

    except Exception:
        logger.error("[SysAdmin] create_invites 失败: %s", traceback.format_exc())
        return jsonify({"code": 500, "message": "服务内部错误", "data": None}), 500


# ─────────────────────────────────────────
# VIP 管理
# ─────────────────────────────────────────

@bp.route("/admin/vip/grant", methods=["POST"])
@require_admin_key
async def admin_grant_vip():
    """
    直接授予 VIP。
    Body: {"user_id": "wx_xxx", "vip_level": 1, "days": 30, "image_gen_quota": 10}
    """
    data = await request.get_json() or {}
    user_id = _sanitize_input(data.get("user_id", ""))
    vip_level = int(data.get("vip_level", 1))
    days = int(data.get("days", 0))

    if not user_id:
        return jsonify({"code": 400, "message": "缺少 user_id", "data": None}), 400
    if vip_level not in (1, 2):
        return jsonify({"code": 400, "message": "vip_level 只能是 1 或 2", "data": None}), 400

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
            }}
        )

        from src.utils.invite import add_image_gen_quota, IMAGE_GEN_QUOTA_VIP, IMAGE_GEN_QUOTA_SVIP
        default_quota = IMAGE_GEN_QUOTA_SVIP if vip_level >= 2 else IMAGE_GEN_QUOTA_VIP
        image_gen_quota = int(data.get("image_gen_quota", default_quota))
        if image_gen_quota > 0:
            await add_image_gen_quota(user_id, image_gen_quota)

        level_label = "SVIP" if vip_level == 2 else "VIP"
        expire_label = f"{days}天" if days > 0 else "永久"
        logger.info("[SysAdmin] 授予VIP | uid=%s | level=%s | expire=%s | quota=+%d",
                    user_id[:8], level_label, expire_label, image_gen_quota)
        return jsonify({"code": 200, "message": f"已授予 {level_label}（{expire_label}），图生图配额 +{image_gen_quota}", "data": None})

    except Exception:
        logger.error("[SysAdmin] grant_vip 失败: %s", traceback.format_exc())
        return jsonify({"code": 500, "message": "服务内部错误", "data": None}), 500


# ─────────────────────────────────────────
# 用户管理
# ─────────────────────────────────────────

@bp.route("/admin/users/kick", methods=["POST"])
@require_admin_key
async def admin_kick_user():
    """
    紧急踢出用户：撤销访问权限 + 邀请码永久作废。
    Body: {"user_id": "wx_xxx", "reason": "违规"}
    """
    data = await request.get_json() or {}
    user_id = _sanitize_input(data.get("user_id", ""))
    reason = _sanitize_input(data.get("reason", "sys_admin_kick"))[:100]

    if not user_id:
        return jsonify({"code": 400, "message": "缺少 user_id", "data": None}), 400

    try:
        from src.utils.invite import admin_kick
        db = get_db_manager().db
        success = await admin_kick(user_id, db, reason=reason)
        if success:
            logger.info("[SysAdmin] 踢出用户 | uid=%s | reason=%s", user_id[:8], reason)
            return jsonify({"code": 200, "message": "用户已踢出", "data": None})
        return jsonify({"code": 500, "message": "踢出失败", "data": None}), 500

    except Exception:
        logger.error("[SysAdmin] kick_user 失败: %s", traceback.format_exc())
        return jsonify({"code": 500, "message": "服务内部错误", "data": None}), 500


# ─────────────────────────────────────────
# 统计
# ─────────────────────────────────────────

@bp.route("/admin/stats", methods=["GET"])
@require_admin_key
async def admin_stats():
    """系统统计：用户数、VIP 数、邀请码使用情况。"""
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
        logger.error("[SysAdmin] stats 失败: %s", traceback.format_exc())
        return jsonify({"code": 500, "message": "服务内部错误", "data": None}), 500

def init_admin_api():
    return bp
