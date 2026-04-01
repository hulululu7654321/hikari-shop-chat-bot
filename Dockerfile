# =====================================================================
# 希卡莉 (Hikari) - 主镜像
# 用途：hikari-api 容器 + hikari-worker 容器共用（入口不同）
#   hikari-api:    gunicorn src.main:app -b 0.0.0.0:8000
#   hikari-worker: python -m src.worker_main
# 基于：重构参考文档 MASTER v3.0
# =====================================================================

# 1. 基础镜像（华为云镜像源，适配国内构建环境）
FROM swr.cn-north-4.myhuaweicloud.com/ddn-k8s/docker.io/python:3.12-slim

# 2. 系统环境变量
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app \
    TZ=Asia/Shanghai \
    DEBIAN_FRONTEND=noninteractive

# 3. 系统依赖（适配 Debian Bookworm debian.sources 路径）
RUN (if [ -f /etc/apt/sources.list.d/debian.sources ]; then \
        sed -i 's/deb.debian.org/mirrors.aliyun.com/g' /etc/apt/sources.list.d/debian.sources; \
    fi) && \
    apt-get update -y && \
    apt-get install -y --no-install-recommends \
        ffmpeg curl tzdata gcc python3-dev libffi-dev libssl-dev \
        libmagic1 gosu \
    && ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

# 4. 工作目录
WORKDIR /app

# 5. Python 依赖（分层缓存：依赖层在代码层之前，代码变动不触发重装）
COPY requirements.txt .
RUN pip install --no-cache-dir -U pip -i https://pypi.tuna.tsinghua.edu.cn/simple && \
    pip install --no-cache-dir -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple

# 6. 创建运行用户和挂载点
RUN useradd -r -u 1001 -g root hikari && \
    mkdir -p \
        /app/logs \
        /app/exports \
        /app/assets/voices \
        /app/assets/samples \
        /app/assets/uploads \
        /app/assets/packages \
    && chown -R hikari:root \
        /app/logs \
        /app/exports \
        /app/assets/voices \
        /app/assets/samples \
        /app/assets/uploads \
        /app/assets/packages \
    && chmod -R 750 \
        /app/logs \
        /app/exports \
        /app/assets/voices \
        /app/assets/samples \
        /app/assets/uploads \
        /app/assets/packages

# 7. 拷贝代码（src/ 包含所有业务逻辑）
COPY gunicorn.conf.py .
COPY src/ ./src/

# 8. 启动脚本（root 阶段赋权，entrypoint 内部 gosu 切换到 hikari）
COPY deployment/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# 9. 暴露端口（hikari-api 监听 8000；hikari-worker 不监听端口，EXPOSE 无实际作用但保留文档意义）
EXPOSE 8000

# 10. 健康检查（仅对 hikari-api 有效；hikari-worker 由 Temporal Dashboard 监控）
HEALTHCHECK --interval=30s --timeout=10s --start-period=20s --retries=3 \
    CMD curl -f http://localhost:8000/api/sys/health || exit 1

# 11. 启动
# 默认为 api 入口；docker-compose 中 hikari-worker 通过 command 覆盖为 python -m src.worker_main
ENTRYPOINT ["/entrypoint.sh"]
CMD ["gunicorn", "src.main:app", \
     "-w", "2", \
     "-k", "uvicorn.workers.UvicornWorker", \
     "-b", "0.0.0.0:8000", \
     "--timeout", "120"]
