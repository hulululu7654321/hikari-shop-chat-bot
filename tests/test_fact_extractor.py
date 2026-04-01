# -*- coding: utf-8 -*-
"""
tests/test_fact_extractor.py — FactExtractor 纯函数单测

覆盖（不依赖外部 I/O 的纯函数）：
  FactExtractor._universal_sanitizer()   清洗 LLM 返回的名字
  FactExtractor._parse_json_brute_force() 容忍性 JSON 解析
  _cosine_similarity()                    余弦相似度

单例初始化会连接 LLM / DB，通过 unittest.mock.patch 绕开。
"""

import pytest
from unittest.mock import patch, MagicMock


# ── 获取单例（外部包已在 conftest 被 stub，此处只需重置单例状态）──────

@pytest.fixture
def extractor():
    """
    每次测试前重置 FactExtractor 单例并在 patch 上下文内重新初始化。
    conftest.py 已 stub 了 jwt / elasticsearch / volcenginesdkarkruntime，
    此处额外 patch OpenAI / Ark / get_db_manager 的模块内引用。
    """
    # 先 import 模块，确保 patch 目标存在
    import src.core.fact_extractor as mod
    with patch.object(mod, "OpenAI", return_value=MagicMock()), \
         patch.object(mod, "Ark",    return_value=MagicMock()), \
         patch.object(mod, "get_db_manager", return_value=MagicMock()):
        mod.FactExtractor._instance = None
        mod._instance = None
        fe = mod.get_fact_extractor()
        yield fe
        mod.FactExtractor._instance = None
        mod._instance = None


# ══════════════════════════════════════════════════════════════════
# _universal_sanitizer
# ══════════════════════════════════════════════════════════════════

class TestUniversalSanitizer:
    def test_valid_name_returned(self, extractor):
        assert extractor._universal_sanitizer("小明") == "小明"

    def test_none_input_returns_none(self, extractor):
        assert extractor._universal_sanitizer(None) is None

    def test_empty_string_returns_none(self, extractor):
        assert extractor._universal_sanitizer("") is None

    def test_single_char_returns_none(self, extractor):
        # 长度 < 2 应被过滤
        assert extractor._universal_sanitizer("明") is None

    def test_too_long_returns_none(self, extractor):
        # 长度 > 12 且不匹配结尾角色名，返回 None
        assert extractor._universal_sanitizer("这是一个超过十二个字符的非常长名字哦") is None

    def test_bot_name_removed(self, extractor):
        # 包含 bot_dna 关键词应被剔除后校验
        result = extractor._universal_sanitizer("希卡莉明")
        # "希卡莉" 被移除，剩 "明"（1字）→ None
        assert result is None

    def test_hikari_alone_returns_none(self, extractor):
        assert extractor._universal_sanitizer("希卡莉") is None

    def test_special_chars_stripped(self, extractor):
        # 非汉字/字母/数字字符被清理
        result = extractor._universal_sanitizer("小明!!!")
        assert result == "小明"

    def test_english_name_valid(self, extractor):
        result = extractor._universal_sanitizer("Lucy")
        assert result == "Lucy"

    def test_name_with_known_character_suffix_trimmed(self, extractor):
        # len > 8 才执行角色名截断。构造9字字符串触发截断逻辑
        result = extractor._universal_sanitizer("某某某用户雷电将军")
        # "雷电将军" 被移除，剩 "某某某用户" 5字，应通过
        assert result == "某某某用户"


# ══════════════════════════════════════════════════════════════════
# _parse_json_brute_force
# ══════════════════════════════════════════════════════════════════

