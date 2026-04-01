# -*- coding: utf-8 -*-
"""
tests/test_hikari_prompts.py — hikari_prompts 无状态函数单测

覆盖函数：
  get_affinity_info()          亲密度分级
  build_nya_prompt()           旧版整合 prompt
  build_nya_prompt_split()     缓存分离版 prompt
  build_coser_analysis_prompt()  图生图 prompt 注入
  build_makeup_analysis_prompt() 妆容 prompt 注入
  build_copywriting_prompt()   文案 prompt 构建
  get_sensing_focus()          辅助字符串
"""

import pytest
from src.core.hikari_prompts import (
    NYA_AFFINITY_LEVELS,
    NYA_STATIC_PREFIX_TEMPLATE,
    NYA_DYNAMIC_SUFFIX_TEMPLATE,
    get_affinity_info,
    build_nya_prompt,
    build_nya_prompt_split,
    build_coser_analysis_prompt,
    build_makeup_analysis_prompt,
    build_copywriting_prompt,
    get_sensing_focus,
    COSER_ANALYSIS_STATIC_PROMPT,
    MAKEUP_ANALYSIS_STATIC_PROMPT,
    COPYWRITING_STATIC_PROMPT,
)


# ══════════════════════════════════════════════════════════════════
# get_affinity_info
# ══════════════════════════════════════════════════════════════════

class TestGetAffinityInfo:
    def test_stranger_lower_bound(self):
        info = get_affinity_info(0)
        assert info["label"] == NYA_AFFINITY_LEVELS["STRANGER"]["label"]

    def test_stranger_upper_bound(self):
        info = get_affinity_info(30)
        assert info["label"] == NYA_AFFINITY_LEVELS["STRANGER"]["label"]

    def test_friend_lower_bound(self):
        info = get_affinity_info(31)
        assert info["label"] == NYA_AFFINITY_LEVELS["FRIEND"]["label"]

    def test_friend_upper_bound(self):
        info = get_affinity_info(70)
        assert info["label"] == NYA_AFFINITY_LEVELS["FRIEND"]["label"]

    def test_soulmate_lower_bound(self):
        info = get_affinity_info(71)
        assert info["label"] == NYA_AFFINITY_LEVELS["SOULMATE"]["label"]

    def test_soulmate_upper_bound(self):
        info = get_affinity_info(100)
        assert info["label"] == NYA_AFFINITY_LEVELS["SOULMATE"]["label"]

    def test_returns_all_required_keys(self):
        info = get_affinity_info(50)
        assert "label" in info
        assert "tone" in info
        assert "call" in info

    def test_soul_config_format_min_max(self):
        # soul_config 使用 min/max 格式而非 range 元组
        custom_levels = {
            "LOW": {"min": 0, "max": 40, "label": "低亲密", "tone": "cool", "call": "你"},
            "HIGH": {"min": 41, "label": "高亲密", "tone": "warm", "call": "亲"},
        }
        assert get_affinity_info(20, custom_levels)["label"] == "低亲密"
        assert get_affinity_info(60, custom_levels)["label"] == "高亲密"

    def test_soul_config_no_max_matches_highest(self):
        # max 为 None 时匹配任意更高分值
        custom_levels = {
            "ALL": {"min": 0, "label": "全覆盖", "tone": "t", "call": "c"},
        }
        assert get_affinity_info(99, custom_levels)["label"] == "全覆盖"

    def test_fallback_when_soul_levels_empty(self):
        # 传空 dict 时走默认分支
        info = get_affinity_info(50, affinity_levels={})
        assert info["label"] == NYA_AFFINITY_LEVELS["FRIEND"]["label"]


# ══════════════════════════════════════════════════════════════════
# build_nya_prompt
# ══════════════════════════════════════════════════════════════════

