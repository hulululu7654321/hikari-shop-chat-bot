# -*- coding: utf-8 -*-
"""
src/modules/users/auth_handler.py
希卡莉 — 用户鉴权业务逻辑层

职责：
  - 微信 code 换 unionid/openid，写库，签发 JWT
  - Refresh Token 轮换
  - 登出（撤销 Refresh Token）
  - 邀请码激活 / 退出内测
  - VIP 状态查询
"""

import os
import hashlib
import aiohttp
from datetime import datetime, timezone, timedelta

from src.utils.logger import get_logger
from src.utils.database import get_db_manager, COL_USERS
from src.utils.auth import (
    create_access_token,
    create_refresh_token,
    revoke_refresh_token,
    verify_refresh_token,
)
from src.utils.invite import (
    check_user_access,
    get_vip_info,
    check_image_gen_quota,
    use_invite_code,
    user_quit,
    INVITE_ONLY,
    DAILY_MSG_LIMIT,
    VIP_LOCK_IMAGE_GEN,
)
from src.core.session_manager import get_session_manager

logger = get_logger(__name__)


# ──────────────────────────────────────────
# 登录
# ──────────────────────────────────────────

async def handle_login(js_code: str | None, cached_uid: str | None) -> dict:
    """
    微信 code 换 unionid/openid，写库，返回双 Token + 用户信息。

    重构对齐：
      - 环境变量名改为 WX_APPID / WX_SECRET（重构文档第十五节）
      - user_id 优先使用 unionid（格式 wx_{unionid}），fallback openid
      - 返回格式严格对齐重构文档 7.4
    """
    wx_appid  = os.getenv("WX_APPID", "")
    wx_secret = os.getenv("WX_SECRET", "")

    openid   = None
    unionid  = None

    if js_code and wx_appid and wx_secret:
        url = (
            f"https://api.weixin.qq.com/sns/jscode2session"
            f"?appid={wx_appid}&secret={wx_secret}"
            f"&js_code={js_code}&grant_type=authorization_code"
        )
        try:
            async with aiohttp.ClientSession() as sess:
                async with sess.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status == 200:
                        wx_res = await resp.json()
                        openid  = wx_res.get("openid")
                        unionid = wx_res.get("unionid")  # 同主体下才有
        except Exception as e:
            logger.warning(f"[Login] 微信接口请求失败: {e}")

    # user_id 决策：unionid > openid > 缓存 uid > dev 兜底
    if unionid:
        user_id = f"wx_{unionid}"
    elif openid:
        user_id = f"wx_{openid}"
    elif cached_uid:
        user_id = cached_uid
    elif js_code:
        user_id = f"dev_{hashlib.sha256(js_code.encode()).hexdigest()[:16]}"
    else:
        user_id = "dev_guest"

    db = get_db_manager().db

    user_doc = await db[COL_USERS].find_one({"user_id": user_id})
    if not user_doc:
        from src.schemas.user import get_default_user_schema
        user_doc = get_default_user_schema(user_id)
        await db[COL_USERS].insert_one(user_doc)

    # Session
    session_info = await get_session_manager().get_or_create_session("miniprogram", user_id)
    session_id = session_info["session_id"]

    # 管理角色
    manage_role = user_doc.get("manage_role")

    # 到店宽限期
    visited_expires_at = None
    if os.getenv("STORE_ENABLED", "false").lower() == "true":
        try:
            redis = await get_db_manager().get_redis()
            ttl = await redis.ttl(f"hikari:store:visited:{user_id}")
            if ttl > 0:
                visited_expires_at = int(
                    (datetime.now(timezone.utc) + timedelta(seconds=ttl)).timestamp()
                )
        except Exception:
            pass

    is_visited_active = visited_expires_at is not None
    has_access = (
        True if manage_role
        else True if is_visited_active
        else await check_user_access(user_id)
    )

    vip_info = await get_vip_info(user_id, db)
    vip_level = vip_info["vip_level"]
    is_vip    = vip_info["is_vip"]

    # 图生图配额
    _, image_gen_quota = await check_image_gen_quota(user_id)
    if not VIP_LOCK_IMAGE_GEN:
        image_gen_quota = -1

    # 今日消息数
    now_bj  = datetime.now(timezone.utc) + timedelta(hours=8)
    today   = now_bj.strftime("%Y%m%d")
    try:
        redis = await get_db_manager().get_redis()
        msg_count_raw = await redis.get(f"hikari:daily_msg:{user_id}:{today}")
        msg_count = int(msg_count_raw) if msg_count_raw else 0
    except Exception:
        msg_count = 0

    daily_limit  = 0 if is_vip else DAILY_MSG_LIMIT
    msg_remaining = -1 if is_vip else max(0, DAILY_MSG_LIMIT - msg_count)

    # 签发双 Token
    access_token, jti = create_access_token(user_id, vip_level=vip_level, role=manage_role)
    refresh_token = await create_refresh_token(user_id, jti)

    return {
        "access_token":    access_token,
        "refresh_token":   refresh_token,
        "expires_in":      7200,
        "user_id":         user_id,
        "user_name":       user_doc.get("user_name"),
        "has_access":      has_access,
        "invite_only":     INVITE_ONLY,
        "vip_level":       vip_level,
        "is_vip":          is_vip,
        "msg_remaining":   msg_remaining,
        "daily_limit":     daily_limit,
        "image_gen_quota": image_gen_quota,
        "manage_role":     manage_role,
        "visited_expires_at": visited_expires_at,
        "session_id":      session_id,
    }


