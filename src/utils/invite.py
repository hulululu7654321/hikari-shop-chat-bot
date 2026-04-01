# -*- coding: utf-8 -*-
"""
invite.py — 希卡莉邀请制 + VIP 访问控制 (v2.0.0)
=====================================================================
重构对齐 [2026-03-29]：
1. 集合名全部改用 database.COL_* 常量，禁止字符串字面量。
2. 新增 check_vip_access(user_id, feature) — 工具容器 vip_guard 的后端逻辑。
3. 新增 consume_quota(user_id, feature) — 配额消耗统一入口。
4. DAILY_MSG_LIMIT_VIP=0 表示 VIP 不限次数（0 为特殊值）。
5. 其余逻辑（邀请码/配额/撤权）保持不变，只对齐集合名。

【邀请制设计】
- INVITE_ONLY=true  → 必须持有有效邀请码才能使用
- INVITE_ONLY=false → 公测模式，所有登录用户均可访问

【VIP 功能锁设计】
- VIP_LOCK_IMAGE_GEN / VIP_LOCK_MAKEUP / VIP_LOCK_COPYWRITING
  false 时对应功能对所有人开放，无需 VIP
- vip_level: 0=普通, 1=VIP, 2=SVIP

【图生图配额】
- MongoDB users.image_gen_quota 主存储（持久化、可审计）
- Redis hikari:image_gen_quota:{user_id} 缓存（TTL=5min）
- 写操作：先写 MongoDB，成功后刷新 Redis

【Redis Keys】
- hikari:invite:access:{user_id}      → "1"/"0"  TTL=5min
- hikari:daily_msg:{user_id}:{date}   → 当天消息计数  TTL 至次日 00:05 北京时间
- hikari:image_gen_quota:{user_id}    → 配额缓存  TTL=5min
=====================================================================
"""

import os
from datetime import datetime, timezone, timedelta

from src.utils.logger import get_logger

logger = get_logger(__name__)

# ── 全局开关 ──
INVITE_ONLY             = os.getenv("INVITE_ONLY", "true").lower() == "true"
DAILY_MSG_LIMIT         = int(os.getenv("DAILY_MSG_LIMIT", "100"))
DAILY_MSG_LIMIT_VIP     = int(os.getenv("DAILY_MSG_LIMIT_VIP", "0"))   # 0 = 不限
INVITE_CODE_EXPIRE_DAYS = int(os.getenv("INVITE_CODE_EXPIRE_DAYS", "7"))

# VIP 功能锁
VIP_LOCK_IMAGE_GEN   = os.getenv("VIP_LOCK_IMAGE_GEN",   "true").lower() == "true"
VIP_LOCK_MAKEUP      = os.getenv("VIP_LOCK_MAKEUP",      "true").lower() == "true"
VIP_LOCK_COPYWRITING = os.getenv("VIP_LOCK_COPYWRITING", "true").lower() == "true"

# 图生图配额默认赠送量（每次授权/购买套餐时叠加）
IMAGE_GEN_QUOTA_VIP  = int(os.getenv("IMAGE_GEN_QUOTA_VIP",  "30"))
IMAGE_GEN_QUOTA_SVIP = int(os.getenv("IMAGE_GEN_QUOTA_SVIP", "60"))

_QUOTA_CACHE_TTL = 300   # Redis 缓存 5 分钟


# ══════════════════════════════════════════════════════════════════
# 访问权限检查（邀请制）
# ══════════════════════════════════════════════════════════════════

