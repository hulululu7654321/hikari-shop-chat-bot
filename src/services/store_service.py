# -*- coding: utf-8 -*-
"""
src/services/store_service.py — 门店基础服务
=====================================================================
提供跨模块共用的门店工具函数，供 core 层、modules 层共同调用。

遵守 MASTER v3.0 单向依赖：
  services 层 ← core 层（可调用）
  services 层 ← modules 层（可调用）
  services 层 × 禁止 import core / modules 层

导出函数：
  load_store_shops()               — 多店配置加载（纯函数，无状态）
  invalidate_packages_cache()      — 清除套餐 Redis 缓存
  build_packages_prompt_text()     — 构建套餐注入文本
  get_store_packages_prompt()      — 供 dialogue_engine 注入套餐提示
  push_live2d_motion()             — 推送 Live2D 动作到屏幕端
  push_name_learned()              — 识别到名字后通知屏幕端并缓存套餐
  is_assist_trigger()              — 检测消息是否包含人工服务呼叫关键词
  push_assist_request()            — 触发人工服务呼叫，推屏幕端
  push_danmaku_to_redis()          — 弹幕写入 Redis，触发群体感知广播
=====================================================================
"""

import os
import re
import json
import asyncio
import logging

from src.utils.database import get_db_manager, COL_PACKAGES
from src.utils.socket_manager import get_socket_manager
from src.utils.logger import get_logger

logger = get_logger(__name__)


# ══════════════════════════════════════════════════════════════════
# 店铺配置
# ══════════════════════════════════════════════════════════════════

def _expand_env_vars(text: str) -> str:
    def _replacer(m: re.Match) -> str:
        val = os.getenv(m.group(1), "")
        if not val:
            logger.warning("[StoreService] 环境变量未设置: %s", m.group(1))
        return val
    return re.sub(r'\$\{([^}]+)\}', _replacer, text)


def load_store_shops() -> list:
    """
    多店配置加载，三级 fallback：
    1. STORE_SHOPS_FILE（JSON 文件路径）
    2. STORE_SHOPS（JSON 字符串环境变量）
    3. STORE_KEY + STORE_ID（单店快速配置）
    供 store/api.py 和 manage/api.py 共用，无副作用。
    """
    shops_file = os.getenv("STORE_SHOPS_FILE", "").strip()
    if shops_file:
        if not os.path.exists(shops_file):
            logger.error("[StoreService] STORE_SHOPS_FILE 不存在: %s", shops_file)
        else:
            try:
                with open(shops_file, "r", encoding="utf-8") as f:
                    raw_text = f.read()
                data = json.loads(_expand_env_vars(raw_text))
                if isinstance(data, list):
                    return data
                logger.error("[StoreService] STORE_SHOPS_FILE 根节点必须是 JSON 数组")
            except Exception as e:
                logger.error("[StoreService] STORE_SHOPS_FILE 解析失败: %s", e)
        return []

    raw = os.getenv("STORE_SHOPS", "").strip()
    if raw:
        try:
            return json.loads(raw)
        except Exception:
            logger.error("[StoreService] STORE_SHOPS 格式错误")
            return []

    key = os.getenv("STORE_KEY", "")
    sid = os.getenv("STORE_ID", "hikari_store_default")
    if key:
        return [{"store_id": sid, "store_key": key, "name": "默认店铺"}]
    return []


# ══════════════════════════════════════════════════════════════════
# 套餐缓存
# ══════════════════════════════════════════════════════════════════

async def invalidate_packages_cache() -> None:
    """清除所有用户的套餐注入缓存（hikari:store:packages_injected:*）。"""
    try:
        redis = await get_db_manager().get_redis()
        keys = []
        cursor = 0
        while True:
            cursor, batch = await redis.scan(
                cursor, match="hikari:store:packages_injected:*", count=100
            )
            keys.extend(batch)
            if cursor == 0:
                break
        if keys:
            await redis.delete(*keys)
            logger.info("[StoreService] 清除套餐缓存 | count=%d", len(keys))
    except Exception as e:
        logger.debug("[StoreService] 清除套餐缓存失败，跳过: %s", e)


async def build_packages_prompt_text(store_id: str) -> str:
    """构建指定门店的套餐注入文本（供屏幕端展示和 dialogue_engine 注入）。"""
    try:
        db = get_db_manager().db
        cursor = db[COL_PACKAGES].find(
            {"store_id": store_id, "is_active": True},
            {"_id": 0, "name": 1, "price": 1, "description": 1}
        ).sort("sort_order", 1)
        packages = await cursor.to_list(length=50)
        if not packages:
            return ""
        lines = [f"- {p['name']}（¥{p['price']}）：{p['description']}" for p in packages]
        return "\n".join(lines)
    except Exception:
        return ""


