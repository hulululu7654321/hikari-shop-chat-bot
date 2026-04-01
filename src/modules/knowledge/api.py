# -*- coding: utf-8 -*-
"""
src/modules/knowledge/api.py — B 端知识库管理 API
路由前缀：/api/k/
鉴权：X-Skill-Key（@require_skill_key）
"""

import os
import traceback
from quart import Blueprint, request, jsonify

from src.utils.auth import require_skill_key
from src.utils.logger import get_logger
from src.services.rag_service import get_rag_service, DEFAULT_TENANT

logger = get_logger(__name__)

bp = Blueprint("knowledge_api", __name__, url_prefix="/api/k")


# ══════════════════════════════════════════════════════════════════
# 知识条目
# ══════════════════════════════════════════════════════════════════

@bp.route("/entries", methods=["POST"])
@require_skill_key
async def add_entry():
    """
    写入知识条目。
    Body: {
        content: str,           # 必填，知识正文
        title?: str,            # 可选，默认取 content 前30字
        category?: str,         # product/faq/script/course/custom，默认 custom
        metadata?: dict,        # 附加信息，如 {"价格": "299"}
        tenant_id?: str         # 默认 hikari_default
    }
    """
    data    = await request.get_json(silent=True) or {}
    content = str(data.get("content", "")).strip()
    if not content:
        return jsonify({"code": 400, "message": "content 不能为空", "data": None}), 400

    try:
        rag      = get_rag_service()
        entry_id = await rag.upsert_entry(
            content=content,
            title=str(data.get("title", "")).strip(),
            category=str(data.get("category", "custom")),
            metadata=data.get("metadata") or {},
            source="manual",
            tenant_id=str(data.get("tenant_id", DEFAULT_TENANT)),
        )
        logger.info("[KB] 写入条目 | entry=%s", entry_id)
        return jsonify({"code": 200, "message": "ok", "data": {"entry_id": entry_id}})
    except Exception:
        logger.error("[KB] add_entry 失败: %s", traceback.format_exc())
        return jsonify({"code": 500, "message": "添加失败", "data": None}), 500


@bp.route("/entries/<entry_id>", methods=["DELETE"])
@require_skill_key
async def delete_entry(entry_id: str):
    """软删除一条条目。Body: { tenant_id?: str }"""
    data      = await request.get_json(silent=True) or {}
    tenant_id = str(data.get("tenant_id", DEFAULT_TENANT))

    try:
        rag = get_rag_service()
        ok  = await rag.deactivate(entry_id, tenant_id=tenant_id)
        logger.info("[KB] 软删除条目 | entry=%s | ok=%s", entry_id, ok)
        return jsonify({"code": 200, "message": "ok", "data": {"deleted": ok}})
    except Exception:
        logger.error("[KB] delete_entry 失败: %s", traceback.format_exc())
        return jsonify({"code": 500, "message": "删除失败", "data": None}), 500


@bp.route("/entries", methods=["GET"])
@require_skill_key
async def list_entries():
    """
    分页列出知识条目。
    Query: tenant_id, category, page, page_size
    """
    tenant_id = request.args.get("tenant_id", DEFAULT_TENANT)
    category  = request.args.get("category") or None
    page      = int(request.args.get("page", 1))
    page_size = min(int(request.args.get("page_size", 20)), 100)

    try:
        rag    = get_rag_service()
        result = await rag.list_entries(
            tenant_id=tenant_id,
            category=category,
            page=page,
            page_size=page_size,
        )
        return jsonify({"code": 200, "message": "ok", "data": result})
    except Exception:
        logger.error("[KB] list_entries 失败: %s", traceback.format_exc())
        return jsonify({"code": 500, "message": "查询失败", "data": None}), 500


# ══════════════════════════════════════════════════════════════════
# 同步
# ══════════════════════════════════════════════════════════════════

@bp.route("/sync/feishu", methods=["POST"])
@require_skill_key
async def sync_feishu():
    """
    从飞书同步（bitable 或 doc，由 type 字段区分）。
    Body（bitable）: {
        type: "bitable",
        app_token: str, table_id: str, category: str,
        field_template?: str, title_field?: str, metadata_fields?: list,
        tenant_id?: str
    }
    Body（doc）: {
        type: "doc",
        doc_token: str, category?: str, chunk_size?: int,
        tenant_id?: str
    }
    """
    data      = await request.get_json(silent=True) or {}
    sync_type = str(data.get("type", "bitable")).lower()
    tenant_id = str(data.get("tenant_id", DEFAULT_TENANT))
    rag       = get_rag_service()

    try:
        if sync_type == "bitable":
            app_token = str(data.get("app_token", "")).strip()
            table_id  = str(data.get("table_id", "")).strip()
            if not app_token or not table_id:
                return jsonify({"code": 400, "message": "app_token 和 table_id 不能为空", "data": None}), 400
            count = await rag.sync_from_feishu_bitable(
                app_token=app_token,
                table_id=table_id,
                category=str(data.get("category", "product")),
                field_template=data.get("field_template") or None,
                title_field=str(data.get("title_field", "")),
                metadata_fields=data.get("metadata_fields") or None,
                tenant_id=tenant_id,
            )
            logger.info("[KB] 飞书 Bitable 同步 | count=%d", count)

        elif sync_type == "doc":
            doc_token = str(data.get("doc_token", "")).strip()
            if not doc_token:
                return jsonify({"code": 400, "message": "doc_token 不能为空", "data": None}), 400
            count = await rag.sync_from_feishu_doc(
                doc_token=doc_token,
                category=str(data.get("category", "faq")),
                chunk_size=int(data.get("chunk_size", 300)),
                tenant_id=tenant_id,
            )
            logger.info("[KB] 飞书 Doc 同步 | count=%d", count)

        else:
            return jsonify({"code": 400, "message": "type 只能是 bitable 或 doc", "data": None}), 400

        return jsonify({"code": 200, "message": "ok", "data": {"synced": count}})

    except Exception:
        logger.error("[KB] sync_feishu 失败: %s", traceback.format_exc())
        return jsonify({"code": 500, "message": "飞书同步失败", "data": None}), 500


