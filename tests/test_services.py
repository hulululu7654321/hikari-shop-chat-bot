# -*- coding: utf-8 -*-
"""
tests/test_services.py — 触发词判断函数单测

覆盖（纯函数，无 I/O 依赖）：
  copywriting_service.is_copywriting_trigger()
  makeup_service.is_makeup_trigger()
"""

import pytest


# ══════════════════════════════════════════════════════════════════
# is_copywriting_trigger
# ══════════════════════════════════════════════════════════════════

class TestIsCopywritingTrigger:
    @pytest.fixture(autouse=True)
    def import_fn(self):
        from src.services.copywriting_service import is_copywriting_trigger
        self.fn = is_copywriting_trigger

    # ── 触发条件：需要图片 ───────────────────────────────────────────

    def test_no_image_ids_not_triggered(self):
        triggered, platform, hint = self.fn("小红书文案 银狼cos", [])
        assert triggered is False
        assert platform == ""

    def test_no_keyword_not_triggered(self):
        triggered, _, _ = self.fn("今天天气真好", ["img1"])
        assert triggered is False

    # ── 平台触发词 ──────────────────────────────────────────────────

    def test_xiaohongshu_trigger(self):
        triggered, platform, hint = self.fn("小红书文案 漫展日记", ["img1"])
        assert triggered is True
        assert platform == "xiaohongshu"
        assert "漫展日记" in hint

    def test_weibo_trigger(self):
        triggered, platform, _ = self.fn("微博文案 cos银狼", ["img1"])
        assert triggered is True
        assert platform == "weibo"

    def test_bilibili_trigger(self):
        triggered, platform, _ = self.fn("B站文案 今天漫展", ["img1"])
        assert triggered is True
        assert platform == "bilibili"

    def test_bilibili_lowercase(self):
        triggered, platform, _ = self.fn("b站文案 测试", ["img1"])
        assert triggered is True
        assert platform == "bilibili"

    # ── 通用触发词默认 xiaohongshu ──────────────────────────────────

    def test_generic_trigger_defaults_xiaohongshu(self):
        triggered, platform, _ = self.fn("帮我写文案", ["img1"])
        assert triggered is True
        assert platform == "xiaohongshu"

    def test_generic_trigger_peizhen(self):
        triggered, platform, _ = self.fn("配文 银狼", ["img1"])
        assert triggered is True
        assert platform == "xiaohongshu"

    # ── hint 提取：触发词被剔除 ──────────────────────────────────────

    def test_hint_strips_keyword(self):
        _, _, hint = self.fn("小红书文案 银狼cos漫展记录", ["img1"])
        assert "小红书文案" not in hint
        assert "银狼cos漫展记录" in hint

    def test_hint_empty_when_only_keyword(self):
        _, _, hint = self.fn("小红书文案", ["img1"])
        assert hint == ""

    # ── 多张图 ──────────────────────────────────────────────────────

    def test_multiple_image_ids_triggers(self):
        triggered, _, _ = self.fn("小红书文案", ["img1", "img2", "img3"])
        assert triggered is True


# ══════════════════════════════════════════════════════════════════
# is_makeup_trigger
# ══════════════════════════════════════════════════════════════════

class TestIsMakeupTrigger:
    @pytest.fixture(autouse=True)
    def import_fn(self):
        from src.services.makeup_service import is_makeup_trigger
        self.fn = is_makeup_trigger

    # ── 触发条件：需要 image_id ──────────────────────────────────────

    def test_no_image_id_not_triggered(self):
        triggered, hint = self.fn("帮我看看妆", "")
        assert triggered is False

    def test_no_keyword_not_triggered(self):
        triggered, hint = self.fn("今天去漫展", "img1")
        assert triggered is False

    # ── 各触发词 ────────────────────────────────────────────────────

    def test_trigger_kankan_zhuang(self):
        triggered, _ = self.fn("帮我看看妆 银狼", "img1")
        assert triggered is True

    def test_trigger_zhuangrong_fenxi(self):
        triggered, _ = self.fn("妆容分析", "img1")
        assert triggered is True

    def test_trigger_fenxi_zhuangrong(self):
        triggered, _ = self.fn("分析妆容 请帮忙", "img1")
        assert triggered is True

    def test_trigger_haiyuandu(self):
        triggered, _ = self.fn("还原度怎么样", "img1")
        assert triggered is True

    def test_trigger_zhuangrong_fanfan(self):
        triggered, _ = self.fn("妆容反馈", "img1")
        assert triggered is True

    # ── hint 提取：触发词被剔除 ──────────────────────────────────────

    def test_hint_strips_keyword(self):
        _, hint = self.fn("帮我看看妆 蕾姆风格", "img1")
        assert "帮我看看妆" not in hint
        assert "蕾姆风格" in hint

    def test_hint_empty_when_only_keyword(self):
        _, hint = self.fn("妆容分析", "img1")
        assert hint == ""

    def test_hint_with_text_before_keyword(self):
        _, hint = self.fn("快帮我分析妆容谢谢", "img1")
        # 触发词"分析妆容"被移除，剩余部分作为 hint
        assert "分析妆容" not in hint
