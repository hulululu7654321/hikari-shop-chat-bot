# -*- coding: utf-8 -*-
"""
日志管理工具 — 希卡莉 (v3.6.0)
=====================================================================
重构对齐 [2026-03-29]：
- 目录结构对齐 MASTER v3.0（src/utils/logger.py，路径不变）
- LOG_FORMAT=json 时输出结构化 JSON，含 inst/lvl/msg
- LOG_FORMAT=text 时彩色终端输出，含实例 ID
- 日志文件路径锁定 /app/logs（容器内路径）
=====================================================================
"""

import os
import sys
import json
import logging
import threading
import socket
from contextvars import ContextVar
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path


# ==========================================
# 0. Trace Context
# ==========================================

# 每个异步任务独立的 trace_id，跨 await 自动传播
trace_id_var: ContextVar[str] = ContextVar("trace_id", default="-")


# ==========================================
# 1. 终端颜色配置
# ==========================================

class LogColor:
    BLUE    = '\033[94m'
    CYAN    = '\033[96m'
    GREEN   = '\033[92m'
    YELLOW  = '\033[93m'
    RED     = '\033[91m'
    BOLD    = '\033[1m'
    MAGENTA = '\033[95m'
    PINK    = '\033[38;5;206m'
    ORANGE  = '\033[38;5;214m'
    END     = '\033[0m'


# ==========================================
# 2. 格式化器
# ==========================================

class HikariNyaFormatter(logging.Formatter):
    def __init__(self, use_json: bool = False):
        super().__init__()
        self.use_json    = use_json
        self.instance_id = socket.gethostname()[:10]

    def format(self, record):
        record.asctime = datetime.now(timezone.utc).strftime('%H:%M:%S.%f')[:-3]
        if self.use_json:
            return self._format_json(record)
        return self._format_text(record)

    def _format_json(self, record) -> str:
        log_data = {
            "ts":      record.asctime,
            "inst":    self.instance_id,
            "lvl":     record.levelname,
            "name":    record.name,
            "trace":   trace_id_var.get(),
            "msg":     record.getMessage(),
        }
        if record.exc_info:
            log_data["exception"] = self.formatException(record.exc_info)
        return json.dumps(log_data, ensure_ascii=False)

    def _format_text(self, record) -> str:
        level_color = {
            'DEBUG':   LogColor.CYAN,
            'INFO':    LogColor.GREEN,
            'WARNING': LogColor.YELLOW,
            'ERROR':   LogColor.RED,
        }.get(record.levelname, '')
        msg = record.getMessage()

        # 原子操作高亮
        if any(kw in msg for kw in ["$inc", "$set", "$push", "Atomic"]):
            msg = f"{LogColor.ORANGE}{LogColor.BOLD}⚙️ [ATOMIC] {msg}{LogColor.END}"

        sid       = str(getattr(record, 'session_id', 'SYSTEM'))
        sid_short = sid[:8] if len(sid) > 8 else sid.center(8)
        trace     = trace_id_var.get()
        trace_short = trace[:8] if trace != "-" else "--------"

        return (
            f"[{LogColor.BLUE}{record.asctime}{LogColor.END}] "
            f"[{LogColor.PINK}{self.instance_id}{LogColor.END}] "
            f"[{level_color}{record.levelname:^7}{LogColor.END}] "
            f"[{LogColor.BOLD}{LogColor.CYAN}{sid_short}{LogColor.END}] "
            f"[{LogColor.MAGENTA}{trace_short}{LogColor.END}] "
            f"- {msg}"
        )


# ==========================================
# 3. 日志管理器（单例）
# ==========================================

class LoggerManager:
    _instance = None
    _lock     = threading.Lock()

    def __new__(cls):
        with cls._lock:
            if cls._instance is None:
                cls._instance = super().__new__(cls)
                cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if self._initialized:
            return

        # 容器内固定路径 /app/logs；本地开发降级到项目根 logs/
        log_dir_env = os.getenv("LOG_DIR", "")
        if log_dir_env:
            self.base_log_dir = Path(log_dir_env)
        else:
            self.base_log_dir = Path("/app/logs")

        self.hostname = socket.gethostname()
        self.log_file = self.base_log_dir / f"hikari_{self.hostname}.log"
        self.level    = getattr(logging, os.getenv("LOG_LEVEL", "INFO").upper(), logging.INFO)
        self.log_fmt  = os.getenv("LOG_FORMAT", "text").lower()

        self._ensure_log_dir()
        self._initialized = True
        print(f"🐾 [Logger] 容器实例: {self.hostname} | level={os.getenv('LOG_LEVEL','INFO')} | fmt={self.log_fmt}")

    def _ensure_log_dir(self):
        try:
            self.base_log_dir.mkdir(parents=True, exist_ok=True)
        except Exception:
            # 目录不可写时禁用文件日志，仅走 stdout
            self.log_file = None

    def get_logger(self, name: str) -> logging.Logger:
        logger = logging.getLogger(name)
        if logger.hasHandlers():
            return logger   # 已初始化过，直接返回，不重复添加 handler

        logger.setLevel(self.level)
        logger.propagate = False
        is_json = (self.log_fmt == "json")

        # 控制台 handler（docker logs / 本地终端）
        console = logging.StreamHandler(sys.stdout)
        console.setFormatter(HikariNyaFormatter(use_json=is_json))
        logger.addHandler(console)

        # 文件 handler（带自动轮转，3 个备份，每个 15 MB）
        if self.log_file is not None:
            try:
                fh = RotatingFileHandler(
                    filename=str(self.log_file),
                    maxBytes=15 * 1024 * 1024,
                    backupCount=3,
                    encoding="utf-8",
                )
                fh.setFormatter(HikariNyaFormatter(use_json=is_json))
                logger.addHandler(fh)
            except Exception as e:
                print(f"❌ [Logger] 文件 handler 创建失败（仅控制台输出）: {e}")

        return logger


# ==========================================
# 4. 公共导出
# ==========================================

def get_logger(name: str = "hikari") -> logging.Logger:
    """
    所有模块统一调用此函数获取 logger：
        from src.utils.logger import get_logger
        logger = get_logger(__name__)
    """
    return LoggerManager().get_logger(name)
