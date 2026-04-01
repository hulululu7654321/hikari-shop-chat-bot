# -*- coding: utf-8 -*-
"""
图生图服务 — Seedream 四宫格工作流

工作流：
1. 视觉分析：视觉模型分析 coser 图片，识别角色 + 设计4个动作
2. 图生图：原图 base64 + 分析文字 + 约束条件 → Seedream 四宫格输出
"""

import os
import re
import base64
import io
import traceback
from datetime import datetime, timezone
from typing import Optional

from openai import AsyncOpenAI

from src.utils.logger import get_logger
from src.utils.database import get_db_manager, COL_MESSAGES
from src.utils.socket_manager import get_socket_manager
from src.utils.vision_helper import get_vision_helper
from src.utils.api_retry import with_retry
from src.core.hikari_prompts import COSER_IMAGE_GEN_CONSTRAINTS, build_coser_analysis_prompt

logger = get_logger(__name__)

IMAGE_GEN_TRIGGERS = [
    "希卡莉启动pose替身",
]


def _make_client() -> AsyncOpenAI:
    return AsyncOpenAI(
        base_url="https://ark.cn-beijing.volces.com/api/v3",
        api_key=os.getenv("DOUBAO_API_KEY"),
    )


# ── 第一步：视觉分析 ─────────────────────────────────────────────────────────

async def analyze_coser(
    image_base64: str, user_hint: str = "", image_id: str = ""
) -> str:
    """
    视觉模型分析 coser 图片 + 联网搜索角色信息。
    返回：角色分析 + 4个动作设计的文字描述。
    降级：responses API 失败时回退到 chat.completions（不联网）。
    """
    try:
        client = _make_client()
        model = os.getenv("DOUBAO_MAIN_EP", "")
        analysis_prompt = build_coser_analysis_prompt(user_hint)

        img_bytes = base64.b64decode(image_base64)
        img_file = io.BytesIO(img_bytes)
        img_file.name = f"{image_id or 'coser'}.jpg"
        uploaded = await with_retry(
            _make_client().files.create,
            file=img_file,
            purpose="user_data",
            task_name="图片上传",
        )
        file_id = uploaded.id
        logger.info(f"image_gen: image uploaded | file_id={file_id[:16]}")

        resp = await with_retry(
            client.responses.create,
            model=model,
            input=[
                {
                    "role": "user",
                    "content": [
                        {"type": "input_image", "file_id": file_id},
                        {"type": "input_text", "text": analysis_prompt},
                    ],
                }
            ],
            temperature=0.7,
            max_output_tokens=1000,
            tools=[{"type": "web_search"}],
            extra_body={"thinking": {"type": "disabled"}},
            task_name="图生图视觉分析",
        )
        result = resp.output_text.strip()
        logger.info(f"image_gen: analysis done | len={len(result)} hint={repr(user_hint[:20])}")
        return result

    except Exception as e:
        logger.error(f"image_gen: vision analysis failed: {e}, falling back to chat.completions")
        try:
            model = os.getenv("DOUBAO_MAIN_EP", "")
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
                            {"type": "text", "text": build_coser_analysis_prompt(user_hint)},
                        ],
                    }
                ],
                temperature=0.7,
                max_tokens=800,
                extra_body={"thinking": {"type": "disabled"}},
                task_name="图生图视觉分析降级",
            )
            return r.choices[0].message.content.strip()
        except Exception as e2:
            logger.error(f"image_gen: fallback also failed: {e2}")
            return ""


# ── 第二步：Seedream 图生图 ──────────────────────────────────────────────────

async def generate_quad_image(image_base64: str, analysis_text: str) -> Optional[str]:
    """
    调 Seedream 生成四宫格动作参考图。
    返回生成图 URL（24小时有效），失败返回 None。
    """
    seedream_ep = os.getenv("SEEDREAM_EP", "")
    if not seedream_ep:
        logger.error("image_gen: SEEDREAM_EP not configured")
        return None

    try:
        client = _make_client()
        final_prompt = f"{analysis_text}\n{COSER_IMAGE_GEN_CONSTRAINTS}"
        logger.info(f"image_gen: quad image prompt len={len(final_prompt)}")

        resp = await with_retry(
            client.images.generate,
            model=seedream_ep,
            prompt=final_prompt,
            size="2K",
            response_format="url",
            extra_body={
                "image": f"data:image/jpeg;base64,{image_base64}",
                "watermark": False,
                "sequential_image_generation": "disabled",
            },
            task_name="Seedream四宫格生成",
        )

        if resp.data and resp.data[0].url:
            logger.info("image_gen: quad image generated")
            return resp.data[0].url
        return None

    except Exception as e:
        logger.error(f"image_gen: quad image failed: {e}")
        return None


