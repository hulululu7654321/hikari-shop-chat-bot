# -*- coding: utf-8 -*-
"""
资产辅助工具 — 希卡莉 (v3.0.0)
=====================================================================
重构对齐 [2026-03-29]：
- import 路径对齐新目录结构
- 环境变量对齐 MASTER v3.0 第十五节
- 无旧版兼容，clean 版本
=====================================================================
"""

import os
import re
import time
from datetime import datetime
from pathlib import Path

from src.utils.logger import get_logger

logger = get_logger(__name__)


class AssetHelper:
    def __init__(self):
        self.sample_root      = Path(os.getenv("SAMPLE_ROOT", "/app/assets/samples"))
        self.voice_root       = Path(os.getenv("VOICE_OUTPUT_ROOT", "/app/assets/voices"))
        self.host_sample_root = os.getenv("HOST_SAMPLE_ROOT", "").rstrip("\\")
        self.default_character = os.getenv("DEFAULT_VOICE_CHARACTER", "女性1_zh")

        self.sample_root.mkdir(parents=True, exist_ok=True)
        self.voice_root.mkdir(parents=True, exist_ok=True)

        logger.info(f"[AssetHelper] 初始化完成 | host_sample_root={self.host_sample_root or '(未配置)'}")

    def list_available_characters(self) -> list[str]:
        """扫描 samples 目录下所有声线包文件夹"""
        try:
            return [d.name for d in self.sample_root.iterdir() if d.is_dir()]
        except Exception as e:
            logger.error(f"[AssetHelper] 扫描声线包失败: {e}")
            return [self.default_character]

    def get_reference_pair(
        self, character: str | None = None, emotion: str = "neutral"
    ) -> tuple[str | None, str, str]:
        """
        根据角色和情绪定位参考音频。
        返回 (容器内wav路径, 参考文本, 宿主机Windows路径)
        宿主机路径供 TTS 引擎（运行在 Windows 上）定位文件。
        全部失败返回 (None, "", "")。
        """
        target_char = character or self.default_character
        char_dir    = self.sample_root / target_char

        if not char_dir.exists():
            logger.warning(f"[AssetHelper] 角色库 [{target_char}] 不存在，回退默认角色")
            char_dir    = self.sample_root / self.default_character
            target_char = self.default_character

        internal_wav = char_dir / f"{emotion}.wav"
        internal_txt = char_dir / f"{emotion}.txt"

        # 情绪级回退 → neutral
        if not internal_wav.exists() and emotion != "neutral":
            internal_wav = char_dir / "neutral.wav"
            internal_txt = char_dir / "neutral.txt"

        if not internal_wav.exists():
            logger.error(f"[AssetHelper] 角色 {target_char} 缺失 neutral.wav")
            return None, "", ""

        # 读参考文本
        ref_text = ""
        if internal_txt.exists():
            try:
                ref_text = internal_txt.read_text(encoding="utf-8").strip()
            except Exception:
                pass

        # 安全校验：ref_wav 路径必须在 sample_root 下，防路径注入
        try:
            internal_wav.resolve().relative_to(self.sample_root.resolve())
        except ValueError:
            logger.error(f"[AssetHelper] 路径安全检查失败: {internal_wav}")
            return None, "", ""

        # 宿主机 Windows 路径（供 TTS 引擎使用）
        win_path = ""
        if self.host_sample_root:
            win_path = f"{self.host_sample_root}\\{target_char}\\{internal_wav.name}"

        return str(internal_wav), ref_text, win_path

    def get_voice_metadata(
        self, user_id: str, session_id: str, real_name: str = "匿名Coser"
    ) -> tuple[Path, str]:
        """
        确定 TTS 输出文件路径。
        格式：{VOICE_OUTPUT_ROOT}/{safe_name}_{user_id[:8]}/{session_id[:8]}_{timestamp}.wav
        返回 (full_path: Path, rel_path: str)
        """
        safe_name   = self._sanitize_filename(real_name)
        folder_name = f"{safe_name}_{user_id[:8]}"
        char_dir    = self.voice_root / folder_name
        char_dir.mkdir(parents=True, exist_ok=True)

        timestamp = datetime.now().strftime("%H%M%S_%f")
        file_name = f"{session_id[:8]}_{timestamp}.wav"
        full_path = char_dir / file_name
        rel_path  = f"{folder_name}/{file_name}"

        return full_path, rel_path

    def sanitize_text(self, text: str) -> str:
        """
        TTS 文本净化：去除情绪标签、括号内容，规范发音。
        "coser" → "扣瑟"（防 TTS 英文发音怪）
        """
        if not text:
            return ""
        t = re.sub(r'[*（(【\[].*?[*）)\]】]', "", text)
        t = t.lower().replace("coser", "扣瑟")
        t = re.sub(r'[^\u4e00-\u9fa5a-zA-Z0-9,，.?!！？。]', "", t)
        return t.strip()

    def auto_cleanup(self, max_age_days: int = 7) -> None:
        """清理超期语音文件"""
        threshold = time.time() - max_age_days * 86400
        try:
            for f in self.voice_root.rglob("*.wav"):
                if f.stat().st_mtime < threshold:
                    f.unlink()
        except Exception as e:
            logger.error(f"[AssetHelper] 清理过期文件失败: {e}")

    # ── 内部工具 ──────────────────────────────────────

    @staticmethod
    def _sanitize_filename(name: str) -> str:
        """允许中文、字母、数字、下划线，其余替换为下划线"""
        if not name:
            return "Unknown"
        return re.sub(r"[^\w\u4e00-\u9fa5]", "_", name).strip("_") or "Unknown"


# ══════════════════════════════════════════════════════════════════
# 单例导出
# ══════════════════════════════════════════════════════════════════

_instance: AssetHelper | None = None


def get_asset_helper() -> AssetHelper:
    global _instance
    if _instance is None:
        _instance = AssetHelper()
    return _instance
