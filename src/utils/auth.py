# -*- coding: utf-8 -*-
"""
JWT 鉴权工具 — 希卡莉 (v2.0.0)
=====================================================================
重构对齐 [2026-03-29]，严格遵守 MASTER v3.0 第七节规范：

Access Token（有效期 2 小时）
  payload: {sub, jti, iat, exp, v:1, vip, role}
  函数: create_access_token() → (token, jti)
        verify_access_token() → {user_id, vip_level, role, jti} | None

Refresh Token（有效期 30 天）
  格式: secrets.token_hex(32)，非 JWT
  存储: Redis hikari:refresh:{user_id}:{jti}，TTL=30天
  函数: create_refresh_token() / verify_refresh_token() / revoke_refresh_token()

装饰器
  @require_auth      → 注入 g.user_id / g.vip_level / g.role，失败 401
  @require_role(r)   → 检查 g.role，必须在 @require_auth 之后使用，失败 403
  @require_admin_key → X-Admin-Key 时序安全比较
  @require_skill_key → X-Skill-Key 时序安全比较

decode_token_payload() → 屏幕端读 store_id 用
=====================================================================
"""

import os
import secrets
import uuid
import jwt
from functools import wraps
from datetime import datetime, timezone, timedelta
from quart import request, jsonify, g

from src.utils.logger import get_logger

logger = get_logger(__name__)

# ── Token 有效期 ──
_ACCESS_TOKEN_EXPIRE_SECONDS  = 7200    # 2 小时
_REFRESH_TOKEN_EXPIRE_SECONDS = 2592000 # 30 天
_JWT_VERSION = 1


# ══════════════════════════════════════════════════════════════════
# 内部工具
# ══════════════════════════════════════════════════════════════════

def _get_secret() -> str:
    secret = os.getenv("JWT_SECRET_KEY", "")
    if not secret or secret.startswith("FILL_IN"):
        raise RuntimeError(
            "[Auth] JWT_SECRET_KEY 未配置！\n"
            "生成命令: python -c \"import secrets; print(secrets.token_hex(32))\""
        )
    return secret


def _refresh_key(user_id: str, jti: str) -> str:
    return f"hikari:refresh:{user_id}:{jti}"


# ══════════════════════════════════════════════════════════════════
# Access Token
# ══════════════════════════════════════════════════════════════════

def create_access_token(
    user_id: str,
    vip_level: int = 0,
    role: str | None = None,
) -> tuple[str, str]:
    """
    签发 Access Token。
    返回 (access_token, jti)
    jti 用于关联 Refresh Token，也用于日志追踪。
    """
    jti = str(uuid.uuid4())
    now = datetime.now(timezone.utc)
    payload = {
        "sub":  user_id,
        "jti":  jti,
        "iat":  now,
        "exp":  now + timedelta(seconds=_ACCESS_TOKEN_EXPIRE_SECONDS),
        "v":    _JWT_VERSION,
        "vip":  vip_level,
        "role": role,
    }
    token = jwt.encode(payload, _get_secret(), algorithm="HS256")
    return token, jti


def verify_access_token(token: str) -> dict | None:
    """
    验证 Access Token。
    返回（成功）: {"user_id": str, "vip_level": int, "role": str|None, "jti": str}
    返回（失败）: None
    """
    try:
        payload = jwt.decode(token, _get_secret(), algorithms=["HS256"])
        return {
            "user_id":   payload["sub"],
            "vip_level": int(payload.get("vip", 0)),
            "role":      payload.get("role"),
            "jti":       payload.get("jti", ""),
        }
    except jwt.ExpiredSignatureError:
        logger.debug("[Auth] Access Token 已过期")
        return None
    except jwt.InvalidTokenError as e:
        logger.debug(f"[Auth] Access Token 无效: {e}")
        return None


# ══════════════════════════════════════════════════════════════════
# Refresh Token
# ══════════════════════════════════════════════════════════════════

async def create_refresh_token(user_id: str, jti: str) -> str:
    """
    生成 Refresh Token，写入 Redis，返回 token 字符串。
    token 格式：secrets.token_hex(32)（64 位随机字符串，非 JWT）
    Redis key: hikari:refresh:{user_id}:{jti}，TTL=30天
    """
    token = secrets.token_hex(32)
    try:
        from src.utils.database import get_db_manager
        redis = await get_db_manager().get_redis()
        await redis.setex(_refresh_key(user_id, jti), _REFRESH_TOKEN_EXPIRE_SECONDS, token)
    except Exception as e:
        logger.error(f"[Auth] create_refresh_token 写 Redis 失败: {e}")
        raise
    return token


