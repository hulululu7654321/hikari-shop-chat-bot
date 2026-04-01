# -*- coding: utf-8 -*-
"""
主动推送 Temporal Workflow

PushSchedulerWorkflow 作为 Cron Workflow 每天在 PUSH_CRON_EXPR 定义的时间执行一次。
每次执行一个 Activity：run_daily_push（全量扫描活跃用户并尝试推送）。

推送防骚扰策略（延续旧 push_worker 逻辑）：
1. 每用户每 24 小时最多推送 1 次（hikari:push:lock:{user_id}）
2. 用户最近 6 小时内有活跃对话不推
3. 已推送过的条目 72 小时内不重复推（hikari:pushed:{user_id}）
4. 用户当前不在线（无 WS 连接）则跳过
"""

from __future__ import annotations

import os
import asyncio
import traceback
from datetime import datetime, timedelta, timezone

from temporalio import activity, workflow
from temporalio.common import RetryPolicy

from src.utils.logger import get_logger

logger = get_logger(__name__)


# ════════════════════════════════════════════════════════════════════════════
# Activity
# ════════════════════════════════════════════════════════════════════════════

@activity.defn(name="run_daily_push")
async def run_daily_push() -> None:
    """
    全量扫描最近 30 天活跃用户，逐一调用推送逻辑。
    串行执行，防止同时发起大量 LLM 调用。
    超时 3600s，重试 0 次（失败不重试，等下一次 Cron）。
    """
    from openai import AsyncOpenAI
    from src.utils.database import get_db_manager, COL_USERS, COL_MESSAGES
    from src.utils.socket_manager import get_socket_manager
    from src.services.rag_service import get_rag_service
    from src.utils.api_retry import with_retry

    db_manager = get_db_manager()
    db = db_manager.db
    redis = await db_manager.get_redis()

    # 取最近 30 天有过对话的用户
    cutoff_30d = datetime.now(timezone.utc) - timedelta(days=30)
    cursor = db[COL_USERS].find(
        {"last_active_at": {"$gte": cutoff_30d}},
        {"user_id": 1, "user_name": 1, "affinity": 1, "mood": 1},
    )
    users = await cursor.to_list(length=10000)
    logger.info(f"run_daily_push: scanning {len(users)} users")

    sm = get_socket_manager()
    rag = get_rag_service()
    pushed_count = 0

    for u in users:
        user_id = u.get("user_id", "")
        if not user_id:
            continue
        try:
            pushed = await _push_one_user(
                user_id=user_id,
                user_doc=u,
                db=db,
                redis=redis,
                sm=sm,
                rag=rag,
            )
            if pushed:
                pushed_count += 1
        except Exception:
            logger.error(f"run_daily_push: user push failed | user={user_id[:8]} {traceback.format_exc()}")
        # 每个用户间隔，防 WS 广播风暴
        await asyncio.sleep(0.1)

    logger.info(f"run_daily_push: done | {pushed_count}/{len(users)} pushed")


