# -*- coding: utf-8 -*-
"""
src/main.py — hikari-api 容器入口
=====================================================================
三层解耦（MASTER v3.0 第二节）：
  URL 契约（永不改）→ Nginx 路由 → 代码蓝图

蓝图注册表：
  /api/u/    modules/user/api.py          用户域（登录/WS/历史/图片/语音/报告）
  /api/m/    modules/manage/api.py        管理域（后台管理小程序）
  /api/sys/  modules/manage/admin_api.py  系统域（X-Admin-Key 紧急操作）
  /api/s/    modules/store/api.py         到店域（STORE_ENABLED=true 时注册）
  /api/k/    modules/knowledge/api.py     知识库域（X-Skill-Key）
  /api/p/    modules/public/api.py        公开域（hikari-shop 容器，此处为本地开发兜底）

健康检查：GET /api/sys/health（无鉴权，供 Nginx upstream 探测）
Temporal 推送调度由 hikari-worker 负责，hikari-api 不挂载 push_scheduler。
=====================================================================
"""

import os
import sys
import uuid
import traceback
from datetime import datetime, timezone

from quart import Quart, request, jsonify
from quart_cors import cors

ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "../"))
if ROOT_DIR not in sys.path:
    sys.path.append(ROOT_DIR)

from src.utils.logger import get_logger, trace_id_var
from src.utils.database import get_db_manager

logger = get_logger(__name__)


# ──────────────────────────────────────────
# ASGI 防崩中间件
# ──────────────────────────────────────────

class ASGIWebsocketCrashGuard:
    """拦截 Quart/Uvicorn 之间的 WebSocket 异常断连报错，防止单次网络波动崩溃 Worker。"""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        try:
            await self.app(scope, receive, send)
        except RuntimeError as e:
            if "Unexpected ASGI message 'websocket.close'" in str(e):
                logger.warning("[ASGI-Guard] 已拦截无效 WebSocket Close 报错，保住主进程。")
            else:
                raise


# ──────────────────────────────────────────
# 应用工厂
# ──────────────────────────────────────────

def create_app() -> Quart:
    app = Quart(__name__)
    app.asgi_app = ASGIWebsocketCrashGuard(app.asgi_app)

    # ── 启动预热 ──────────────────────────────
    @app.before_serving
    async def startup():
        try:
            db_manager = get_db_manager()
            await db_manager.ensure_indexes()

            from src.services.rag_service import get_rag_service
            await get_rag_service().ensure_indexes()

            from src.utils.qdrant_client import ensure_collection
            await ensure_collection()

            logger.info("[System-Warmup] 索引初始化完毕")
        except Exception as e:
            logger.error(f"[Critical-Fail] 后端初始化失败: {e}")
            sys.exit(1)

        from src.utils.socket_manager import get_socket_manager
        app.add_background_task(get_socket_manager().start_stream_consumer)
        logger.info("[Stream-Consumer] Redis Stream 消费者已启动")

    # ── 请求 Trace ────────────────────────────
    @app.before_request
    async def inject_trace_id():
        # 优先使用上游传入的 X-Trace-ID（跨容器调用链路延续）
        # 否则生成新的 trace_id（链路起点）
        tid = request.headers.get("X-Trace-ID") or uuid.uuid4().hex[:16]
        trace_id_var.set(tid)

    # ── 蓝图注册 ──────────────────────────────

    # 用户域 /api/u/
    try:
        from src.modules.users.api import init_user_api
        app.register_blueprint(init_user_api())
        logger.info("[Blueprint] user_api     /api/u/ 注册成功")
    except Exception as e:
        logger.error(f"[Blueprint-Error] user_api: {e}")

    # 管理域 /api/m/
    try:
        from src.modules.manage.api import init_manage_api
        app.register_blueprint(init_manage_api())
        logger.info("[Blueprint] manage_api   /api/m/ 注册成功")
    except Exception as e:
        logger.error(f"[Blueprint-Error] manage_api: {e}")

    # 系统域 /api/sys/
    try:
        from src.modules.manage.admin_api import init_admin_api
        app.register_blueprint(init_admin_api())
        logger.info("[Blueprint] admin_api    /api/sys/ 注册成功")
    except Exception as e:
        logger.error(f"[Blueprint-Error] admin_api: {e}")

    # 到店域 /api/s/（按需启用）
    if os.getenv("STORE_ENABLED", "false").lower() == "true":
        try:
            from src.modules.store.api import init_store_api
            app.register_blueprint(init_store_api())
            logger.info("[Blueprint] store_api    /api/s/ 注册成功")
        except Exception as e:
            logger.error(f"[Blueprint-Error] store_api: {e}")

    # 知识库域 /api/k/
    try:
        from src.modules.knowledge.api import init_knowledge_api
        app.register_blueprint(init_knowledge_api())
        logger.info("[Blueprint] knowledge_api /api/k/ 注册成功")
    except Exception as e:
        logger.error(f"[Blueprint-Error] knowledge_api: {e}")

    # 公开域 /api/p/（生产由 hikari-shop 容器承载，此处供本地开发兜底）
    if os.getenv("REGISTER_PUBLIC_API", "false").lower() == "true":
        try:
            from src.modules.public.api import init_public_api
            app.register_blueprint(init_public_api())
            logger.info("[Blueprint] public_api   /api/p/ 注册成功")
        except Exception as e:
            logger.error(f"[Blueprint-Error] public_api: {e}")

    # ── 跨域 ──────────────────────────────────
    cors_origin  = os.getenv("CORS_ORIGIN", "*")
    app_with_cors = cors(app, allow_origin=cors_origin)

    # ── 健康检查（无鉴权） ──────────────────────
    @app.route("/api/sys/health", methods=["GET"])
    async def health_check():
        return jsonify({
            "code":    200,
            "message": "ok",
            "data": {
                "status":    "healthy",
                "timestamp": datetime.now(timezone.utc).isoformat(),
            },
        }), 200

    # ── 错误处理 ──────────────────────────────
    @app.errorhandler(404)
    async def not_found(e):
        return jsonify({"code": 404, "message": "路径不存在", "data": None}), 404

    @app.errorhandler(500)
    async def internal_error(e):
        logger.error(f"[System-500] {traceback.format_exc()}")
        return jsonify({"code": 500, "message": "服务内部错误", "data": None}), 500

    return app_with_cors


# ── 物理实例化 ──
app = create_app()

if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("FLASK_PORT", 8000))
    uvicorn.run(
        "src.main:app",
        host="0.0.0.0",
        port=port,
        reload=(os.getenv("APP_ENVIRONMENT") == "development"),
        workers=1,
    )
