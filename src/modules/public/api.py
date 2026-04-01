# -*- coding: utf-8 -*-
"""
src/modules/public/api.py — 公开域 API（hikari-shop 容器）
路由前缀：/api/p/
鉴权：无（公开访问）
容器：hikari-shop（独立容器，Nginx 层 /api/p/ → hikari-shop:8001）
"""

import os
import traceback
from quart import Blueprint, request, jsonify, send_from_directory

from src.utils.database import get_db_manager, COL_PACKAGES
from src.utils.logger import get_logger

logger = get_logger(__name__)

bp = Blueprint("public_api", __name__, url_prefix="/api/p")

# ── 套餐图片根目录 ──
_PACKAGES_ASSET_ROOT = os.getenv("ASSETS_ROOT", "/app/assets") + "/packages"


# ══════════════════════════════════════════════════════════════════
# 商城信息
# ══════════════════════════════════════════════════════════════════

@bp.route("/shop", methods=["GET"])
async def shop_info():
    """
    商城基本信息（店名、slogan、联系方式）。
    数据来自环境变量，无需数据库查询。
    """
    return jsonify({
        "code": 200,
        "message": "ok",
        "data": {
            "name":          os.getenv("SHOP_NAME", "希卡莉经纪事务所"),
            "slogan":        os.getenv("SHOP_SLOGAN", "专属 coser 的 AI 伴侣 · 线上线下一体服务"),
            "contact_wechat": os.getenv("SHOP_CONTACT_WECHAT", ""),
            "contact_note":   os.getenv("SHOP_CONTACT_NOTE", "添加时备注：希卡莉"),
        },
    })


# ══════════════════════���═══════════════════════════════════════════
# 套餐
# ══════════════════════════════════════════════════════════════════

@bp.route("/packages", methods=["GET"])
async def list_packages():
    """
    上架套餐列表（公开，无需登录）。
    Query: store_id（可选，不传则返回所有上架套餐）
    返回所有 is_active=True 的套餐（含到店套餐和线上套餐）。
    """
    store_id = request.args.get("store_id", "").strip()

    try:
        db    = get_db_manager().db
        query = {"is_active": True}
        if store_id:
            query["store_id"] = store_id

        cursor   = db[COL_PACKAGES].find(
            query,
            {"_id": 0, "package_id": 1, "name": 1, "price": 1,
             "description": 1, "images": 1, "sort_order": 1,
             "category": 1, "type": 1, "store_id": 1, "store_name": 1}
        ).sort("sort_order", 1)
        packages = await cursor.to_list(length=200)

        # 拼接图片 URL（images 字段存储格式已是 "pkg_xxx/filename"，直接拼前缀）
        base_url = os.getenv("PUBLIC_BASE_URL", "").rstrip("/")
        if not base_url:
            # PUBLIC_BASE_URL 未配时用请求 host 兜底
            base_url = ""
        for pkg in packages:
            imgs = pkg.get("images") or []
            pkg["images"] = [
                f"{base_url}/api/p/images/{img}" if base_url else f"/api/p/images/{img}"
                for img in imgs
            ]

        return jsonify({"code": 200, "message": "ok", "data": {"list": packages}})

    except Exception:
        logger.error("[Public] list_packages 失败: %s", traceback.format_exc())
        return jsonify({"code": 500, "message": "服务内部错误", "data": None}), 500


# ══════════════════════════════════════════════════════════════════
# 套餐图片
# ══════════════════════════════════════════════════════════════════

@bp.route("/images/<package_id>/<path:img>", methods=["GET"])
async def package_image(package_id: str, img: str):
    """
    套餐图片文件（静态资源）。
    路径：ASSETS_ROOT/packages/{package_id}/{img}
    生产环境建议 Nginx 直接 alias，此接口作为兜底。
    """
    # 路径安全：normpath + 前缀校验，防止 %2e%2e 等编码绕过
    try:
        pkg_dir  = os.path.realpath(os.path.join(_PACKAGES_ASSET_ROOT, package_id))
        img_path = os.path.realpath(os.path.join(pkg_dir, img))
        root     = os.path.realpath(_PACKAGES_ASSET_ROOT)
        if not pkg_dir.startswith(root + os.sep) or not img_path.startswith(pkg_dir + os.sep):
            return jsonify({"code": 400, "message": "非法路径", "data": None}), 400
    except Exception:
        return jsonify({"code": 400, "message": "非法路径", "data": None}), 400

    try:
        return await send_from_directory(pkg_dir, img)
    except Exception:
        return jsonify({"code": 404, "message": "图片不存在", "data": None}), 404


def init_public_api():
    return bp
