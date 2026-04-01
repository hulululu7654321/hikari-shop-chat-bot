# =====================================================================
# 希卡莉 (Hikari) - Gunicorn 配置
# 用途：hikari-api 容器专用（hikari-worker 不使用此文件）
# 基于：重构参考文档 MASTER v3.0
# =====================================================================

import os
import sys

# 监听地址（对齐重构文档端口分配，hikari-api = 8000）
bind = "0.0.0.0:8000"

# Worker 模式：ASGI，支持 async def 视图和 WebSocket
worker_class = "uvicorn.workers.UvicornWorker"

# Worker 数量：2 个进程
# 重构后并发任务（图生图/TTS/推送）已迁移到 Temporal Worker 容器，
# api 容器只处理 HTTP/WS 请求，2 worker 足够，避免多进程竞争 Redis Stream 消费
workers = 2

# 每 Worker 并发连接数
worker_connections = 2048

# 超时（TTS 任务已由 Temporal 异步接管，HTTP 请求本身不需要 300s）
timeout = 120
keepalive = 5

# 预加载（Copy-on-Write 节省内存）
preload_app = True

# 确保 src/ 可被 import
sys.path.append(os.getcwd())

# 日志（输出到标准流，docker logs 可直接观测）
accesslog = "-"
errorlog  = "-"
loglevel  = os.getenv("LOG_LEVEL", "info").lower()

access_log_format = '%(h)s %(l)s %(u)s %(t)s "%(r)s" %(s)s %(b)s "%(f)s" "%(a)s" %(D)sµs'

# 安全限制
limit_request_line       = 4094
limit_request_fields     = 100
limit_request_field_size = 8190