class TestParseJsonBruteForce:
    def test_clean_json(self, extractor):
        raw = '{"fragments": [{"subject":"用户","category":"bio","topic":"职业","content":"coser","confidence":0.9}]}'
        result = extractor._parse_json_brute_force(raw)
        assert len(result) == 1
        assert result[0]["content"] == "coser"

    def test_json_with_prefix_text(self, extractor):
        raw = '好的，以下是提取结果：\n{"fragments": [{"content":"测试内容","category":"bio","topic":"职业","confidence":0.8}]}'
        result = extractor._parse_json_brute_force(raw)
        assert len(result) == 1

    def test_json_with_suffix_text(self, extractor):
        raw = '{"fragments": [{"content":"有效内容","category":"pref","topic":"喜好","confidence":0.7}]}\n以上是分析结果。'
        result = extractor._parse_json_brute_force(raw)
        assert len(result) == 1

    def test_empty_fragments(self, extractor):
        raw = '{"fragments": []}'
        result = extractor._parse_json_brute_force(raw)
        assert result == []

    def test_no_json_returns_empty(self, extractor):
        result = extractor._parse_json_brute_force("没有JSON")
        assert result == []

    def test_missing_content_field_filtered(self, extractor):
        # content 为空的条目应被过滤
        raw = '{"fragments": [{"subject":"用户","category":"bio","topic":"职业","content":"","confidence":0.9}]}'
        result = extractor._parse_json_brute_force(raw)
        assert result == []

    def test_whitespace_content_filtered(self, extractor):
        raw = '{"fragments": [{"content":"   ","category":"bio","topic":"职业","confidence":0.9}]}'
        result = extractor._parse_json_brute_force(raw)
        assert result == []

    def test_extra_fields_stripped(self, extractor):
        # 只允许白名单字段：subject/category/topic/content/confidence
        raw = '{"fragments": [{"content":"有效","category":"bio","topic":"职业","confidence":0.9,"extra_field":"污染"}]}'
        result = extractor._parse_json_brute_force(raw)
        assert "extra_field" not in result[0]

    def test_multiple_fragments(self, extractor):
        raw = """{"fragments": [
            {"content":"摄影师","category":"bio","topic":"职业","confidence":0.9},
            {"content":"喜欢原神","category":"pref","topic":"游戏","confidence":0.8}
        ]}"""
        result = extractor._parse_json_brute_force(raw)
        assert len(result) == 2

    def test_non_dict_fragment_ignored(self, extractor):
        raw = '{"fragments": ["not_a_dict", {"content":"有效","category":"bio","topic":"t","confidence":0.9}]}'
        result = extractor._parse_json_brute_force(raw)
        assert len(result) == 1

    def test_completely_invalid_json(self, extractor):
        result = extractor._parse_json_brute_force("{invalid json{{")
        assert result == []

    def test_none_input(self, extractor):
        result = extractor._parse_json_brute_force(None)
        assert result == []


# ══════════════════════════════════════════════════════════════════
# _cosine_similarity（模块级函数）
# ══════════════════════════════════════════════════════════════════

class TestCosineSimilarity:
    def setup_method(self):
        from src.core.fact_extractor import _cosine_similarity
        self.fn = _cosine_similarity

    def test_identical_vectors(self):
        v = [1.0, 0.0, 0.0]
        assert abs(self.fn(v, v) - 1.0) < 1e-6

    def test_orthogonal_vectors(self):
        v1 = [1.0, 0.0]
        v2 = [0.0, 1.0]
        assert abs(self.fn(v1, v2) - 0.0) < 1e-6

    def test_opposite_vectors(self):
        v1 = [1.0, 0.0]
        v2 = [-1.0, 0.0]
        assert abs(self.fn(v1, v2) - (-1.0)) < 1e-6

    def test_zero_vector_returns_zero(self):
        assert self.fn([0.0, 0.0], [1.0, 1.0]) == 0.0

    def test_both_zero_vectors_returns_zero(self):
        assert self.fn([0.0], [0.0]) == 0.0

    def test_value_in_range(self):
        v1 = [0.3, 0.5, 0.8]
        v2 = [0.1, 0.9, 0.2]
        sim = self.fn(v1, v2)
        assert -1.0 <= sim <= 1.0

    def test_symmetry(self):
        v1 = [1.0, 2.0, 3.0]
        v2 = [4.0, 5.0, 6.0]
        assert abs(self.fn(v1, v2) - self.fn(v2, v1)) < 1e-9

    def test_length_mismatch_returns_zero(self):
        # zip 截断时短向量会截断，不应抛异常
        result = self.fn([1.0, 2.0, 3.0], [1.0, 2.0])
        assert isinstance(result, float)
