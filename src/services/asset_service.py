# -*- coding: utf-8 -*-
"""
TTS 资产引擎服务
"""

import os
import asyncio
import itertools
import traceback
from datetime import datetime, timezone
from typing import Optional

import aiohttp
from bson import ObjectId

from src.utils.database import get_db_manager, COL_USERS, COL_SESSIONS, COL_MESSAGES
from src.utils.asset_helper import get_asset_helper
from src.utils.logger import get_logger
from src.utils.socket_manager import get_socket_manager

logger = get_logger(__name__)


class AssetService:
    def __init__(self, ws_manager=None):
        self.ws_manager = ws_manager or get_socket_manager()
        self.helper = get_asset_helper()
        self.db_manager = get_db_manager()

        # TTS 集群配置
        self.api_host = os.getenv("TTS_ENGINE_HOST", "http://host.docker.internal").rstrip("/")
        ports_env = os.getenv("TTS_ENGINE_PORTS", "9880,9881")
        self.tts_ports = [p.strip() for p in ports_env.split(",")]
        self._port_cycle = itertools.cycle(self.tts_ports)
        self.tts_timeout = int(os.getenv("TTS_TIMEOUT", 300))

        # 算力车道信号量（每端口一个并发槽）
        max_conc = int(os.getenv("MAX_TTS_CONCURRENCY", len(self.tts_ports)))
        self.concurrency_guard = asyncio.Semaphore(max_conc)

        # 内存排他锁：防止同一 msg_id 重复触发
        self._active_msg_locks: dict[str, bool] = {}

        logger.info(f"asset_service initialized | tts_ports={self.tts_ports} max_conc={max_conc}")

    @property
    def users_col(self):
        return get_db_manager().db[COL_USERS]

    @property
    def sessions_col(self):
        return get_db_manager().db[COL_SESSIONS]

    @property
    def messages_col(self):
        return get_db_manager().db[COL_MESSAGES]

    # ── 公共接口 ───────────────────────────────────────────────────────────────

    async def get_voice_library(self) -> list[str]:
        """返回可用声线列表。"""
        try:
            return self.helper.list_available_characters()
        except Exception:
            return ["女性1_zh"]

    async def set_user_voice_preference(self, user_id: str, character: str) -> bool:
        """持久化用户声线偏好到 users 集合。"""
        try:
            await self.users_col.update_one(
                {"user_id": user_id},
                {"$set": {"voice_character": character, "updated_at": datetime.now(timezone.utc)}},
                upsert=True,
            )
            return True
        except Exception:
            return False

    async def push_voice_generation(
        self,
        user_id: str,
        session_id: str,
        text: str,
        msg_id: str,
        **kwargs,
    ) -> None:
        """
        入口：防重拦截 → 身份溯源 → TTS 推理 → WS 推流 → 消息存档。
        由 VoiceGenWorkflow activity 调用。
        """
        if not msg_id:
            return

        # 防重拦截
        if msg_id in self._active_msg_locks:
            logger.warning(f"asset_service: duplicate trigger blocked | msg_id={msg_id[:8]}")
            return
        self._active_msg_locks[msg_id] = True

        try:
            # 身份溯源
            user_doc = await self.users_col.find_one({"user_id": user_id})
            real_name = (user_doc.get("user_name", "神秘访客") if user_doc else "神秘访客")
            character = kwargs.get("character") or (
                user_doc.get("voice_character", os.getenv("DEFAULT_VOICE_CHARACTER", "女性1_zh"))
                if user_doc
                else os.getenv("DEFAULT_VOICE_CHARACTER", "女性1_zh")
            )

            await self.sessions_col.update_one(
                {"session_id": session_id},
                {"$set": {"voice_status": "generating"}},
            )

            # 路径定标
            file_path, rel_path = self.helper.get_voice_metadata(user_id, session_id, real_name)

            # 参考音频 & 文本
            internal_wav, ref_text, raw_win_path = self.helper.get_reference_pair(character, "neutral")
            lang = self._detect_lang(internal_wav)
            clean_text = self.helper.sanitize_text(text)

            # TTS 推理（线性故障转移）
            async with self.concurrency_guard:
                audio_rel_path = await self._execute_inference_linear(
                    clean_text, raw_win_path, ref_text, lang, file_path, rel_path
                )

            if not audio_rel_path:
                raise RuntimeError("all TTS nodes failed")

            voice_url = f"/api/u/voices/{audio_rel_path}"

            # WS 推流给手机端
            await self.ws_manager.send_to_user(
                session_id,
                {
                    "type": "voice",
                    "msg_id": msg_id,
                    "voice_url": voice_url,
                    "voice_status": "ready",
                },
            )

            # 消息存档
            target_id = ObjectId(msg_id) if len(msg_id) == 24 else msg_id
            await self.messages_col.update_one(
                {"_id": target_id},
                {"$set": {"voice_url": voice_url, "voice_status": "ready"}},
            )

            logger.info(f"asset_service: voice ready | msg_id={msg_id[:8]} character={character} user={real_name}")

            # 套餐用户：同步推给屏幕端播放
            await self._push_screen_voice(user_id, msg_id, voice_url, kwargs.get("emotion", "neutral"))

        except Exception:
            logger.error(f"asset_service push_voice_generation error: {traceback.format_exc()}")
            await self.sessions_col.update_one(
                {"session_id": session_id},
                {"$set": {"voice_status": "failed"}},
            )
        finally:
            self._active_msg_locks.pop(msg_id, None)

    # ── 内部方法 ───────────────────────────────────────────────────────────────

    def _detect_lang(self, wav_path: str) -> str:
        """从 lang.txt 读取语言标识，默认 zh。"""
        base_path = os.path.splitext(wav_path)[0]
        candidates = [
            f"{base_path}.lang",
            os.path.join(os.path.dirname(wav_path), "lang.txt"),
        ]
        for fpath in candidates:
            if os.path.exists(fpath):
                try:
                    with open(fpath, "r", encoding="utf-8") as f:
                        return f.read().strip().lower()
                except Exception:
                    pass
        return "zh"

    async def _execute_inference_linear(
        self,
        text: str,
        ref_wav: str,
        ref_text: str,
        lang: str,
        file_path: str,
        rel_path: str,
    ) -> Optional[str]:
        """
        线性故障转移：依次尝试所有 TTS 端口，不并行。
        安全校验：拒绝 ref_wav 路径注入。
        """
        if ref_wav:
            linux_root = str(self.helper.sample_root).rstrip("/")
            win_root = self.helper.host_sample_root.rstrip("\\")
            normalized = ref_wav.replace("\\", "/")
            is_linux_valid = normalized.startswith(linux_root + "/")
            is_win_valid = ref_wav.startswith(win_root + "\\") or normalized.startswith(
                win_root.replace("\\", "/") + "/"
            )
            if not (is_linux_valid or is_win_valid):
                logger.error(f"asset_service: rejected illegal ref_wav path: {ref_wav}")
                return None

        payload = {
            "text": text,
            "text_lang": lang,
            "ref_audio_path": ref_wav,
            "prompt_text": ref_text,
            "prompt_lang": lang,
            "top_k": int(os.getenv("TTS_TOP_K", 15)),
            "top_p": float(os.getenv("TTS_TOP_P", 0.8)),
            "temperature": float(os.getenv("TTS_TEMPERATURE", 0.8)),
            "speed_factor": float(os.getenv("TTS_SPEED_FACTOR", 1.0)),
            "cut_punc": "",
            "split_method": os.getenv("TTS_SPLIT_METHOD", "cut5"),
            "batch_size": int(os.getenv("TTS_BATCH_SIZE", 1)),
            "sample_steps": int(os.getenv("TTS_SAMPLE_STEPS", 16)),
            "is_half": os.getenv("TTS_IS_HALF", "False").lower() == "true",
            "streaming_mode": 0,
            "media_type": "wav",
        }

        for _ in range(len(self.tts_ports)):
            port = next(self._port_cycle)
            url = f"{self.api_host}:{port}/tts"
            logger.info(f"asset_service: tts attempt | url={url}")
            try:
                timeout = aiohttp.ClientTimeout(total=self.tts_timeout)
                async with aiohttp.ClientSession(timeout=timeout) as session:
                    async with session.post(url, json=payload) as resp:
                        if resp.status == 200:
                            audio_data = await resp.read()
                            await asyncio.to_thread(self._sync_save, file_path, audio_data)
                            return rel_path
                        logger.error(f"asset_service: tts node error | port={port} status={resp.status}")
            except Exception as e:
                logger.error(f"asset_service: tts node unreachable | port={port} error={e}")

        return None

    def _sync_save(self, path: str, data: bytes) -> None:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(data)

    async def _push_screen_voice(
        self, user_id: str, msg_id: str, voice_url: str, emotion: str
    ) -> None:
        """套餐用户在店时，将语音同步推给屏幕端直接播放。"""
        try:
            redis = await self.db_manager.get_redis()
            if not await redis.get(f"hikari:store:paid:{user_id}"):
                return
            instore_raw = await redis.get(f"hikari:store:instore:{user_id}")
            if not instore_raw:
                return
            store_id = instore_raw if isinstance(instore_raw, str) else instore_raw.decode()
            screen_session = f"store_screen_{store_id}"
            motion = emotion if emotion in ("happy", "sad", "angry") else "talk"
            await self.ws_manager.send_to_user(
                screen_session,
                {
                    "type": "screen_voice",
                    "voice_url": voice_url,
                    "msg_id": msg_id,
                    "motion": motion,
                },
            )
            logger.info(f"asset_service: screen voice pushed | user={user_id[:8]} motion={motion}")
        except Exception as e:
            logger.debug(f"asset_service: screen voice push skipped: {e}")


# ── 单例工厂 ──────────────────────────────────────────────────────────────────

_asset_service_instance: AssetService | None = None


def get_asset_service(ws_manager=None) -> AssetService:
    global _asset_service_instance
    if _asset_service_instance is None:
        _asset_service_instance = AssetService(ws_manager)
    return _asset_service_instance
