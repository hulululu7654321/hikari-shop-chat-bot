# -*- coding: utf-8 -*-
"""
希卡莉·希卡莉喵 — 调度指挥部 (v6.0.0)
=====================================================================
重构对齐 [MASTER v3.0 / 2026-03-29]：

【集合名常量化】
- db["users"]    → db[COL_USERS]
- db["messages"] → db[COL_MESSAGES]
- db["facts"]    → db[COL_FACTS]

【Bug 修复同步】
- get_user_history profile.mood：
  user_doc.get("current_mood") → user_doc.get("mood")（Bug10 修复对齐）

【Temporal 接入（图生图任务）】
- Temporal Client 提交 ImageGenWorkflow（workflow_id=imagegen-{msg_id} 幂等）
- Temporal 不可用时降级 asyncio.create_task 直接执行（无 Redis Queue 中间层）

【其他改动】
- DatabaseManager() → get_db_manager()
- store_api 延迟 import 路径对齐 Task7：src.modules.store.api
=====================================================================
"""

import asyncio
import re
import os
import traceback
from datetime import datetime, timezone
from typing import Dict, Any, Optional, List

from quart import current_app

from src.utils.logger import get_logger
from src.utils.database import get_db_manager, COL_USERS, COL_MESSAGES, COL_FACTS
from src.core.session_manager import get_session_manager
from src.core.dialogue_engine import get_dialogue_engine
from src.services.asset_service import get_asset_service
from src.utils.vision_helper import get_vision_helper
from src.utils.socket_manager import get_socket_manager
from src.services.image_gen_service import is_image_gen_trigger

logger = get_logger(__name__)