@bp.route("/sync/file", methods=["POST"])
@require_skill_key
async def sync_file():
    """
    上传本地文件同步（multipart/form-data）。
    字段：file（文件）, category（可选）, tenant_id（可选）
    支持：.md / .txt / .pdf，最大 5MB
    """
    files = await request.files
    if "file" not in files:
        return jsonify({"code": 400, "message": "请上传文件（字段名 file）", "data": None}), 400

    file     = files["file"]
    filename = file.filename or "upload.txt"
    if not any(filename.endswith(ext) for ext in (".md", ".txt", ".pdf")):
        return jsonify({"code": 400, "message": "仅支持 .md / .txt / .pdf 文件", "data": None}), 400

    form      = await request.form
    category  = form.get("category", "custom")
    tenant_id = form.get("tenant_id", DEFAULT_TENANT)

    try:
        # 前置体积预检（用 Content-Length header 拦截明显超限的请求，避免全量读入内存）
        # multipart overhead 极小，此处拦截足够准确；精确校验在 file.read() 后兜底
        content_length = request.content_length or 0
        if content_length > 5 * 1024 * 1024:
            return jsonify({"code": 400, "message": "文件不能超过 5MB", "data": None}), 400

        file_bytes = await file.read()
        if len(file_bytes) > 5 * 1024 * 1024:
            return jsonify({"code": 400, "message": "文件不能超过 5MB", "data": None}), 400

        rag   = get_rag_service()
        count = await rag.sync_from_file(
            file_bytes=file_bytes,
            filename=filename,
            category=category,
            tenant_id=tenant_id,
        )
        logger.info("[KB] 文件同步 | filename=%s | count=%d", filename, count)
        return jsonify({"code": 200, "message": "ok", "data": {"synced": count, "filename": filename}})

    except Exception:
        logger.error("[KB] sync_file 失败: %s", traceback.format_exc())
        return jsonify({"code": 500, "message": "文件处理失败", "data": None}), 500


# ══════════════════════════════════════════════════════════════════
# 推送
# ══════════════════════════════════════════════════════════════════

@bp.route("/push", methods=["POST"])
@require_skill_key
async def manual_push():
    """
    手动触发一次主动推送（测试/运营用）。
    Body: { user_id: str, category?: str, tenant_id?: str }
    Temporal 不可用时降级直接通过 rag_service 检索推送。
    """
    data    = await request.get_json(silent=True) or {}
    user_id = str(data.get("user_id", "")).strip()
    if not user_id:
        return jsonify({"code": 400, "message": "user_id 不能为空", "data": None}), 400

    tenant_id = str(data.get("tenant_id", DEFAULT_TENANT))

    # 优先通过 Temporal 触发（fire-and-forget）
    try:
        from temporalio.client import Client as TemporalClient
        from src.workflows.push_workflow import PushSchedulerWorkflow
        temporal_host = os.getenv("TEMPORAL_HOST", "hikari-temporal:7233")
        task_queue    = os.getenv("TEMPORAL_TASK_QUEUE", "hikari-main")
        client        = await TemporalClient.connect(temporal_host)
        await client.start_workflow(
            PushSchedulerWorkflow.run,
            id=f"manual-push-{user_id}",
            task_queue=task_queue,
        )
        logger.info("[KB] 手动推送 Temporal 触发 | user=%s", user_id[:8])
        return jsonify({"code": 200, "message": "推送已提交", "data": {"user_id": user_id}})
    except Exception as te:
        logger.warning("[KB] Temporal 不可用，降级直接执行: %s", te)

    # 降级
    try:
        from src.utils.socket_manager import get_socket_manager
        results = await get_rag_service().retrieve(query="", tenant_id=tenant_id)
        session_id = f"sess_{user_id}"
        sm = get_socket_manager()
        for item in results[:3]:
            await sm.send_to_user(session_id, {
                "type":    "knowledge_push",
                "content": item.get("content", ""),
            })
        logger.info("[KB] 手动推送降级执行 | user=%s | items=%d", user_id[:8], len(results))
        return jsonify({"code": 200, "message": "ok", "data": {"pushed": len(results)}})
    except Exception:
        logger.error("[KB] manual_push 失败: %s", traceback.format_exc())
        return jsonify({"code": 500, "message": "推送失败", "data": None}), 500


def init_knowledge_api():
    return bp