# ── 完整任务入口 ─────────────────────────────────────────────────────────────

async def handle_image_gen_task(
    user_id: str,
    session_id: str,
    image_id: str,
    user_text: str,
    msg_id: str,
) -> None:
    """
    完整异步任务：视觉分析 → 图生图 → 落盘 → WebSocket 推送。
    由 ImageGenWorkflow activity 调用。
    """
    try:
        vision_helper = get_vision_helper()
        db = get_db_manager().db
        socket_manager = get_socket_manager()

        # 1. 读取原图 base64
        img_path = os.path.join(vision_helper.upload_root, user_id, f"{image_id}.jpg")
        image_base64 = vision_helper.get_image_base64(img_path)
        if not image_base64:
            logger.error(f"image_gen: source image not found | path={img_path}")
            await socket_manager.send_to_user(
                session_id,
                {
                    "type": "image_gen_result",
                    "msg_id": msg_id,
                    "success": False,
                    "error": "找不到原图喵，重新上传试试？",
                },
            )
            return

        # 2. 提取 user_hint（触发词之外的内容）
        trigger_pattern = "|".join(re.escape(kw) for kw in IMAGE_GEN_TRIGGERS)
        user_hint = re.sub(trigger_pattern, "", user_text or "").strip()
        logger.info(f"image_gen: step1 analyze | image_id={image_id[:8]} hint={repr(user_hint[:30])}")

        # 3. Step1：视觉分析
        analysis_text = await analyze_coser(image_base64, user_hint=user_hint, image_id=image_id)
        if not analysis_text:
            await socket_manager.send_to_user(
                session_id,
                {"type": "image_gen_result", "msg_id": msg_id, "success": False, "error": "角色分析失败了喵"},
            )
            return

        # 4. Step2：图生图
        logger.info("image_gen: step2 quad generate")
        gen_url = await generate_quad_image(image_base64, analysis_text)
        if not gen_url:
            await socket_manager.send_to_user(
                session_id,
                {
                    "type": "image_gen_result",
                    "msg_id": msg_id,
                    "success": False,
                    "error": "图片生成失败了喵，稍后再试？",
                },
            )
            return

        # 5. 落地：下载到本地防 24h URL 失效
        local_url = await vision_helper.save_gen_image(user_id, gen_url)
        if not local_url:
            logger.error(f"image_gen: save_gen_image failed | msg_id={msg_id[:8]}")
            await socket_manager.send_to_user(
                session_id,
                {
                    "type": "image_gen_result",
                    "msg_id": msg_id,
                    "success": False,
                    "error": "图片保存失败了喵，稍后再试？",
                },
            )
            return

        # 6. 落盘：插入新消息（独立气泡，不覆盖视觉回复）
        insert_res = await db[COL_MESSAGES].insert_one(
            {
                "session_id": session_id,
                "user_id": user_id,
                "role": "assistant",
                "content": "",
                "gen_image_url": local_url,
                "gen_image_status": "ready",
                "char_text": analysis_text,
                "analysis_text": analysis_text,
                "voice_status": "none",
                "created_at": datetime.now(timezone.utc),
            }
        )
        new_msg_id = str(insert_res.inserted_id)

        # 7. 推送
        await socket_manager.send_to_user(
            session_id,
            {
                "type": "image_gen_result",
                "msg_id": new_msg_id,
                "success": True,
                "gen_image_url": local_url,
                "char_text": analysis_text,
            },
        )
        logger.info(f"image_gen: task done | original_msg={msg_id[:8]} new_msg={new_msg_id[:8]}")

    except Exception:
        logger.error(f"image_gen: task crashed: {traceback.format_exc()}")
        try:
            await get_socket_manager().send_to_user(
                session_id,
                {
                    "type": "image_gen_result",
                    "msg_id": msg_id,
                    "success": False,
                    "error": "本喵的画图系统抽风了喵",
                },
            )
        except Exception:
            pass


def is_image_gen_trigger(text: str) -> bool:
    return any(kw in text for kw in IMAGE_GEN_TRIGGERS)
