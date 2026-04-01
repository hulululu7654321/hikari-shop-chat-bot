# -*- coding: utf-8 -*-
"""
src/modules/tools/shared/vip_guard.py — VIP 权限守卫（v1.0.0）
=====================================================================
供所有工具容器（imagegen / makeup / copywriting）使用的共享守卫层。
严格对齐 MASTER §13.3 函数签名，禁止修改签名。

【函数说明】
- check_vip_access(user_id, feature) → (bool, str)
    reason: "not_vip" | "quota_exceeded" | "ok"
    检查顺序：功能锁关闭 → VIP身份 → 仅image_gen做配额检查

- consume_quota(user_id, feature) → bool
    仅 image_gen 有配额，其余直接返回 True。
    必须在 check_vip_access 通过后调用。

【环境变量依赖】
VIP_LOCK_IMAGE_GEN   / VIP_LOCK_MAKEUP / VIP_LOCK_COPYWRITING
IMAGE_GEN_QUOTA_VIP  / IMAGE_GEN_QUOTA_SVIP
=====================================================================
"""

import os
from datetime import datetime, timezone

from src.utils.logger import get_logger

logger = get_logger(__name__)

# ── 功能锁开关（从环境变量读取，容器启动时固定） ──────────────────────────────
VIP_LOCK_IMAGE_GEN   = os.getenv("VIP_LOCK_IMAGE_GEN",   "true").lower() == "true"
VIP_LOCK_MAKEUP      = os.getenv("VIP_LOCK_MAKEUP",      "true").lower() == "true"
VIP_LOCK_COPYWRITING = os.getenv("VIP_LOCK_COPYWRITING", "true").lower() == "true"

_LOCK_MAP: dict[str, bool] = {
    "image_gen":   VIP_LOCK_IMAGE_GEN,
    "makeup":      VIP_LOCK_MAKEUP,
    "copywriting": VIP_LOCK_COPYWRITING,
}

_QUOTA_CACHE_TTL = 300  # Redis 缓存 5 分钟


# ══════════════════════════════════════════════════════════════════
# 内部：配额读写
# ══════════════════════════════════════════════════════════════════

def _quota_key(user_id: str) -> str:
    return f"hikari:image_gen_quota:{user_id}"


async def _check_image_gen_quota(user_id: str) -> tuple[bool, int]:
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

        user      = await db_mgr.db[COL_USERS].find_one(
            {"user_id": user_id}, {"image_gen_quota": 1}
        )
        remaining = int(user.get("image_gen_quota", 0)) if user else 0

        try:
            await redis.setex(_quota_key(user_id), _QUOTA_CACHE_TTL, str(remaining))
        except Exception:
            pass

        return remaining > 0, remaining

    except Exception as e:
        logger.warning(f"[VipGuard] quota check 异常，保守拦截 | uid={user_id[:8]} | err={e}")
        return False, 0


async def _decrement_image_gen_quota(user_id: str) -> None:
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
            return_document=True,
        )
        new_val = int(result.get("image_gen_quota", 0)) if result else 0
        logger.info(f"[VipGuard] 扣减配额 | uid={user_id[:8]} | 剩余={new_val}")

        try:
            redis = await db_mgr.get_redis()
            await redis.setex(_quota_key(user_id), _QUOTA_CACHE_TTL, str(new_val))
        except Exception as _e:
            logger.warning(
                f"[VipGuard] decrement 刷缓存失败（MongoDB已落盘）| uid={user_id[:8]} | err={_e}"
            )

    except Exception as e:
        logger.warning(f"[VipGuard] decrement 异常 | uid={user_id[:8]} | err={e}")


# ══════════════════════════════════════════════════════════════════
# 公开接口（MASTER §13.3 签名锁定）
# ══════════════════════════════════════════════════════════════════

async def check_vip_access(
    user_id: str,
    feature: str,  # "image_gen" | "makeup" | "copywriting"
) -> tuple[bool, str]:
    """
    检查 VIP 权限和配额。
    返回 (allowed, reason)
    reason: "not_vip" | "quota_exceeded" | "ok"

    检查顺序：
    1. 功能锁关闭（VIP_LOCK_*=false）→ 直接放行
    2. VIP 身份检查（含到期校验）→ 非 VIP 拦截，reason="not_vip"
    3. 仅 image_gen 做配额检查 → reason="quota_exceeded"
    """
    if not _LOCK_MAP.get(feature, True):
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
            allowed, _ = await _check_image_gen_quota(user_id)
            if not allowed:
                return False, "quota_exceeded"

        return True, "ok"

    except Exception as e:
        logger.warning(
            f"[VipGuard] check_vip_access 异常，降级拦截 | uid={user_id[:8]} | err={e}"
        )
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
        await _decrement_image_gen_quota(user_id)
        return True
    except Exception as e:
        logger.warning(f"[VipGuard] consume_quota 异常 | uid={user_id[:8]} | err={e}")
        return False
