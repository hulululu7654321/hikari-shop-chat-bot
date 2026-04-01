# -*- coding: utf-8 -*-
"""
飞书 MCP 客户端 — 希卡莉 (v2.1.0)
=====================================================================
重构对齐 [2026-03-29]：
- import 路径对齐新目录结构
- 环境变量 FEISHU_MCP_URL 对齐 MASTER v3.0 第十五节
  （容器名改为 hikari-lark-mcp，默认地址 http://hikari-lark-mcp:3001）
- 无旧版兼容，clean 版本

架构：
  hikari-lark-mcp 容器（lark-openapi-mcp，SSE 模式，端口 3001）
      ↑ HTTP POST /call-tool
  FeishuMCPClient（本文件）
      ↑ 调用
  rag_service.sync_from_feishu()
=====================================================================
"""

import os
import json
import traceback
from typing import Any

import httpx

from src.utils.logger import get_logger

logger = get_logger(__name__)

# MASTER v3.0 第十五节：容器名 hikari-lark-mcp，端口 3001
_MCP_BASE = os.getenv("FEISHU_MCP_URL", "http://hikari-lark-mcp:3001")


class FeishuMCPClient:
    """
    通过飞书官方 MCP SSE HTTP 接口读取飞书数据。
    工具调用端点：POST {base}/call-tool
    """

    def __init__(self):
        self.base = _MCP_BASE.rstrip("/")

    async def _call_tool(self, tool_name: str, arguments: dict) -> Any:
        """
        调用 MCP 工具。
        Body: {"name": tool_name, "arguments": {...}}
        返回已解析的 Python 对象（dict / str）。
        超时 30s，失败直接抛出异常由调用方处理。
        """
        url     = f"{self.base}/call-tool"
        payload = {"name": tool_name, "arguments": arguments}

        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(url, json=payload)
            resp.raise_for_status()
            data = resp.json()

        # MCP 工具结果格式：{"content": [{"type": "text", "text": "..."}]}
        for block in data.get("content", []):
            if block.get("type") == "text":
                text = block.get("text", "")
                try:
                    return json.loads(text)
                except json.JSONDecodeError:
                    return text
        return data

    # ── 多维表格（Bitable）────────────────────────────

    async def fetch_bitable(
        self,
        app_token: str,
        table_id: str,
        page_size: int = 100,
    ) -> list[dict[str, Any]]:
        """
        拉取多维表格所有记录，自动翻页。
        MCP 工具：bitable.v1.appTableRecord.list
        返回 fields 字典列表（含 _record_id）。
        """
        all_records: list[dict] = []
        page_token: str | None  = None

        while True:
            args: dict[str, Any] = {
                "app_token": app_token,
                "table_id":  table_id,
                "page_size": page_size,
            }
            if page_token:
                args["page_token"] = page_token

            try:
                result = await self._call_tool("bitable.v1.appTableRecord.list", args)
            except Exception:
                logger.error(f"[FeishuMCP] Bitable 拉取失败:\n{traceback.format_exc()}")
                break

            if not isinstance(result, dict):
                break

            items = result.get("data", {}).get("items", [])
            for item in items:
                fields = dict(item.get("fields", {}))
                fields["_record_id"] = item.get("record_id", "")
                all_records.append(fields)

            has_more   = result.get("data", {}).get("has_more", False)
            page_token = result.get("data", {}).get("page_token")
            if not has_more:
                break

        logger.info(f"[FeishuMCP] Bitable {table_id[:8]} 拉取完成 | 共 {len(all_records)} 条")
        return all_records

    # ── 普通文档（Doc）────────────────────────────────

    async def fetch_doc(self, doc_token: str) -> str:
        """
        拉取飞书文档纯文本内容。
        MCP 工具：docx.v1.document.rawContent
        """
        try:
            result = await self._call_tool(
                "docx.v1.document.rawContent",
                {"document_id": doc_token},
            )
            content = (
                result.get("data", {}).get("content", "")
                if isinstance(result, dict)
                else str(result)
            )
            logger.info(f"[FeishuMCP] Doc {doc_token[:8]} 拉取完成 | {len(content)} 字")
            return content
        except Exception:
            logger.error(f"[FeishuMCP] Doc 拉取失败:\n{traceback.format_exc()}")
            return ""

    # ── 工具：记录 → 可读文本 ─────────────────────────

    @staticmethod
    def bitable_record_to_text(record: dict, template: str | None = None) -> str:
        """
        把多维表格一行记录转成自然语言文本，供向量化。
        template 为可选格式字符串，如 "{品牌}：{商品名称}，价格{价格}元"。
        """
        if template:
            try:
                return template.format(**record)
            except KeyError:
                pass

        parts = [
            f"{k}：{v}"
            for k, v in record.items()
            if not k.startswith("_") and v and str(v).strip()
        ]
        return "，".join(parts)


# ══════════════════════════════════════════════════════════════════
# 单例导出
# ══════════════════════════════════════════════════════════════════

_instance: FeishuMCPClient | None = None


def get_feishu_helper() -> FeishuMCPClient:
    global _instance
    if _instance is None:
        _instance = FeishuMCPClient()
    return _instance