async def verify_refresh_token(user_id: str, token: str, jti: str) -> bool:
    """
    验证 Refresh Token 是否在 Redis 中存在且匹配。
    返回 True=有效，False=无效或已撤销。
    """
    try:
        from src.utils.database import get_db_manager
        redis  = await get_db_manager().get_redis()
        stored = await redis.get(_refresh_key(user_id, jti))
        return stored is not None and secrets.compare_digest(stored, token)
    except Exception as e:
        logger.error(f"[Auth] verify_refresh_token 失败: {e}")
        return False


async def revoke_refresh_token(user_id: str, jti: str) -> None:
    """
    撤销 Refresh Token，删除 Redis key。
    踢人 / 登出时调用，撤权最多 2 小时生效（Access Token 自然过期）。
    """
    try:
        from src.utils.database import get_db_manager
        redis = await get_db_manager().get_redis()
        await redis.delete(_refresh_key(user_id, jti))
        logger.info(f"[Auth] Refresh Token 已撤销 | uid={user_id[:8]} | jti={jti[:8]}")
    except Exception as e:
        logger.error(f"[Auth] revoke_refresh_token 失败: {e}")


def decode_token_payload(token: str) -> dict | None:
    """
    解码 JWT 返回完整 payload（不校验有效期）。
    屏幕端 Token 读取 store_id 字段使用此函数。
    """
    try:
        return jwt.decode(
            token, _get_secret(), algorithms=["HS256"],
            options={"verify_exp": False},
        )
    except jwt.InvalidTokenError:
        return None


# ══════════════════════════════════════════════════════════════════
# 装饰器
# ══════════════════════════════════════════════════════════════════

def require_auth(f):
    """
    验证 Authorization: Bearer <access_token>
    通过后注入：
      g.user_id   — 用户 ID
      g.vip_level — VIP 等级（0/1/2）
      g.role      — 管理角色（None / "admin" / "store_clerk"）
    失败返回 401。
    """
    @wraps(f)
    async def decorated(*args, **kwargs):
        auth_header = request.headers.get("Authorization", "")
        if not auth_header.startswith("Bearer "):
            return jsonify({"code": 401, "message": "未提供认证 Token", "data": None}), 401

        token  = auth_header[len("Bearer "):]
        result = verify_access_token(token)
        if not result:
            return jsonify({"code": 401, "message": "Token 无效或已过期，请重新登录", "data": None}), 401

        g.user_id   = result["user_id"]
        g.vip_level = result["vip_level"]
        g.role      = result["role"]
        g.jti       = result["jti"]
        return await f(*args, **kwargs)

    return decorated


def require_role(role: str):
    """
    验证 g.role，必须在 @require_auth 之后使用。
    失败返回 403。

    用法：
        @bp.route('/api/m/users')
        @require_auth
        @require_role("admin")
        async def list_users():
            ...
    """
    def decorator(f):
        @wraps(f)
        async def decorated(*args, **kwargs):
            if getattr(g, "role", None) != role:
                return jsonify({"code": 403, "message": "权限不足", "data": None}), 403
            return await f(*args, **kwargs)
        return decorated
    return decorator


def require_admin_key(f):
    """
    验证 X-Admin-Key 请求头。
    使用 secrets.compare_digest 防止时序攻击。
    失败返回 401。
    """
    @wraps(f)
    async def decorated(*args, **kwargs):
        admin_key = os.getenv("ADMIN_API_KEY", "")
        provided  = request.headers.get("X-Admin-Key", "")
        if not admin_key or not provided:
            return jsonify({"code": 401, "message": "未提供 Admin Key", "data": None}), 401
        if not secrets.compare_digest(provided, admin_key):
            return jsonify({"code": 401, "message": "Admin Key 无效", "data": None}), 401
        return await f(*args, **kwargs)
    return decorated


def require_skill_key(f):
    """
    验证 X-Skill-Key 请求头。
    使用 secrets.compare_digest 防止时序攻击。
    失败返回 401。
    """
    @wraps(f)
    async def decorated(*args, **kwargs):
        skill_key = os.getenv("HIKARI_SKILL_KEY", "")
        provided  = request.headers.get("X-Skill-Key", "")
        if not skill_key or not provided:
            return jsonify({"code": 401, "message": "未提供 Skill Key", "data": None}), 401
        if not secrets.compare_digest(provided, skill_key):
            return jsonify({"code": 401, "message": "Skill Key 无效", "data": None}), 401
        return await f(*args, **kwargs)
    return decorated
