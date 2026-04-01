# -*- coding: utf-8 -*-
"""
WebSocket 消息总线 — 希卡莉 (v5.0.0)
=====================================================================
v5.0 [2026-04-01]：PubSub 广播 → Redis Stream 定向推送

【架构变更】
- 每个 api 实例启动时生成唯一 instance_id（uuid4）
- connect() 时写 hikari:ws_owner:{session_id} → instance_id（TTL 24h）
- disconnect() 时删除 hikari:ws_owner:{session_id}
- send_to_user() 查 owner → xadd 到目标实例专属 Stream
  fallback：查不到 owner 时降级写 pending List（用户已离线，等重连补发）
- start_stream_consumer() 替代 start_bus_listener()，XREAD BLOCK 拉取本实例 Stream
- 消息处理后立即 XDEL，无 consumer group / PEL 积压
- flush_pending() / is_user_online() / get_session_id_for_user() 逻辑完全不变

【对外接口不变】
send_to_user / is_user_online / get_session_id_for_user / flush_pending
connect / disconnect / close_all — 签名和行为与 v4.x 完全兼容
=====================================================================
"""

import json
import uuid
import asyncio

from src.utils.logger import get_logger
from src.utils.database import get_db_manager

logger = get_logger(__name__)

# 本实例唯一标识（进程级，重启后自动更新）
_INSTANCE_ID: str = uuid.uuid4().hex

# Redis key 前缀
_STREAM_KEY_PREFIX  = "hikari:ws_stream:"   # hikari:ws_stream:{instance_id}
_OWNER_KEY_PREFIX   = "hikari:ws_owner:"    # hikari:ws_owner:{session_id}  → instance_id
_PENDING_KEY_PREFIX = "hikari:pending:"     # hikari:pending:{session_id}   → List（断线补发）
_SID_KEY_PREFIX     = "hikari:ws_sid:"      # hikari:ws_sid:{user_id}       → session_id

# Stream 单条消息字段名
_STREAM_FIELD = "msg"

# Stream 最大长度（每实例）：超出后自动裁剪最旧消息，防止 Redis 内存泄漏
_STREAM_MAXLEN = 2000

# XREAD 阻塞超时（毫秒）：2秒后返回空列表继续循环，保持响应性
_XREAD_BLOCK_MS = 2000


