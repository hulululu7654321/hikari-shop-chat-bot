#!/bin/bash
# =====================================================================
# 希卡莉 (Hikari) - 容器启动脚本
# 职责：
#   1. 以 root 身份对挂载卷执行 chown，确保应用用户可写
#   2. gosu 切换到 hikari 用户运行服务，避免 root 跑服务
# =====================================================================
set -e

# 需要可写的挂载目录（bind mount 由 hikari-init 容器创建，此处只修权）
WRITABLE_DIRS="/app/assets/voices /app/assets/uploads /app/assets/packages /app/logs /app/exports"

echo "[entrypoint] 修复挂载目录权限..."
for dir in $WRITABLE_DIRS; do
    mkdir -p "$dir"
    chown -R 1001:0 "$dir"
    echo "[entrypoint] OK: $dir"
done

echo "[entrypoint] 切换到 hikari 用户启动服务..."
exec gosu hikari "$@"