async def _push_one_user(
    user_id: str,
    user_doc: dict,
    db,
    redis,
    sm,
    rag,
) -> bool:
    """
    为单个用户执行推送逻辑。
    返回 True 表示本次成功推送。
    """
    from src.utils.database import COL_MESSAGES

    # 1. 24h 冷却检查（hikari:push:lock:{user_id}）
    lock_key = f"hikari:push:lock:{user_id}"
    if await redis.get(lock_key):
        return False

    # 2. 最近 6h 活跃检查
    cutoff_6h = datetime.now(timezone.utc) - timedelta(hours=6)
    recent = await db[COL_MESSAGES].find_one(
        {"user_id": user_id, "role": "user", "created_at": {"$gte": cutoff_6h}},
        {"_id": 1},
    )
    if recent:
        return False

    # 3. 在线检查（无 WS 连接则跳过）
    if not await sm.is_user_online(user_id):
        return False
    session_id = await sm.get_session_id_for_user(user_id)

    # 4. RAG 匹配知识条目
    matches = await rag.match_for_user(user_id=user_id, top_k=3)
    if not matches:
        return False

    # 5. 过滤已推送条目（hikari:pushed:{user_id}，72h）
    pushed_key = f"hikari:pushed:{user_id}"
    already_pushed_raw = await redis.smembers(pushed_key)
    already_pushed = {
        m.decode() if isinstance(m, bytes) else m for m in already_pushed_raw
    }
    fresh_matches = [
        m for m in matches if m.get("entry_id", m.get("title", "")) not in already_pushed
    ]
    if not fresh_matches:
        return False

    best = fresh_matches[0]

    # 6. 生成希卡莉口吻推送文案
    push_text = await _generate_push_text(user_doc, best)
    if not push_text:
        return False

    # 7. 落盘 + WS 推送
    from src.utils.database import COL_MESSAGES
    now = datetime.now(timezone.utc)
    res = await db[COL_MESSAGES].insert_one(
        {
            "session_id": session_id,
            "user_id": user_id,
            "role": "assistant",
            "content": push_text,
            "voice_status": "none",
            "is_proactive_push": True,
            "kb_category": best.get("category", ""),
            "created_at": now,
        }
    )
    mid = str(res.inserted_id)

    await sm.send_to_user(
        session_id,
        {
            "type": "text",
            "msg_id": mid,
            "content": push_text,
            "affinity": user_doc.get("affinity", 10),
            "mood": "happy",
            "voice_status": "none",
        },
    )
    await sm.send_to_user(
        session_id,
        {"type": "voice", "msg_id": mid, "voice_status": "none"},
    )

    # 8. 记录推送状态
    await redis.setex(lock_key, 86400, "1")                # 24h 冷却锁
    await redis.sadd(pushed_key, best.get("entry_id", best.get("title", mid)))
    await redis.expire(pushed_key, 86400 * 3)              # 已推条目保留 72h

    logger.info(f"_push_one_user: pushed | user={user_id[:8]} entry={best.get('title', '')[:20]}")
    return True


async def _generate_push_text(user_doc: dict, kb_entry: dict) -> str:
    """
    用豆包生成希卡莉口吻的推送文案（max_tokens=200，不联网）。
    失败时直接截取知识条目内容作为降级。
    """
    from openai import AsyncOpenAI
    from src.utils.api_retry import with_retry

    user_name = user_doc.get("user_name", "宝宝")
    affinity = user_doc.get("affinity", 10)
    call_name = "亲爱的" if affinity >= 70 else ("宝宝" if affinity >= 30 else user_name)

    content = kb_entry.get("content", "")
    category = kb_entry.get("category", "")
    meta = kb_entry.get("metadata", {})

    meta_hint = ""
    for k in ["价格", "时间", "地点", "链接"]:
        if meta.get(k):
            meta_hint += f"，{k}是{meta[k]}"

    prompt = (
        f"你是希卡莉喵，正在主动给用户发一条消息，向他们推荐以下信息：\n\n"
        f"信息内容：{content}{meta_hint}\n"
        f"信息类型：{category}\n"
        f"用户称呼：{call_name}\n\n"
        "要求：\n"
        "- 50字以内，口吻娇憨自然，像朋友在分享消息，不要像广告\n"
        "- 自称'本喵'，结尾可以带一个小问句邀请用户回应\n"
        "- 严禁 Markdown，严禁 emoji（用文字表情代替）\n"
        "- 只输出推送文案，不要其他内容"
    )
    try:
        client = AsyncOpenAI(
            base_url="https://ark.cn-beijing.volces.com/api/v3",
            api_key=os.getenv("DOUBAO_API_KEY"),
        )
        resp = await with_retry(
            client.chat.completions.create,
            model=os.getenv("DOUBAO_MAIN_EP", ""),
            messages=[{"role": "user", "content": prompt}],
            temperature=0.8,
            max_tokens=200,
            extra_body={"thinking": {"type": "disabled"}},
            task_name="推送文案生成",
        )
        return resp.choices[0].message.content.strip()
    except Exception as e:
        logger.warning(f"_generate_push_text: failed, using fallback | {e}")
        return content[:100]


# ════════════════════════════════════════════════════════════════════════════
# Workflow
# ════════════════════════════════════════════════════════════════════════════

@workflow.defn(name="PushSchedulerWorkflow")
class PushSchedulerWorkflow:
    """
    Cron Workflow，调度表达式由 PUSH_CRON_EXPR 环境变量控制（UTC 时间）。
    默认：0 2 * * *（UTC 02:00 = 北京时间 10:00）。
    单 activity：run_daily_push，超时 3600s，重试 0 次。
    """

    @workflow.run
    async def run(self) -> None:
        task_queue = "hikari-main"

        await workflow.execute_activity(
            run_daily_push,
            task_queue=task_queue,
            start_to_close_timeout=timedelta(seconds=3600),
            retry_policy=RetryPolicy(maximum_attempts=1),  # 不重试
        )
