# -*- coding: utf-8 -*-
"""
src/modules/users/chat_handler.py
希卡莉 — 对话业务逻辑层

职责：
  - _resolve_user_context：身份识别 + 访问控制（不做业务逻辑）
  - process_interaction_pipeline：多模态交互主管线
    ├─ 文案生成  → Temporal CopywritingWorkflow
    ├─ 妆容分析  → Temporal MakeupWorkflow
    └─ 普通对话  → orchestrator.handle_user_interaction
"""

import os
import asyncio
import traceback
from datetime import datetime, timezone

from src.utils.logger import get_logger
from src.utils.database import get_db_manager, _sanitize_input, COL_USERS, COL_MESSAGES
from src.utils.socket_manager import get_socket_manager
from src.utils.wx_security import check_msg_safe
from src.utils.invite import (
    check_user_access, get_vip_info,
    check_daily_msg_limit, increment_daily_msg,
    check_image_gen_quota, decrement_image_gen_quota,
    VIP_LOCK_IMAGE_GEN, VIP_LOCK_MAKEUP, VIP_LOCK_COPYWRITING,
)
from src.core.orchestrator import get_bot_orchestrator
from src.services.amap_service import build_amap_context

logger = get_logger(__name__)

# ──────────────────────────────────────────
# 高德 / 联网搜索触发关键词
# ──────────────────────────────────────────

_AMAP_TRIGGERS = [
    "怎么去", "怎么走", "怎么坐", "坐地铁", "坐公交", "坐几号线",
    "换乘", "路线", "步行怎么走", "开车怎么走",
    "附近", "周边", "旁边", "附近有没有", "哪里有",
    "今天天气", "明天天气", "后天天气", "天气怎么样", "天气如何",
    "要带伞吗", "下雨吗", "会下雨吗", "要下雨", "几度了",
    "漫展怎么去", "会场在哪", "展馆在哪", "场馆在哪",
]

_SEARCH_TRIGGERS = [
    # 原有
    "帮我搜", "帮我查", "查一下", "查查看", "搜一下", "搜搜看",
    "联网搜索", "联网查", "最新消息", "最新动态", "最新进展",
    "今天新闻", "今日新闻", "实时", "现在几点",
    "多少钱", "什么价格", "开始了吗", "结束了吗",
    "上映了吗", "出来了吗", "发布了吗",
    # 询问最新状态
    "你知道最新", "有没有最新", "最新的是什么", "最新情况", "最新消息是",
    "现在最新", "目前最新",
    # 询问近期动态
    "最近有没有", "最近有什么", "最近出了", "最近发生了什么", "最近怎么了",
    "近期有没有", "近期动态",
    # 询问当前状态
    "你知道现在", "你知道今天", "现在什么情况", "目前什么情况",
    "现在还有没有", "还开着吗", "还营业吗",
    # 询问发布/上线
    "出了吗", "上线了吗", "更新了吗",
    "什么时候出", "什么时候发布", "什么时候开始",
    # 价格/票务
    "现在什么价", "票还有吗", "还有票吗", "能买到吗",
    # cosplay/漫展圈
    "几号开幕", "几号闭幕", "报名截止", "还能报名吗",
    "嘉宾有谁", "阵容出来了吗", "这期主题是", "下一期是",
]


# ──────────────────────────────────────────
# 用户上下文解析
# ──────────────────────────────────────────