async def check_user_access(user_id: str) -> bool:
    """
    检查用户是否有访问权限。
    INVITE_ONLY=false 时直接返回 True。
    先查 Redis 缓存（TTL=5min），未命中回源 MongoDB。
    异常时降级放行。
    """
    if not INVITE_ONLY:
        return True

    try:
        from src.utils.database import get_db_manager, COL_USERS
        db_mgr = get_db_manager()
        redis  = await db_mgr.get_redis()

        cache_key = f"hikari:invite:access:{user_id}"
        cached    = await redis.get(cache_key)
        if cached == "1":
            return True
        if cached == "0":
            return False

        user = await db_mgr.db[COL_USERS].find_one(
            {"user_id": user_id}, {"has_access": 1}
        )
        has_access = bool(user and user.get("has_access", False))
        await redis.setex(cache_key, 300, "1" if has_access else "0")
        return has_access

    except Exception as e:
        logger.warning(f"[Invite] 访问权检查异常，降级放行: {e}")
        return True


async def grant_access(user_id: str, db) -> None:
    """授予访问权限：写 MongoDB + 刷 Redis 缓存为 "1"，立即生效"""
    try:
        from src.utils.database import get_db_manager, COL_USERS
        await db[COL_USERS].update_one(
            {"user_id": user_id},
            {"$set": {"has_access": True, "access_granted_at": datetime.now(timezone.utc)}},
        )
        redis = await get_db_manager().get_redis()
        await redis.setex(f"hikari:invite:access:{user_id}", 300, "1")
        logger.info(f"[Invite] 授予访问权限 | uid={user_id[:8]}")
    except Exception as e:
        logger.error(f"[Invite] grant_access 失败: {e}")
        raise


async def revoke_access(user_id: str, db) -> None:
    """撤销访问权限：写 MongoDB + 刷 Redis 缓存为 "0"，立即踢出"""
    try:
        from src.utils.database import get_db_manager, COL_USERS
        await db[COL_USERS].update_one(
            {"user_id": user_id},
            {"$set": {"has_access": False, "access_revoked_at": datetime.now(timezone.utc)}},
        )
        redis = await get_db_manager().get_redis()
        await redis.setex(f"hikari:invite:access:{user_id}", 300, "0")
        logger.info(f"[Invite] 撤销访问权限 | uid={user_id[:8]}")
    except Exception as e:
        logger.error(f"[Invite] revoke_access 失败: {e}")
        raise


# ══════════════════════════════════════════════════════════════════
# VIP 状态查询
# ══════════════════════════════════════════════════════════════════

async def get_vip_info(user_id: str, db) -> dict:
    """
    查询用户 VIP 状态。
    返回 {vip_level, is_vip, vip_expires_at, is_expired}
    异常时返回全 0 安全默认值。
    """
    try:
        from src.utils.database import COL_USERS
        user = await db[COL_USERS].find_one(
            {"user_id": user_id}, {"vip_level": 1, "vip_expires_at": 1}
        )
        if not user:
            return {"vip_level": 0, "is_vip": False, "vip_expires_at": None, "is_expired": False}

        vip_level  = user.get("vip_level", 0)
        expires    = user.get("vip_expires_at")
        is_expired = False

        if vip_level > 0 and expires is not None:
            expires_aware = expires.replace(tzinfo=timezone.utc) if expires.tzinfo is None else expires
            if expires_aware < datetime.now(timezone.utc):
                vip_level  = 0
                is_expired = True

        return {
            "vip_level":     vip_level,
            "is_vip":        vip_level > 0,
            "vip_expires_at": expires,
            "is_expired":    is_expired,
        }
    except Exception as e:
        logger.warning(f"[VIP] get_vip_info 异常: {e}")
        return {"vip_level": 0, "is_vip": False, "vip_expires_at": None, "is_expired": False}


# ══════════════════════════════════════════════════════════════════
# VIP 功能锁 + 配额检查（工具容器 vip_guard 后端逻辑）
# ══════════════════════════════════════════════════════════════════