class SocketManager:
    def __init__(self):
        # session_id → asyncio.Queue（仅本实例有效）
        self.active_queues: dict[str, asyncio.Queue] = {}
        self._instance_id = _INSTANCE_ID
        self._stream_key  = f"{_STREAM_KEY_PREFIX}{_INSTANCE_ID}"

    # ──────────────────────────────────────────────────────────────
    # 连接管理
    # ──────────────────────────────────────────────────────────────

    async def connect(self, session_id: str, websocket, user_id: str = "") -> asyncio.Queue:
        """
        注册 WebSocket 连接，返回对应的 Queue。
        写 Redis：
          hikari:ws_sid:{user_id}     → session_id（在线状态，TTL 24h）
          hikari:ws_owner:{session_id} → instance_id（路由表，TTL 24h）
        """
        queue = asyncio.Queue()
        self.active_queues[session_id] = queue

        try:
            redis = await get_db_manager().get_redis()
            pipe  = redis.pipeline()
            if user_id:
                pipe.setex(f"{_SID_KEY_PREFIX}{user_id}", 86400, session_id)
            pipe.setex(f"{_OWNER_KEY_PREFIX}{session_id}", 86400, self._instance_id)
            await pipe.execute()
        except Exception as e:
            logger.warning(f"[Socket] connect: 写 Redis 失败: {e}")

        logger.info(
            f"[Socket] 连接注册 | session={session_id} | instance={self._instance_id[:8]} | "
            f"节点总连接数={len(self.active_queues)}"
        )
        return queue

    async def disconnect(self, session_id: str, websocket=None) -> None:
        """
        卸载连接队列，删除 owner 路由记录。
        不删除 hikari:ws_sid（TTL 24h 自然过期）——保证 PushSchedulerWorkflow 24h 内仍会推送
        pending 消息，用户重连时自动补发。
        """
        if session_id in self.active_queues:
            del self.active_queues[session_id]

        try:
            redis = await get_db_manager().get_redis()
            await redis.delete(f"{_OWNER_KEY_PREFIX}{session_id}")
        except Exception as e:
            logger.warning(f"[Socket] disconnect: 删除 owner key 失败: {e}")

        logger.info(
            f"[Socket] 连接卸载 | session={session_id} | "
            f"节点剩余连接数={len(self.active_queues)}"
        )

    # ──────────────────────────────────────────────────────────────
    # 消息发送
    # ──────────────────────────────────────────────────────────────

    async def send_to_user(self, session_id: str, payload: dict) -> None:
        """
        两层推送：
        1. 持久化到 Redis List hikari:pending:{session_id}（TTL=10分钟）→ 断线补发
        2. 查 owner 路由 → xadd 到目标实例的专属 Stream
           若查不到 owner（用户已离线），只写 pending，等重连时 flush_pending 补发
        """
        try:
            redis   = await get_db_manager().get_redis()
            msg_str = json.dumps(payload, ensure_ascii=False)

            # 持久化 pending（断线重连时补发，逻辑与 v4.x 完全相同）
            await redis.lpush(f"{_PENDING_KEY_PREFIX}{session_id}", msg_str)
            await redis.expire(f"{_PENDING_KEY_PREFIX}{session_id}", 600)

            # 查路由：该 session 属于哪个实例
            owner_instance = await redis.get(f"{_OWNER_KEY_PREFIX}{session_id}")
            if not owner_instance:
                # 用户离线或 owner key 已过期，pending 已写，等重连补发
                logger.debug(f"[Socket] 用户离线，消息进 pending | target={session_id}")
                return

            # 定向写入目标实例的 Stream
            target_stream = f"{_STREAM_KEY_PREFIX}{owner_instance}"
            await redis.xadd(
                target_stream,
                {_STREAM_FIELD: json.dumps(
                    {"session_id": session_id, "payload": payload},
                    ensure_ascii=False,
                )},
                maxlen=_STREAM_MAXLEN,
                approximate=True,
            )

            logger.info(
                f"[Socket] Stream 发送 | target={session_id} | "
                f"instance={owner_instance[:8]} | type={payload.get('type')}"
            )
        except Exception as e:
            logger.error(f"[Socket] send_to_user 失败: {e}")

    async def flush_pending(self, session_id: str, queue: asyncio.Queue) -> None:
        """
        重连时从 Redis pending 列表补发积压消息到 Queue。
        lpush 是头插，reverse 还原顺序后入队。
        （逻辑与 v4.x 完全不变）
        """
        try:
            redis = await get_db_manager().get_redis()
            key   = f"{_PENDING_KEY_PREFIX}{session_id}"
            msgs  = await redis.lrange(key, 0, -1)
            if msgs:
                for msg_str in reversed(msgs):
                    try:
                        await queue.put(json.loads(msg_str))
                    except Exception:
                        pass
                await redis.delete(key)
                logger.info(f"[Socket] 补发积压消息 {len(msgs)} 条 | session={session_id}")
        except Exception as e:
            logger.warning(f"[Socket] flush_pending 失败: {e}")

    async def get_session_id_for_user(self, user_id: str) -> str:
        """
        从 Redis 查询用户当前 session_id。
        查不到时降级返回 f"sess_{user_id}"，保证不返回空。
        （逻辑与 v4.x 完全不变）
        """
        try:
            redis = await get_db_manager().get_redis()
            val   = await redis.get(f"{_SID_KEY_PREFIX}{user_id}")
            if val:
                return val
            return f"sess_{user_id}"
        except Exception as e:
            logger.warning(f"[Socket] 查询 ws_sid 失败: {e}")
            return f"sess_{user_id}"

    async def is_user_online(self, user_id: str) -> bool:
        """
        判断用户当前是否有活跃的 WebSocket 连接。
        仅当 Redis 中存在 hikari:ws_sid:{user_id} 时返回 True。
        （逻辑与 v4.x 完全不变）
        """
        try:
            redis = await get_db_manager().get_redis()
            return bool(await redis.get(f"{_SID_KEY_PREFIX}{user_id}"))
        except Exception as e:
            logger.warning(f"[Socket] 查询在线状态失败: {e}")
            return False

    # ──────────────────────────────────────────────────────────────
    # Redis Stream 消费（后台常驻协程，替代 PubSub bus_listener）
    # ──────────────────────────────────────────────────────────────

    async def start_stream_consumer(self) -> None:
        """
        消费本实例专属 Stream hikari:ws_stream:{instance_id}。
        收到消息后若 session_id 在本实例 active_queues 中则入队，然后立即 XDEL。
        Redis 断线后等待 5s 自动重连，无限循环保活。
        由 main.py 启动时通过 add_background_task 挂载。

        设计说明：
        - 不使用 consumer group，无 PEL 积压问题
        - 实例崩溃后旧 Stream 自然废弃（MAXLEN 兜底 + TTL 通过 XADD 不设置，
          由 MAXLEN 控制体积）
        - XREAD BLOCK 2000ms：阻塞等待新消息，超时返回空列表继续循环
        """
        last_id = "$"   # 只消费从现在起的新消息（不重放历史）
        logger.info(
            f"[Socket] Stream consumer 启动 | stream={self._stream_key} | "
            f"instance={self._instance_id[:8]}"
        )

        while True:
            redis = None
            try:
                redis = await get_db_manager().get_redis_pubsub()

                while True:
                    # XREAD BLOCK：最多阻塞 2s，超时返回 None
                    results = await redis.xread(
                        {self._stream_key: last_id},
                        block=_XREAD_BLOCK_MS,
                        count=100,
                    )

                    if not results:
                        # 超时，无新消息，继续等待
                        continue

                    # results 格式：[(stream_key, [(msg_id, {field: value}), ...])]
                    _, messages = results[0]
                    msg_ids_to_delete = []

                    for msg_id, fields in messages:
                        last_id = msg_id  # 推进游标
                        msg_ids_to_delete.append(msg_id)
                        try:
                            data       = json.loads(fields[_STREAM_FIELD])
                            target_sid = data.get("session_id")
                            payload    = data.get("payload")

                            if target_sid in self.active_queues:
                                await self.active_queues[target_sid].put(payload)
                                logger.debug(
                                    f"[Socket] Stream-Hit | target={target_sid} | "
                                    f"type={payload.get('type')}"
                                )
                            else:
                                logger.debug(f"[Socket] Stream-Miss | target={target_sid}")
                        except Exception as e:
                            logger.warning(f"[Socket] 消息解析异常: {e}")

                    # 批量删除已处理消息，保持 Stream 干净
                    if msg_ids_to_delete:
                        try:
                            await redis.xdel(self._stream_key, *msg_ids_to_delete)
                        except Exception as e:
                            logger.warning(f"[Socket] xdel 失败（非致命）: {e}")

            except (asyncio.CancelledError, GeneratorExit):
                break
            except Exception as e:
                logger.error(f"[Socket] Stream consumer 链路断开，5s 后重连: {e}")
                last_id = "$"   # 重连后只消费新消息，避免重复处理断线期间的旧消息
                await asyncio.sleep(5.0)
            finally:
                if redis:
                    try:
                        await redis.aclose()
                    except Exception:
                        pass

    async def close_all(self) -> None:
        """清空所有本地连接（容器关闭时调用）"""
        self.active_queues.clear()


# ══════════════════════════════════════════════════════════════════
# 单例导出
# ══════════════════════════════════════════════════════════════════

_socket_manager: SocketManager | None = None


def get_socket_manager() -> SocketManager:
    global _socket_manager
    if _socket_manager is None:
        _socket_manager = SocketManager()
    return _socket_manager
