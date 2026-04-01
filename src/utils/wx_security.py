# -*- coding: utf-8 -*-
"""
微信内容安全 — 希卡莉 (v2.1.0)
=====================================================================
重构对齐 [2026-03-29]：
1. 环境变量对齐 MASTER v3.0：WX_APPID / WX_SECRET（旧版 WX_APP_ID / WX_APP_SECRET）
2. Redis Key 对齐第五节：hikari:wx_access_token（旧版 hikari:wx:access_token）
3. DatabaseManager 改用 get_db_manager() 单例工厂，不再直接实例化
4. SECURITY_FAIL_OPEN 默认值改为 false（生产安全）
5. 逻辑不变：文字安全 check_msg_safe / 图片安全 check_image_safe
=====================================================================
"""

import os
import aiohttp

from src.utils.logger import get_logger
from src.utils.database import get_db_manager

logger = get_logger(__name__)

# 生产环境必须为 false：接口异常时拒绝而非放行
_FAIL_OPEN = os.getenv("SECURITY_FAIL_OPEN", "false").lower() == "true"

# Redis Key（对齐 MASTER 第五节）
_WX_TOKEN_KEY = "hikari:wx_access_token"


# ══════════════════════════════════════════════════════════════════
# Access Token 管理
# ══════════════════════════════════════════════════════════════════

async def get_wx_access_token() -> str | None:
    """
    获取微信全局 Access Token。
    Redis 缓存（hikari:wx_access_token），提前 5 分钟过期。
    未命中时调用微信 API 获取并写缓存。
    """
    # 先读缓存
    try:
        redis  = await get_db_manager().get_redis()
        cached = await redis.get(_WX_TOKEN_KEY)
        if cached:
            return cached
    except Exception as e:
        logger.warning(f"[WX-Sec] Redis 读取 token 失败，降级重新获取: {e}")

    # 从环境变量读取（对齐 MASTER v3.0 第十五节）
    appid  = os.getenv("WX_APPID")
    secret = os.getenv("WX_SECRET")
    if not appid or not secret:
        # 未配置时静默返回 None（开发/离线环境）
        return None

    url = (
        f"https://api.weixin.qq.com/cgi-bin/token"
        f"?grant_type=client_credential&appid={appid}&secret={secret}"
    )
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                data = await resp.json()
                if "access_token" in data:
                    token = data["access_token"]
                    # 提前 5 分钟过期（300s 缓冲）
                    ttl = max(int(data.get("expires_in", 7200)) - 300, 60)
                    try:
                        redis = await get_db_manager().get_redis()
                        await redis.setex(_WX_TOKEN_KEY, ttl, token)
                        logger.info("[WX-Sec] Access Token 已写入 Redis 缓存")
                    except Exception as _e:
                        logger.warning(f"[WX-Sec] Redis 写入 token 失败: {_e}")
                    return token
                else:
                    logger.error(f"[WX-Sec] Token 获取失败: {data}")
                    return None
    except Exception as e:
        logger.error(f"[WX-Sec] 请求微信 API 失败: {e}")
        return None


# ══════════════════════════════════════════════════════════════════
# 文字安全检查
# ══════════════════════════════════════════════════════════════════

async def check_msg_safe(openid: str, content: str) -> bool:
    """
    文字内容安全检查。
    返回 True=安全，False=违规。
    空内容 / 无 token 时直接放行。
    异常时返回 _FAIL_OPEN（生产环境应为 False）。
    """
    if not content:
        return True

    token = await get_wx_access_token()
    if not token:
        return True  # 未配置微信凭据，开发模式放行

    url = f"https://api.weixin.qq.com/wxa/msg_sec_check?access_token={token}"
    payload = {
        "version": 2,
        "openid":  openid or "anonymous",
        "scene":   1,
        "content": content[:2500],
    }

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                url, json=payload, timeout=aiohttp.ClientTimeout(total=10)
            ) as resp:
                data = await resp.json()

                if data.get("errcode") == 0:
                    result = data.get("result", {})
                    if result.get("suggest") != "pass":
                        logger.warning(
                            f"[WX-Sec] 拦截违规文字 | uid={openid[:8]} | label={result.get('label')}"
                        )
                        return False
                    return True

                elif data.get("errcode") == 40001:
                    # Access Token 失效，清除缓存，下次重新获取
                    try:
                        redis = await get_db_manager().get_redis()
                        await redis.delete(_WX_TOKEN_KEY)
                    except Exception:
                        pass
                    logger.warning("[WX-Sec] Access Token 已失效，已清除缓存")
                    return _FAIL_OPEN

                else:
                    logger.warning(f"[WX-Sec] msg_sec_check 异常返回: {data}")
                    return _FAIL_OPEN

    except Exception as e:
        logger.error(f"[WX-Sec] 文字安检失败: {e}")
        return _FAIL_OPEN


# ══════════════════════════════════════════════════════════════════
# 图片安全检查
# ══════════════════════════════════════════════════════════════════

async def check_image_safe(openid: str, image_bytes: bytes) -> bool:
    """
    图片内容安全检查（微信 img_sec_check 接口）。
    轮询检查结果，最多 10 次，间隔 1s。
    返回 True=安全，False=违规。
    无 token 时放行（开发模式）。
    异常时返回 _FAIL_OPEN。
    """
    import asyncio

    token = await get_wx_access_token()
    if not token:
        logger.warning("[WX-Sec] 无 Access Token，图片安检放行（仅限开发环境）")
        return True

    url  = f"https://api.weixin.qq.com/wxa/img_sec_check?access_token={token}"
    form = aiohttp.FormData()
    form.add_field(
        "media",
        image_bytes,
        filename="check.jpg",
        content_type="image/jpeg",
    )

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                url, data=form, timeout=aiohttp.ClientTimeout(total=15)
            ) as resp:
                data = await resp.json()

                if data.get("errcode") == 87014:
                    logger.warning(f"[WX-Sec] 拦截违规图片 | uid={openid[:8]}")
                    return False
                elif data.get("errcode") == 0:
                    return True
                else:
                    logger.warning(f"[WX-Sec] img_sec_check 异常返回: {data}")
                    return _FAIL_OPEN

    except Exception as e:
        logger.error(f"[WX-Sec] 图片安检失败: {e}")
        return _FAIL_OPEN
