# -*- coding: utf-8 -*-
"""
用户核心物理模型 (v3.11.0)
=====================================================================
字段变更记录：

v3.11.0（清理版）：
- 删除 last_voice_path：TTS路径已由 asset_service 动态生成，不需要缓存
- 删除 voice_status：只在 sessions/messages 集合使用，users 里播种无意义
- 删除 nya_state（含 current_mood/vibe/observations）：早期情绪状态机，
  已被 affinity_service（mood）和 fact_extractor（vector_memory）彻底替代
- 删除 platform_user_id：值和 user_id 完全相同，冗余
- 删除 platform：users 集合里从不被读取，sessions 集合由 session_manager 维护
- 删除 REQUIRED_FIELDS / filter_ghost_fields：整个代码库未被调用，死代码
- 新增 mood 播种：affinity_service 每轮写入，login/copywriting 读取，补充初始值
- 新增 last_active_at 播种：dialogue_engine 写入，PushSchedulerWorkflow 读取，补充初始值

保留字段说明：
- phone：店铺模式预留，待实现"签到时写入/更新手机号"功能
- access_note / access_revoked_at：有写入，作为审计追溯字段保留
- vip_granted_at：manage_api/admin_api 授权时写入，get_user_info 接口返回
=====================================================================
"""

from datetime import datetime, timezone
from typing import Dict, Any


def get_default_user_schema(user_id: str, platform: str = "miniprogram") -> Dict[str, Any]:
    """
    [物理播种协议]：生成初始 User 集合文档。
    通常配合 MongoDB 的 $setOnInsert 运算符使用，确保字段在首次创建时完整播种。
    """
    now = datetime.now(timezone.utc)

    return {
        # ── 身份 ──────────────────────────────────────
        "user_id": user_id,

        # ── 核心状态 ──────────────────────────────────
        "affinity": 10,
        "mood": "neutral",          # affinity_service 每轮写入当前情绪
        "user_name": "匿名Coser",

        # ── 时间轴 ────────────────────────────────────
        "last_active_at": now,      # dialogue_engine 每轮更新，PushSchedulerWorkflow 用于活跃度判断
        "total_interactions": 0,

        # ── TTS 偏好 ──────────────────────────────────
        "voice_character": "女性1_zh",

        # ── 图生图配额（invite.py 按套餐 $inc 累加，vip_guard.py 消耗）──
        "image_gen_quota": 0,

        # ── 画像碎片（fact_extractor 动态写入） ────────
        # category 体系：bio / pref / exp / rel / trait / plan / vision
        # scalar 类（real_name/cn/name）走 $set，list 类走 $addToSet
        # 初始化为空字典，各 category key 由 fact_extractor 按需动态创建，无需预播种
        "metadata": {},

        # ── 用户对希卡莉的个性化要求（fact_extractor user_pref 分类写入）──
        # topic → content 的映射，同 topic 新值覆盖旧值
        # 例：{"回复长度": "用户希望简短", "提问频率": "用户不希望被问太多问题"}
        "preferences": {},

        # ── 店铺模式 ──────────────────────────────────
        "phone": "",                # 预留：待实现签到时写入手机号
        "store_visit_count": 0,
        "last_store_visit": None,

        # ── 邀请制访问控制（v9.0）─────────────────────
        "has_access": False,
        "invite_code": None,
        "access_source": None,      # "invite_code" | "admin_direct"
        "access_granted_at": None,
        "access_revoked_at": None,  # 审计字段
        "access_note": "",          # 管理员备注，审计用

        # ── VIP 状态（v9.0）──────────────────────────
        "vip_level": 0,             # 0=普通, 1=VIP, 2=SVIP
        "vip_expires_at": None,     # None=永久，datetime=到期自动降级
        "vip_granted_at": None,     # 授权时间，manage_api/admin_api 写入

        # ── 管理角色（v9.1）──────────────────────────
        "manage_role": None,        # None | "admin" | "store_clerk"
        "manage_store_id": None,    # store_clerk 绑定店铺，admin 为 None
        "manage_granted_by": None,  # 授权人 user_id，不存 key 明文
        "manage_granted_at": None,

        # ── 审计 ──────────────────────────────────────
        "created_at": now,
        "updated_at": now,
    }
