# -*- coding: utf-8 -*-
"""
NekosBest 表情 GIF 服务

职责：根据 LLM 输出的 mood_tag 从 NekosBest 拉取动漫 GIF URL。
- 不是每句话都触发，由 dialogue_engine 判断是否输出 mood_tag
- mood_tag 为空则不发 GIF
- 任何失败静默降级，不影响主流程
- Redis 缓存：同一 tag 在 10 秒内不重复请求（hikari:nekos:{tag}）
- Redis 不可用时回退进程内内存缓存
"""

import time
from typing import Optional

import httpx

from src.utils.logger import get_logger

logger = get_logger(__name__)

NEKOS_BASE = "https://nekos.best/api/v2"
TIMEOUT = 4.0
CACHE_TTL = 10  # 秒，Redis setex 要求整数

# mood_tag → NekosBest endpoint 映射
# neutral 不映射（平静聊天不发 GIF）
MOOD_MAP: dict[str, str] = {
    "happy": "smile",  # 愉悦 → 微笑 GIF
    "sad": "cry",      # 失落 → 哭泣 GIF
    "angry": "poke",   # 微愠 → 戳戳 GIF
}

# 进程内降级缓存：{mood_tag: (url, expire_ts)}
_fallback_cache: dict[str, tuple[str, float]] = {}


async def get_sticker_url(mood_tag: str) -> Optional[str]:
    """
    根据 mood_tag 返回 GIF URL，失败返回 None。
    优先走 Redis 跨进程缓存，Redis 不可用时降级到进程内缓存。
    """
    if not mood_tag:
        return None

    endpoint = MOOD_MAP.get(mood_tag)
    if not endpoint:
        logger.debug(f"nekos: unknown mood_tag={mood_tag}")
        return None

    redis_key = f"hikari:nekos:{mood_tag}"

    # 1. 优先命中 Redis 缓存
    redis = None
    try:
        from src.utils.database import get_db_manager

        redis = await get_db_manager().get_redis()
        cached_url = await redis.get(redis_key)
        if cached_url:
            logger.debug(f"nekos: redis cache hit | tag={mood_tag}")
            return cached_url if isinstance(cached_url, str) else cached_url.decode()
    except Exception as e:
        logger.debug(f"nekos: redis unavailable, fallback to memory cache: {e}")
        redis = None

    # 2. Redis 不可用时命中进程内降级缓存
    if redis is None:
        cached = _fallback_cache.get(mood_tag)
        if cached and time.time() < cached[1]:
            logger.debug(f"nekos: memory cache hit (degraded) | tag={mood_tag}")
            return cached[0]

    # 3. 缓存未命中，请求 NekosBest
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            resp = await client.get(f"{NEKOS_BASE}/{endpoint}")

        if resp.status_code != 200:
            logger.debug(f"nekos: request failed | status={resp.status_code}")
            return None

        results = resp.json().get("results", [])
        if not results:
            return None

        url = results[0].get("url", "")
        if not url:
            return None

        # 4. 写回缓存
        if redis is not None:
            try:
                await redis.setex(redis_key, CACHE_TTL, url)
            except Exception as e:
                logger.debug(f"nekos: redis write failed, writing memory cache: {e}")
                _fallback_cache[mood_tag] = (url, time.time() + CACHE_TTL)
        else:
            _fallback_cache[mood_tag] = (url, time.time() + CACHE_TTL)

        logger.info(f"nekos: fetched | tag={mood_tag} url={url[:60]}")
        return url

    except httpx.TimeoutException:
        logger.debug(f"nekos: request timeout | tag={mood_tag}")
        return None
    except Exception as e:
        logger.debug(f"nekos: fetch failed | tag={mood_tag} error={e}")
        return None
