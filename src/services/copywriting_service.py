# -*- coding: utf-8 -*-
"""
二创文案服务

工作流：
【第一轮】用户发图 + 触发词
  → 读磁盘拿所有图 base64（vision_helper 已压缩到 512px）
  → 多张拼网格大图（N 张 → 1 次上传，节省 token）
  → 上传网格图拿 grid_fid，连同状态存入 Redis
  → 推送询问气泡

【第二轮】等待参考图阶段，三种情况：
  A. 用户发了一张图 → 校验是否文案截图 → 是则附参考图生成，否则自由发挥
  B. 用户发了纯文字 → 语义判断是否取消 → 是则清 Redis，否则继续对话
  C. 10 分钟超时 → Redis 自动过期
"""

import os
import re
import io
import math
import base64
import asyncio
import json
import traceback
from datetime import datetime, timezone

from openai import AsyncOpenAI

from src.utils.logger import get_logger
from src.utils.database import get_db_manager, COL_MESSAGES, COL_USERS
from src.utils.socket_manager import get_socket_manager
from src.utils.vision_helper import get_vision_helper
from src.utils.api_retry import with_retry
from src.core.hikari_prompts import (
    build_copywriting_prompt,
    COPYWRITING_REF_CHECK_PROMPT,
    COPYWRITING_CANCEL_CHECK_PROMPT,
)

logger = get_logger(__name__)

# ── Redis Key（对齐文档规范，单 key 存 state+fids JSON）──────────────────────
_STATE_KEY = "hikari:copy:state:{user_id}"
_TTL = 600  # 10 分钟

# ── 触发词 ────────────────────────────────────────────────────────────────────

COPYWRITING_PLATFORM_TRIGGERS: dict[str, list[str]] = {
    "xiaohongshu": ["小红书文案", "红书文案", "写小红书", "小红书配文"],
    "weibo":       ["微博文案", "写微博", "微博配文"],
    "bilibili":    ["B站文案", "b站文案", "写动态", "B站动态文案"],
}
COPYWRITING_GENERIC_TRIGGERS = ["帮我写文案", "写个文案", "配文", "发图文案", "出图文案"]
PLATFORM_LABELS: dict[str, str] = {
    "xiaohongshu": "小红书",
    "weibo":       "微博",
    "bilibili":    "B站",
}

_ALL_TRIGGERS = [
    kw for kws in COPYWRITING_PLATFORM_TRIGGERS.values() for kw in kws
] + COPYWRITING_GENERIC_TRIGGERS


# ══════════════════════════════════════════
# 公开判断函数
# ══════════════════════════════════════════

def is_copywriting_trigger(text: str, image_ids: list) -> tuple[bool, str, str]:
    """
    判断是否触发第一轮。必须：至少1个 image_id + 消息含触发词。
    返回 (is_triggered, platform, user_hint)
    """
    if not image_ids:
        return False, "", ""
    for platform, kws in COPYWRITING_PLATFORM_TRIGGERS.items():
        for kw in kws:
            if kw in text:
                return True, platform, text.replace(kw, "").strip()
    for kw in COPYWRITING_GENERIC_TRIGGERS:
        if kw in text:
            return True, "xiaohongshu", text.replace(kw, "").strip()
    return False, "", ""


async def is_waiting_ref(user_id: str) -> bool:
    """当前用户是否处于等待参考图阶段。"""
    state = await _get_state(user_id)
    return state.get("state") == "waiting_ref"


# ══════════════════════════════════════════
# 内部工具
# ══════════════════════════════════════════

def _make_client() -> AsyncOpenAI:
    return AsyncOpenAI(
        base_url="https://ark.cn-beijing.volces.com/api/v3",
        api_key=os.getenv("DOUBAO_API_KEY"),
    )


