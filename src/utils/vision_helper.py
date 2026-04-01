# -*- coding: utf-8 -*-
"""
视觉处理工具 — 希卡莉 (v2.0.0)
=====================================================================
重构对齐 [2026-03-29]：
- import 路径对齐新目录结构
- save_gen_image 返回路径前缀改为 /api/u/images/（对齐 MASTER 路由规范）
- process_user_image 返回字段补充 small_base64（文案场景省 token）
- 无旧版兼容，clean 版本
=====================================================================
"""

import os
import io
import base64
import uuid
import aiohttp
import magic
from datetime import datetime, timezone
from typing import Any
from PIL import Image

from src.utils.logger import get_logger

logger = get_logger(__name__)


class VisionHelper:
    def __init__(self):
        self.upload_root  = os.getenv("VISION_UPLOAD_ROOT", "/app/assets/uploads")
        self.max_file_size = int(os.getenv("VISION_MAX_SIZE_MB", "10")) * 1024 * 1024
        self.allowed_mime  = {"image/jpeg", "image/png", "image/webp"}

        os.makedirs(self.upload_root, exist_ok=True)
        logger.info(f"[VisionHelper] 初始化完成 | upload_root={self.upload_root}")

    async def process_user_image(
        self,
        user_id: str,
        file_stream: io.BytesIO,
        filename: str,
        max_dimension: int = 1280,
    ) -> dict[str, Any]:
        """
        用户上传图片处理管线：校验 → 隔离目录 → 双版本压缩 → 归档。

        双版本：
          高清版：max_dimension px（默认1280，妆容/图生图用）
          小图版：固定 512px（文案场景省 token）
          两版本均强制转 RGB + JPEG quality=85

        返回字段：
          status / image_id / mime_type / base64 / small_base64 /
          file_size / dimensions（高清版宽高）
        """
        try:
            file_data = file_stream.read()
            file_size = len(file_data)

            # 1. 体积检查
            if file_size > self.max_file_size:
                raise ValueError(
                    f"文件体积 {file_size // 1024}KB 超出限制 "
                    f"{self.max_file_size // 1024 // 1024}MB"
                )

            # 2. MIME 幻数检测（防伪造后缀）
            mime_type = magic.from_buffer(file_data, mime=True)
            if mime_type not in self.allowed_mime:
                raise ValueError(f"不支持的文件类型: {mime_type}")

            # 3. 用户隔离目录
            user_folder = os.path.join(self.upload_root, user_id)
            os.makedirs(user_folder, exist_ok=True)

            image_id = str(uuid.uuid4())

            with Image.open(io.BytesIO(file_data)) as img:
                orig_w, orig_h = img.size
                rgb = img.convert("RGB")

                # ── 高清版 ──────────────────────────────────────
                ratio    = min(max_dimension / orig_w, max_dimension / orig_h, 1.0)
                hd_size  = (int(orig_w * ratio), int(orig_h * ratio))
                hd_img   = rgb.resize(hd_size, Image.Resampling.LANCZOS)
                hd_path  = os.path.join(user_folder, f"{image_id}.jpg")
                hd_img.save(hd_path, "JPEG", quality=85, optimize=True)

                buf = io.BytesIO()
                hd_img.save(buf, format="JPEG")
                hd_b64 = base64.b64encode(buf.getvalue()).decode("utf-8")

                # ── 小图版（512px，文案省 token）────────────────
                sm_ratio = min(512 / orig_w, 512 / orig_h, 1.0)
                sm_size  = (int(orig_w * sm_ratio), int(orig_h * sm_ratio))
                sm_img   = rgb.resize(sm_size, Image.Resampling.LANCZOS)
                sm_path  = os.path.join(user_folder, f"{image_id}_small.jpg")
                sm_img.save(sm_path, "JPEG", quality=85, optimize=True)

                sm_buf = io.BytesIO()
                sm_img.save(sm_buf, format="JPEG")
                sm_b64 = base64.b64encode(sm_buf.getvalue()).decode("utf-8")

            logger.info(
                f"[VisionHelper] 图片处理完成 | uid={user_id[:8]} | "
                f"原始={orig_w}x{orig_h} 高清={hd_size[0]}x{hd_size[1]} "
                f"小图={sm_size[0]}x{sm_size[1]}"
            )

            return {
                "status":       "success",
                "image_id":     image_id,
                "mime_type":    "image/jpeg",
                "base64":       hd_b64,
                "small_base64": sm_b64,
                "file_size":    file_size,
                "dimensions":   {"width": hd_size[0], "height": hd_size[1]},
            }

        except Exception as e:
            logger.error(f"[VisionHelper] 图片处理失败: {e}")
            return {"status": "error", "message": str(e)}

    def get_image_base64(self, img_path: str) -> str | None:
        """
        读取已存图片的 Base64。
        历史消息重放 / 视觉描述时使用。
        失败返回 None。
        """
        try:
            if not os.path.exists(img_path):
                return None
            with open(img_path, "rb") as f:
                return base64.b64encode(f.read()).decode("utf-8")
        except Exception:
            return None

    async def save_gen_image(self, user_id: str, gen_url: str) -> str | None:
        """
        将 Seedream 临时 URL（24h 后失效）下载并落地到本地。
        必须在任务完成时立即调用，不能延迟。

        返回：/api/u/images/{user_id}/{image_id}.jpg
        （对齐 MASTER v3.0 路由规范 /api/u/images/）
        """
        try:
            user_folder = os.path.join(self.upload_root, user_id)
            os.makedirs(user_folder, exist_ok=True)

            image_id  = f"gen_{uuid.uuid4()}"
            save_path = os.path.join(user_folder, f"{image_id}.jpg")

            async with aiohttp.ClientSession() as session:
                async with session.get(
                    gen_url, timeout=aiohttp.ClientTimeout(total=120)
                ) as resp:
                    if resp.status != 200:
                        logger.error(f"[VisionHelper] 下载生成图失败 status={resp.status}")
                        return None
                    image_data = await resp.read()

            # 用 PIL 重新保存，统一格式，剔除临时 URL 可能携带的元数据
            with Image.open(io.BytesIO(image_data)) as img:
                img.convert("RGB").save(save_path, "JPEG", quality=90, optimize=True)

            rel_url = f"/api/u/images/{user_id}/{image_id}.jpg"
            logger.info(f"[VisionHelper] 生成图落地完成: {rel_url}")
            return rel_url

        except Exception as e:
            import traceback
            logger.error(f"[VisionHelper] 生成图落地失败: {e!r}\n{traceback.format_exc()}")
            return None


# ══════════════════════════════════════════════════════════════════
# 单例导出
# ══════════════════════════════════════════════════════════════════

_instance: VisionHelper | None = None


def get_vision_helper() -> VisionHelper:
    global _instance
    if _instance is None:
        _instance = VisionHelper()
    return _instance