async def check_vip_access(
    user_id: str,
    feature: str,   # "image_gen" | "makeup" | "copywriting"
) -> tuple[bool, str]:
    """
    检查 VIP 权限和配额。

    返回 (allowed: bool, reason: str)
    reason: "not_vip" | "quota_exceeded" | "ok"

    检查顺序：
    1. 功能锁关闭（VIP_LOCK_*=false）→ 直接放行
    2. VIP 身份检查 → 非 VIP 拦截，reason="not_vip"
    3. 仅 image_gen 做配额检查 → reason="quota_exceeded"
    """
    _lock_map = {
        "image_gen":   VIP_LOCK_IMAGE_GEN,
        "makeup":      VIP_LOCK_MAKEUP,
        "copywriting": VIP_LOCK_COPYWRITING,
    }
    if not _lock_map.get(feature, True):
        # 功能锁关闭，全员可用
        return True, "ok"

    try:
        from src.utils.database import get_db_manager, COL_USERS
        db_mgr = get_db_manager()
        user   = await db_mgr.db[COL_USERS].find_one(
            {"user_id": user_id},
            {"vip_level": 1, "vip_expires_at": 1},
        )
        vip_level = user.get("vip_level", 0) if user else 0

        # VIP 到期检查
        if vip_level > 0 and user:
            expires = user.get("vip_expires_at")
            if expires is not None:
                expires_aware = (
                    expires.replace(tzinfo=timezone.utc)
                    if expires.tzinfo is None else expires
                )
                if expires_aware < datetime.now(timezone.utc):
                    vip_level = 0

        if vip_level == 0:
            return False, "not_vip"

        # 仅 image_gen 做配额检查
        if feature == "image_gen":
            allowed, _ = await check_image_gen_quota(user_id)
            if not allowed:
                return False, "quota_exceeded"

        return True, "ok"

    except Exception as e:
        logger.warning(f"[VIP] check_vip_access 异常，降级拦截 | uid={user_id[:8]} | err={e}")
        return False, "not_vip"


async def consume_quota(user_id: str, feature: str) -> bool:
    """
    消费一次配额（仅 image_gen 有配额）。
    必须在 check_vip_access 通过后调用。
    其他 feature 直接返回 True（无需扣减）。
    """
    if feature != "image_gen":
        return True
    try:
        await decrement_image_gen_quota(user_id)
        return True
    except Exception as e:
        logger.error(f"[VIP] consume_quota 失败 | uid={user_id[:8]} | err={e}")
        return False


# ══════════════════════════════════════════════════════════════════
# 每日对话次数限制
# ══════════════════════════════════════════════════════════════════

async def check_daily_msg_limit(user_id: str, is_vip: bool) -> tuple:
    """
    【只检查，不递增】
    返回 (allowed: bool, used_count: int, limit: int)
    VIP 用户：DAILY_MSG_LIMIT_VIP=0 表示不限（返回 limit=0）
    异常时降级放行。
    """
    if is_vip:
        return True, 0, DAILY_MSG_LIMIT_VIP

    try:
        from src.utils.database import get_db_manager
        redis   = await get_db_manager().get_redis()
        now_bj  = datetime.now(timezone.utc) + timedelta(hours=8)
        today   = now_bj.strftime("%Y%m%d")
        key     = f"hikari:daily_msg:{user_id}:{today}"
        raw     = await redis.get(key)
        count   = int(raw) if raw else 0
        return count < DAILY_MSG_LIMIT, count, DAILY_MSG_LIMIT
    except Exception as e:
        logger.warning(f"[VIP] check_daily_msg_limit 异常，降级放行: {e}")
        return True, 0, DAILY_MSG_LIMIT


async def increment_daily_msg(user_id: str) -> None:
    """
    【只递增，不检查】
    在消息进入推理流程后调用（所有检查通过之后）。
    TTL 精确到北京时间次日 00:05（+300s 跨天缓冲）。
    """
    try:
        from src.utils.database import get_db_manager
        redis  = await get_db_manager().get_redis()
        now_bj = datetime.now(timezone.utc) + timedelta(hours=8)
        today  = now_bj.strftime("%Y%m%d")
        key    = f"hikari:daily_msg:{user_id}:{today}"
        count  = await redis.incr(key)
        if count == 1:
            seconds_to_midnight = int(
                (timedelta(hours=24) - timedelta(
                    hours=now_bj.hour, minutes=now_bj.minute, seconds=now_bj.second
                )).total_seconds()
            ) + 300
            await redis.expire(key, max(seconds_to_midnight, 300))
    except Exception as e:
        logger.warning(f"[VIP] increment_daily_msg 异常: {e}")


