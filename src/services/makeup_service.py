# -*- coding: utf-8 -*-
"""
妆容分析服务

工作流：
1. 轻量校验：图片是否和 cosplay 妆容相关（max_output_tokens=10）
2. 是：视觉分析 + 联网搜索角色妆容参考 → 输出分析报告 → 希卡莉口吻转换
3. 否：提示用户图片和妆容无关
"""

import os
import re
import io
import base64
import traceback
from datetime import datetime, timezone

from openai import AsyncOpenAI

from src.utils.logger import get_logger
from src.utils.database import get_db_manager, COL_MESSAGES
from src.utils.socket_manager import get_socket_manager
from src.utils.vision_helper import get_vision_helper
from src.utils.api_retry import with_retry
from src.core.hikari_prompts import MAKEUP_CHECK_PROMPT, build_makeup_analysis_prompt

logger = get_logger(__name__)

# ── 触发词 ────────────────────────────────────────────────────────────────────

MAKEUP_TRIGGERS = [
    "帮我看看妆", "看看妆容", "妆容分析", "分析妆容",
    "妆怎么样", "妆好看吗", "还原度怎么样", "还原度分析",
    "帮我分析妆", "妆容怎么样", "妆容反馈",
]


def is_makeup_trigger(text: str, image_id: str) -> tuple[bool, str]:
    """
    判断是否触发妆容分析。必须：有 image_id + 消息含触发词。
    返回 (is_triggered, user_hint)
    """
    if not image_id:
        return False, ""
    for kw in MAKEUP_TRIGGERS:
        if kw in text:
            return True, text.replace(kw, "").strip()
    return False, ""


# ── 内部工具 ──────────────────────────────────────────────────────────────────

def _make_client() -> AsyncOpenAI:
    return AsyncOpenAI(
        base_url="https://ark.cn-beijing.volces.com/api/v3",
        api_key=os.getenv("DOUBAO_API_KEY"),
    )


async def _check_is_makeup_image(client: AsyncOpenAI, file_id: str) -> bool:
    """
    轻量校验：图片是否和 cosplay 妆容相关。
    max_output_tokens=10，只需 YES/NO，极省 token。
    校验失败时放行，不影响用户体验。
    """
    try:
        resp = await with_retry(
            client.responses.create,
            model=os.getenv("DOUBAO_MAIN_EP", ""),
            input=[
                {
                    "role": "user",
                    "content": [
                        {"type": "input_image", "file_id": file_id},
                        {"type": "input_text", "text": MAKEUP_CHECK_PROMPT},
                    ],
                }
            ],
            temperature=0.0,
            max_output_tokens=10,
            extra_body={"thinking": {"type": "disabled"}},
            task_name="妆容图校验",
        )
        answer = resp.output_text.strip().upper()
        result = answer.startswith("Y")
        logger.info(f"makeup: image check | answer={answer!r} result={result}")
        return result
    except Exception as e:
        logger.warning(f"makeup: image check failed, pass through: {e}")
        return True


async def _analyze_makeup(
    client: AsyncOpenAI,
    file_id: str,
    image_base64: str,
    user_hint: str = "",
) -> str:
    """
    主路径：responses API + web_search + file_id。
    降级：chat.completions + base64（不联网）。
    """
    model = os.getenv("DOUBAO_MAIN_EP", "")
    prompt = build_makeup_analysis_prompt(user_hint)

    try:
        resp = await with_retry(
            client.responses.create,
            model=model,
            input=[
                {
                    "role": "user",
                    "content": [
                        {"type": "input_image", "file_id": file_id},
                        {"type": "input_text", "text": prompt},
                    ],
                }
            ],
            temperature=0.7,
            max_output_tokens=1000,
            tools=[{"type": "web_search"}],
            extra_body={"thinking": {"type": "disabled"}},
            task_name="妆容分析",
        )
        result = resp.output_text.strip()
        logger.info(f"makeup: analysis done | len={len(result)} hint={repr(user_hint[:20])}")
        return result

    except Exception as e:
        logger.error(f"makeup: vision analysis failed: {e}, falling back")
        try:
            r = await with_retry(
                _make_client().chat.completions.create,
                model=model,
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "image_url",
                                "image_url": {"url": f"data:image/jpeg;base64,{image_base64}"},
                            },
                            {"type": "text", "text": prompt},
                        ],
                    }
                ],
                temperature=0.7,
                max_tokens=800,
                extra_body={"thinking": {"type": "disabled"}},
                task_name="妆容分析降级",
            )
            result = r.choices[0].message.content.strip()
            logger.info(f"makeup: fallback done | len={len(result)}")
            return result
        except Exception as e2:
            logger.error(f"makeup: fallback also failed: {e2}")
            return ""


