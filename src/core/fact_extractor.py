# -*- coding: utf-8 -*-
"""
嗅探雷达核心 — 希卡莉 (v7.0.0)
=====================================================================
重构对齐 [MASTER v3.0 / 2026-03-29]：

【核心变更】向量存储从 MongoDB vector_memory 迁移到 Qdrant：
1. 废弃 self.vector_col（core_vector_memory 集合不再使用）。
2. retrieve_relevant_facts：改用 qdrant_client.search_vectors()。
3. _conflict_check_and_sink：改用 qdrant_client.get_user_vectors()
   做全量打捞，冲突检测后改用 qdrant_client.deactivate_vector()。
4. _vectorize_and_store_v3：改用 qdrant_client.upsert_vector()
   直接向 Qdrant 写入，不再操作 MongoDB vector_memory。
5. fact_id 格式统一：去掉 "fact_" 前缀（MD5 hex 已足够唯一），
   与 qdrant_client.upsert_vector 的 fact_id 入参保持一致。

【不变逻辑】
- 集合名改用 COL_FACTS / COL_USERS / COL_SESSIONS 常量。
- DatabaseManager() → get_db_manager() 单例工厂。
- LLM 客户端环境变量对齐：DOUBAO_API_KEY / DOUBAO_MAIN_EP。
- sense() / _audit_and_sink() / _parse_json_brute_force()
  逻辑完全保留，resource_anchor、语义去噪、哈希去重均不变。
- _cosine_similarity 移除（向量比较交给 Qdrant，本地不再计算）。
=====================================================================
"""

import os
import json
import asyncio
import hashlib
import re
from datetime import datetime, timezone
from typing import List, Dict, Any, Optional

from openai import OpenAI
from volcenginesdkarkruntime import Ark

from src.utils.logger import get_logger
from src.utils.database import get_db_manager, COL_FACTS, COL_USERS, COL_SESSIONS
from src.utils import qdrant_client as qc
from src.core.hikari_prompts import PERSONA_SENSING_PROMPT_TEMPLATE

logger = get_logger(__name__)

# audit_buffer 阈值配置
# ⚠️ 两个常量必须满足：_AUDIT_BUFFER_FLUSH_SIZE <= _AUDIT_BUFFER_MAX_SIZE
# 如果调大 FLUSH_SIZE，必须同步调大 MAX_SIZE，否则 $slice 会在 flush 前截断旧消息
_AUDIT_BUFFER_FLUSH_SIZE = 3   # buffer 积累到此条数时触发 flush（len(buffer) >= N）
_AUDIT_BUFFER_MAX_SIZE   = 10  # $slice 保留的最大条数，需大于等于 FLUSH_SIZE

# 单 category 向量数软上限
# 超过此值时，_conflict_check_and_sink 写入前先对该 category 执行一次局部整合。
# 正常运行下（AutoDream 每日整合）几乎不会触发；
# 主要防御场景：DREAM_ENABLED=false 长期关闭 + 重度用户高频使用。
_CAT_VECTOR_LIMIT = 30

# 内部实名提取 Prompt
IDENTITY_PROMPT_INTERNAL = """你是一个专业的 CN 提取专家。从文本中提取用户的【姓名/CN/昵称】。
⚠️ 严格红线：严禁提取 AI 助手（希卡莉/Hikari/本喵）的名字，严禁提取讨论的动漫/影视角色名。
只输出名字，不要解释。若无输出 NONE。输入："{text}" """