# ══════════════════════════════════════════════════════════════════
# 邀请码操作
# ══════════════════════════════════════════════════════════════════

async def create_invite_code(
    db,
    created_by: str = "admin",
    expire_days: int = None,
    note: str = "",
) -> dict:
    """
    生成新邀请码，存入 auth_invite_codes 集合。
    expire_days=None 时使用 INVITE_CODE_EXPIRE_DAYS 默认值。
    """
    import secrets
    from src.utils.database import COL_INVITE_CODES

    effective_days = (
        expire_days if (expire_days is not None and expire_days > 0)
        else INVITE_CODE_EXPIRE_DAYS
    )
    now  = datetime.now(timezone.utc)
    code = secrets.token_urlsafe(9)   # 12 字符 URL-safe 字符串

    doc = {
        "code":       code,
        "status":     "unused",
        "created_by": created_by,
        "created_at": now,
        "expires_at": now + timedelta(days=effective_days),
        "note":       note,
        "used_by":    None,
        "used_at":    None,
    }
    await db[COL_INVITE_CODES].insert_one(doc)
    logger.info(f"[Invite] 生成邀请码 | code={code} | expire={effective_days}天 | note={note!r}")
    return doc


async def use_invite_code(code: str, user_id: str, db) -> tuple[bool, str]:
    """
    用户激活邀请码。乐观锁防并发重复激活。
    返回 (success: bool, message: str)
    """
    from src.utils.database import get_db_manager, COL_INVITE_CODES, COL_USERS

    now    = datetime.now(timezone.utc)
    invite = await db[COL_INVITE_CODES].find_one({"code": code})

    if not invite:
        return False, "邀请码不存在喵，检查一下有没有打错？"
    if invite["status"] == "used":
        return False, "这个邀请码已经被别人用了喵"
    if invite["status"] == "revoked":
        return False, "这个邀请码已失效喵，找管理员要一个新的吧"
    if invite["status"] != "unused":
        return False, "邀请码状态异常喵"

    expires_at = invite.get("expires_at")
    if expires_at:
        expires_aware = (
            expires_at.replace(tzinfo=timezone.utc)
            if expires_at.tzinfo is None else expires_at
        )
        if expires_aware < now:
            return False, "邀请码已过期喵，找管理员要一个新的吧"

    user = await db[COL_USERS].find_one({"user_id": user_id}, {"has_access": 1})
    if user and user.get("has_access"):
        return False, "你已经有访问权限了喵，不需要再激活～"

    # 乐观锁：只有 status=unused 时才更新
    result = await db[COL_INVITE_CODES].update_one(
        {"code": code, "status": "unused"},
        {"$set": {"status": "used", "used_by": user_id, "used_at": now}},
    )
    if result.modified_count == 0:
        return False, "邀请码刚刚被用掉了喵，手速太慢了"

    await db[COL_USERS].update_one(
        {"user_id": user_id},
        {"$set": {
            "has_access":        True,
            "invite_code":       code,
            "access_source":     "invite_code",
            "access_granted_at": now,
        }},
    )

    # 刷新 Redis 缓存，让权限立即生效
    try:
        redis = await get_db_manager().get_redis()
        await redis.setex(f"hikari:invite:access:{user_id}", 300, "1")
    except Exception:
        pass

    logger.info(f"[Invite] 邀请码激活 | uid={user_id[:8]} | code={code}")
    return True, "欢迎加入希卡莉的小圈子喵！✨"


# ══════════════════════════════════════════════════════════════════
# VIP 授权（套餐叠加）
# ══════════════════════════════════════════════════════════════════

