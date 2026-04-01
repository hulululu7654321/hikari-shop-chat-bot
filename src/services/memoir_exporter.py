# -*- coding: utf-8 -*-
"""
画像归档导出器

每次调用生成一份 Markdown 格式的「灵魂报告」，落盘至
MEMOIR_EXPORT_DIR/{safe_name}_{user_id}/Memoir_{ts}.md
并通过 WebSocket 通知前端下载链接。
"""

import os
import re
import asyncio
import traceback
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

from openai import AsyncOpenAI

from src.utils.database import get_db_manager, COL_USERS, COL_FACTS
from src.utils.logger import get_logger
from src.utils.socket_manager import get_socket_manager
from src.core.hikari_prompts import (
    MEMOIR_GENERATION_PROMPT,
    PERSONA_REPORT_TEMPLATE,
    get_affinity_info,
)

logger = get_logger(__name__)


class MemoirExporter:
    _instance: "MemoirExporter | None" = None

    def __new__(cls) -> "MemoirExporter":
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self) -> None:
        if self._initialized:
            return

        self.db_manager = get_db_manager()
        self.db = self.db_manager.db
        self.socket_manager = get_socket_manager()

        self.client = AsyncOpenAI(
            base_url="https://ark.cn-beijing.volces.com/api/v3",
            api_key=os.getenv("DOUBAO_API_KEY"),
        )
        self.model = os.getenv("DOUBAO_MAIN_EP", "")

        # 导出根目录：优先使用 env，回退到 /app/exports
        export_dir = os.getenv("MEMOIR_EXPORT_DIR", "/app/exports")
        self.export_root = Path(export_dir)

        self._initialized = True
        logger.info(f"memoir_exporter initialized | export_root={self.export_root}")

    async def export_user_memoir_llm(
        self,
        user_id: str,
        user_name: str | None = None,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        """
        灵魂报告生成管线：
        1. 读取用户档案和事实记忆
        2. 按 category 聚合压缩记忆片段
        3. LLM 生成报告正文
        4. 渲染 Markdown 模板并落盘
        5. 若 session_id 非空，WebSocket 推送下载链接
        """
        try:
            # 1. 读取用户基础档案
            user_doc = await self.db[COL_USERS].find_one({"user_id": user_id})
            if not user_doc:
                return {"success": False, "msg": "用户档案不存在"}

            user_name = user_name or user_doc.get("user_name", "匿名Coser")
            safe_name = re.sub(r"[^\w\u4e00-\u9fa5]", "_", user_name)

            # 2. 从 COL_FACTS 聚合记忆（向量数据已迁 Qdrant，画像聚合以 facts 为准）
            cursor = self.db[COL_FACTS].find(
                {"user_id": user_id, "is_active": {"$ne": False}}
            ).sort("updated_at", -1).limit(50)
            memories = await cursor.to_list(length=50)

            if not memories:
                return {"success": False, "msg": "记忆池为空，无法生成报告"}

            # 3. 按 category 聚合，每组保留 importance 最高的 N 条
            fragments_per_cat = int(os.getenv("MEMOIR_FRAGMENTS_PER_CAT", 6))
            max_total = int(os.getenv("MEMOIR_MAX_FRAGMENTS", 30))

            cat_map: dict[str, list] = {}
            for m in memories:
                cat = m.get("category", "general")
                cat_map.setdefault(cat, []).append(m)

            compressed: list[str] = []
            for cat, items in cat_map.items():
                items.sort(
                    key=lambda x: x.get("importance", x.get("confidence", 0)),
                    reverse=True,
                )
                for item in items[:fragments_per_cat]:
                    if item.get("content"):
                        compressed.append(f"[{cat}] {item['content']}")

            compressed = compressed[:max_total]
            fragments_str = "\n".join(f"- {line}" for line in compressed)
            logger.debug(
                f"memoir: compressed {len(compressed)} fragments from {len(memories)} raw | user={user_id[:8]}"
            )

            prompt = (
                str(MEMOIR_GENERATION_PROMPT)
                .replace("{user_name}", str(user_name))
                .replace("{memory_fragments}", str(fragments_str))
            )

            # 4. LLM 生成报告正文
            logger.info(f"memoir: generating report | user={user_name}")
            res = await self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": "你是一只性格傲娇的看板猫娘希卡莉。"},
                    {"role": "user", "content": prompt},
                ],
            )
            report_content = res.choices[0].message.content.strip()

            # 5. 渲染 Markdown 模板
            affinity = int(user_doc.get("affinity", 10))
            final_md = (
                str(PERSONA_REPORT_TEMPLATE)
                .replace("{user_name}", str(user_name))
                .replace("{user_id}", str(user_id[:8]))
                .replace("{affinity}", str(affinity))
                .replace(
                    "{affinity_label}",
                    str(get_affinity_info(affinity).get("label", "初识")),
                )
                .replace("{last_active}", datetime.now(timezone.utc).astimezone(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M"))
                .replace("{llm_report_content}", str(report_content))
            )

            # 6. 落盘（UID 物理隔离：exports/{safe_name}_{user_id}/Memoir_{ts}.md）
            sub_folder = f"{safe_name}_{user_id}"
            target_dir = self.export_root / sub_folder
            target_dir.mkdir(parents=True, exist_ok=True)

            file_name = f"Memoir_{int(datetime.now().timestamp())}.md"
            file_path = target_dir / file_name

            def _write(path: str, data: str) -> None:
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC)
                try:
                    os.write(fd, data.encode("utf-8"))
                    os.fsync(fd)
                finally:
                    os.close(fd)

            await asyncio.to_thread(_write, str(file_path), final_md)

            relative_url = f"{sub_folder}/{file_name}"
            logger.info(f"memoir: report saved | path={relative_url}")

            # 7. WebSocket 推送
            if session_id:
                await self.socket_manager.send_to_user(
                    session_id,
                    {
                        "type": "memoir_ready",
                        "file_url": relative_url,
                        "user_name": user_name,
                        "is_report": True,
                    },
                )

            return {"success": True, "file_url": relative_url}

        except Exception:
            logger.error(f"memoir: export failed: {traceback.format_exc()}")
            return {"success": False}


def get_memoir_exporter() -> MemoirExporter:
    return MemoirExporter()