async def get_store_packages_prompt(user_id: str) -> str:
    """
    供 dialogue_engine 调用，返回当前用户的套餐注入文本。
    双重守卫：用户在店 AND 套餐缓存存在，两者缺一返回空字符串。
    """
    try:
        if os.getenv("STORE_ENABLED", "false").lower() != "true":
            return ""
        redis = await get_db_manager().get_redis()
        instore_raw = await redis.get(f"hikari:store:instore:{user_id}")
        if not instore_raw:
            return ""
        cached = await redis.get(f"hikari:store:packages_injected:{user_id}")
        if not cached:
            return ""
        return cached.decode() if isinstance(cached, bytes) else cached
    except Exception:
        return ""


# ══════════════════════════════════════════════════════════════════
# 屏幕端推送
# ══════════════════════════════════════════════════════════════════

async def push_live2d_motion(user_id: str, motion_type: str) -> None:
    """
    对话完成后向屏幕端推送 Live2D 动作指令。
    用户不在店时静默跳过。
    由 orchestrator 在每轮对话结束后调用。
    """
    try:
        redis = await get_db_manager().get_redis()
        instore_raw = await redis.get(f"hikari:store:instore:{user_id}")
        if not instore_raw:
            return
        store_id = instore_raw.decode() if isinstance(instore_raw, bytes) else instore_raw
        motion_map = {
            "talk":      {"motion": "talk",      "expression": None,    "priority": 1},
            "happy":     {"motion": "happy",     "expression": "smile", "priority": 2},
            "sad":       {"motion": "sad",       "expression": "sad",   "priority": 2},
            "angry":     {"motion": "angry",     "expression": "angry", "priority": 2},
            "broadcast": {"motion": "broadcast", "expression": "smile", "priority": 3},
            "idle":      {"motion": "idle",      "expression": None,    "priority": 0},
            "wave":      {"motion": "wave",      "expression": "smile", "priority": 2},
        }
        params = motion_map.get(motion_type, motion_map["talk"])
        await get_socket_manager().send_to_user(f"store_screen_{store_id}", {
            "type":       "live2d_motion",
            "motion":     params["motion"],
            "expression": params["expression"],
            "priority":   params["priority"],
        })
        logger.debug("[StoreService] Live2D motion=%s | user=%s", motion_type, user_id[:8])
    except Exception as e:
        logger.debug("[StoreService] Live2D 推送失败，静默跳过: %s", e)


async def push_name_learned(user_id: str, name: str) -> None:
    """
    fact_extractor 识别到用户名字后调用。
    推屏幕端 Live2D 动作 + 缓存套餐文本供 dialogue_engine 注入。
    用户不在店时静默跳过。
    """
    try:
        if not name or "匿名" in name:
            return
        redis = await get_db_manager().get_redis()
        instore_raw = await redis.get(f"hikari:store:instore:{user_id}")
        if not instore_raw:
            return
        store_id = instore_raw.decode() if isinstance(instore_raw, bytes) else instore_raw
        await get_socket_manager().send_to_user(f"store_screen_{store_id}", {
            "type":               "store_event",
            "event":              "name_learned",
            "user_id_masked":     user_id[:8] + "****",
            "user_name":          name,
            "live2d_motion":      "happy",
            "live2d_expression":  "smile",
        })
        packages_text = await build_packages_prompt_text(store_id)
        if packages_text:
            instore_ttl = int(os.getenv("STORE_INSTORE_TTL", 240)) * 60
            await redis.setex(
                f"hikari:store:packages_injected:{user_id}",
                instore_ttl,
                packages_text,
            )
            logger.info("[StoreService] 套餐文本已缓存 | user=%s", user_id[:8])
        logger.info("[StoreService] 名字同步到屏幕端 | name=%s | user=%s", name, user_id[:8])
    except Exception as e:
        logger.debug("[StoreService] push_name_learned 失败，静默跳过: %s", e)


# ══════════════════════════════════════════════════════════════════
# 人工服务呼叫 & 弹幕群体感知
# ══════════════════════════════════════════════════════════════════

_ASSIST_TRIGGERS = [
    "叫一下店员", "叫店员", "叫一下工作人员", "叫工作人员",
    "需要帮助", "需要人工", "需要人工服务",
    "请人来", "有人吗", "有人在吗",
    "麻烦过来一下", "过来一下", "请过来",
    "我需要帮忙", "帮我叫一下",
]


def is_assist_trigger(text: str) -> bool:
    """检测消息是否包含人工服务呼叫关键词，供 chat_handler 导入。"""
    return any(kw in text for kw in _ASSIST_TRIGGERS)