# ──────────────────────────────────────────
# Token 刷新
# ──────────────────────────────────────────

async def handle_refresh(user_id: str, refresh_token: str, jti: str) -> dict | None:
    """
    验证 Refresh Token，返回新 Access Token + 新 Refresh Token（轮换）。
    验证失败返回 None。
    """
    valid = await verify_refresh_token(user_id, refresh_token, jti)
    if not valid:
        return None

    # 撤销旧 Refresh Token
    await revoke_refresh_token(user_id, jti)

    # 查当前 VIP / 角色（可能已变更）
    db = get_db_manager().db
    user_doc  = await db[COL_USERS].find_one({"user_id": user_id}, {"vip_level": 1, "manage_role": 1})
    vip_level = (user_doc.get("vip_level") or 0) if user_doc else 0
    role      = (user_doc.get("manage_role")) if user_doc else None

    new_access_token, new_jti = create_access_token(user_id, vip_level=vip_level, role=role)
    new_refresh_token = await create_refresh_token(user_id, new_jti)

    return {
        "access_token":  new_access_token,
        "refresh_token": new_refresh_token,
        "expires_in":    7200,
    }


# ──────────────────────────────────────────
# 登出
# ──────────────────────────────────────────

async def handle_logout(user_id: str, jti: str) -> None:
    """撤销 Refresh Token，使当前会话无效。"""
    await revoke_refresh_token(user_id, jti)


# ──────────────────────────────────────────
# 邀请码激活
# ──────────────────────────────────────────

async def handle_activate_invite(user_id: str, code: str) -> tuple[bool, str]:
    """
    激活邀请码。
    返回 (success, message)。
    """
    db = get_db_manager().db
    return await use_invite_code(code, user_id, db)


# ──────────────────────────────────────────
# 退出内测
# ──────────────────────────────────────────

async def handle_quit(user_id: str) -> bool:
    """撤销访问权限，邀请码永久作废。"""
    db = get_db_manager().db
    return await user_quit(user_id, db)


# ──────────────────────────────────────────
# VIP 状态查询
# ──────────────────────────────────────────

async def get_vip_status(user_id: str) -> dict:
    """
    返回 VIP 状态 + 访问权 + 今日消息数 + 图生图配额。
    对齐重构文档路由 GET /api/u/vip。
    """
    db    = get_db_manager().db
    redis = await get_db_manager().get_redis()

    has_access = await check_user_access(user_id)
    vip_info   = await get_vip_info(user_id, db)
    is_vip     = vip_info["is_vip"]

    now_bj = datetime.now(timezone.utc) + timedelta(hours=8)
    today  = now_bj.strftime("%Y%m%d")
    try:
        msg_count_raw = await redis.get(f"hikari:daily_msg:{user_id}:{today}")
        msg_count = int(msg_count_raw) if msg_count_raw else 0
    except Exception:
        msg_count = 0

    _, image_gen_remaining = await check_image_gen_quota(user_id)
    if not VIP_LOCK_IMAGE_GEN:
        image_gen_remaining = -1

    return {
        "has_access":              has_access,
        "invite_only":             INVITE_ONLY,
        "vip_level":               vip_info["vip_level"],
        "is_vip":                  is_vip,
        "vip_expires_at":          vip_info["vip_expires_at"].isoformat() if vip_info.get("vip_expires_at") else None,
        "today_msg_count":         msg_count,
        "daily_msg_limit":         DAILY_MSG_LIMIT,
        "msg_remaining":           max(0, DAILY_MSG_LIMIT - msg_count) if not is_vip else -1,
        "image_gen_quota_remaining": image_gen_remaining,
    }