async def _resolve_user_context(user_id: str, session_id: str) -> dict:
    """
    解析当前请求的用户身份，返回访问控制结论。
    职责边界：只做身份识别和访问权判断，不做业务逻辑。

    返回字段：
      blocked       bool   True = 拒绝此条消息，调用方直接 return
      error_code    str    "no_access" | "daily_limit" | None
      error_content str    推给用户的错误提示文字
      is_store_screen bool 屏幕端（后续 increment_daily_msg 守卫用）
      is_manage     bool   管理角色（VIP 锁由 vip_info["is_vip"]=True 体现）
      vip_info      dict   VIP 状态字典，管理角色直接给满权限值
    """
    db = get_db_manager().db

    # ① 屏幕端：跳过所有检查
    is_store_screen = session_id.startswith("store_screen_")
    if is_store_screen:
        return {
            "blocked": False, "error_code": None, "error_content": "",
            "is_store_screen": True, "is_manage": False,
            "vip_info": {"vip_level": 0, "is_vip": False, "vip_expires_at": None, "is_expired": False},
        }

    # ② 在店客户 / ③ 宽限期用户
    is_instore = False
    is_visited = False
    if os.getenv("STORE_ENABLED", "false").lower() == "true":
        try:
            redis = await get_db_manager().get_redis()
            is_instore = bool(await redis.get(f"hikari:store:instore:{user_id}"))
            if not is_instore:
                is_visited = bool(await redis.get(f"hikari:store:visited:{user_id}"))
        except Exception:
            pass

    # ④ 管理角色（非在店/宽限期时才查 DB）
    is_manage = False
    if not is_instore and not is_visited:
        try:
            doc = await db[COL_USERS].find_one({"user_id": user_id}, {"manage_role": 1})
            is_manage = bool(doc and doc.get("manage_role"))
        except Exception as e:
            logger.warning(f"[Context] manage_role 查询失败，降级为普通用户 | uid={user_id[:8]} | err={e}")

    # 邀请制检查：在店/宽限期/管理角色 均豁免
    if not is_instore and not is_visited and not is_manage:
        if not await check_user_access(user_id):
            return {
                "blocked": True,
                "error_code": "no_access",
                "error_content": "需要邀请码才能和本喵说话喵，在设置页激活一下～",
                "is_store_screen": False, "is_manage": False,
                "vip_info": {"vip_level": 0, "is_vip": False, "vip_expires_at": None, "is_expired": False},
            }

    # VIP 信息
    if is_manage:
        vip_info = {"vip_level": 1, "is_vip": True, "vip_expires_at": None, "is_expired": False}
    else:
        vip_info = await get_vip_info(user_id, db)

    # 每日次数检查（只检查不递增）
    allowed, used, limit = await check_daily_msg_limit(user_id, vip_info["is_vip"])
    if not allowed:
        return {
            "blocked": True,
            "error_code": "daily_limit",
            "error_content": f"今天已经和本喵聊了 {used} 条了喵，今日上限 {limit} 条，明天再来吧～（VIP 不限）",
            "is_store_screen": False, "is_manage": is_manage,
            "vip_info": vip_info,
        }

    return {
        "blocked": False, "error_code": None, "error_content": "",
        "is_store_screen": False, "is_manage": is_manage,
        "vip_info": vip_info,
    }


# ──────────────────────────────────────────
# 主交互管线
# ──────────────────────────────────────────

