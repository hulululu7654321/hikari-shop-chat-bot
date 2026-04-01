# -*- coding: utf-8 -*-
"""
API 重试工具 — 希卡莉 (v1.1.0)
=====================================================================
重构对齐 [2026-03-29]：
- import 路径对齐新目录结构（src.utils.logger）
- 逻辑完全保持不变
- 适用：豆包 API 429 限流 + 5xx 瞬时故障指数退避重试
=====================================================================
"""

import asyncio
import random
import functools
from typing import Callable, Any
from openai import RateLimitError, APIStatusError

from src.utils.logger import get_logger

logger = get_logger(__name__)

# ── 默认重试参数 ──
_DEFAULT_MAX_RETRIES = 3
_DEFAULT_BASE_DELAY  = 2.0    # 首次等待（秒）
_DEFAULT_MAX_DELAY   = 30.0   # 单次等待上限（秒）


def _is_retryable(exc: Exception) -> bool:
    """判断是否值得重试"""
    if isinstance(exc, RateLimitError):
        return True   # 429：限流
    if isinstance(exc, APIStatusError) and exc.status_code >= 500:
        return True   # 5xx：服务端瞬时故障
    return False


async def with_retry(
    func: Callable,
    *args,
    max_retries: int  = _DEFAULT_MAX_RETRIES,
    base_delay: float = _DEFAULT_BASE_DELAY,
    max_delay: float  = _DEFAULT_MAX_DELAY,
    task_name: str    = "",
    **kwargs
) -> Any:
    """
    带重试的异步函数调用。
    task_name 仅用于日志，不传给被调用的函数。

    用法：
        result = await with_retry(
            client.files.create,
            file=buf, purpose="user_data",
            task_name="妆容图上传"
        )
    """
    last_exc = None
    label    = task_name or getattr(func, "__name__", "unknown")

    for attempt in range(max_retries + 1):
        try:
            return await func(*args, **kwargs)
        except Exception as e:
            last_exc = e

            if not _is_retryable(e):
                raise   # 非限流错误直接抛出，不掩盖 bug

            if attempt >= max_retries:
                logger.error(f"[Retry] {label} 重试 {max_retries} 次后仍失败: {e}")
                raise

            delay = min(base_delay * (2 ** attempt) + random.uniform(0, 1), max_delay)

            if isinstance(e, RateLimitError):
                logger.warning(
                    f"[Retry] {label} 触发限流(429) | "
                    f"第 {attempt + 1}/{max_retries} 次重试 | 等待 {delay:.1f}s"
                )
            else:
                logger.warning(
                    f"[Retry] {label} 服务端错误({getattr(e, 'status_code', '?')}) | "
                    f"第 {attempt + 1}/{max_retries} 次重试 | 等待 {delay:.1f}s"
                )

            await asyncio.sleep(delay)

    raise last_exc  # 防御性兜底，理论上不会到这里


def retryable(
    max_retries: int  = _DEFAULT_MAX_RETRIES,
    base_delay: float = _DEFAULT_BASE_DELAY,
    max_delay: float  = _DEFAULT_MAX_DELAY,
    task_name: str    = "",
):
    """
    装饰器版本，适合装饰整个异步函数。

    用法：
        @retryable(max_retries=3, task_name="视觉分析")
        async def analyze_coser(...):
            ...
    """
    def decorator(func: Callable) -> Callable:
        @functools.wraps(func)
        async def wrapper(*args, **kwargs):
            label    = task_name or func.__name__
            last_exc = None

            for attempt in range(max_retries + 1):
                try:
                    return await func(*args, **kwargs)
                except Exception as e:
                    last_exc = e

                    if not _is_retryable(e):
                        raise

                    if attempt >= max_retries:
                        logger.error(f"[Retry] {label} 重试 {max_retries} 次后仍失败: {e}")
                        raise

                    delay = min(base_delay * (2 ** attempt) + random.uniform(0, 1), max_delay)

                    if isinstance(e, RateLimitError):
                        logger.warning(
                            f"[Retry] {label} 触发限流(429) | "
                            f"第 {attempt + 1}/{max_retries} 次重试 | 等待 {delay:.1f}s"
                        )
                    else:
                        logger.warning(
                            f"[Retry] {label} 服务端错误 | "
                            f"第 {attempt + 1}/{max_retries} 次重试 | 等待 {delay:.1f}s"
                        )

                    await asyncio.sleep(delay)

            raise last_exc

        return wrapper
    return decorator