class TestBuildNyaPrompt:
    def test_returns_string(self):
        result = build_nya_prompt(50, "coser", "漫展记录")
        assert isinstance(result, str)

    def test_contains_user_name(self):
        result = build_nya_prompt(50, "coser", "obs", user_name="小明")
        assert "小明" in result

    def test_default_user_name(self):
        result = build_nya_prompt(50, "coser", "obs")
        assert "匿名用户" in result

    def test_contains_persona_context(self):
        result = build_nya_prompt(50, "专业coser", "obs")
        assert "专业coser" in result

    def test_contains_observations(self):
        result = build_nya_prompt(50, "ctx", "最近在画画")
        assert "最近在画画" in result

    def test_affinity_label_in_prompt(self):
        result = build_nya_prompt(0, "ctx", "obs")
        assert NYA_AFFINITY_LEVELS["STRANGER"]["label"] in result


# ══════════════════════════════════════════════════════════════════
# build_nya_prompt_split
# ══════════════════════════════════════════════════════════════════

class TestBuildNyaPromptSplit:
    def test_returns_two_strings(self):
        static, dynamic = build_nya_prompt_split(50, "ctx", "obs")
        assert isinstance(static, str)
        assert isinstance(dynamic, str)

    def test_static_is_prefix_template_by_default(self):
        static, _ = build_nya_prompt_split(50, "ctx", "obs")
        # 静态部分应包含身份设定关键词
        assert "希卡莉" in static
        assert "边界守则" in static

    def test_dynamic_contains_persona_context(self):
        _, dynamic = build_nya_prompt_split(50, "我是银狼coser", "obs")
        assert "我是银狼coser" in dynamic

    def test_dynamic_contains_observations(self):
        _, dynamic = build_nya_prompt_split(50, "ctx", "昨天去漫展")
        assert "昨天去漫展" in dynamic

    def test_absence_hint_injected(self):
        _, dynamic = build_nya_prompt_split(50, "ctx", "obs", absence_hint="失联30天")
        assert "失联30天" in dynamic
        assert "回归感知" in dynamic

    def test_no_absence_hint_when_empty(self):
        _, dynamic = build_nya_prompt_split(50, "ctx", "obs", absence_hint="")
        assert "回归感知" not in dynamic

    def test_user_preferences_injected(self):
        _, dynamic = build_nya_prompt_split(50, "ctx", "obs", user_preferences="说话简短")
        assert "说话简短" in dynamic
        assert "用户个性化要求" in dynamic

    def test_no_user_preferences_when_empty(self):
        _, dynamic = build_nya_prompt_split(50, "ctx", "obs", user_preferences="")
        assert "用户个性化要求" not in dynamic

    def test_soul_persona_template_overrides_static(self):
        custom_soul = {"persona_template": "自定义人设内容"}
        static, _ = build_nya_prompt_split(50, "ctx", "obs", soul=custom_soul)
        assert static == "自定义人设内容"

    def test_soul_none_uses_default_static(self):
        static, _ = build_nya_prompt_split(50, "ctx", "obs", soul=None)
        assert static == NYA_STATIC_PREFIX_TEMPLATE

    def test_soul_affinity_levels_override(self):
        custom_soul = {
            "affinity_levels": {
                "X": {"min": 0, "label": "特别等级", "tone": "t", "call": "君"}
            }
        }
        _, dynamic = build_nya_prompt_split(50, "ctx", "obs", soul=custom_soul)
        assert "特别等级" in dynamic

    def test_static_has_no_format_placeholders(self):
        # 静态部分不含 {} 占位符（否则缓存会不一致）
        static, _ = build_nya_prompt_split(50, "ctx", "obs")
        import re
        # 排除示例中可能有的 {xxx} 样式（仅校验无未填充占位符）
        unfilled = re.findall(r'\{[a-zA-Z_]+\}', static)
        assert unfilled == [], f"静态前缀含未填充占位符: {unfilled}"


# ══════════════════════════════════════════════════════════════════
# build_coser_analysis_prompt
# ════════════════════��═════════════════════════════════════════════