def _make_grid_image(base64_list: list[str]) -> str:
    """
    多张 base64 图拼网格，返回拼好的 base64 JPEG。
    单张直接返回。cell 尺寸取所有图中最大宽/高，小图居中留黑边。
    """
    if len(base64_list) == 1:
        return base64_list[0]
    try:
        from PIL import Image

        imgs = [
            Image.open(io.BytesIO(base64.b64decode(b))).convert("RGB")
            for b in base64_list
        ]
        # 防止超大图耗尽内存：单张限 4096x4096
        _MAX_DIM = 4096
        imgs = [
            img.resize(
                (min(img.width, _MAX_DIM), min(img.height, _MAX_DIM)),
                Image.LANCZOS,
            ) if img.width > _MAX_DIM or img.height > _MAX_DIM else img
            for img in imgs
        ]
        n = len(imgs)
        cols = math.ceil(math.sqrt(n))
        rows = math.ceil(n / cols)
        cell_w = max(img.width for img in imgs)
        cell_h = max(img.height for img in imgs)

        grid = Image.new("RGB", (cols * cell_w, rows * cell_h), (20, 20, 20))
        for idx, img in enumerate(imgs):
            r, c = divmod(idx, cols)
            x = c * cell_w + (cell_w - img.width) // 2
            y = r * cell_h + (cell_h - img.height) // 2
            grid.paste(img, (x, y))

        buf = io.BytesIO()
        grid.save(buf, format="JPEG", quality=85, optimize=True)
        result = base64.b64encode(buf.getvalue()).decode()
        logger.info(f"copywriting: grid {n}imgs → {cols}x{rows} cell={cell_w}x{cell_h}px")
        return result
    except ImportError:
        logger.error("copywriting: Pillow not installed, using first image")
        return base64_list[0]
    except Exception as e:
        logger.error(f"copywriting: grid failed: {e}, using first image")
        return base64_list[0]


async def _upload_b64(client: AsyncOpenAI, b64: str, name: str = "img.jpg") -> str:
    """上传 base64 图到豆包文件服务，返回 file_id，失败返回空字符串。"""
    try:
        buf = io.BytesIO(base64.b64decode(b64))
        buf.name = name
        uploaded = await with_retry(
            client.files.create,
            file=buf,
            purpose="user_data",
            task_name="图片上传",
        )
        return uploaded.id
    except Exception as e:
        logger.error(f"copywriting: file upload failed: {e}")
        return ""


# ── Redis 状态机（单 key 存 state + grid_fid）────────────────────────────────

async def _get_state(user_id: str) -> dict:
    try:
        redis = await get_db_manager().get_redis()
        raw = await redis.get(_STATE_KEY.format(user_id=user_id))
        return json.loads(raw) if raw else {}
    except Exception:
        return {}


async def _save_state(user_id: str, state: dict) -> None:
    try:
        redis = await get_db_manager().get_redis()
        await redis.setex(_STATE_KEY.format(user_id=user_id), _TTL, json.dumps(state))
    except Exception as e:
        logger.warning(f"copywriting: state save failed: {e}")


async def _clear_state(user_id: str) -> None:
    try:
        redis = await get_db_manager().get_redis()
        await redis.delete(_STATE_KEY.format(user_id=user_id))
    except Exception:
        pass


# ── 轻量 LLM 判断 ─────────────────────────────────────────────────────────────

async def _check_is_ref_screenshot(client: AsyncOpenAI, ref_b64: str) -> bool:
    """判断图片是否是含大量文字的截图（文案参考）。max_tokens=10。"""
    try:
        resp = await with_retry(
            client.chat.completions.create,
            model=os.getenv("DOUBAO_MAIN_EP", ""),
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{ref_b64}"}},
                        {"type": "text", "text": COPYWRITING_REF_CHECK_PROMPT},
                    ],
                }
            ],
            temperature=0.0,
            max_tokens=10,
            task_name="参考图校验",
        )
        answer = resp.choices[0].message.content.strip().upper()
        result = answer.startswith("Y")
        logger.info(f"copywriting: ref check | answer={answer!r} result={result}")
        return result
    except Exception as e:
        logger.warning(f"copywriting: ref check failed, free style: {e}")
        return False


async def _check_is_cancel(client: AsyncOpenAI, text: str) -> bool:
    """判断纯文字消息是否是取消意图。max_tokens=10。"""
    try:
        prompt = COPYWRITING_CANCEL_CHECK_PROMPT.format(text=text)
        resp = await with_retry(
            client.chat.completions.create,
            model=os.getenv("DOUBAO_MAIN_EP", ""),
            messages=[{"role": "user", "content": prompt}],
            temperature=0.0,
            max_tokens=10,
            task_name="取消意图判断",
        )
        answer = resp.choices[0].message.content.strip().upper()
        result = answer.startswith("Y")
        logger.info(f"copywriting: cancel check | answer={answer!r} is_cancel={result}")
        return result
    except Exception as e:
        logger.warning(f"copywriting: cancel check failed, pass through: {e}")
        return False