async def process_interaction_pipeline(
    user_id: str,
    session_id: str,
    message: str,
    image_ids: list | None = None,
    image_id: str | None = None,
    is_ws: bool = False,
    enable_search: bool = False,
    client_msg_id: str | None = None,
):
    """
    多模态交互主管线。
    image_ids 优先；仅传 image_id 时自动转成单元素列表，向后兼容。

    工具分支路由（对齐重构文档第十四节）：
      文案生成 → Temporal CopywritingWorkflow
      妆容分析 → Temporal MakeupWorkflow
      普通对话 → orchestrator.handle_user_interaction（内部触发 VoiceGenWorkflow / ImageGenWorkflow）
    """
    if image_ids is None:
        image_ids = [image_id] if image_id else []
    primary_image_id = image_ids[0] if image_ids else None

    try:
        orchestrator   = get_bot_orchestrator()
        socket_manager = get_socket_manager()
        db             = get_db_manager().db

        safe_msg = _sanitize_input(message)[:500]

        # ── 身份识别 + 访问控制 ──
        ctx = await _resolve_user_context(user_id, session_id)
        if ctx["blocked"]:
            if is_ws:
                await socket_manager.send_to_user(session_id, {
                    "type": "error",
                    "error_code": ctx["error_code"],
                    "content":    ctx["error_content"],
                })
            return {"success": False, "error_code": ctx["error_code"]}

        is_store_screen = ctx["is_store_screen"]
        vip_info        = ctx["vip_info"]

        # ── 店铺辅助触发（弹幕 + 人工呼叫） ──
        if os.getenv("STORE_ENABLED", "false").lower() == "true":
            await _handle_store_side_effects(user_id, session_id, safe_msg)

        # ── 触发优先级：高德 > 豆包联网 > 普通对话（互斥）──
        amap_enabled = os.getenv("AMAP_ENABLED", "true").lower() == "true"
        enable_amap  = amap_enabled and any(kw in safe_msg for kw in _AMAP_TRIGGERS)
        amap_data    = ""

        if enable_amap:
            logger.info(f"[Amap-Trigger] 关键词命中 | msg={safe_msg[:30]}")
            user_doc_city = await db[COL_USERS].find_one({"user_id": user_id})
            user_city = ""
            if user_doc_city:
                meta = user_doc_city.get("metadata", {})
                user_city = meta.get("城市", "") or meta.get("city", "") or meta.get("地区", "") or ""
            amap_data = await build_amap_context(safe_msg, user_city)
            enable_search = False

        if not enable_amap and not enable_search:
            enable_search = any(kw in safe_msg for kw in _SEARCH_TRIGGERS)
            if enable_search:
                logger.info(f"[Search-Trigger] 关键词命中 | msg={safe_msg[:30]}")

        # ── 文案生成（转发到 hikari-copywriting 专用容器）──
        from src.services.copywriting_service import is_copywriting_trigger
        is_copy, copy_platform, copy_hint = is_copywriting_trigger(safe_msg, image_ids)
        if is_copy and not enable_amap and not enable_search:
            if VIP_LOCK_COPYWRITING and not vip_info["is_vip"]:
                if is_ws:
                    await socket_manager.send_to_user(session_id, {
                        "type": "error", "error_code": "vip_required",
                        "content": "文案生成是 VIP 专属功能喵，升级后才能使用哦～",
                    })
                return {"success": False, "error_code": "vip_required"}

            if not await check_msg_safe(openid=user_id, content=safe_msg):
                if is_ws:
                    await socket_manager.send_to_user(session_id, {
                        "type": "text",
                        "msg_id": f"msg_{int(datetime.now().timestamp())}",
                        "content": "这句话的频率不对喵……我们聊点别的吧~",
                        "affinity": 10, "mood": "neutral",
                    })
                return {"success": False, "msg": "内容违规拦截"}

            msg_id = f"msg_{int(datetime.now().timestamp())}"
            await db[COL_MESSAGES].insert_one({
                "session_id":    session_id,
                "user_id":       user_id,
                "role":          "user",
                "content":       safe_msg,
                "image_id":      image_ids[0] if image_ids else "",
                "image_ids":     image_ids,
                "client_msg_id": client_msg_id or "",
                "created_at":    datetime.now(timezone.utc),
            })

            if not is_store_screen:
                await increment_daily_msg(user_id)

            # 标记转发给 hikari-copywriting，由 api.py WS handler 执行 HTTP 转发
            return {"success": True, "forward_tool": "copywriting_round1",
                    "forward_payload": {"session_id": session_id, "image_ids": image_ids,
                                        "message": safe_msg, "msg_id": msg_id}}

        # ── 微信文字安检 ──
        if not await check_msg_safe(openid=user_id, content=safe_msg):
            if is_ws:
                await socket_manager.send_to_user(session_id, {
                    "type": "text",
                    "msg_id": f"msg_{int(datetime.now().timestamp())}",
                    "content": "这句话的频率不对喵……我们聊点别的吧~",
                    "affinity": 10, "mood": "neutral",
                })
            return {"success": False, "msg": "内容违规拦截"}

        # ── 妆容分析（转发到 hikari-makeup 专用容器）──
        from src.services.makeup_service import is_makeup_trigger
        is_makeup, makeup_hint = is_makeup_trigger(safe_msg, primary_image_id or "")
        if is_makeup:
            if VIP_LOCK_MAKEUP and not vip_info["is_vip"]:
                if is_ws:
                    await socket_manager.send_to_user(session_id, {
                        "type": "error", "error_code": "vip_required",
                        "content": "妆容分析是 VIP 专属功能喵，升级后才能使用哦～",
                    })
                return {"success": False, "error_code": "vip_required"}

            await db[COL_MESSAGES].insert_one({
                "session_id":    session_id,
                "user_id":       user_id,
                "role":          "user",
                "content":       safe_msg,
                "image_id":      primary_image_id or "",
                "client_msg_id": client_msg_id or "",
                "created_at":    datetime.now(timezone.utc),
            })

            confirm_text = "好，本喵帮你看看妆容，分析一下还原度和细节，稍等喵～"
            confirm_res  = await db[COL_MESSAGES].insert_one({
                "session_id":  session_id,
                "user_id":     user_id,
                "role":        "assistant",
                "content":     confirm_text,
                "voice_status": "none",
                "created_at":  datetime.now(timezone.utc),
            })
            confirm_mid = str(confirm_res.inserted_id)

            await socket_manager.send_to_user(session_id, {
                "type": "text", "msg_id": confirm_mid,
                "content": confirm_text, "affinity": 10, "mood": "neutral",
                "voice_status": "none",
            })
            if is_ws:
                await socket_manager.send_to_user(session_id, {
                    "type": "voice", "msg_id": confirm_mid, "voice_status": "none",
                })

            if not is_store_screen:
                await increment_daily_msg(user_id)

            # 标记转发给 hikari-makeup，由 api.py WS handler 执行 HTTP 转发
            return {"success": True, "forward_tool": "makeup",
                    "forward_payload": {"session_id": session_id,
                                        "image_id": primary_image_id or "",
                                        "message": safe_msg, "msg_id": confirm_mid}}

        # ── 视觉搜索中继 ──
        search_query = safe_msg
        if primary_image_id and enable_search:
            visual_desc = await orchestrator.get_visual_description(user_id, primary_image_id)
            if visual_desc:
                search_query = f"{safe_msg} (视觉参考: {visual_desc})"

        # ── 图生图：检测触发词，转发给 hikari-imagegen 专用容器 ──
        from src.services.image_gen_service import is_image_gen_trigger
        is_image_gen_intent = is_image_gen_trigger(safe_msg) and bool(primary_image_id)

        if is_image_gen_intent:
            if VIP_LOCK_IMAGE_GEN and not vip_info["is_vip"]:
                if is_ws:
                    await socket_manager.send_to_user(session_id, {
                        "type": "error", "error_code": "vip_required",
                        "content": "图生图是 VIP 专属功能喵，升级后才能解锁哦～",
                    })
                return {"success": False, "error_code": "vip_required"}

            if VIP_LOCK_IMAGE_GEN and vip_info["is_vip"]:
                quota_allowed, _ = await check_image_gen_quota(user_id)
                if not quota_allowed:
                    if is_ws:
                        await socket_manager.send_to_user(session_id, {
                            "type": "error", "error_code": "quota_exceeded",
                            "content": "图生图次数已用完喵，购买套餐可以补充次数哦～（剩余：0次）",
                        })
                    return {"success": False, "error_code": "quota_exceeded"}

            msg_id = f"msg_{int(datetime.now().timestamp())}"
            if not is_store_screen:
                await increment_daily_msg(user_id)
            if VIP_LOCK_IMAGE_GEN and vip_info["is_vip"]:
                await decrement_image_gen_quota(user_id)

            # 落盘用户消息（loadHistory 刷新时能显示）
            await db[COL_MESSAGES].insert_one({
                "session_id":    session_id,
                "user_id":       user_id,
                "role":          "user",
                "content":       safe_msg,
                "image_id":      primary_image_id or "",
                "client_msg_id": client_msg_id or "",
                "created_at":    datetime.now(timezone.utc),
            })

            # 落盘确认气泡，推送给前端（替身启动中提示）
            confirm_text = "收到，本喵帮你启动替身！正在分析角色造型，四宫格生成中……稍等喵～"
            confirm_res  = await db[COL_MESSAGES].insert_one({
                "session_id":  session_id,
                "user_id":     user_id,
                "role":        "assistant",
                "content":     confirm_text,
                "voice_status": "none",
                "created_at":  datetime.now(timezone.utc),
            })
            confirm_mid = str(confirm_res.inserted_id)
            if is_ws:
                await socket_manager.send_to_user(session_id, {
                    "type": "text", "msg_id": confirm_mid,
                    "content": confirm_text, "affinity": 10, "mood": "neutral",
                    "voice_status": "none",
                })
                await socket_manager.send_to_user(session_id, {
                    "type": "voice", "msg_id": confirm_mid, "voice_status": "none",
                })

            # 标记转发给 hikari-imagegen，由 api.py WS handler 执行 HTTP 转发
            return {"success": True, "forward_tool": "imagegen",
                    "forward_payload": {"session_id": session_id,
                                        "image_id": primary_image_id,
                                        "message": safe_msg, "msg_id": msg_id}}

        # ── 声线偏好 ──
        user_doc   = await db[COL_USERS].find_one({"user_id": user_id})
        voice_char = (user_doc.get("voice_character", "女性1_zh") if user_doc else "女性1_zh")

        # ── 递增每日消息数（所有检查通过后） ──
        if not is_store_screen:
            await increment_daily_msg(user_id)

        # ── 调用 Orchestrator ──
        result = await orchestrator.handle_user_interaction(
            user_id=user_id,
            message=search_query,
            image_id=primary_image_id,
            client_msg_id=client_msg_id or None,
            character=voice_char,
            enable_search=enable_search,
            enable_amap=enable_amap,
            amap_data=amap_data,
            ws_session_id=session_id,
        )

        if not result or not result.get("success"):
            if is_ws:
                await socket_manager.send_to_user(session_id, {
                    "type": "error", "content": "咦，本喵的脑子抖了一下……再说一遍？",
                })
            return result

        # ─��� 图生图配额扣减（已移至图生图分支，此处仅保留 msg_id / skip_tts）──
        msg_id       = result.get("msg_id", f"msg_{int(datetime.now().timestamp())}")
        skip_tts     = result.get("skip_tts", enable_search) or (enable_amap and bool(amap_data))

        # ── GIF 表情贴纸 ──
        sticker_url = ""
        mood_tag    = result.get("mood_tag", "")
        if mood_tag:
            try:
                from src.services.nekos_service import get_sticker_url
                sticker_url = await get_sticker_url(mood_tag) or ""
                if sticker_url and result.get("msg_id"):
                    from bson import ObjectId
                    try:
                        await db[COL_MESSAGES].update_one(
                            {"_id": ObjectId(result["msg_id"])},
                            {"$set": {"sticker_url": sticker_url}},
                        )
                    except Exception as e:
                        logger.warning(f"[GIF] sticker_url 回写失败: {e}")
            except Exception:
                logger.warning(f"[GIF] get_sticker_url 异常: {traceback.format_exc()}")

        # ── 推送回复 ──
        await socket_manager.send_to_user(session_id, {
            "type":         "text",
            "msg_id":       msg_id,
            "content":      result.get("reply_text", ""),
            "affinity":     result.get("affinity", 10),
            "mood":         result.get("emotion_tag", "neutral"),
            "voice_status": "none" if skip_tts else "loading",
            "gen_pending":  False,
            "sticker_url":  sticker_url,
        })

        if skip_tts and is_ws:
            await socket_manager.send_to_user(session_id, {
                "type": "voice", "msg_id": msg_id, "voice_status": "none",
            })

        return result

    except Exception:
        logger.error(f"[Pipeline-Collapse]: {traceback.format_exc()}")
        return None