async def push_assist_request(user_id: str, display_name: str) -> None:
    """客户在对话中触发人工服务关键词时推送屏幕端，用户不在店时静默跳过。"""
    try:
        if os.getenv("STORE_ENABLED", "false").lower() != "true":
            return
        redis = await get_db_manager().get_redis()
        instore_raw = await redis.get(f"hikari:store:instore:{user_id}")
        if not instore_raw:
            return
        store_id = instore_raw.decode() if isinstance(instore_raw, bytes) else instore_raw
        name = display_name or "有位客人"
        tts_text = f"{name}需要帮助，请店员过去看一下喵～"
        await get_socket_manager().send_to_user(f"store_screen_{store_id}", {
            "type": "store_event",
            "event": "assist_requested",
            "display_name": name,
            "tts_text": tts_text,
            "live2d_motion": "wave",
            "live2d_expression": "neutral",
        })
        logger.info("[StoreService] 人工呼叫推送 | user=%s | name=%s", user_id[:8], name)
    except Exception as e:
        logger.debug("[StoreService] push_assist_request 失败，静默跳过: %s", e)


async def push_danmaku_to_redis(store_id: str, user_name: str, text: str) -> None:
    """弹幕写入 Redis List（TTL 90秒滑动窗口），检测是否触发群体感知。"""
    try:
        import time
        redis = await get_db_manager().get_redis()
        key = f"hikari:store:danmaku:{store_id}"
        coolkey = f"hikari:store:danmaku_cool:{store_id}"

        entry = json.dumps({"name": user_name, "text": text, "ts": int(time.time())})
        await redis.lpush(key, entry)
        await redis.ltrim(key, 0, 29)
        await redis.expire(key, 90)

        if await redis.exists(coolkey):
            return

        raw_list = await redis.lrange(key, 0, -1)
        now = int(time.time())
        window = []
        for r in raw_list:
            try:
                item = json.loads(r)
                if now - item["ts"] <= 90:
                    window.append(item)
            except Exception:
                pass

        if len(window) < 10:
            recent = [i for i in window if now - i["ts"] <= 60]
            unique_users = len(set(i["name"] for i in recent))
            if len(recent) < 6 or unique_users < 4:
                return

        asyncio.create_task(_analyze_danmaku_and_reply(store_id, window, redis, coolkey))

    except Exception:
        logger.warning("[StoreService] 弹幕写入失败: %s", traceback.format_exc())


async def _analyze_danmaku_and_reply(store_id: str, window: list, redis, coolkey: str) -> None:
    """LLM 判断弹幕主题相近后生成希卡莉广播回复（内部函数）。"""
    try:
        lock_key = f"hikari:store:danmaku_lock:{store_id}"
        acquired = await redis.setnx(lock_key, "1")
        if not acquired:
            return
        await redis.expire(lock_key, 30)

        texts = [f"{i['name']}：{i['text']}" for i in window[:8]]
        danmaku_str = "\n".join(texts)

        from openai import AsyncOpenAI
        client = AsyncOpenAI(
            api_key=os.getenv("DOUBAO_API_KEY", ""),
            base_url=os.getenv("DOUBAO_MAIN_EP", "https://ark.cn-beijing.volces.com/api/v3"),
        )
        model = os.getenv("LLM_EXTRACT_MODEL", "")

        judge_resp = await client.chat.completions.create(
            model=model,
            max_tokens=20,
            temperature=0.0,
            messages=[{"role": "user", "content": (
                f"以下是店内顾客发的弹幕消息：\n{danmaku_str}\n\n"
                "这些弹幕是否在讨论同一个话题或表达相近的情绪？"
                "只回答 yes 或 no，不要解释。"
            )}]
        )
        if not judge_resp.choices:
            return
        verdict = judge_resp.choices[0].message.content.strip().lower()
        if "yes" not in verdict:
            return

        reply_resp = await client.chat.completions.create(
            model=model,
            max_tokens=60,
            temperature=0.8,
            messages=[{"role": "user", "content": (
                f"店内顾客在聊：\n{danmaku_str}\n\n"
                "你是店铺虚拟助手希卡莉，用一句可爱的话回应他们，不超过20字，结尾加喵～"
            )}]
        )
        if not reply_resp.choices:
            return
        reply_text = reply_resp.choices[0].message.content.strip()

        await get_socket_manager().send_to_user(f"store_screen_{store_id}", {
            "type": "store_broadcast",
            "tts_text": reply_text,
            "live2d_motion": "broadcast",
            "live2d_expression": "smile",
        })
        await redis.setex(coolkey, 120, "1")
        logger.info("[StoreService] 弹幕群体广播 | store=%s | reply=%s", store_id, reply_text)

    except Exception as e:
        logger.warning("[StoreService] 弹幕分析失败: %s", e)
    finally:
        try:
            await redis.delete(f"hikari:store:danmaku_lock:{store_id}")
        except Exception:
            pass