# ── 文案生成 ──────────────────────────────────────────────────────────────────

async def _generate_copy(
    client: AsyncOpenAI,
    grid_fid: str,
    platform: str,
    user_hint: str,
    ref_fid: str = "",
) -> str:
    """responses.create + file_id 生成文案，失败降级去掉参考图再试。"""
    model = os.getenv("DOUBAO_MAIN_EP", "")
    prompt = build_copywriting_prompt(platform=platform, user_hint=user_hint, has_ref=bool(ref_fid))

    content: list = [{"type": "input_image", "file_id": grid_fid}]
    if ref_fid:
        content.append({"type": "input_image", "file_id": ref_fid})
    content.append({"type": "input_text", "text": prompt})

    try:
        resp = await with_retry(
            client.responses.create,
            model=model,
            input=[{"role": "user", "content": content}],
            temperature=0.8,
            max_output_tokens=600,
            extra_body={"thinking": {"type": "disabled"}},
            task_name="文案生成",
        )
        result = resp.output_text.strip()
        logger.info(
            f"copywriting: copy generated | platform={platform} ref={'yes' if ref_fid else 'no'} len={len(result)}"
        )
        return result
    except Exception as e:
        logger.error(f"copywriting: generation failed, falling back: {e}")
        if not ref_fid:
            return ""
        # 降级：去掉参考图
        try:
            resp2 = await with_retry(
                client.responses.create,
                model=model,
                input=[{"role": "user", "content": [
                    {"type": "input_image", "file_id": grid_fid},
                    {"type": "input_text", "text": prompt},
                ]}],
                temperature=0.8,
                max_output_tokens=600,
                extra_body={"thinking": {"type": "disabled"}},
                task_name="文案生成降级",
            )
            result2 = resp2.output_text.strip()
            logger.info(f"copywriting: fallback done | len={len(result2)}")
            return result2
        except Exception as e2:
            logger.error(f"copywriting: fallback also failed: {e2}")
            return ""


# ── 推送工具 ──────────────────────────────────────────────────────────────────

async def _send_bubble(db, sm, session_id: str, user_id: str, text: str) -> str:
    """落盘 assistant 气泡并推送，返回 msg_id。"""
    res = await db[COL_MESSAGES].insert_one(
        {
            "session_id": session_id,
            "user_id": user_id,
            "role": "assistant",
            "content": text,
            "voice_status": "none",
            "created_at": datetime.now(timezone.utc),
        }
    )
    mid = str(res.inserted_id)
    # 从 DB 读真实好感度，避免硬编码导致前端好感度跳回初始值
    user_doc = await db[COL_USERS].find_one({"user_id": user_id}, {"affinity": 1, "mood": 1})
    real_affinity = user_doc.get("affinity", 10) if user_doc else 10
    real_mood = user_doc.get("mood", "neutral") if user_doc else "neutral"
    await sm.send_to_user(
        session_id,
        {
            "type": "text",
            "msg_id": mid,
            "content": text,
            "affinity": real_affinity,
            "mood": real_mood,
            "voice_status": "none",
        },
    )
    await sm.send_to_user(session_id, {"type": "voice", "msg_id": mid, "voice_status": "none"})
    return mid


async def _push_result(
    db, sm, session_id: str, user_id: str, platform: str, label: str, copy_text: str
) -> None:
    """落盘文案气泡并推送 copywriting_result 事件。"""
    if not copy_text:
        await sm.send_to_user(
            session_id,
            {
                "type": "copywriting_result",
                "success": False,
                "msg_id": "err",
                "error": "文案生成失败了喵，稍后再试？",
            },
        )
        return
    res = await db[COL_MESSAGES].insert_one(
        {
            "session_id": session_id,
            "user_id": user_id,
            "role": "assistant",
            "content": copy_text,
            "copywriting_platform": platform,
            "voice_status": "none",
            "created_at": datetime.now(timezone.utc),
        }
    )
    new_mid = str(res.inserted_id)
    await sm.send_to_user(
        session_id,
        {
            "type": "copywriting_result",
            "msg_id": new_mid,
            "success": True,
            "platform": platform,
            "platform_label": label,
            "content": copy_text,
        },
    )
    logger.info(f"copywriting: result pushed | len={len(copy_text)} msg_id={new_mid[:8]}")