class TestBuildCoserAnalysisPrompt:
    def test_no_hint_returns_static(self):
        result = build_coser_analysis_prompt()
        assert result == COSER_ANALYSIS_STATIC_PROMPT

    def test_empty_hint_returns_static(self):
        result = build_coser_analysis_prompt(user_hint="")
        assert result == COSER_ANALYSIS_STATIC_PROMPT

    def test_whitespace_hint_returns_static(self):
        result = build_coser_analysis_prompt(user_hint="   ")
        assert result == COSER_ANALYSIS_STATIC_PROMPT

    def test_hint_prepended(self):
        result = build_coser_analysis_prompt(user_hint="银狼")
        assert result.startswith("## 已知角色信息")
        assert "银狼" in result
        assert COSER_ANALYSIS_STATIC_PROMPT in result

    def test_hint_instructs_skip_recognition(self):
        result = build_coser_analysis_prompt(user_hint="蕾姆")
        assert "跳过第一步" in result


# ══════════════════════════════════════════════════════════════════
# build_makeup_analysis_prompt
# ══════════════════════════════════════════════════════════════════

class TestBuildMakeupAnalysisPrompt:
    def test_no_hint_returns_static(self):
        result = build_makeup_analysis_prompt()
        assert result == MAKEUP_ANALYSIS_STATIC_PROMPT

    def test_empty_hint_returns_static(self):
        result = build_makeup_analysis_prompt(user_hint="")
        assert result == MAKEUP_ANALYSIS_STATIC_PROMPT

    def test_hint_prepended(self):
        result = build_makeup_analysis_prompt(user_hint="芙宁娜")
        assert result.startswith("## 已知角色信息")
        assert "芙宁娜" in result
        assert MAKEUP_ANALYSIS_STATIC_PROMPT in result

    def test_hint_instructs_skip_recognition(self):
        result = build_makeup_analysis_prompt(user_hint="雷电将军")
        assert "跳过第一步" in result


# ══════════════════════════════════════════════════════════════════
# build_copywriting_prompt
# ══════════════════════════════════════════════════════════════════

class TestBuildCopywritingPrompt:
    def test_default_platform_xiaohongshu(self):
        result = build_copywriting_prompt()
        assert "小红书" in result

    def test_weibo_platform(self):
        result = build_copywriting_prompt(platform="weibo")
        assert "微博" in result

    def test_bilibili_platform(self):
        result = build_copywriting_prompt(platform="bilibili")
        assert "B站" in result

    def test_unknown_platform_fallback_to_xiaohongshu(self):
        result = build_copywriting_prompt(platform="tiktok_unknown")
        assert "小红书" in result

    def test_user_hint_injected(self):
        result = build_copywriting_prompt(user_hint="银狼cos漫展记录")
        assert "银狼cos漫展记录" in result
        assert "最高优先级，文案必须围绕这个写" in result

    def test_empty_user_hint_not_injected(self):
        result = build_copywriting_prompt(user_hint="")
        # 没有用户 hint 时，不应注入"最高优先级"块
        assert "最高优先级，文案必须围绕这个写" not in result

    def test_has_ref_addon(self):
        result_no_ref  = build_copywriting_prompt(has_ref=False)
        result_with_ref = build_copywriting_prompt(has_ref=True)
        assert "参考文案说明" in result_with_ref
        assert "参考文案说明" not in result_no_ref

    def test_contains_static_base(self):
        result = build_copywriting_prompt()
        assert COPYWRITING_STATIC_PROMPT in result


# ══════════════════════════════════════════════════════════════════
# get_sensing_focus
# ══════════════════════════════════════════════════════════════════

class TestGetSensingFocus:
    def test_returns_string(self):
        assert isinstance(get_sensing_focus(), str)

    def test_non_empty(self):
        assert len(get_sensing_focus()) > 0