async def grant_vip_by_package(
    user_id: str,
    vip_level: int,
    vip_days: int,
    db,
    granted_by: str = "admin",
) -> dict:
    """
    套餐下单触发的 VIP 授权（叠加计算，非覆盖）。
    - 现有 VIP 未过期 → 在现有到期时间基础上 + vip_days
    - 已过期 / 无 VIP  → 从现在起算 vip_days 天
    - 永久 VIP（expires=None）→ 跳过，保持永久不变
    vip_days 必须 > 0，永久授权由管理员直接操作。
    返回 {vip_expires_at, is_stacked, skipped}
    """
    if vip_days <= 0:
        raise ValueError("grant_vip_by_package: vip_days 必须 > 0，永久VIP请用管理员直接授权")

    from src.utils.database import COL_USERS

    now  = datetime.now(timezone.utc)
    user = await db[COL_USERS].find_one(
        {"user_id": user_id}, {"vip_level": 1, "vip_expires_at": 1}
    )
    existing_expires = user.get("vip_expires_at") if user else None
    is_stacked       = False

    if existing_expires is not None:
        expires_aware = (
            existing_expires.replace(tzinfo=timezone.utc)
            if existing_expires.tzinfo is None else existing_expires
        )
        if expires_aware > now:
            new_expires = expires_aware + timedelta(days=vip_days)
            is_stacked  = True
        else:
            new_expires = now + timedelta(days=vip_days)
    else:
        # expires=None：永久 VIP 或从未有过 VIP
        if user and user.get("vip_level", 0) > 0:
            # 当前是永久 VIP，叠加无意义，跳过
            logger.warning(f"[VIP] grant_vip_by_package 跳过：用户已是永久VIP | uid={user_id[:8]}")
            return {"vip_expires_at": None, "is_stacked": False, "skipped": True}
        new_expires = now + timedelta(days=vip_days)

    await db[COL_USERS].update_one(
        {"user_id": user_id},
        {"$set": {
            "vip_level":      vip_level,
            "vip_expires_at": new_expires,
            "vip_granted_at": now,
            "vip_granted_by": granted_by,
        }},
    )
    logger.info(
        f"[VIP] 套餐授权 | uid={user_id[:8]} | level={vip_level} | "
        f"days={vip_days} | stacked={is_stacked} | expires={new_expires.date()}"
    )
    return {"vip_expires_at": new_expires, "is_stacked": is_stacked, "skipped": False}


# ══════════════════════════════════════════════════════════════════
# 图生图配额（MongoDB 主存储 + Redis 缓存）
# ══════════════════════════════════════════════════════════════════

def _quota_key(user_id: str) -> str:
    return f"hikari:image_gen_quota:{user_id}"


async def check_image_gen_quota(user_id: str) -> tuple[bool, int]:
    """
    【只检查，不扣减】
    读取顺序：Redis 缓存 → MongoDB 回源并写缓存。
    异常时返回 (False, 0)，保守拦截。
    """
    try:
        from src.utils.database import get_db_manager, COL_USERS
        db_mgr = get_db_manager()
        redis  = await db_mgr.get_redis()

        cached = await redis.get(_quota_key(user_id))
        if cached is not None:
            remaining = int(cached)
            return remaining > 0, remaining

        # 缓存未命中，回源 MongoDB
        user      = await db_mgr.db[COL_USERS].find_one(
            {"user_id": user_id}, {"image_gen_quota": 1}
        )
        remaining = int(user.get("image_gen_quota", 0)) if user else 0

        try:
            await redis.setex(_quota_key(user_id), _QUOTA_CACHE_TTL, str(remaining))
        except Exception:
            pass   # 缓存写失败不影响结果

        return remaining > 0, remaining

    except Exception as e:
        logger.warning(f"[ImageGenQuota] check 异常，保守拦截 | uid={user_id[:8]} | err={e}")
        return False, 0