# ══════════════════════════════════════════
# 第一轮主任务
# ══════════════════════════════════════════

async def handle_copywriting_round1(
    user_id: str,
    session_id: str,
    image_ids: list,
    platform: str,
    user_hint: str,
    msg_id: str,
) -> None:
    try:
        vh = get_vision_helper()
        sm = get_socket_manager()
        db = get_db_manager().db
        client = _make_client()

        # 读磁盘（优先 _small.jpg，省 token）
        b64_list: list[str] = []
        for iid in image_ids:
            small_path = os.path.join(vh.upload_root, user_id, f"{iid}_small.jpg")
            path = (
                small_path
                if os.path.exists(small_path)
                else os.path.join(vh.upload_root, user_id, f"{iid}.jpg")
            )
            b64 = vh.get_image_base64(path)
            if b64:
                b64_list.append(b64)

        if not b64_list:
            await sm.send_to_user(
                session_id,
                {
                    "type": "copywriting_result",
                    "msg_id": msg_id,
                    "success": False,
                    "error": "找不到图片了喵，重新发一下？",
                },
            )
            return

        # 拼网格（CPU 密集，扔线程池）
        grid_b64 = await asyncio.get_event_loop().run_in_executor(None, _make_grid_image, b64_list)

        # 上传网格图 → file_id
        grid_fid = await _upload_b64(client, grid_b64, "grid.jpg")
        if not grid_fid:
            await sm.send_to_user(
                session_id,
                {
                    "type": "copywriting_result",
                    "msg_id": msg_id,
                    "success": False,
                    "error": "图片上传失败了喵，稍后再试？",
                },
            )
            return

        # 存 Redis（state + grid_fid 合并为单 key）
        await _save_state(
            user_id,
            {
                "state": "waiting_ref",
                "grid_fid": grid_fid,
                "platform": platform,
                "user_hint": user_hint,
                "session_id": session_id,
                "count": len(b64_list),
            },
        )

        # 推送询问气泡
        label = PLATFORM_LABELS.get(platform, "小红书")
        await _send_bubble(
            db,
            sm,
            session_id,
            user_id,
            f"本喵收到 {len(b64_list)} 张图了，要写{label}文案对吧喵。"
            f"告诉本喵这组图的主题是什么？"
            f"比如「银狼cos」「今天漫展集邮」「和朋友吃了顿好的」，随便说喵。"
            f"有参考文案截图的话一起发过来，没有就直接说主题就好喵～",
        )
        logger.info(f"copywriting: round1 done | {len(b64_list)}imgs grid_fid={grid_fid[:16]}")

    except Exception:
        logger.error(f"copywriting: round1 crashed: {traceback.format_exc()}")
        try:
            await get_socket_manager().send_to_user(
                session_id,
                {
                    "type": "copywriting_result",
                    "msg_id": msg_id,
                    "success": False,
                    "error": "本喵的文案系统抽风了喵",
                },
            )
        except Exception:
            pass


# ══════════════════════════════════════════
# 第二轮主任务
# ══════════════════════════════════════════

