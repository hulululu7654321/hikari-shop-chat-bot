# -*- coding: utf-8 -*-
"""
src/modules/public/app.py — hikari-shop 容器入口
创建独立 Quart app，注册 public blueprint。
"""

from quart import Quart, jsonify
from quart_cors import cors

from src.modules.public.api import bp

app = Quart(__name__)
app = cors(app, allow_origin="*")

app.register_blueprint(bp)


@app.route("/api/sys/health", methods=["GET"])
async def health():
    return jsonify({"code": 200, "message": "ok", "data": {"service": "hikari-shop"}}), 200