# ──────────────────────────────────────────
# 店铺侧效（弹幕 + 人工呼叫）
# ──────────────────────────────────────────

async def _handle_store_side_effects(user_id: str, session_id: str, safe_msg: str) -> None:
    """弹幕推送 + 人工服务呼叫，失败静默。"""
    try:
        from src.services.store_service import is_assist_trigger, push_assist_request
        if is_assist_trigger(safe_msg):
            doc   = await get_db_manager().db[COL_USERS].find_one({"user_id": user_id}, {"user_name": 1})
            name  = (doc.get("user_name", "") if doc else "") or "神秘冒险者"
            asyncio.create_task(push_assist_request(user_id, name))
    except Exception:
        pass

    try:
        redis         = await get_db_manager().get_redis()
        store_id_raw  = await redis.get(f"hikari:store:instore:{user_id}")
        if store_id_raw:
            store_id = store_id_raw if isinstance(store_id_raw, str) else store_id_raw.decode()
            doc = await get_db_manager().db[COL_USERS].find_one({"user_id": user_id}, {"user_name": 1})
            name = (doc.get("user_name", "") if doc else "") or "神秘冒险者"
            await get_socket_manager().send_to_user(
                f"store_screen_{store_id}",
                {"type": "store_event", "event": "user_danmaku", "display_name": name, "text": safe_msg},
            )
            from src.services.store_service import push_danmaku_to_redis
            asyncio.create_task(push_danmaku_to_redis(store_id, name, safe_msg))
    except Exception:
        pass