async def add_image_gen_quota(user_id: str, quota: int) -> None:
    """
    【只叠加，不检查】
    先原子写 MongoDB（$inc），成功后刷新 Redis 缓存。
    quota 必须 > 0。
    """
    if quota <= 0:
        return
    try:
        from src.utils.database import get_db_manager, COL_USERS
        db_mgr = get_db_manager()

        result = await db_mgr.db[COL_USERS].find_one_and_update(
            {"user_id": user_id},
            {"$inc": {"image_gen_quota": quota}},
            projection={"image_gen_quota": 1},
            return_document=True,   # AFTER，拿更新后的值
        )
        new_val = int(result.get("image_gen_quota", quota)) if result else quota
        logger.info(f"[ImageGenQuota] 叠加配额 | uid={user_id[:8]} | +{quota} → 剩余{new_val}")

        try:
            redis = await db_mgr.get_redis()
            await redis.setex(_quota_key(user_id), _QUOTA_CACHE_TTL, str(new_val))
        except Exception as _e:
            logger.warning(f"[ImageGenQuota] add 刷缓存失败（MongoDB已落盘）| uid={user_id[:8]} | err={_e}")

    except Exception as e:
        logger.warning(f"[ImageGenQuota] add 异常 | uid={user_id[:8]} | err={e}")


async def decrement_image_gen_quota(user_id: str) -> None:
    """
    【只扣减，不检查】
    聚合管道原子操作：quota = max(quota - 1, 0)，避免先读后写竞态。
    成功后刷新 Redis 缓存。
    """
    try:
        from src.utils.database import get_db_manager, COL_USERS
        db_mgr = get_db_manager()

        result = await db_mgr.db[COL_USERS].find_one_and_update(
            {"user_id": user_id},
            [{"$set": {"image_gen_quota": {"$max": [{"$subtract": ["$image_gen_quota", 1]}, 0]}}}],
            projection={"image_gen_quota": 1},
            return_document=True,   # AFTER
        )
        new_val = int(result.get("image_gen_quota", 0)) if result else 0
        logger.info(f"[ImageGenQuota] 扣减配额 | uid={user_id[:8]} | 剩余{new_val}")

        try:
            redis = await db_mgr.get_redis()
            await redis.setex(_quota_key(user_id), _QUOTA_CACHE_TTL, str(new_val))
        except Exception as _e:
            logger.warning(f"[ImageGenQuota] decrement 刷缓存失败（MongoDB已落盘）| uid={user_id[:8]} | err={_e}")

    except Exception as e:
        logger.warning(f"[ImageGenQuota] decrement 异常 | uid={user_id[:8]} | err={e}")


# ══════════════════════════════════════════════════════════════════
# 撤权操作
# ══════════════════════════════════════════════════════════════════

async def user_quit(user_id: str, db) -> bool:
    """用户主动退出：撤销访问权限，邀请码永久作废"""
    return await _do_revoke(user_id, db, reason="user_quit")


async def admin_kick(user_id: str, db, reason: str = "admin_kick") -> bool:
    """管理员踢出用户：撤销访问权限，邀请码永久作废"""
    return await _do_revoke(user_id, db, reason=reason)


async def _do_revoke(user_id: str, db, reason: str) -> bool:
    """内部：执行撤销操作"""
    try:
        from src.utils.database import COL_USERS, COL_INVITE_CODES
        now  = datetime.now(timezone.utc)
        user = await db[COL_USERS].find_one({"user_id": user_id}, {"invite_code": 1})
        invite_code = user.get("invite_code") if user else None

        if invite_code:
            await db[COL_INVITE_CODES].update_one(
                {"code": invite_code},
                {"$set": {"status": "revoked", "revoked_at": now, "revoke_reason": reason}},
            )

        await revoke_access(user_id, db)
        logger.info(f"[Invite] 撤销完成 | uid={user_id[:8]} | reason={reason}")
        return True
    except Exception as e:
        logger.error(f"[Invite] _do_revoke 失败: {e}")
        return False