class HikariBotOrchestrator:
    _instance = None
    _lock = asyncio.Lock()

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super(HikariBotOrchestrator, cls).__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if getattr(self, "_initialized", False):
            return
        self.session_manager = get_session_manager()
        self.engine          = get_dialogue_engine()
        self.asset_service   = get_asset_service()
        self.vision_helper   = get_vision_helper()
        self.db_manager      = get_db_manager()
        self._initialized    = True
        logger.info("🧠 [Orchestrator] v6.0.0 就绪 | COL_* 常量 + Temporal 图生图路由")

    # ──────────────────────────────────────────────────────────────
    # 历史记录分页打捞
    # ──────────────────────────────────────────────────────────────

    async def get_user_history(
        self,
        user_id: str,
        session_id: str = "sess_miniprogram",
        limit: int = 30,
        before_id: str = None,
    ) -> Dict[str, Any]:
        """
        [分页打捞] 游标分页，before_id 为上一页最早那条消息的 _id。

        三路 $or 查询兼容历史数据格式：
            user_id / metadata.user_id / session_id

        image_id 饱和式打捞：
            根路径 → metadata → content 正则三路兜底
        """
        try:
            from bson import ObjectId
            db = self.db_manager.db

            user_doc = await db[COL_USERS].find_one({"user_id": user_id})
            profile = {
                "affinity":  user_doc.get("affinity", 10)       if user_doc else 10,
                "mood":      user_doc.get("mood", "neutral")     if user_doc else "neutral",
                "user_name": user_doc.get("user_name", "匿名Coser") if user_doc else "匿名Coser",
            }

            query: dict = {
                "$or": [
                    {"user_id":          user_id},
                    {"metadata.user_id": user_id},
                    {"session_id":       session_id},
                ]
            }
            if before_id:
                try:
                    query["_id"] = {"$lt": ObjectId(before_id)}
                except Exception:
                    pass

            cursor   = db[COL_MESSAGES].find(query).sort("created_at", -1).limit(limit)
            raw_docs = await cursor.to_list(length=limit)
            raw_docs.reverse()

            messages_list = []
            for doc in raw_docs:
                meta      = doc.get("metadata", {})
                raw_role  = str(doc.get("role") or meta.get("role") or "assistant").lower()
                final_role = "user" if any(k in raw_role for k in ["user", "human"]) else "assistant"

                # 饱和式打捞 image_id：根路径 → metadata → content 正则
                img_id = doc.get("image_id") or meta.get("image_id")
                content = str(doc.get("content") or meta.get("content") or "")
                if not img_id and "image_id:" in content:
                    m = re.search(r'image_id:([a-f0-9-]+)', content)
                    if m:
                        img_id = m.group(1)

                v_url    = doc.get("voice_url") or meta.get("voice_url") or ""
                v_status = doc.get("voice_status") or meta.get("voice_status") or "none"
                if v_url:
                    v_status = "ready"
                elif final_role == "assistant" and v_status != "none":
                    v_status = "loading"

                is_report        = bool(doc.get("report_url") or doc.get("type") == "report" or meta.get("is_report"))
                gen_image_url    = doc.get("gen_image_url") or ""
                gen_image_status = doc.get("gen_image_status") or ("ready" if gen_image_url else "")

                created_at = doc.get("created_at")
                messages_list.append({
                    "id":               str(doc.get("_id")),
                    "role":             final_role,
                    "content":          content,
                    "image_id":         img_id or "",
                    "is_report":        is_report,
                    "report_url":       doc.get("report_url") or meta.get("report_url") or "",
                    "voice_url":        v_url,
                    "voice_status":     v_status,
                    "gen_image_url":    gen_image_url,
                    "gen_image_status": gen_image_status,
                    "char_text":        doc.get("char_text") or doc.get("analysis_text") or "",
                    "client_msg_id":    doc.get("client_msg_id") or "",
                    "sticker_url":      doc.get("sticker_url") or "",
                    "created_at":       created_at.isoformat() if isinstance(created_at, datetime) else str(created_at),
                })

            tags_cloud = await db[COL_FACTS].distinct("category", {"user_id": user_id})
            return {
                "list":       messages_list,
                "tags_cloud": tags_cloud,
                "profile":    profile,
                "has_more":   len(messages_list) >= limit,
            }

        except Exception:
            logger.error(f"❌ [Orchestrator-History]: {traceback.format_exc()}")
            return {"list": [], "tags_cloud": [], "profile": {"affinity": 10, "mood": "neutral"}}

    # ──────────────────────────────────────────────────────────────
    # 视觉画面描述（语义提纯）
    # ──────────────────────────────────────────────────────────────

    async def get_visual_description(self, user_id: str, image_id: str) -> Optional[str]:
        """
        [原子探测] 将图片 ID 转化为 30 字以内画面事实，物理屏蔽 UUID。
        """
        try:
            logger.info(f"👁️ [Vision-Relay] 语义探测资产: {image_id[:8]}...")
            probe_text = "用 30 字以内简要描述画面内容。严禁输出任何 ID、编号、链接、物理路径喵！"
            result     = await self.engine.process_message(
                user_id=user_id,
                raw_text=probe_text,
                image_id=image_id,
                enable_search=False,
            )
            desc = result.get("reply", "")
            # 物理洗白：强制抹除 UUID 格式和括号内容
            desc = re.sub(r'[a-f0-9-]{32,}', '', desc)
            desc = re.sub(r'\[.*?\]|\(.*?\)|\（.*?\）', '', desc).strip()
            return desc[:100]
        except Exception as e:
            logger.error(f"⚠️ [Vision-Relay] 探测失败: {e}")
            return None

    # ──────────────────────────────────────────────────────────────
    # 核心多模态管线
    # ──────────────────────────────────────────────────────────────

    async def handle_user_interaction(
        self,
        user_id: str,
        message: str,
        image_id: Optional[str] = None,
        client_msg_id: Optional[str] = None,
        character: str = "女性1_zh",
        platform: str = "miniprogram",
        enable_search: bool = False,
        enable_amap: bool = False,
        amap_data: str = "",
        ws_session_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        [中枢管线] 语义化中继 → 推理 → 物理锚定落盘

        执行顺序：
        1. session 确保存在
        2. 图片 → 视觉语义化（visual_desc）
        3. 用户消息原子落盘（COL_MESSAGES）
        4. RAG 知识库检索（高德场景跳过）
        5. dialogue_engine.process_message() 推理
        6. 解析 [emotion] 前缀 + UUID 过滤
        7. 助手回复落盘（COL_MESSAGES）
        8. TTS 异步触发
        9. 图生图任务路由（Temporal → asyncio 两级降级）
        10. 后处理（事实沉淀 + 好感度 + 里程碑 + 归档 + 整活）
        11. 店铺模式：推 Live2D 动作到屏幕端
        """
        if not message and not image_id:
            return {"success": False, "reply_text": "瞄？"}

        try:
            db = self.db_manager.db

            await self.session_manager.get_or_create_session(platform=platform, user_id=user_id)
            uid = user_id
            sid = ws_session_id or await get_socket_manager().get_session_id_for_user(uid)

            # ── 1. 视觉语义化 ────────────────────────────────────
            visual_desc = ""
            if image_id:
                visual_desc = await self.get_visual_description(uid, image_id) or ""

            # ── 2. 用户消息原子落盘 ──────────────────────────────
            await db[COL_MESSAGES].insert_one({
                "session_id":    sid,
                "user_id":       uid,
                "role":          "user",
                "content":       message,
                "image_id":      image_id or "",
                "client_msg_id": client_msg_id or "",
                "created_at":    datetime.now(timezone.utc),
            })

            # ── 3. 推理文本构造（语义中继：屏蔽物理 ID）───────────
            inference_text = message
            if image_id and visual_desc:
                inference_text = f"{message} (希卡莉看到画面内容: {visual_desc})"

            # ── 4. RAG 知识库检索（高德场景跳过）────────────────
            rag_context = ""
            if not enable_amap:
                try:
                    from src.services.rag_service import get_rag_service, DEFAULT_TENANT
                    _rag = get_rag_service()
                    if _rag.should_trigger(message):
                        _user_doc = await db[COL_USERS].find_one(
                            {"user_id": uid}, {"tenant_ids": 1, "tenant_id": 1}
                        )
                        _doc      = _user_doc or {}
                        _tenants: list = (
                            _doc.get("tenant_ids")
                            or ([_doc["tenant_id"]] if _doc.get("tenant_id") else None)
                            or [DEFAULT_TENANT]
                        )
                        _tenants = list(dict.fromkeys(t for t in _tenants if t))

                        _tasks       = [_rag.retrieve(query=message, tenant_id=t) for t in _tenants]
                        _all_results = await asyncio.gather(*_tasks, return_exceptions=True)

                        _merged: list = []
                        for _res in _all_results:
                            if isinstance(_res, list):
                                _merged.extend(_res)
                        _merged.sort(key=lambda x: x.get("score", 0), reverse=True)

                        if _merged:
                            rag_context = _rag.format_context(_merged[:5])
                            logger.info(f"[RAG] 命中 {len(_merged)} 条 | tenants={_tenants}")
                except Exception as _rag_err:
                    logger.warning(f"[RAG] 检索失败，跳过: {_rag_err}")

            # 合并高德数据和 RAG context
            combined_context = "\n\n".join(filter(None, [amap_data, rag_context]))

            # ── 5. 推理 ─────────────────────────────────────────
            result = await self.engine.process_message(
                user_id=uid,
                raw_text=inference_text,
                image_id=image_id,
                enable_search=enable_search,
                enable_amap=enable_amap,
                search_results=combined_context,
            )

            reply      = result.get("reply", "")
            audio_text = result.get("audio_text", "")
            skip_tts   = result.get("skip_tts", False)
            logger.info(
                f"🔍 [Debug-TTS] enable_search={enable_search} | "
                f"skip_tts={skip_tts} | keys={list(result.keys())}"
            )

            # ── 6. 解析 [emotion] 前缀 + UUID 过滤 ──────────────
            emo, clean = "neutral", reply
            m = re.match(r'^\[(\w+)\]\s*(.*)', reply, re.DOTALL)
            if m:
                emo   = m.group(1).lower().replace("normal", "neutral")
                clean = m.group(2).strip()
            # 物理防护：再次过滤回复中的 UUID
            clean = re.sub(r'[a-f0-9-]{32,}', '[秘密记录]', clean)

            # 图生图场景：希卡莉的确认回复强制跳过 TTS
            is_image_gen = bool(image_id and is_image_gen_trigger(message))
            if is_image_gen:
                skip_tts = True

            # ── 7. 助手回复原子落盘 ──────────────────────────────
            # sticker_url 先落空字符串，miniprogram_api 拿到真实 GIF URL 后再 $set 回写
            insert_res = await db[COL_MESSAGES].insert_one({
                "session_id":   sid,
                "user_id":      uid,
                "role":         "assistant",
                "content":      clean,
                "created_at":   datetime.now(timezone.utc),
                "voice_status": "none" if skip_tts else "generating",
                "sticker_url":  "",
            })
            mid = str(insert_res.inserted_id)

            # ── 8. TTS 异步触发 ──────────────────────────────────
            if not skip_tts:
                asyncio.create_task(
                    self.trigger_voice_task(uid, sid, audio_text or clean, mid, character, emo)
                )

            # ── 9. 图生图任务路由（两级降级：Temporal → asyncio）──
            # [工具分支扩展点] 新增工具任务时在此追加同类 if 块。
            # 触发重构为独立路由层的条件：工具分支超过 4 个，或出现嵌套条件。
            if is_image_gen:
                logger.info(f"🎨 [ImageGen] 触发图生图 | image_id={image_id[:8]}")
                await _dispatch_image_gen_task(
                    user_id=uid,
                    session_id=sid,
                    image_id=image_id,
                    message=message,
                    msg_id=mid,
                )

            # ── 10. 后处理（事实沉淀 + 好感度 + 里程碑 + 归档 + 整活）
            _safe_image_id = image_id or ""
            try:
                current_app.add_background_task(
                    self._async_post_processing,
                    sid, uid, message, clean, emo, visual_desc, _safe_image_id, enable_amap, enable_search,
                )
            except Exception:
                asyncio.create_task(
                    self._async_post_processing(
                        sid, uid, message, clean, emo, visual_desc, _safe_image_id, enable_amap, enable_search,
                    )
                )

            # ── 11. 店铺模式：推 Live2D 动作到屏幕端（通过 Activity 解耦）────────────
            if os.getenv("STORE_ENABLED", "false").lower() == "true":
                try:
                    from src.workflows.store_interaction_activity import push_live2d_motion
                    asyncio.create_task(push_live2d_motion(uid, emo if emo != "neutral" else "talk"))
                except Exception:
                    pass

            return {
                "success":      True,
                "msg_id":       mid,
                "reply_text":   clean,
                "audio_text":   audio_text or clean,
                "skip_tts":     skip_tts,
                "emotion_tag":  emo,
                "user_id":      uid,
                "session_id":   sid,
                "affinity":     result.get("affinity", 10),
                "is_image_gen": is_image_gen,
                "mood_tag":     result.get("mood_tag", ""),
            }

        except Exception:
            logger.error(f"❌ [Orchestrator-Interaction]: {traceback.format_exc()}")
            return {"success": False, "reply_text": "本喵的视觉神经刚才好像断开了..."}

    # ──────────────────────────────────────────────────────────────
    # TTS 异步触发
    # ──────────────────────────────────────────────────────────────

    async def trigger_voice_task(
        self,
        user_id: str,
        session_id: str,
        text: str,
        msg_id: str,
        character: str,
        emotion: str,
    ) -> None:
        """从 DB 打捞真实 user_name，调用 asset_service 触发 TTS 生成"""
        try:
            db   = self.db_manager.db
            u    = await db[COL_USERS].find_one({"user_id": user_id})
            name = u.get("user_name", "匿名Coser") if u else "匿名Coser"
            await self.asset_service.push_voice_generation(
                user_id=user_id,
                session_id=session_id,
                text=text,
                msg_id=msg_id,
                character=character or "女性1_zh",
                emotion=emotion,
                user_name=name,
            )
        except Exception:
            logger.error(f"❌ [Orchestrator-TTS]: {traceback.format_exc()}")

    # ──────────────────────────────────────────────────────────────
    # 后处理管线
    # ──────────────────────────────────────────────────────────────

    async def _async_post_processing(
        self,
        sid: str,
        uid: str,
        u_msg: str,
        b_msg: str,
        emo: str = "neutral",
        vision_desc: str = "",
        image_id: str = "",
        enable_amap: bool = False,
        enable_search: bool = False,
    ) -> None:
        """
        [多模态审计] 确保语义事实与物理资产 image_id 同时入库。

        1. 事实沉淀（高德场景跳过：查路线内容不应写入用户画像）
        2. 好感度脉冲，拿到新旧值供里程碑判定
        3. 里程碑检测（30 / 70 / 90）
        4. 画像归档调度（每累计 N 条碎片触发一次）
        5. 冷场整活检测（异步独立，失败静默）
        """
        try:
            from src.core.fact_extractor import get_fact_extractor
            from src.services.affinity_service import get_affinity_service
            from src.core.trigger_manager import get_trigger_manager

            db    = self.db_manager.db
            u_doc = await db[COL_USERS].find_one({"user_id": uid})
            if not u_doc:
                return

            # 1. 事实沉淀
            # 高德/联网搜索：完全跳过（路线/天气/即时查询结果不应进画像）
            # 兴趣偏好由 fact_extractor 从多轮普通对话中归纳，单次搜索行为不足以代表长期兴趣
            if not enable_amap and not enable_search:
                await get_fact_extractor().sense(
                    user_doc=u_doc,
                    session_id=sid,
                    user_text=u_msg,
                    bot_text=b_msg,
                    stage="POST",
                    vision_desc=vision_desc,
                    image_id=image_id,
                )

            # 2. 好感度脉冲
            affinity_result = await get_affinity_service().process_daily_chat(
                sid, u_msg, bot_emotion=emo
            )

            trigger = get_trigger_manager()

            # 3. 里程碑检测
            if affinity_result:
                old_a     = affinity_result.get("old", 0)
                new_a     = affinity_result.get("new", 0)
                milestone = await trigger.check_affinity_milestone(sid, old_a, new_a)
                if milestone:
                    logger.info(f"🎊 [Milestone] uid={uid[:8]} | 阈值={milestone}")

            # 4. 画像归档调度
            asyncio.create_task(trigger.check_and_run_persona_archival(sid))

            # 5. 冷场整活检测
            asyncio.create_task(_check_engagement(sid, trigger))

        except Exception:
            logger.error(f"❌ [Orchestrator-Post]: {traceback.format_exc()}")


# ══════════════════════════════════════════════════════════════════
# 模块级工具函数
# ══════════════════════════════════════════════════════════════════

async def _check_engagement(sid: str, trigger) -> None:
    """
    [冷场整活] 独立协程：读取最近 3 条 user 消息，判断是否需要整活。
    触发后调用 dialogue_engine 生成主动消息，落盘并推送给前端。
    失败静默，不影响任何主流程。
    """
    try:
        db = get_db_manager().db
        cursor = db[COL_MESSAGES].find(
            {"session_id": sid, "role": "user"},
            {"content": 1},
        ).sort("created_at", -1).limit(3)
        recent  = await cursor.to_list(length=3)
        history = [{"role": "user", "content": m.get("content", "")} for m in recent]

        should_engage = await trigger.check_engagement_need(sid, history)
        if not should_engage:
            return

        # 反查 user_id
        session = await get_session_manager().get_session(sid)
        if not session:
            return
        uid = session.get("user_id")
        if not uid:
            return

        # 调用 dialogue_engine 生成整活消息
        engine = get_dialogue_engine()
        result = await engine.process_message(
            user_id=uid,
            raw_text="[系统：用户已很久没有发言，请主动整活，说一句有趣的话来活跃气氛]",
        )
        reply = result.get("reply", "")
        if not reply:
            return

        # 解析 [emotion] 前缀
        emo, clean = "neutral", reply
        m = re.match(r'^\[(\w+)\]\s*(.*)', reply, re.DOTALL)
        if m:
            emo   = m.group(1).lower().replace("normal", "neutral")
            clean = m.group(2).strip()

        # 读取用户声线偏好
        u_doc = await db[COL_USERS].find_one({"user_id": uid})
        voice_char = (u_doc.get("voice_character", "女性1_zh") if u_doc else "女性1_zh")

        # 落盘
        insert_res = await db[COL_MESSAGES].insert_one({
            "session_id":   sid,
            "user_id":      uid,
            "role":         "assistant",
            "content":      clean,
            "created_at":   datetime.now(timezone.utc),
            "voice_status": "generating",
        })
        mid = str(insert_res.inserted_id)

        # WS 推送
        socket_manager = get_socket_manager()
        await socket_manager.send_to_user(sid, {
            "type":         "text",
            "msg_id":       mid,
            "content":      clean,
            "affinity":     0,
            "mood":         emo,
            "voice_status": "loading",
        })

        # TTS 异步触发
        orchestrator = get_bot_orchestrator()
        asyncio.create_task(
            orchestrator.trigger_voice_task(uid, sid, clean, mid, voice_char, emo)
        )
        logger.info(f"🐾 [Engagement] 整活消息已推送 | sid={sid[:8]} | len={len(clean)}")

    except Exception:
        pass


async def _dispatch_image_gen_task(
    user_id: str,
    session_id: str,
    image_id: str,
    message: str,
    msg_id: str,
) -> None:
    """
    [图生图任务路由] 两级降级：
    1. Temporal WorkflowClient.start_workflow(ImageGenWorkflow)  — 正常路径
    2. asyncio.create_task(image_gen_service 直接执行)            — Temporal 不可用时

    workflow_id = f"imagegen-{msg_id}" 保证幂等（同一条消息不会重复触发）。
    """
    # 优先：Temporal Workflow
    try:
        from temporalio.client import Client as TemporalClient
        from src.workflows.image_gen_workflow import ImageGenWorkflow, ImageGenInput
        temporal_host = os.getenv("TEMPORAL_HOST", "hikari-temporal:7233")
        task_queue    = os.getenv("TEMPORAL_TASK_QUEUE", "hikari-main")
        client        = await TemporalClient.connect(temporal_host)
        await client.start_workflow(
            ImageGenWorkflow.run,
            ImageGenInput(
                user_id=user_id,
                session_id=session_id,
                image_id=image_id,
                message=message,
                msg_id=msg_id,
            ),
            id=f"imagegen-{msg_id}",
            task_queue=task_queue,
        )
        logger.info(f"📥 [ImageGen-Temporal] 任务已提交 | workflow_id=imagegen-{msg_id}")
        return
    except Exception as _te:
        logger.warning(f"[ImageGen-Temporal] 提交失败，降级直接执行: {_te}")

    # 降级：asyncio 直接执行（Temporal 不可用时兜底）
    from src.services.image_gen_service import handle_image_gen_task
    asyncio.create_task(
        handle_image_gen_task(user_id, session_id, image_id, message, msg_id)
    )


# ══════════════════════════════════════════════════════════════════
# 单例导出
# ══════════════════════════════════════════════════════════════════

def get_bot_orchestrator() -> HikariBotOrchestrator:
    return HikariBotOrchestrator()