async def _wrap_as_hikari(client: AsyncOpenAI, analysis_text: str) -> str:
    """
    把分析结果转成希卡莉口吻输出。
    失败时直接返回原始分析结果，不影响主流程。
    """
    model = os.getenv("DOUBAO_MAIN_EP", "")
    prompt = (
        "你是希卡莉喵，一只傲娇护短的白猫娘。"
        "用你的语气重新表达下面这段妆容分析，要求：\n"
        "- 保留所有分析内容和结论，不能删减或改变意思\n"
        "- 自称「本喵」，语气傲娇但真心在帮忙，嘴硬心软\n"
        "- 结构保持【角色识别】【整体还原度】【问题和改法】三段，不要加其他段落\n"
        "- 每段开头可以带一句希卡莉风格的吐槽或点评，但分析内容必须完整保留\n"
        "- 结尾加一句傲娇的鼓励\n"
        "- 严禁 Markdown 符号\n\n"
        f"原始分析：\n{analysis_text}"
    )
    try:
        r = await with_retry(
            client.chat.completions.create,
            model=model,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.8,
            max_tokens=800,
            extra_body={"thinking": {"type": "disabled"}},
            task_name="妆容口吻转换",
        )
        result = r.choices[0].message.content.strip()
        logger.info(f"makeup: tone wrap done | len={len(result)}")
        return result
    except Exception as e:
        logger.warning(f"makeup: tone wrap failed, using raw analysis: {e}")
        return analysis_text


# ── 完整任务入口 ──────────────────────────────────────────────────────────────

async def handle_makeup_task(
    user_id: str,
    session_id: str,
    image_id: str,
    user_text: str,
    msg_id: str,
) -> None:
    """
    完整异步任务：校验 → 分析 → 口吻转换 → 落盘 → WebSocket 推送。
    由 MakeupWorkflow activity 调用。
    """
    try:
        vh = get_vision_helper()
        db = get_db_manager().db
        sm = get_socket_manager()
        client = _make_client()

        # 1. 读取原图 base64
        img_path = os.path.join(vh.upload_root, user_id, f"{image_id}.jpg")
        image_base64 = vh.get_image_base64(img_path)
        if not image_base64:
            logger.error(f"makeup: image not found | path={img_path}")
            await sm.send_to_user(
                session_id,
                {
                    "type": "makeup_result",
                    "msg_id": msg_id,
                    "success": False,
                    "error": "找不到图片了喵，重新发一下？",
                },
            )
            return

        # 2. 上传图片拿 file_id
        buf = io.BytesIO(base64.b64decode(image_base64))
        buf.name = f"{image_id}.jpg"
        uploaded = await with_retry(
            client.files.create,
            file=buf,
            purpose="user_data",
            task_name="妆容图上传",
        )
        file_id = uploaded.id
        logger.info(f"makeup: image uploaded | file_id={file_id[:16]}")

        # 3. 轻量校验
        is_makeup = await _check_is_makeup_image(client, file_id)
        if not is_makeup:
            reject_text = "这张图好像和妆容没什么关系喵，换一张 coser 的妆容照片试试？"
            res = await db[COL_MESSAGES].insert_one(
                {
                    "session_id": session_id,
                    "user_id": user_id,
                    "role": "assistant",
                    "content": reject_text,
                    "voice_status": "none",
                    "created_at": datetime.now(timezone.utc),
                }
            )
            await sm.send_to_user(
                session_id,
                {
                    "type": "text",
                    "msg_id": str(res.inserted_id),
                    "content": reject_text,
                    "affinity": 10,
                    "mood": "neutral",
                    "voice_status": "none",
                },
            )
            await sm.send_to_user(
                session_id,
                {"type": "voice", "msg_id": str(res.inserted_id), "voice_status": "none"},
            )
            return

        # 4. 提取 user_hint
        trigger_pattern = "|".join(re.escape(kw) for kw in MAKEUP_TRIGGERS)
        user_hint = re.sub(trigger_pattern, "", user_text or "").strip()
        user_hint = re.sub(r"^[，。,.\s]+", "", user_hint).strip()
        logger.info(f"makeup: analyzing | hint={repr(user_hint[:30])}")

        # 5. 视觉分析 + 联网搜索
        analysis_text = await _analyze_makeup(client, file_id, image_base64, user_hint)
        if not analysis_text:
            await sm.send_to_user(
                session_id,
                {
                    "type": "makeup_result",
                    "msg_id": msg_id,
                    "success": False,
                    "error": "妆容分析失败了喵，稍后再试？",
                },
            )
            return

        # 6. 希卡莉口吻转换
        hikari_text = await _wrap_as_hikari(client, analysis_text)

        # 7. 落盘
        res = await db[COL_MESSAGES].insert_one(
            {
                "session_id": session_id,
                "user_id": user_id,
                "role": "assistant",
                "content": hikari_text,
                "voice_status": "none",
                "created_at": datetime.now(timezone.utc),
            }
        )
        new_mid = str(res.inserted_id)

        # 8. 推送结果
        await sm.send_to_user(
            session_id,
            {
                "type": "makeup_result",
                "msg_id": new_mid,
                "success": True,
                "content": hikari_text,
            },
        )
        logger.info(f"makeup: task done | msg_id={new_mid[:8]}")

    except Exception:
        logger.error(f"makeup: task crashed: {traceback.format_exc()}")
        try:
            await get_socket_manager().send_to_user(
                session_id,
                {
                    "type": "makeup_result",
                    "msg_id": msg_id,
                    "success": False,
                    "error": "本喵的妆容分析系统抽风了喵",
                },
            )
        except Exception:
            pass