async def handle_copywriting_round2(
    user_id: str,
    session_id: str,
    ref_image_id: str,
    user_text: str,
    msg_id: str,
    client_msg_id: str = "",
    image_ids_hint: list | None = None,
) -> bool:
    """
    返回 True  = 本函数已处理，pipeline 不再继续。
    返回 False = 普通聊天，pipeline 继续走正常对话流。
    image_ids_hint: 本次消息携带的图片列表，检测是否想重新触发 Round1。
    """
    if image_ids_hint is None:
        image_ids_hint = []

    _should_clear = True
    try:
        vh = get_vision_helper()
        sm = get_socket_manager()
        db = get_db_manager().db
        client = _make_client()

        # 用户在等待状态下又发了多图 + 触发词 → 重新开始 Round1
        if len(image_ids_hint) >= 2 and user_text:
            if any(kw in user_text for kw in _ALL_TRIGGERS):
                logger.info(f"copywriting: re-trigger detected, clearing state | user={user_id[:8]}")
                await _clear_state(user_id)
                _should_clear = False
                return False

        state = await _get_state(user_id)
        if not state:
            await _send_bubble(
                db, sm, session_id, user_id,
                "等太久状态过期了喵，重新发图片和触发词吧～",
            )
            return True

        grid_fid = state.get("grid_fid", "")
        if not grid_fid:
            await _send_bubble(
                db, sm, session_id, user_id,
                "等太久状态过期了喵，重新发图片和触发词吧～",
            )
            return True

        platform = state.get("platform", "xiaohongshu")
        label = PLATFORM_LABELS.get(platform, "小红书")

        # 落盘用户第二轮消息（带 client_msg_id 供前端精确匹配乐观气泡）
        if user_text or ref_image_id:
            await db[COL_MESSAGES].insert_one(
                {
                    "session_id": session_id,
                    "user_id": user_id,
                    "role": "user",
                    "content": user_text or "",
                    "image_id": ref_image_id or "",
                    "client_msg_id": client_msg_id,
                    "created_at": datetime.now(timezone.utc),
                }
            )

        # 取消检测（纯文字才判断）
        if not ref_image_id and user_text:
            is_cancel = await _check_is_cancel(client, user_text)
            if is_cancel:
                await _clear_state(user_id)
                _should_clear = False
                await _send_bubble(
                    db, sm, session_id, user_id,
                    "好的，取消了喵。要重新写的话，把图片都发过来再说触发词就行～",
                )
                return True

        # 合并主题
        user_hint = user_text.strip() if user_text.strip() else state.get("user_hint", "")

        # 处理参考图（可选）
        ref_fid = ""
        ref_valid = False
        if ref_image_id:
            small_path = os.path.join(vh.upload_root, user_id, f"{ref_image_id}_small.jpg")
            path = (
                small_path
                if os.path.exists(small_path)
                else os.path.join(vh.upload_root, user_id, f"{ref_image_id}.jpg")
            )
            ref_b64 = vh.get_image_base64(path) or ""
            if ref_b64:
                ref_valid = await _check_is_ref_screenshot(client, ref_b64)
                if ref_valid:
                    ref_fid = await _upload_b64(client, ref_b64, "ref.jpg")
                else:
                    await _send_bubble(
                        db, sm, session_id, user_id,
                        "这张好像不是文案截图喵，本喵照你说的主题自由发挥了～",
                    )

        # 没有主题描述也没有参考图 → 放行走普通聊天
        if not user_hint and not ref_image_id:
            _should_clear = False
            return False

        # 推送生成中提示
        _hint_display = user_hint[:30] + "…" if len(user_hint) > 30 else user_hint
        if user_hint and ref_valid:
            hint_text = f"收到主题「{_hint_display}」，本喵照着参考风格给你写{label}文案，稍等喵～"
        elif user_hint:
            hint_text = f"收到主题「{_hint_display}」，本喵给你写{label}文案，稍等喵～"
        elif ref_valid:
            hint_text = f"本喵照着参考风格给你写{label}文案，稍等喵～"
        else:
            hint_text = f"好，本喵给你写{label}文案，稍等喵～"
        await _send_bubble(db, sm, session_id, user_id, hint_text)

        # 生成文案
        copy_text = await _generate_copy(
            client, grid_fid, platform, user_hint, ref_fid
        )
        await _push_result(db, sm, session_id, user_id, platform, label, copy_text)
        return True

    except Exception:
        logger.error(f"copywriting: round2 crashed: {traceback.format_exc()}")
        try:
            await get_socket_manager().send_to_user(
                session_id,
                {
                    "type": "copywriting_result",
                    "msg_id": msg_id,
                    "success": False,
                    "error": "本喵的文案系统抽风了喵",
                },
            )
        except Exception:
            pass
        return True
    finally:
        if _should_clear:
            await _clear_state(user_id)