class FactExtractor:
    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super(FactExtractor, cls).__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if self._initialized:
            return

        # base_url：火山方舟固定接入点（硬编码，与 workflow / dialogue_engine 保持一致）
        # DOUBAO_MAIN_EP：ep-xxx endpoint ID，用作 model 字段
        doubao_base = "https://ark.cn-beijing.volces.com/api/v3"
        api_key = os.getenv("DOUBAO_API_KEY", "")

        # 对话推理客户端（身份识别 / 画像提取）
        self.client = OpenAI(base_url=doubao_base, api_key=api_key)
        # Embedding 客户端（豆包多模态向量）
        self.embed_client = Ark(base_url=doubao_base, api_key=api_key)

        self.model       = os.getenv("LLM_EXTRACT_MODEL", os.getenv("LLM_RERANK_MODEL", ""))
        self.embed_model = os.getenv("LLM_EMBEDDING_MODEL", "")

        self.db_manager = get_db_manager()

        self.bot_dna      = ["希卡莉", "hikari", "nya", "喵", "看板", "bot", "assistant"]
        self.SCALAR_CATS  = {"real_name", "cn", "name"}
        self.VALUE_THRESHOLD = 0.7   # 累计置信度超过此值时进入 Qdrant

        self._initialized = True
        logger.info("🐾 [Fact-Extractor] v7.0.0 启动 | Qdrant 向量存储已激活")

    # ──────────────────────────────────────────────────────────────
    # Embedding
    # ──────────────────────────────────────────────────────────────

    async def _get_multimodal_embedding(self, text: str) -> List[float]:
        """获取豆包多模态 Embedding（2048 维）"""
        try:
            res = await asyncio.to_thread(
                self.embed_client.multimodal_embeddings.create,
                model=self.embed_model,
                input=[{"type": "text", "text": text}],
            )
            if hasattr(res, "data"):
                if isinstance(res.data, list):
                    return res.data[0].embedding
                elif hasattr(res.data, "embedding"):
                    return res.data.embedding
            if hasattr(res, "embedding"):
                return res.embedding
            raise ValueError("Embedding 结构解析失败")
        except Exception as e:
            logger.error(f"❌ [Embedding] 获取失败: {e}")
            raise

    # ──────────────────────────────────────────────────────────────
    # RAG 检索（改用 Qdrant）
    # ──────────────────────────────────────────────────────────────

    async def retrieve_relevant_facts(
        self, user_id: str, query_text: str, top_k: int = 5
    ) -> str:
        """
        [RAG 物理打捞] 向量语义检索，返回含资产提示的记忆描述。

        改动：从 MongoDB vector_memory 余弦扫描 → Qdrant search_vectors()
        score_threshold=0.6 与旧版阈值对齐。
        """
        try:
            query_vec = await self._get_multimodal_embedding(query_text)
            hits = await qc.search_vectors(
                user_id=user_id,
                query_vector=query_vec,
                top_k=top_k,
                score_threshold=0.6,
            )
            parts = []
            for h in hits:
                content = h.get("content", "")
                if h.get("image_id"):
                    content = f"{content} [附图记事]"
                parts.append(f"- {content}")
            return "\n".join(parts)
        except Exception:
            return ""

    # ──────────────────────────────────────────────────────────────
    # 嗅探雷达主入口
    # ──────────────────────────────────────────────────────────────

    async def sense(
        self,
        user_doc: Dict,
        session_id: str,
        user_text: str,
        bot_text: str = "",
        stage: str = "PRE",
        vision_desc: str = "",
        image_id: str = "",
    ):
        """
        [嗅探雷达入口] 执行语义提纯并锚定物理资产 image_id。

        PRE 阶段：实名提取（仅匿名用户），命中则触发 +5 affinity 脉冲。
        POST 阶段：画像事实提取 + Qdrant 向量落盘。
        """
        raw_uid      = user_doc.get("user_id")
        current_name = str(user_doc.get("user_name") or "匿名用户")
        is_anonymous = "匿名" in current_name

        try:
            if stage == "PRE" and is_anonymous:
                res = await asyncio.to_thread(
                    self.client.chat.completions.create,
                    model=self.model,
                    messages=[{
                        "role":    "user",
                        "content": IDENTITY_PROMPT_INTERNAL.format(text=user_text),
                    }],
                    max_tokens=10,
                    stream=False,
                )
                detected = self._universal_sanitizer(
                    res.choices[0].message.content.strip()
                )
                if detected and detected.upper() != "NONE":
                    await self._atomic_identity_sync(raw_uid, session_id, detected)
                    return {"detected_name": detected}

            elif stage == "POST":
                # 分布式锁（per user_id）防止多 Gunicorn Worker 并发重复提取同一用户的事实。
                # 锁获取超时（blocking_timeout=5s）时降级继续执行而非阻塞，保证业务不中断。
                # Redis 不可用时同样降级，记 warning 后继续（单进程内已无并发竞争风险）。
                lock_key = f"hikari:fact_lock:{raw_uid}"
                redis = None
                redis_lock = None
                lock_acquired = False
                try:
                    redis = await self.db_manager.get_redis()
                    redis_lock = redis.lock(lock_key, timeout=30, blocking_timeout=5)
                    lock_acquired = await redis_lock.acquire()
                    if not lock_acquired:
                        logger.warning(
                            f"⚠️ [Fact-Lock] 锁等待超时，降级继续 | uid={raw_uid[:8]}"
                        )
                except Exception as lock_err:
                    logger.warning(
                        f"⚠️ [Fact-Lock] Redis 锁不可用，降级继续 | uid={raw_uid[:8]} | {lock_err}"
                    )

                try:
                    db = self.db_manager.db

                    # 语义提纯：记录"希卡莉看到的画面内容"，不记录物理 UUID
                    msg_context = user_text
                    if vision_desc:
                        msg_context = f"【画面感知：{vision_desc}】\n用户消息: {user_text}"

                    # 原子追加（$push + $slice），替代旧的 find_one + Python append + $set 全量替换。
                    # $slice 保留最近 _AUDIT_BUFFER_MAX_SIZE 条，防止 buffer 无上限增长导致敏感消息长期驻留。
                    # return_document=True 返回更新后文档，供后续 len 判断，比原来少一次 RTT。
                    updated = await db[COL_SESSIONS].find_one_and_update(
                        {"session_id": session_id},
                        {
                            "$push": {
                                "audit_buffer": {
                                    "$each":  [f"会话: {msg_context}"],
                                    "$slice": -_AUDIT_BUFFER_MAX_SIZE,
                                }
                            }
                        },
                        upsert=True,
                        return_document=True,
                    )
                    buffer = updated.get("audit_buffer", []) if updated else [f"会话: {msg_context}"]

                    if vision_desc or is_anonymous or (len(buffer) >= _AUDIT_BUFFER_FLUSH_SIZE):
                        dialogue_context = "\n".join(buffer)
                        await db[COL_SESSIONS].update_one(
                            {"session_id": session_id},
                            {"$set": {"audit_buffer": []}},
                            upsert=True,
                        )
                        existing_mem = await self.retrieve_relevant_facts(
                            raw_uid, dialogue_context
                        )
                        await self._audit_and_sink(
                            raw_uid, session_id, dialogue_context,
                            existing_mem, is_anonymous, image_id,
                        )
                finally:
                    # 无论临界区是否成功，持有锁时必须释放
                    if lock_acquired and redis_lock is not None:
                        try:
                            await redis_lock.release()
                        except Exception as rel_err:
                            logger.warning(
                                f"⚠️ [Fact-Lock] 锁释放失败（已超时自动过期）| uid={raw_uid[:8]} | {rel_err}"
                            )

        except Exception as e:
            logger.error(f"❌ [Fact-Sense] 阶段异常 | stage={stage} | {e}")
        return None

    # ──────────────────────────────────────────────────────────────
    # 深度审计 + 事实落盘
    # ──────────────────────────────────────────────────────────────

    async def _audit_and_sink(
        self,
        user_id: str,
        session_id: str,
        context: str,
        existing_mem: str,
        is_anonymous: bool,
        image_id: str,
    ):
        """[深度审计 V2] LLM 提取画像事实 → COL_FACTS 落盘 → Qdrant 向量化"""
        prompt = (
            f"分析以下交互，提取画像事实。"
            f"已有认知：{existing_mem if existing_mem else '无'}\n"
            f"当前对话：{context}\n"
            f"{PERSONA_SENSING_PROMPT_TEMPLATE}"
        )

        resp = await asyncio.to_thread(
            self.client.chat.completions.create,
            model=self.model,
            messages=[{"role": "user", "content": prompt}],
            stream=False,
        )
        fragments = self._parse_json_brute_force(resp.choices[0].message.content)
        if not fragments:
            return

        unique_batch: Dict[str, Dict] = {}
        for f in fragments:
            val = str(f.get("content", "")).strip()
            if not val or val.lower() in ["none", "null"]:
                continue

            # 物理脱毒：严禁在事实正文中存储 UUID 格式字符串
            val = re.sub(
                r'[a-f0-9]{8}-([a-f0-9]{4}-){3}[a-f0-9]{12}', '', val
            ).strip()
            if len(val) < 2:
                continue

            subject = f.get("subject", "general").strip()
            cat     = f.get("category", "facts").strip()
            topic   = f.get("topic", "default").strip()

            # 哈希种子用 topic：同一话题不同表述归入同一条目，mention_count 正确累计
            seed      = f"{user_id}_{cat}_{subject}_{topic}"
            atomic_id = hashlib.md5(seed.encode()).hexdigest()[:16]   # 纯 MD5，不加前缀

            if atomic_id not in unique_batch:
                unique_batch[atomic_id] = f

        db = self.db_manager.db

        for atomic_id, f in unique_batch.items():
            val            = str(f.get("content", "")).strip()
            cat            = f.get("category", "facts").strip()
            llm_confidence = float(f.get("confidence", 1.0))

            # 标量类型（real_name / cn / name）且当前匿名 → 触发实名同步
            if cat in self.SCALAR_CATS and is_anonymous:
                clean_n = self._universal_sanitizer(val)
                if clean_n:
                    await self._atomic_identity_sync(user_id, session_id, clean_n)
                    is_anonymous = False

            # 1. 事实属性落盘（COL_FACTS）：原子累加 mention_count，$max 保留历史最高置信度
            await db[COL_FACTS].update_one(
                {"fact_id": atomic_id},
                {
                    "$set": {
                        "user_id":              user_id,
                        "subject":              f.get("subject", "general"),
                        "category":             cat,
                        "topic":                f.get("topic", "default"),
                        "content":              val,
                        "associated_image_id":  image_id,
                        "is_active":            True,
                        "updated_at":           datetime.now(timezone.utc),
                    },
                    "$max":          {"confidence": llm_confidence},
                    "$inc":          {"mention_count": 1},
                    "$setOnInsert":  {"created_at": datetime.now(timezone.utc)},
                },
                upsert=True,
            )

            # 读取落盘后的状态，计算累计置信度
            # 公式：saturate(llm_confidence + 0.05 * (mention_count - 1))
            # 含义：每多被提及一次额外加 0.05，上限 1.0
            fact_doc = await db[COL_FACTS].find_one(
                {"fact_id": atomic_id}, {"mention_count": 1, "confidence": 1}
            )
            mention_count       = fact_doc.get("mention_count", 1) if fact_doc else 1
            stored_confidence   = fact_doc.get("confidence", llm_confidence) if fact_doc else llm_confidence
            accumulated_conf    = min(1.0, stored_confidence + 0.05 * (mention_count - 1))

            logger.debug(
                f"[Fact-Sink] topic={f.get('topic')} | "
                f"llm={llm_confidence:.2f} | mentions={mention_count} | "
                f"accumulated={accumulated_conf:.2f}"
            )

            # 2. 更新 User 元数据（COL_USERS）
            if cat == "user_pref":
                # 用户对希卡莉的个性化要求：按 topic 覆写，存入 preferences 字段
                topic = f.get("topic", "default").strip()
                await db[COL_USERS].update_one(
                    {"user_id": user_id},
                    {"$set": {f"preferences.{topic}": val}},
                )
            elif cat in self.SCALAR_CATS:
                await db[COL_USERS].update_one(
                    {"user_id": user_id},
                    {"$set": {f"metadata.{cat}": val}},
                )
            else:
                await db[COL_USERS].update_one(
                    {"user_id": user_id},
                    {"$addToSet": {f"metadata.{cat}": val}},
                )

            # 3. 累计置信度达标 → 冲突检测 + Qdrant 向量落盘
            if accumulated_conf >= self.VALUE_THRESHOLD:
                asyncio.create_task(self._conflict_check_and_sink(
                    user_id, val, accumulated_conf, cat, atomic_id, image_id,
                ))

    # ──────────────────────────────────────────────────────────────
    # 冲突检测 + Qdrant 向量落盘
    # ──────────────────────────────────────────────────────────────

    async def _conflict_check_and_sink(
        self,
        user_id: str,
        fact: str,
        score: float,
        category: str,
        atomic_id: str,
        image_id: str,
    ):
        """
        [冲突检测 + 向量落盘] 检测已有记忆中是否有语义相近但内容矛盾的旧条目。

        相似度 > 0.85（话题相同）且 content 不同 → 旧记忆标记 is_active=False。
        之后无论检测成功与否，都正常执行向量化落盘。

        改动：
        - 旧版：扫描 MongoDB vector_memory + Python 余弦计算
        - 新版：qc.get_user_vectors() 全量打捞 + score 字段直接比较
               冲突标记改为 qc.deactivate_vector()
        """
        try:
            new_vec = await self._get_multimodal_embedding(fact)

            # 只拉同 category 向量（包含 vector 字段），在 category 范围内检测冲突
            all_vecs = await qc.get_user_vectors(user_id, with_vectors=True, category=category)

            # 向量数软上限：超限时先局部整合再写入，防止 AutoDream 关闭时向量无限堆积。
            # 失败静默：整合异常不阻断写入主流程。
            if len(all_vecs) >= _CAT_VECTOR_LIMIT:
                logger.info(
                    f"🧹 [VectorCap] category={category} 向量数={len(all_vecs)} 超限，"
                    f"触发局部整合 | uid={user_id[:8]}"
                )
                try:
                    await self._consolidate_single_category(user_id, category)
                    # 整合后重新拉取，避免冲突检测基于已失活的旧向量
                    all_vecs = await qc.get_user_vectors(user_id, with_vectors=True, category=category)
                except Exception as _cap_err:
                    logger.warning(f"⚠️ [VectorCap] 局部整合失败，跳过继续写入 | {_cap_err}")
            db = self.db_manager.db

            for mem in all_vecs:
                mem_fact_id = mem.get("fact_id", "")
                if mem_fact_id == atomic_id:
                    continue  # 跳过自身

                mem_vec = mem.get("vector", [])
                if not mem_vec:
                    continue

                # 本地余弦计算（仅用于冲突检测，不是主检索路径）
                sim = _cosine_similarity(new_vec, mem_vec)
                if sim > 0.85 and mem.get("content", "") != fact:
                    logger.info(
                        f"🔄 [Conflict] 旧记忆过期 | uid={user_id[:8]} | "
                        f"sim={sim:.3f} | 旧: {mem.get('content','')[:30]} → 新: {fact[:30]}"
                    )
                    # Qdrant 软删除
                    await qc.deactivate_vector(mem_fact_id)
                    # COL_FACTS 同步标记失活
                    await db[COL_FACTS].update_one(
                        {"fact_id": mem_fact_id},
                        {"$set": {"is_active": False, "updated_at": datetime.now(timezone.utc)}},
                    )

        except Exception as e:
            logger.warning(f"⚠️ [Conflict-Check] 冲突检测异常，跳过直接落盘: {e}")

        # 无论冲突检测成功与否，都正常执行向量化落盘
        await self._vectorize_and_store_v3(user_id, fact, score, category, atomic_id, image_id)

    async def _vectorize_and_store_v3(
        self,
        user_id: str,
        fact: str,
        score: float,
        category: str,
        atomic_id: str,
        image_id: str,
    ):
        """
        [向量落盘 V3] 通过 qdrant_client.upsert_vector 写入 Qdrant。

        改动：
        - 旧版：先查 vector_col 是否 content 相同再决定是否重新 Embedding，
               然后 update_one 写 MongoDB vector_memory
        - 新版：直接 upsert_vector()；Qdrant 自身幂等，相同 fact_id 覆盖旧记录
               跳过重新 Embedding 的逻辑改为：只在 content 变化时调用 Embedding

        payload 字段遵循 qdrant_client.upsert_vector 要求：
            user_id / content / category / importance / image_id / is_active / updated_at
        """
        try:
            # 检查 Qdrant 中是否已有相同 fact_id + 相同 content（避免重复 Embedding）
            # with_vectors=True：content 未变时直接复用旧向量，无需重新 Embedding
            existing_vecs = await qc.get_user_vectors(user_id, with_vectors=True, category=category)
            existing = next(
                (v for v in existing_vecs if v.get("fact_id") == atomic_id), None
            )

            if existing and existing.get("content") == fact:
                # content 未变，只更新 importance 和 image_id（不重新 Embedding）
                updated_payload = {
                    **existing,
                    "importance":  score,
                    "image_id":    image_id or existing.get("image_id", ""),
                    "updated_at":  datetime.now(timezone.utc).isoformat(),
                    "is_active":   True,
                }
                updated_payload.pop("vector", None)  # upsert_vector 单独传 vector
                await qc.upsert_vector(
                    fact_id=atomic_id,
                    vector=existing["vector"],
                    payload=updated_payload,
                )
                return

            # content 变化或首次写入，重新生成 Embedding
            vector = await self._get_multimodal_embedding(fact)
            await qc.upsert_vector(
                fact_id=atomic_id,
                vector=vector,
                payload={
                    "user_id":    user_id,
                    "content":    fact,         # 语义正文，不含 UUID
                    "category":   category,
                    "importance": score,
                    "image_id":   image_id,     # 物理资产指针，挂载在 payload
                    "is_active":  True,
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                },
            )
            logger.info(
                f"✨ [Vector-Sink] 记忆已写入 Qdrant | "
                f"uid={user_id[:8]} | cat={category} | "
                f"image={(image_id or '')[:8] or '无'}"
            )
        except Exception as e:
            logger.warning(f"⚠️ [Vector-Sink] 落盘失败: {e}")

    # ──────────────────────────────────────────────────────────────
    # 工具函数
    # ──────────────────────────────────────────────────────────────

    def _universal_sanitizer(self, text: str) -> Optional[str]:
        """清洗 LLM 返回的名字，剔除 bot 自称和角色名，长度校验"""
        if not text:
            return None
        clean = text
        for dna in self.bot_dna:
            clean = re.sub(re.escape(dna), "", clean, flags=re.IGNORECASE)
        clean = re.sub(r'[^\w\u4e00-\u9fa5]', '', clean).strip()
        if len(clean) > 8:
            clean = re.sub(r"(雷电将军|露西|芙宁娜|流萤|纳西妲)$", "", clean)
        return clean if 2 <= len(clean) <= 12 else None

    async def _atomic_identity_sync(
        self, user_id: str, session_id: str, name: str
    ):
        """原子同步实名到 users 和 sessions 集合"""
        db = self.db_manager.db
        await asyncio.gather(
            db[COL_USERS].update_one(
                {"user_id": user_id},
                {"$set": {"user_name": name, "updated_at": datetime.now(timezone.utc)}},
            ),
            db[COL_SESSIONS].update_one(
                {"session_id": session_id},
                {"$set": {"user_name": name}},
            ),
            return_exceptions=True,
        )

    # ──────────────────────────────────────────────────────────────
    # AutoDream：记忆整合（CC 模式 4 阶段）
    # ──────────────────────────────────────────────────────────────

    async def consolidate_facts(self, user_id: str) -> dict:
        """
        [AutoDream] 4 阶段记忆整合：Orient → Gather → Consolidate → Prune

        触发条件（由调用方判断）：用户 core_facts 活跃条目数 > CONSOLIDATE_THRESHOLD。

        阶段说明：
        - Orient  : 统计各 category 下的 fact 数量，确定整合焦点
        - Gather  : 按 category 批量拉取 content，组成整合输入
        - Consolidate: LLM 将同 category 的碎片合并为 1-3 条精炼摘要
        - Prune   : 将被合并的旧 fact 标记 is_active=False，写入新摘要 fact

        返回：{"merged": int, "pruned": int} 供日志使用
        """
        CONSOLIDATE_THRESHOLD = int(os.getenv("AUTODREAM_THRESHOLD", 30))
        CONSOLIDATE_PROMPT = """你是希卡莉喵的记忆整合模块。请将以下关于同一用户的零散事实片段压缩成 1-2 条精炼摘要。

要求：
1. 保留具体信息（职业、名字、喜好、经历），删除重复和冗余。
2. 每条摘要用陈述句，20-50字。
3. 必须严格压缩：输入再多碎片，最终只输出 1-2 条——合并同类项，不要逐条保留。
4. 只输出 JSON：{{"summaries": ["摘要1", "摘要2"]}}，不输出任何解释。

分类：{category}
事实片段：
{fragments}"""

        db = self.db_manager.db
        merged_count = 0
        pruned_count = 0

        try:
            # ── Orient：统计各 category 活跃 fact 数 ──────────────
            pipeline = [
                {"$match": {"user_id": user_id, "is_active": True}},
                {"$group": {"_id": "$category", "count": {"$sum": 1}}},
                {"$sort": {"count": -1}},
            ]
            cat_counts = await db[COL_FACTS].aggregate(pipeline).to_list(length=50)
            # 只整合条目数 >= 5 的 category
            focus_cats = [c["_id"] for c in cat_counts if c["count"] >= 5]

            if not focus_cats:
                return {"merged": 0, "pruned": 0}

            for category in focus_cats:
                # ── Gather：拉取该 category 下所有活跃 fact ────────
                cursor = db[COL_FACTS].find(
                    {"user_id": user_id, "category": category, "is_active": True},
                    {"fact_id": 1, "topic": 1, "content": 1, "confidence": 1},
                ).sort("confidence", -1).limit(20)
                facts = await cursor.to_list(length=20)

                if len(facts) < 5:
                    continue

                fragments_text = "\n".join(
                    f"[{f.get('topic', '未知')}] {f.get('content', '')}"
                    for f in facts
                )

                # ── Consolidate：LLM 压缩 ────────────────────────
                try:
                    resp = await asyncio.to_thread(
                        self.client.chat.completions.create,
                        model=self.model,
                        messages=[{
                            "role": "user",
                            "content": CONSOLIDATE_PROMPT.format(
                                category=category,
                                fragments=fragments_text,
                            ),
                        }],
                        max_tokens=300,
                        stream=False,
                    )
                    raw = resp.choices[0].message.content
                    match = re.search(r'\{"summaries"\s*:\s*\[.*?\]\s*\}', raw, re.DOTALL)
                    if not match:
                        continue
                    summaries = json.loads(match.group()).get("summaries", [])
                    if not summaries:
                        continue
                except Exception as e:
                    logger.warning(f"[AutoDream] Consolidate LLM 失败 | cat={category} | {e}")
                    continue

                # ── Prune：软删除旧 fact，写入合并摘要 ──────────
                old_fact_ids = [f["fact_id"] for f in facts]
                now = datetime.now(timezone.utc)

                # 标记旧 fact 失活（MongoDB + Qdrant）
                await db[COL_FACTS].update_many(
                    {"fact_id": {"$in": old_fact_ids}},
                    {"$set": {"is_active": False, "updated_at": now}},
                )
                for fid in old_fact_ids:
                    try:
                        await qc.deactivate_vector(fid)
                    except Exception:
                        pass
                pruned_count += len(old_fact_ids)

                # 写入合并摘要 fact（上限与 prompt 对齐：最多 2 条）
                for summary in summaries[:2]:
                    summary = summary.strip()
                    if not summary:
                        continue
                    seed = f"{user_id}_{category}_dream_summary_{summary[:20]}"
                    new_id = hashlib.md5(seed.encode()).hexdigest()[:16]

                    await db[COL_FACTS].update_one(
                        {"fact_id": new_id},
                        {
                            "$set": {
                                "user_id":    user_id,
                                "subject":    "用户",
                                "category":   category,
                                "topic":      "整合摘要",
                                "content":    summary,
                                "is_active":  True,
                                "confidence": 0.9,
                                "updated_at": now,
                                "source":     "autodream",
                            },
                            "$setOnInsert": {"created_at": now, "mention_count": 1},
                        },
                        upsert=True,
                    )
                    # 向量化落盘（直接 await，Temporal Activity 上下文中 create_task 不安全）
                    await self._vectorize_and_store_v3(
                        user_id, summary, 0.9, category, new_id, ""
                    )
                    merged_count += 1

            logger.info(
                f"[AutoDream] 整合完成 | uid={user_id[:8]} | "
                f"merged={merged_count} | pruned={pruned_count}"
            )
        except Exception as e:
            logger.error(f"[AutoDream] consolidate_facts 异常: {e}")

        return {"merged": merged_count, "pruned": pruned_count}

    async def _consolidate_single_category(self, user_id: str, category: str) -> None:
        """
        [局部整合] 对单个 category 执行一次 AutoDream 压缩，复用 consolidate_facts 的核心逻辑。

        调用场景：_conflict_check_and_sink 检测到该 category 向量数超过 _CAT_VECTOR_LIMIT。
        与 AutoDream 并发安全：deactivate_vector 和 upsert_vector 均幂等，
        最坏情况下双方写入相同摘要（fact_id 由内容 MD5 决定），下次整合会合并，无数据损坏。
        """
        db = self.db_manager.db
        now = datetime.now(timezone.utc)

        cursor = db[COL_FACTS].find(
            {"user_id": user_id, "category": category, "is_active": True},
            {"fact_id": 1, "topic": 1, "content": 1, "confidence": 1},
        ).sort("confidence", -1).limit(20)
        facts = await cursor.to_list(length=20)

        if len(facts) < 5:
            return

        fragments_text = "\n".join(
            f"[{f.get('topic', '未知')}] {f.get('content', '')}"
            for f in facts
        )

        resp = await asyncio.to_thread(
            self.client.chat.completions.create,
            model=self.model,
            messages=[{
                "role": "user",
                "content": CONSOLIDATE_PROMPT.format(
                    category=category,
                    fragments=fragments_text,
                ),
            }],
            max_tokens=300,
            stream=False,
        )
        raw = resp.choices[0].message.content
        match = re.search(r'\{"summaries"\s*:\s*\[.*?\]\s*\}', raw, re.DOTALL)
        if not match:
            return
        summaries = json.loads(match.group()).get("summaries", [])
        if not summaries:
            return

        old_fact_ids = [f["fact_id"] for f in facts]
        await db[COL_FACTS].update_many(
            {"fact_id": {"$in": old_fact_ids}},
            {"$set": {"is_active": False, "updated_at": now}},
        )
        for fid in old_fact_ids:
            try:
                await qc.deactivate_vector(fid)
            except Exception:
                pass

        for summary in summaries[:2]:
            summary = summary.strip()
            if not summary:
                continue
            seed = f"{user_id}_{category}_dream_summary_{summary[:20]}"
            new_id = hashlib.md5(seed.encode()).hexdigest()[:16]
            await db[COL_FACTS].update_one(
                {"fact_id": new_id},
                {
                    "$set": {
                        "user_id":    user_id,
                        "subject":    "用户",
                        "category":   category,
                        "topic":      "整合摘要",
                        "content":    summary,
                        "is_active":  True,
                        "confidence": 0.9,
                        "updated_at": now,
                        "source":     "autodream",
                    },
                    "$setOnInsert": {"created_at": now, "mention_count": 1},
                },
                upsert=True,
            )
            await self._vectorize_and_store_v3(user_id, summary, 0.9, category, new_id, "")

        logger.info(
            f"🧹 [VectorCap] 局部整合完成 | uid={user_id[:8]} | "
            f"category={category} | pruned={len(old_fact_ids)} | merged={len(summaries[:2])}"
        )

    def _parse_json_brute_force(self, raw: Any) -> List[Dict]:
        """
        解析 LLM 返回的 JSON，容忍前后有解释文字。

        策略：
        1. 非贪婪匹配找 {"fragments": [...]} 结构
        2. 字段白名单校验，过��结构非法条目
        3. 任何异常返回 []，不向上抛出
        """
        REQUIRED = {"content"}
        ALLOWED  = {"subject", "category", "topic", "content", "confidence"}

        try:
            text = str(raw)
            match = re.search(r'\{"fragments"\s*:\s*\[.*?\]\s*\}', text, re.DOTALL)
            if not match:
                match = re.search(r'\{[^{}]*"fragments"[^{}]*\[[\s\S]*?\][^{}]*\}', text)
            if not match:
                return []

            data = json.loads(match.group())
            fragments = data.get("fragments", [])
            if not isinstance(fragments, list):
                return []

            valid = []
            for f in fragments:
                if not isinstance(f, dict):
                    continue
                if not f.get("content", "").strip():
                    continue
                clean = {k: v for k, v in f.items() if k in ALLOWED}
                valid.append(clean)
            return valid
        except Exception as e:
            logger.debug(f"[Parse] JSON 解析失败: {e}")
            return []


# ══════════════════════════════════════════════════════════════════
# 模块级工具（供冲突检测使用，不依赖实例）
# ══════════════════════════════════════════════════════════════════

def _cosine_similarity(v1: List[float], v2: List[float]) -> float:
    """
    本地余弦相似度计算（仅用于冲突检测前的快速筛查）。
    主检索路径已迁移到 Qdrant，此函数仅保留供 _conflict_check_and_sink 使用。
    """
    try:
        import math
        dot = sum(a * b for a, b in zip(v1, v2))
        m1  = math.sqrt(sum(a * a for a in v1))
        m2  = math.sqrt(sum(b * b for b in v2))
        return dot / (m1 * m2) if m1 > 0 and m2 > 0 else 0.0
    except Exception:
        return 0.0


# ══════════════════════════════════════════════════════════════════
# 单例导出
# ══════════════════════════════════════════════════════════════════

_instance: FactExtractor | None = None


def get_fact_extractor() -> FactExtractor:
    global _instance
    if _instance is None:
        _instance = FactExtractor()
    return _instance
