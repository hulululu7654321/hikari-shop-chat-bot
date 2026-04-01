# -*- coding: utf-8 -*-
"""
tests/test_utils.py — 工具函数单测

覆盖（纯函数，无 I/O）：
  logger.HikariNyaFormatter._format_json()  JSON 格式化
  logger.HikariNyaFormatter._format_text()  文本格式化（trace 注入）
  auth._refresh_key()                        Redis key 格式
"""

import json
import logging
import pytest


# ══════════════════════════════════════════════════════════════════
# HikariNyaFormatter
# ══════════════════════════════════════════════════════════════════

class TestHikariNyaFormatter:
    @pytest.fixture(autouse=True)
    def setup(self):
        from src.utils.logger import HikariNyaFormatter, trace_id_var
        self.trace_id_var = trace_id_var
        self.JsonFormatter = HikariNyaFormatter(use_json=True)
        self.TextFormatter = HikariNyaFormatter(use_json=False)
        yield
        # 清理 trace_id
        trace_id_var.set("-")

    def _make_record(self, msg="test message", level=logging.INFO):
        record = logging.LogRecord(
            name="test", level=level, pathname="", lineno=0,
            msg=msg, args=(), exc_info=None
        )
        record.asctime = "12:00:00.000"
        return record

    # ── JSON 格式化 ─────────────────────────────────────────────────

    def test_json_output_is_valid_json(self):
        record = self._make_record("hello")
        output = self.JsonFormatter.format(record)
        parsed = json.loads(output)
        assert isinstance(parsed, dict)

    def test_json_contains_required_fields(self):
        record = self._make_record("hello world")
        output = json.loads(self.JsonFormatter.format(record))
        assert "ts" in output
        assert "lvl" in output
        assert "msg" in output
        assert "trace" in output
        assert "inst" in output

    def test_json_msg_content(self):
        record = self._make_record("specific message")
        output = json.loads(self.JsonFormatter.format(record))
        assert output["msg"] == "specific message"

    def test_json_trace_default(self):
        self.trace_id_var.set("-")
        record = self._make_record()
        output = json.loads(self.JsonFormatter.format(record))
        assert output["trace"] == "-"

    def test_json_trace_propagated(self):
        self.trace_id_var.set("abc12345")
        record = self._make_record()
        output = json.loads(self.JsonFormatter.format(record))
        assert output["trace"] == "abc12345"

    def test_json_level_name(self):
        record = self._make_record(level=logging.ERROR)
        output = json.loads(self.JsonFormatter.format(record))
        assert output["lvl"] == "ERROR"

    # ── 文本格式化 ──────────────────────────────────────────────────

    def test_text_output_is_string(self):
        record = self._make_record("hello")
        output = self.TextFormatter.format(record)
        assert isinstance(output, str)

    def test_text_contains_message(self):
        record = self._make_record("看看我在文本里")
        output = self.TextFormatter.format(record)
        assert "看看我在文本里" in output

    def test_text_contains_trace_id(self):
        self.trace_id_var.set("traceid99")
        record = self._make_record()
        output = self.TextFormatter.format(record)
        # trace_short 取前8位
        assert "traceid9" in output

    def test_text_trace_default_placeholder(self):
        self.trace_id_var.set("-")
        record = self._make_record()
        output = self.TextFormatter.format(record)
        assert "--------" in output


# ══════════════════════════════════════════════════════════════════
# auth._refresh_key
# ══════════════════════════════════════════════════════════════════

class TestRefreshKey:
    def setup_method(self):
        from src.utils.auth import _refresh_key
        self.fn = _refresh_key

    def test_key_format(self):
        key = self.fn("user123", "jti456")
        assert key == "hikari:refresh:user123:jti456"

    def test_key_contains_user_id(self):
        key = self.fn("uid_abc", "jti_xyz")
        assert "uid_abc" in key

    def test_key_contains_jti(self):
        key = self.fn("uid_abc", "jti_xyz")
        assert "jti_xyz" in key

    def test_different_users_different_keys(self):
        k1 = self.fn("user1", "jti1")
        k2 = self.fn("user2", "jti1")
        assert k1 != k2

    def test_different_jti_different_keys(self):
        k1 = self.fn("user1", "jti1")
        k2 = self.fn("user1", "jti2")
        assert k1 != k2
