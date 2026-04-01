# 希卡莉·希卡莉喵 (Hikari-Chat-cosplayer) 开发文档

> 版本：v8.5 · 重构基准：MASTER v3.0 · 文档生成日期：2026-04-01 · 修订：Redis Stream 定向推送替代 PubSub 广播、core_facts/core_memories 索引补全、copywriting 降级告警（v8.2→v8.3）；rag_service deactivate Qdrant ID 类型修复、图生图配额扣减条件修复、.env 补全密码变量（v8.3→v8.4）；三层架构契约规则、跨小程序数据打通规则、模块间通信规则正式写入文档（v8.4→v8.5）

---

## 一、项目概述

### 1.1 业务定位

**希卡莉·希卡莉喵**是一套面向 cosplay 圈子的 AI 陪伴 Bot 后端系统，通过微信小程序为用户提供情感化长期陪伴服务。角色设定为"白猫娘经纪喵"，深度融入 coser/摄影/妆造圈子生态，核心价值在于有记忆、有情感的持续陪伴，而非工具性一次性问答。系统同时支持线下门店 Live2D 屏幕端接入，实现线上线下一体化运营。

### 1.2 核心特性

- **多模态对话**：文字 + 图片联合推理，视觉语义化后注入 LLM 上下文，物理屏蔽 UUID 防泄露
- **用户画像沉淀**：两阶段嗅探雷达（PRE 实名提取 + POST 多维事实沉淀），持久化至 Qdrant 向量库
- **Hybrid RAG 检索**：Qdrant 语义向量 + ES IK 分词全文检索双路并行，LLM Rerank 精选，2-Gram 兜底降级
- **好感度成长系统**：0~100 梯度，三档 NPC 语气（陌生人/朋友/灵魂伴侣），里程碑（30/70/90）触发特殊事件
- **工具生态**：图生图（Seedream 四宫格 CoserPose 替身）、妆容分析、二创文案（小红书/微博/B站），均走 Temporal 异步编排
- **线下门店模式**：Live2D 屏幕端 WebSocket 实时同步（动作/表情/弹幕群体感知），多店配置三级 Fallback
- **邀请制访问控制**：INVITE_ONLY 开关，VIP/SVIP 功能锁，图生图专项配额，原子操作防竞态
- **Soul 热配置**：人设模板/好感度等级/硬性限制从 MongoDB 实时读取，60s 内存缓存，无需重启更新
- **Temporal 任务编排**：完全替代旧版 Redis Queue Worker，支持 Cron 调度、幂等重试、两级降级（Temporal → asyncio）

### 1.3 技术栈总览

| 分类 | 技术 |
|---|---|
| 后端框架 | Python 3.12 · Quart（ASGI）· Gunicorn + Uvicorn Worker |
| 主数据库 | MongoDB 7.0（Motor 异步驱动，WiredTiger 缓存 1.5GB） |
| 缓存 / 消息总线 | Redis 7.0（AOF 持久化，Stream 定向推送跨实例广播） |
| 向量数据库 | Qdrant 1.9（2048 维余弦距离，hikari_memories 集合） |
| 全文检索 | Elasticsearch 8.13（IK Max Word 中文分词，可降级 2-Gram） |
| 任务编排 | Temporal 1.29（PostgreSQL 持久化，Cron Workflow） |
| AI 推理 | 字节跳动豆包 Doubao-Seed-1.8（火山方舟 Ark，Responses API） |
| 向量嵌入 | 豆包 multimodal_embeddings（2048 维） |
| 图生图 | Seedream（火山方舟 ep，四宫格生成） |
| TTS 引擎 | GPT-SoVITS 本地集群（多端口 Failover，宿主机运行） |
| 知识库同步 | 飞书 lark-openapi-mcp（SSE 模式，Node.js） |
| 外部服务 | 高德地图 REST API · 微信小程序（登录 + 内容安全） · NekosBest GIF |
| 网关 | Nginx 1.28（反向代理 + 路由分发 + SSL） |
| 容器编排 | Docker Compose（17 容器，~7.4GB 内存预算） |

---

## 二、目录结构

```
Hikari-Chat-cosplayer/
├── src/
│   ├── main.py                        # ASGI 应用工厂，蓝图注册，启动预热
│   ├── worker_main.py                 # Temporal Worker 入口，按需注册 Workflow
│   ├── core/                          # 核心中枢层（纯业务逻辑，禁止 import modules 层）
│   │   ├── orchestrator.py            # 全局调度指挥，11 步多模态交互管线
│   │   ├── dialogue_engine.py         # LLM 推理引擎，Hybrid RAG，Responses API
│   │   ├── fact_extractor.py          # 嗅探雷达，PRE/POST 两阶段画像沉淀，Qdrant 写入
│   │   ├── session_manager.py         # Session 原子播种，Redis 缓存层
│   │   ├── memory_storage.py          # 对话记忆双写（MongoDB + ES），2-Gram 降级
│   │   ├── soul_config.py             # SOUL 热配置，core_config 读取，60s 内存缓存
│   │   ├── hikari_prompts.py          # 全部 Prompt 常量 + 无状态构建函数
│   │   └── trigger_manager.py         # 好感度里程碑 / 画像归档调度 / 冷场整活触发
│   ├── services/                      # 业务服务层
│   │   ├── affinity_service.py        # 好感度脉冲，双写 Session + User，击穿 Redis
│   │   ├── asset_service.py           # TTS 资产引擎，多端口 Failover，防重锁，WS 推流
│   │   ├── amap_service.py            # 高德地图，LLM 意图提取 → REST API → system prompt
│   │   ├── image_gen_service.py       # Seedream 四宫格图生图（视觉分析 + 生成 + 落地）
│   │   ├── makeup_service.py          # 妆容分析（轻量校验 → 视觉联网分析 → 口吻转换）
│   │   ├── copywriting_service.py     # 二创文案，两轮状态机，网格图合并上传
│   │   ├── rag_service.py             # B 端知识库，多租户隔离，飞书同步，Qdrant 向量化
│   │   ├── memoir_exporter.py         # 灵魂报告生成（Markdown，WS 通知下载）
│   │   ├── store_service.py           # 门店共用工具（多店配置 / Live2D 推送 / 弹幕群体感知）
│   │   ├── nekos_service.py           # 情绪 GIF（NekosBest API，Redis + 内存双缓存）
│   │   └── ws_result_push.py          # 工具结果 WS 推送统一入口
│   ├── modules/                       # 路由层（蓝图）
│   │   ├── users/
│   │   │   ├── api.py                 # /api/u/ 用户域（登录/WS/历史/图片/语音/报告/声线）
│   │   │   ├── auth_handler.py        # 微信登录 / Token 轮换 / 邀请码激活
│   │   │   └── chat_handler.py        # 多模态交互主管线，工具分流路由
│   │   ├── manage/
│   │   │   ├── api.py                 # /api/m/ 管理域（用户/VIP/角色/套餐/订单/Soul配置）
│   │   │   └── admin_api.py           # /api/sys/ 系统域（X-Admin-Key 紧急操作）
│   │   ├── store/
│   │   │   └── api.py                 # /api/s/ 到店域（签到/结账/屏幕端WS/弹幕/订单）
│   │   ├── knowledge/
│   │   │   └── api.py                 # /api/k/ 知识库域（X-Skill-Key，条目CRUD/飞书同步）
│   │   ├── public/
│   │   │   └── api.py                 # /api/p/ 公开域（商城信息/套餐展示，hikari-shop容器）
│   │   └── tools/
│   │       ├── imagegen/api.py        # hikari-imagegen 容器入口（端口 8002）
│   │       ├── makeup/api.py          # hikari-makeup 容器入口（端口 8003）
│   │       ├── copywriting/api.py     # hikari-copywriting 容器入口（端口 8004）
│   │       ├── copywriting/handler.py # CopywritingWorkflow Activity 实现层
│   │       └── shared/
│   │           ├── image_upload.py    # 图片上传/文件服务蓝图（各工具容器共挂）
│   │           └── vip_guard.py       # VIP 权限守卫（功能锁 + 配额检查）
│   ├── workflows/                     # Temporal Workflow 层
│   │   ├── image_gen_workflow.py      # 图生图 4 步串行 Workflow
│   │   ├── makeup_workflow.py         # 妆容分析 4 步串行 Workflow
│   │   ├── copywriting_workflow.py    # 文案生成 2 步 Workflow
│   │   ├── voice_gen_workflow.py      # TTS 语音生成 2 步 Workflow
│   │   ├── push_workflow.py           # 每日主动推送 Cron Workflow
│   │   ├── health_workflow.py         # 系统健康检查 Cron Workflow（每 5 分钟）
│   │   ├── dream_workflow.py          # AutoDream 记忆整合 Cron Workflow（每日凌晨 3 点）
│   │   └── store_interaction_activity.py  # core 层访问 store_service 的桥接适配层
│   ├── schemas/
│   │   └── user.py                    # 用户文档物理播种协议（get_default_user_schema）
│   └── utils/                         # 公共工具层
│       ├── auth.py                    # JWT 签发/验证，4 个鉴权装饰器
│       ├── database.py                # MongoDB/Redis/ES 单例，COL_* 常量，索引自愈
│       ├── socket_manager.py          # WS 消息总线，Redis Stream 定向推送，断线补发
│       ├── invite.py                  # 邀请制 + VIP 控制，图生图配额原子操作
│       ├── vision_helper.py           # 图片处理（MIME检测/用户隔离/双版本压缩）
│       ├── asset_helper.py            # TTS 声线包路径管理，宿主机 Windows 路径映射
│       ├── wx_security.py             # 微信内容安全检测，access_token Redis 缓存
│       ├── qdrant_client.py           # Qdrant 单例，集合自愈，向量操作封装
│       ├── api_retry.py               # 豆包 API 429/5xx 指数退避重试
│       ├── feishu_helper.py           # 飞书 MCP 客户端（Bitable/Doc 拉取，自动翻页）
│       └── logger.py                  # 结构化日志（text/JSON双模式），TraceID 传播
├── deployment/
│   ├── nginx.conf                     # Nginx 反向代理配置（7 区域路由规则）
│   ├── init-mongo.js                  # MongoDB 索引初始化脚本（幂等）
│   ├── store_shops.json               # 多店铺配置（store_id/store_key/Live2D 动作映射）
│   ├── entrypoint.sh                  # 容器启动脚本
│   ├── checkin.html                   # 门店签到 H5 页面
│   ├── store_screen.html              # 门店 Live2D 屏幕端前端
│   ├── static/                        # Three.js / VRM 文件（Nginx 静态挂载）
│   └── temporal/
│       └── dynamicconfig/
│           └── development-sql.yaml   # Temporal 动态配置（PostgreSQL 模式）
├── Dockerfile                         # 主镜像（api + worker 共用，Python 3.12-slim）
├── Dockerfile.shop                    # hikari-shop 容器镜像
├── Dockerfile.imagegen                # hikari-imagegen 容器镜像
├── Dockerfile.makeup                  # hikari-makeup 容器镜像
├── Dockerfile.copywriting             # hikari-copywriting 容器镜像
├── Dockerfile.es                      # ES + IK 分词插件镜像
├── docker-compose.yml                 # 全容器编排（17 个容器）
├── gunicorn.conf.py                   # Gunicorn 配置（2 Worker，ASGI 模式，120s 超时）
├── requirements.txt                   # 主容器依赖（api + worker）
├── requirements.shop.txt              # hikari-shop 依赖
├── requirements.tool.txt              # 工具容器依赖（imagegen/makeup/copywriting）
└── .env                               # 环境变量映射模板（所有真实密钥物理透传）
```

---

## 三、核心模块与文件职责详解

### 3.1 核心中枢层（`src/core/`）

#### 3.1.1 `orchestrator.py` — 全局调度指挥（v6.0.0）

系统唯一的多模态交互入口，所有用户消息必须经过此处，以单例模式运行。

**核心方法：`handle_user_interaction()`**，11 步顺序执行：

```
1.  get_or_create_session()         → Session 确保存在
2.  get_visual_description()        → 图片 UUID → 30字语义描述（物理屏蔽 UUID）
3.  db[COL_MESSAGES].insert_one()   → 用户消息原子落盘
4.  RAG 知识库检索                   → 多租户并发 gather，高德/联网场景跳过
5.  dialogue_engine.process_message() → LLM 主推理
6.  [emotion] 前缀解析 + UUID 过滤   → 情绪标签提取，re.sub 洗白回复
7.  db[COL_MESSAGES].insert_one()   → 助手回复原子落盘（含 voice_status）
8.  asyncio.create_task(trigger_voice_task) → TTS 异步触发（非阻塞）
9.  _dispatch_image_gen_task()      → 图生图两级路由（Temporal → asyncio 降级）
10. add_background_task(_async_post_processing) → 事实沉淀+好感度+里程碑+归档+冷场整活
11. push_live2d_motion()            → 店铺模式推 Live2D 动作到屏幕端（STORE_ENABLED）
```

**降级链路**：`_dispatch_image_gen_task` 优先提交 Temporal WorkflowClient，`imagegen-{msg_id}` 幂等 ID 防重复触发；Temporal 不可用时静默降级到 `asyncio.create_task`，直接执行 `handle_image_gen_task`。

**异常防护**：整体 try/except 捕获，崩溃时返回 `{"success": False, "reply_text": "本喵的视觉神经刚才好像断开了..."}` 而非抛出 500。

**附属方法**：
- `get_user_history()`：游标分页历史打捞，三路 `$or` 兼容历史数据格式，饱和式打捞 image_id（根路径 → metadata → content 正则三路兜底）
- `_async_post_processing()`：后处理管线，高德/联网搜索场景跳过事实沉淀（防旧信息污染画像）
- `trigger_voice_task()`：从 DB 打捞真实 user_name 后调用 asset_service

#### 3.1.2 `dialogue_engine.py` — LLM 推理引擎（v5.0.0）

Hybrid RAG + Responses API 的核心推理层，单例模式。

**`process_message()` 执行流程（10 步）**：

```
0. get_soul_config()               → 预取 SOUL 热配置（60s 内存缓存）
1. db[COL_USERS].find_one()        → 状态打捞（affinity/user_name/last_active_at）
2. vision_helper.get_image_base64() → 视觉资产加载
3. _get_recent_history() + fact_extractor.sense(PRE) → 历史召回 + 实名提取
4. identity_harvest_trigger        → 首次对话强制问名字指令注入
5. Hybrid RAG 并行检索             → gram_task(ES/2-Gram) + vec_task(Qdrant) 并行 gather
                                       → 去重合并 → 候选数 > top_k 时触发 LLM Rerank
6. build_nya_prompt_split(soul=soul) → System Prompt 构建（静态前缀+动态上下文）
   + absence_hint                  → 回归感知（当天首消息，6h+ 未活跃触发）
   + last_search_hint              → 近期联网搜索相关记录注入（jieba 分词匹配，非联网/非高德场景）
   + store_packages_hint           → 在店客户 + 名字已识别时注入套餐文本
7. Responses API 推理              → tools=[web_search]（联网时挂载）
                                       → 历史上下文（Fernet 解密，带图仅还原最近1条）
                                       → Session 缓存链式传递（LLM_SESSION_CACHE=true 时）
8. 回复解析                        → 遍历 output 找 message 类型块
9. 状态计算                        → affinity_delta（身份+图片加成），mood_tag 解析
10. _sync_all_atomic_ops()         → 异步落盘（interactions计数/last_active_at/历史/记忆）
```

**安全过滤**：
- `_sanitize_injection()`：正则拦截 6 种越狱指令模式，命中时注入 `[⚠️ 系统警告]` 前缀
- `_purify_for_tts()`：剥离 `*\`#~_`、括号内容、多余省略号，净化 TTS 文本

**历史图片懒加载**：历史消息重建时只为最近 1 条含 `image_id` 的消息还原 base64（无论读取成功与否计数器立即递增），更早的含图消息降级为纯文本传入 LLM，避免多张历史图片占用大量 context token。

**联网搜索特殊处理**：回复写入 `search_cache` 集合（TTL 7天），不进普通 Redis 历史（防截断破坏信息完整性）；高德场景历史完全跳过（防旧地图信息污染）。

**近期联网搜索注入（`last_search_hint`）**：普通对话场景（非联网/非高德）从 `COL_SEARCH_CACHE` 读取最近 10 条搜索记录，用 jieba 分词对当前消息和 query 分别提词，取词集合交集非空的记录（最多 3 条）注入系统提示，带相对时间戳（如"2天前"），标注"只读参考、信息有时效性"防止模型过度延伸。

**Session 缓存模式**（`LLM_SESSION_CACHE=true`）：记录 `previous_response_id`，下轮只传最新一条消息，由 Ark 维护完整上下文；`previous_response_id` 失效时静默降级到无缓存重试。

#### 3.1.3 `fact_extractor.py` — 嗅探雷达（v7.0.0）

用户画像提取引擎，向量存储已从 MongoDB `core_vector_memory` 完全迁移至 Qdrant。

**`sense()` 两阶段**：

- **PRE 阶段**（匿名用户触发）：同步调用 LLM 提取 CN/昵称，禁止提取 AI 自身名字（`bot_dna` 黑名单）。命中后调用 `_atomic_identity_sync()` 原子写库，返回 `{detected_name}` 触发 +5 好感度脉冲。

- **POST 阶段**：通过 `audit_buffer` 积累对话片段（含视觉描述），达到 3 条或触发特殊条件时，调用 LLM 提取 7 大类事实（bio/pref/exp/rel/trait/plan/vision），经置信度阈值过滤（`VALUE_THRESHOLD=0.7`）后写入 `core_facts`（MongoDB）并向量化写入 Qdrant（`upsert_vector`）。冲突检测：先从 Qdrant 全量打捞用户向量，相同 category 冲突时 `deactivate_vector`。

**`retrieve_relevant_facts()`**：豆包 multimodal_embeddings 向量化查询文本，调用 `qdrant_client.search_vectors(score_threshold=0.6)`，返回含资产提示的记忆描述字符串。

**`consolidate_facts()`**（AutoDream，v8.2 新增）：4 阶段异步记忆整合，由 `DreamWorkflow` 每日凌晨触发：

```
Orient  → MongoDB aggregate 统计 core_facts 各 category 活跃条目数，提取 ≥5 条的分类
Gather  → 按 category 拉取最多 20 条 fact（按 confidence 降序），格式化为 [topic] content 列表
Consolidate → 调用 LLM（max_tokens=300）将碎片压缩为 1-3 条精炼摘要（JSON 输出）
Prune   → 旧 fact 双写失活（MongoDB update_many is_active=False + Qdrant deactivate_vector）
          → 写入新摘要 fact（confidence=0.9，source="autodream"，topic="整合摘要"）
          → await _vectorize_and_store_v3() 向量化落盘（不用 create_task，防 Temporal Activity 上下文截断）
```

触发阈值：用户 core_facts 活跃条目数 > `AUTODREAM_THRESHOLD`（默认 30）由 `DreamWorkflow` Activity 判断，`consolidate_facts()` 内部只关注 category 级别（≥5 条才整合）。新摘要 fact_id 由 `MD5(user_id + category + "dream_summary" + summary[:20])[:16]` 生成。

#### 3.1.4 `soul_config.py` — SOUL 热配置

从 `core_config` 集合（`key="soul"` 文档）读取人设配置，模块级内存缓存 60 秒（`_cache_lock` 防并发多次回源）。

配置结构：`persona_template`（覆盖 `hikari_prompts.py` 硬编码）、`affinity_levels`（好感度等级定义）、`hard_limits`（绝对禁止行为）、`tone_rules`（语气规则）。读取失败时降级到 `hikari_prompts.py` 硬编码默认值，系统不崩溃。

`update_soul_config()` 写入 MongoDB 后立即清空内存缓存，实现热更新（秒级生效）。

#### 3.1.5 `trigger_manager.py` — 调度防线（v4.0.0）

异步环境下的幂等性状态管理，负责三类调度任务：

**好感度里程碑检测**（`check_affinity_milestone`）：监测 30/70/90 三个阈值，Redis Key `hikari:milestone:last:{sid}` 存储本 Session 内已触发的最高阈值（单调递增），确保每个阈值只触发一次。

**画像归档调度**（`check_and_run_persona_archival`）：Redis NX 锁（`hikari:lock:archival:{uid}`）防并发。按 `PERSONA_ARCHIVE_THRESHOLD`（默认 5）增量判断，条件满足时调用 `memoir_exporter` 生成灵魂报告。

**冷场整活触发**（`check_engagement_need`）：读取最近 3 条 user 消息，30 秒冷却（`hikari:limit:engagement:{sid}`）。条件满足时调用 `dialogue_engine` 生成主动消息，落盘 + WS 推送 + TTS 触发。

---

### 3.2 业务服务层（`src/services/`）

#### 3.2.1 `affinity_service.py` — 好感度服务

每轮对话触发 `process_daily_chat()`：检测辱骂词（-5）或正常 +1，MongoDB aggregation pipeline 原子更新（`$max/$min` 边界锁定 0~100），同时更新 `mood` 字段。双写 `core_sessions` + `core_users`，写后删除 Redis Session 缓存（`hikari:session:{sid}`）防幻读。返回 `{old, new}` 供里程碑判定。

`update_affinity()` 用于专项跳变（如身份识别 +5，到店下单 +30），接受正负任意增量。

#### 3.2.2 `asset_service.py` — TTS 资产引擎

**并发控制**：`asyncio.Semaphore(MAX_TTS_CONCURRENCY)` 限制同时推理数，`_active_msg_locks` 内存字典防同一 msg_id 重复触发。

**多端口 Failover**：`itertools.cycle` 轮询 `TTS_ENGINE_PORTS`（默认 9880~9883），HTTP POST 调用 GPT-SoVITS，超时 `TTS_TIMEOUT`（默认 300s）。

**推流链路**：推理完成 → 落盘 `core_messages`（`voice_url` + `voice_status=ready`）→ `socket_manager.send_to_user` 推送 WS（含音频文件 URL）。

#### 3.2.3 `amap_service.py` — 高德地图服务

**主路径**：`build_amap_context(user_text)` → LLM 意图提取（`_llm_extract_intent`，temperature=0，返回 JSON）→ 按 intent 类型分发（transit/weather/around/search/transit+around）→ 调高德 REST API → 格式化文本注入 system prompt。

**意图类型**：`transit`（换乘路线）、`weather`（天气）、`around`（周边搜索）、`search`（地点搜索）、`transit+around`（路线 + 目的地周边，asyncio.gather 并行）。

**降级**：LLM 提取失败时回退 `_build_amap_context_regex` 正则提取方案。

**关键约定**：高德场景对话历史跳过写入（`_skip_history=True`），同时跳过后处理事实沉淀，防止路线/天气等即时信息污染用户长期画像。

#### 3.2.4 `image_gen_service.py` — 图生图服务

**触发词**：`"希卡莉启动pose替身"`（列表可扩展）

**两步工作流**：
1. `analyze_coser()`：主路径走 Responses API + file_id + 联网搜索识别角色，分析后设计 4 个动作；降级到 `chat.completions`（不联网）。
2. `generate_quad_image()`：原图 base64 + 分析文字 → Seedream ep 生成四宫格，`size="2K"`，不加水印，关闭序列生成。

**结果处理**：生成 URL 24h 失效，调用 `vision_helper.save_gen_image()` 立即下载落地到本地（`/api/u/images/{user_id}/gen_{uuid}.jpg`），落盘新消息 + WS 推送 `image_gen_result`。

#### 3.2.5 `makeup_service.py` — 妆容分析服务

**触发词**（11 个）：`"帮我看看妆"/"妆容分析"/"还原度分析"` 等，必须同时携带 image_id 才触发。

**三步工作流**：
1. `check_makeup_image()`：上传图片拿 file_id，轻量校验（`max_output_tokens=10`，YES/NO）是否 cosplay 妆容照。
2. `analyze_makeup()`：视觉分析 + 联网搜索角色妆容参考，生成专业分析报告。
3. `wrap_as_hikari_tone()`：将分析报告转成希卡莉傲娇点评口吻。

校验失败时直接提示用户图片不相关，不进入后续步骤。

#### 3.2.6 `copywriting_service.py` — 文案生成服务

**两轮状态机**（Redis Key `hikari:copy:state:{user_id}`，TTL=10min）：

**第一轮**（触发词 + 图片）：读取所有图片磁盘文件 → 拼接网格大图（N 张 → 1 次上传，节省 token）→ 上传拿 `grid_fid` → 存状态至 Redis → 推送询问气泡（等待参考图或主题）。

**第二轮**（等待参考图阶段）：三种情况：A. 用户发参考截图 → 校验是否文案截图（YES/NO）→ 附参考图生成；B. 用户发纯文字 → 语义判断是否取消（YES → 清 Redis，NO → 按主题生成）；C. 10 分钟超时 → Redis 自动过期。

支持平台：`xiaohongshu`/`weibo`/`bilibili`，未指定平台默认小红书。

#### 3.2.7 `rag_service.py` — B 端知识库

多租户隔离（`tenant_id` 字段），知识条目向量化后同时写入 MongoDB `core_kb` 和 Qdrant（以 MongoDB `_id` 作为 Qdrant point ID）。

**触发关键词**（`RAG_TRIGGERS`）：价格/推荐/怎么/课程/排班等 20+ 个，命中才检索，平时不耗成本。

**飞书同步**：通过 `feishu_helper.fetch_bitable()` 拉取多维表格，`source_id` 相同时 upsert（增量同步），支持自动翻页。

**检索 Pipeline**：多租户并发 gather → 按 score 合并排序 → 注入 orchestrator RAG context → dialogue_engine 拼入 system prompt。

#### 3.2.8 `store_service.py` — 门店共用工具

**多店配置**（`load_store_shops()`）三级 Fallback：`STORE_SHOPS_FILE`（JSON文件）→ `STORE_SHOPS`（JSON字符串环境变量）→ `STORE_KEY + STORE_ID`（单店快速配置）。

**Live2D 推送**（`push_live2d_motion`）：通过 `socket_manager.send_to_user(f"store_screen_{store_id}", ...)` 向屏幕端推送 `live2d_motion` 事件（motion/expression/priority）。

**弹幕群体感知**（`push_danmaku_to_redis`）：弹幕写入 Redis List（90s 滑动窗口，最多 30 条），积累到 10 条或 60s 内 6 条来自 4 个不同用户时触发 `_analyze_danmaku_and_reply`：LLM 判断主题相近 → 生成希卡莉广播回复 → 推屏幕端 `store_broadcast` 事件，120s 冷却防刷屏。

**名字同步**（`push_name_learned`）：识别到客户名字后推屏幕端 `name_learned` 事件，同时缓存套餐注入文本（`hikari:store:packages_injected:{user_id}`，TTL = `STORE_INSTORE_TTL` 分钟）。

---

### 3.3 路由层（`src/modules/`）

#### 3.3.1 `users/chat_handler.py` — 多模态交互主管线

`_resolve_user_context()` 身份解析（不做业务逻辑，只做鉴权判断），优先级链：

```
屏幕端(store_screen_*) → 在店客户(hikari:store:instore) → 宽限期(hikari:store:visited)
→ 管理角色(manage_role) → 邀请制(has_access) → 每日次数限制
```

`process_interaction_pipeline()` 工具分流路由：
- `is_copywriting_trigger(text, image_ids)` → 转发 hikari-copywriting 容器
- `is_makeup_trigger(text, image_id)` → 转发 hikari-makeup 容器
- 高德关键词触发 → `build_amap_context()` + `enable_amap=True`
- 联网关键词触发 → `enable_search=True`
- 默认 → `orchestrator.handle_user_interaction()`

微信内容安全检测（`check_msg_safe`）在管线入口处执行，不通过直接返回安全提示。

#### 3.3.2 `users/auth_handler.py` — 鉴权业务层

微信 code → `jscode2session` → `unionid`/`openid`，user_id 决策：`unionid > openid > cached_uid > dev 兜底`。首次登录调用 `get_default_user_schema()` 播种用户文档。

Token 体系：`create_access_token()`（JWT，2h）+ `create_refresh_token()`（secrets.token_hex(32)，非JWT，Redis 存储 30天）。登出时 `revoke_refresh_token()` 删除 Redis Key。

#### 3.3.3 `manage/api.py` — 管理后台

完整路由列表：

| 方法 | 路径 | 功能 | 权限 |
|---|---|---|---|
| GET | /api/m/whoami | 查询当前管理角色 | require_auth |
| GET | /api/m/users | 用户列表（昵称搜索/访问过滤） | admin |
| GET | /api/m/users/{id} | 用户详情 | admin |
| POST | /api/m/users/{id}/vip | 授予 VIP（支持叠加天数） | admin |
| POST | /api/m/users/{id}/kick | 踢出用户 | admin |
| GET/POST | /api/m/invites | 邀请码列表/批量生成 | admin |
| POST | /api/m/invites/{code}/revoke | 作废邀请码 | admin |
| GET | /api/m/roles | 管理角色列表 | admin |
| POST | /api/m/roles/grant | 授予管理角色 | admin |
| POST | /api/m/roles/revoke | 撤销管理角色 | admin |
| GET/POST | /api/m/packages | 套餐列表/创建 | admin/store_clerk |
| PATCH | /api/m/packages/{id} | 更新套餐 | admin/store_clerk |
| POST/DELETE | /api/m/packages/{id}/images | 套餐图片上传/删除 | admin/store_clerk |
| GET | /api/m/store/customers | 在店客户列表 | admin/store_clerk |
| POST | /api/m/orders | 创建订单（触发VIP授权+好感度+30） | admin/store_clerk |
| GET | /api/m/orders | 订单列表 | admin/store_clerk |
| GET | /api/m/screen-codes | 获取屏幕端登录码 | admin/store_clerk |
| POST | /api/m/screen-codes/reset | 重置屏幕登录码 | admin/store_clerk |
| GET/PUT | /api/m/config/soul | 查询/更新 SOUL 热配置 | admin |
| GET | /api/m/system/alerts | 系统健康告警查询 | admin |
| GET | /api/m/system/stats | 系统统计（用户/VIP/邀请码/订单） | admin |
| GET | /api/m/users/by-package | 按套餐查买家（分页） | admin |
| POST | /api/m/users/by-package/export | 导出买家画像报告（MD文件） | admin |
| GET | /api/m/files/buyer-portraits/{filename} | 下载买家画像 MD 文件 | admin（Bearer 或 ?token=） |

`store_clerk` 与 `admin` 区别：`store_clerk` 只能操作自己 `manage_store_id` 绑定的店铺。

#### 3.3.4 `store/api.py` — 到店场景（/api/s/）

| 方法 | 路径 | 功能 | 鉴权 |
|---|---|---|---|
| GET | /api/s/screen-init | 屏幕端初始化（短码验证） | 无（短码校验） |
| GET | /api/s/token | 客户获取会话 Token | X-Store-Key |
| GET | /api/s/tts-config | 屏幕端 TTS 配置 | X-Store-Key |
| POST | /api/s/checkin | 客户签到（生成 instore Token） | X-Store-Key |
| POST | /api/s/checkout | 客户离店 | Bearer JWT |
| GET | /api/s/instore | 查询在店状态 | Bearer JWT |
| GET | /api/s/customers | 在店客户列表 | X-Store-Key |
| GET | /api/s/packages | 店铺套餐列表 | Bearer JWT |
| POST | /api/s/orders | 店铺订单确认 | Bearer JWT |
| GET | /api/s/orders | 客户订单历史 | Bearer JWT |
| POST | /api/s/danmaku | 提交弹幕 | Bearer JWT |
| WS | /api/s/ws/{session_id} | 屏幕端 Live2D WebSocket | store_screen JWT |

签到流程：扫码 → `GET /api/s/token`（X-Store-Key）→ 获得带 store_id 的 JWT → WebSocket 连接 `/api/s/ws/store_screen_{store_id}` → 监听 `live2d_motion` / `store_event` / `store_broadcast` 事件。

---

### 3.4 异步任务与调度（`src/workflows/`）

#### Temporal Workflow 注册表

| Workflow | 触发方式 | Activity 链 | 超时/重试 |
|---|---|---|---|
| ImageGenWorkflow | 对话中触发词 | analyze_coser_image(60s/3) → generate_quad_image(120s/2) → save_gen_image(60s/2) → push_image_gen_result(10s/3) | workflow 超时 10min |
| MakeupWorkflow | 对话中触发词 | check_makeup_image(30s/2) → analyze_makeup(120s/2) → wrap_as_hikari_tone(60s/2) → push_makeup_result(10s/3) | workflow 超时 5min |
| CopywritingWorkflow | Round2 触发 | generate_copywriting(180s/2) → push_copywriting_result(10s/3) | workflow 超时 5min |
| VoiceGenWorkflow | 每轮回复 | generate_tts_audio(TTS_TIMEOUT/1) → push_voice_result(10s/3，失败时才推) | 随 TTS_TIMEOUT |
| PushSchedulerWorkflow | Cron（PUSH_CRON_EXPR） | run_daily_push(3600s/0) | 失败不重试等下次 |
| HealthCheckWorkflow | Cron（*/5 * * * *） | check_system_health(30s/0) → persist_health_result(10s/0) | 每 5 分钟 |
| DreamWorkflow | Cron（DREAM_CRON_EXPR，默认 0 3 * * *） | run_dream_consolidation(3600s/0) | 失败不重试等下次 |

所有 Cron Workflow 通过 `workflow_id` 幂等提交，重启后不会重复创建：
- `hikari-push-scheduler`：PushSchedulerWorkflow，需 `PUSH_ENABLED=true` 才提交
- `hikari-health-check`：HealthCheckWorkflow，无额外开关
- `hikari-dream-consolidation`：DreamWorkflow，需 `DREAM_ENABLED=true` 才提交

`ENABLE_WORKFLOW_*` 环境变量控制按需注册，`false` 时不 import 对应模块，避免 import 错误影响其他 Workflow。

---

### 3.5 公共工具层（`src/utils/`）

#### 3.5.1 `auth.py` — JWT 鉴权工具

**Token 体系**：
- Access Token：JWT，`{sub, jti, iat, exp, v:1, vip, role}`，有效期 2h
- Refresh Token：`secrets.token_hex(32)`（非JWT），Redis `hikari:refresh:{user_id}:{jti}`，TTL=30天

**四个装饰器**：
- `@require_auth`：验证 Bearer JWT，注入 `g.user_id / g.vip_level / g.role`，失败返回 401
- `@require_role(r)`：检查 `g.role`，必须在 `@require_auth` 之后使用，失败返回 403
- `@require_admin_key`：时序安全比较 `X-Admin-Key`（`secrets.compare_digest`），失败返回 401
- `@require_skill_key`：时序安全比较 `X-Skill-Key`，失败返回 401

`decode_token_payload()`：无需鉴权的 JWT Payload 解码，供屏幕端读取 store_id 使用。

#### 3.5.2 `socket_manager.py` — WS 消息总线

**两层推送架构**：
1. `redis.lpush(f"hikari:pending:{session_id}", msg)` + `redis.expire(600)`：消息持久化，断线重连时 `flush_pending()` 补发
2. 查 `hikari:ws_owner:{session_id}` 得到目标实例的 `instance_id`，`xadd` 到 `hikari:ws_stream:{instance_id}`：定向写入目标实例，仅目标实例消费，无跨实例广播

**`instance_id`**：每个 api 进程启动时由 `uuid4().hex` 生成，进程级唯一，重启后自动更新。

**`start_stream_consumer()`**：后台常驻协程，`XREAD BLOCK 2000ms` 拉取本实例专属 Stream，收到消息后若 `session_id` 在本实例 `active_queues` 中则入队，处理后立即 `XDEL`，Redis 断线后 5s 自动重连。Stream 设 `MAXLEN=2000` 防内存泄漏。

**连接管理**：
- `connect()`：pipeline 同时写 `hikari:ws_sid:{user_id}`（在线状态）和 `hikari:ws_owner:{session_id}`（路由表），均 TTL 24h
- `disconnect()`：删除 `hikari:ws_owner:{session_id}`（立即移除路由），保留 `hikari:ws_sid`（TTL 24h 自然过期，保证 push_worker 在断线后 24h 内仍会尝试推送）
- 用户离线时 `send_to_user()` 查不到 owner，只写 pending，等重连时 `flush_pending()` 补发

#### 3.5.3 `logger.py` — 结构化日志

`trace_id_var`（`contextvars.ContextVar`）跨 await 自动传播 TraceID，入口由 `before_request` 从 `X-Trace-ID` Header 或随机生成。JSON 模式输出 `{ts, inst, lvl, name, trace, msg}`，text 模式彩色终端输出含实例 ID 短标识。RotatingFileHandler 自动轮转（15MB × 3 备份）。


---

## 四、通信协议规范

### 4.1 WebSocket 长连接架构

**连接建立**：客户端 → `GET /api/u/ws/{session_id}` → Nginx（WebSocket Upgrade）→ hikari-api Quart WebSocket Handler

**会话绑定**：连接时向 Redis 写入 `hikari:ws_sid:{user_id} = session_id`（TTL=24h），供异步任务查询用户 session_id 进行推送。

**消息收发分离**：
```
recv_loop()  ←  客户端上行（JSON 消息体）→ process_interaction_pipeline()
send_loop()  →  asyncio.Queue → 前端下行（TTS / 图生图 / 文案 / 妆容结果）
```

**断线重连补发**：重连时调用 `socket_manager.flush_pending()`，从 Redis `hikari:pending:{session_id}` 按序补发积压消息（最多 10 分钟内）。

**跨实例定向推送**：`send_to_user()` 查 `hikari:ws_owner:{session_id}` 路由表，定向 `xadd` 到目标实例专属 Stream，仅持有该 WebSocket 的实例消费，不广播，支持多容器水平扩容。

**店铺屏幕端**：`session_id = store_screen_{store_id}`，专用 WS 路由 `/api/s/ws/{session_id}`，接收 `live2d_motion` / `store_event` / `store_broadcast` 事件驱动 Live2D 动画。

### 4.2 WebSocket 事件字典

| 推送方 | type | 触发场景 | 关键 Payload 字段 |
|---|---|---|---|
| dialogue_engine | `text` | 每轮 LLM 回复 | `msg_id, content, affinity, mood, voice_status` |
| asset_service | `voice` | TTS 生成完成 | `msg_id, voice_url, voice_status=ready` |
| image_gen_service | `image_gen_result` | 图生图完成 | `msg_id, success, gen_image_url, char_text` |
| makeup_service | `makeup_result` | 妆容分析完成 | `msg_id, success, content` |
| copywriting_service | `copywriting_result` | 文案生成完成 | `msg_id, success, content, platform` |
| ws_result_push | `tool_error` | 工具执行失败 | `msg_id, success=False, error_code, error` |
| memoir_exporter | `memoir_ready` | 灵魂报告生成完成 | `report_url` |
| trigger_manager | `milestone` | 好感度里程碑触发 | `threshold, affinity` |
| store_service | `live2d_motion` | 希卡莉说话/情绪变化 | `motion, expression, priority` |
| store_service | `store_event` | 客户名字识别/人工呼叫 | `event, user_name, live2d_motion` |
| store_service | `store_broadcast` | 弹幕群体感知广播 | `tts_text, live2d_motion, live2d_expression` |

### 4.3 HTTP API 通用响应格式

所有接口统一返回：
```json
{
  "code": 200,
  "message": "ok",
  "data": { ... }
}
```

错误时 `data` 为 `null`，HTTP 状态码与 `code` 字段保持一致（400/401/403/404/500）。

### 4.4 跨容器调用协议

工具路由（`user/api.py` → 工具容器）走 HTTP，`_forward_tool()` 函数 fire-and-forget，传播 `X-Trace-ID` 延续调用链路。超时 10s，失败记 warning 不影响主流程。

---

## 五、数据库设计

### 5.1 MongoDB 集合一览

| 集合名（COL_* 常量） | 用途 | 核心字段 |
|---|---|---|
| `core_users` | 用户主表 | `user_id, affinity, mood, user_name, vip_level, vip_expires_at, has_access, invite_code, manage_role, manage_store_id, image_gen_quota, metadata{}, preferences{}, voice_character, last_active_at, total_interactions` |
| `core_sessions` | 会话状态表 | `session_id, user_id, platform, affinity, mood, created_at, last_active` |
| `core_messages` | 消息审计表 | `session_id, user_id, role, content, image_id, voice_url, voice_status, gen_image_url, gen_image_status, char_text, report_url, sticker_url, client_msg_id, created_at` |
| `core_memories` | 对话记忆表（ES双写） | `memory_id, user_id, session_id, role, content, created_at, metadata{}` |
| `core_facts` | 结构化事实表（画像） | `user_id, category, content, confidence, is_active, image_id, source, created_at, updated_at`（source="autodream" 标记整合摘要来源） |
| `core_kb` | B端知识库 | `tenant_id, title, content, category, metadata{}, source, source_id, is_active, qdrant_point_id, created_at` |
| `core_config` | 系统热配置 | `key, persona_template, affinity_levels{}, hard_limits[], tone_rules{}, updated_at, updated_by` |
| `core_system_alerts` | 系统健康告警 | `timestamp, tts_ok, redis_ok, mongodb_ok, es_ok, qdrant_ok, tts_available_ports[], created_at` |
| `shop_packages` | 商城套餐 | `package_id, store_id, store_name, name, price, description, images[], category, type, is_active, sort_order, vip_level, vip_days, image_gen_quota` |
| `store_orders` | 到店订单 | `user_id, store_id, package_id, status, created_at` |
| `auth_invite_codes` | 邀请码 | `code, status, created_by, used_by, expires_at, created_at, revoked_at, revoke_reason, note` |
| `search_cache` | 联网搜索结果缓存 | `user_id, query, summary, created_at`（TTL 7天）|

**用户画像字段说明**：
- `metadata`：动态字典，category 体系（bio/pref/exp/rel/trait/plan/vision），scalar 类走 `$set`，list 类走 `$addToSet`
- `preferences`：`{topic → content}` 映射，记录用户对希卡莉的个性化要求（如"回复简短"），同 topic 新值覆盖

### 5.2 核心索引清单

**`core_users`**：
- `idx_user_id_unique`：`{user_id: 1}`，唯一索引，登录查询主键
- `idx_vip_level_created`：`{vip_level: 1, created_at: -1}`，管理后台 VIP 筛选
- `idx_affinity`：`{affinity: 1}`，好感度统计查询
- `idx_last_active`：`{last_active_at: -1}`，push_worker 活跃用户扫描
- `idx_user_name_text`：`{user_name: "text"}`，管理后台昵称模糊搜索
- `hikari_users_manage_role_v1`：`{manage_role: 1}`，稀疏索引，管理角色查询

**`core_sessions`**：
- `hikari_sessions_uid_v1`：`{user_id: 1}`，唯一索引，`ensure_indexes` 已建立
- `idx_session_id_unique`：`{session_id: 1}`，唯一索引（**待建**）
- `idx_session_ttl`：`{updated_at: 1}`，`expireAfterSeconds`，7天无活动自动清理（**待建**）

**`core_messages`**：
- `idx_msg_user_timeline`：`{user_id: 1, created_at: -1}`，历史分页主查询
- `idx_msg_session_timeline`：`{session_id: 1, created_at: -1}`，冷场整活读最近消息
- `idx_msg_voice_status`：`{user_id: 1, voice_status: 1}`，TTS 状态查询
- `idx_msg_image_id`：`{image_id: 1}`，稀疏索引，图片关联查询

**`core_memories`**：
- `hikari_memories_query_v1`：`{user_id: 1, created_at: -1}`，2-Gram 降级检索方向（`ensure_indexes` 已建立）
- `hikari_memories_dedup_v1`：`{user_id: 1, role: 1, content: 1}`，`save_memory()` 写入去重（`ensure_indexes` 已建立）

**`core_facts`**：
- `hikari_facts_uid_v1`：`{user_id: 1}`，`ensure_indexes` 已建立，基础查询索引
- `hikari_facts_category_v1`：`{user_id: 1, category: 1}`，画像聚合主查询（`ensure_indexes` 已建立）
- `hikari_facts_confidence_v1`：`{user_id: 1, confidence: -1, updated_at: -1}`，高置信度优先（`ensure_indexes` 已建立）
- `hikari_facts_active_v1`：`{user_id: 1, is_active: 1}`，有效事实过滤（`ensure_indexes` 已建立）

**`auth_invite_codes`**：
- `idx_invite_code_unique`：`{code: 1}`，唯一索引
- `idx_invite_expires_ttl`：`{expires_at: 1}`，`expireAfterSeconds: 0`，自动清理过期码

**`search_cache`**：
- `hikari_search_cache_ttl_v1`：`{created_at: 1}`，`expireAfterSeconds: 604800`，7天 TTL
- `hikari_search_cache_query_v1`：`{user_id: 1, created_at: -1}`，联网历史注入查询

**`core_system_alerts`**：
- `hikari_alerts_date_v1`：`{created_at: -1}`，查询排序（`ensure_indexes` 已建立）
- `hikari_alerts_ttl_v1`：`{created_at: 1}`，`expireAfterSeconds: 2592000`，30天 TTL 自动清理（`ensure_indexes` 已建立）

### 5.3 Qdrant 向量集合

集合名：`hikari_memories`（常量 `QDRANT_COLLECTION`，禁止在其他文件硬编码）

- 向量维度：2048（豆包 multimodal_embeddings）
- 距离度量：余弦距离（Cosine）
- Payload 字段：`user_id, content, category, importance, image_id, is_active, updated_at, fact_id`
- 检索参数：`score_threshold=0.6`，`top_k=5`（画像检索）

---

## 六、缓存与状态管理规范

### 6.1 Redis Key 命名空间完整清单

| Key 模式 | 数据结构 | TTL | 用途 |
|---|---|---|---|
| `hikari:history:{user_id}` | List | 24h | Fernet 加密对话历史，lpush 头插，ltrim 保留 N 轮 |
| `hikari:resp_id:{user_id}` | String | 24h | Responses API previous_response_id，Session 链式缓存 |
| `hikari:session:{session_id}` | String（JSON） | 7天 | Session 文档缓存，affinity_service 写后击穿 |
| `hikari:ws_sid:{user_id}` | String | 24h | 用户当前 WebSocket session_id 映射（在线状态判断） |
| `hikari:ws_owner:{session_id}` | String | 24h | WebSocket 路由表：session_id → instance_id，connect 写入，disconnect 删除 |
| `hikari:ws_stream:{instance_id}` | Stream | MAXLEN=2000 | 各 api 实例专属消息 Stream，`start_stream_consumer()` 消费后 XDEL |
| `hikari:pending:{session_id}` | List | 10min | 断线补发消息队列，重连时 flush_pending() |
| `hikari:invite:access:{user_id}` | String | 5min | 邀请制访问权限缓存（"1"/"0"） |
| `hikari:daily_msg:{user_id}:{date}` | String（计数） | 至次日0:05 | 当日消息计数，incr 原子递增 |
| `hikari:image_gen_quota:{user_id}` | String（整数） | 5min | 图生图配额 Redis 缓存，MongoDB 主存储 |
| `hikari:refresh:{user_id}:{jti}` | String | 30天 | Refresh Token 存储 |
| `hikari:wx_access_token` | String | Token 期限 - 5min | 微信全局 access_token |
| `hikari:nekos:{mood_tag}` | String | 10s | NekosBest GIF URL 缓存 |
| `hikari:first_msg:{user_id}:{date}` | String | 24h | 当天首消息标志，回归感知触发依据 |
| `hikari:lock:archival:{uid}` | String（NX） | 自动释放 | 画像归档 NX 排他锁，防并发重复归档 |
| `hikari:limit:engagement:{sid}` | String | 30s | 冷场整活冷却计数器 |
| `hikari:store:instore:{user_id}` | String | STORE_INSTORE_TTL 分钟 | 在店客户标记（值为 store_id） |
| `hikari:store:visited:{user_id}` | String | 宽限期 | 离店宽限期标记 |
| `hikari:store:packages_injected:{user_id}` | String | STORE_INSTORE_TTL 分钟 | 套餐文本注入缓存 |
| `hikari:store:danmaku:{store_id}` | List | 90s | 弹幕滑动窗口（最多 30 条） |
| `hikari:store:danmaku_cool:{store_id}` | String | 120s | 弹幕群体广播冷却 |
| `hikari:store:danmaku_lock:{store_id}` | String（NX） | 30s | 弹幕分析互斥锁 |
| `hikari:copy:state:{user_id}` | String（JSON） | 10min | 文案两轮状态机（grid_fid/platform/state） |
| `hikari:milestone:last:{sid}` | String | 永久 | 好感度里程碑进度锚点（存储本 Session 已触发的最高阈值整数，单调递增） |
| `hikari:archive:count:{sid}` | String | 永久 | 画像归档累计计数 |
| `hikari:fact_lock:{user_id}` | String（NX） | 30s | 事实提取 Redis 分布式锁，防多 Worker 并发重复提取 |

### 6.2 对话历史加密规范

Redis 中对话历史（`hikari:history:{user_id}`）使用 Fernet 对称加密（`REDIS_ENCRYPTION_KEY` 环境变量透传）。写入时 `cipher.encrypt(json.encode())`，读取时 `cipher.decrypt(item).decode()`，解密失败时尝试明文 JSON 解析（历史兼容）。未配置密钥时使用临时密钥（重启后历史失效，启动时 `logger.error` 告警）。

---

## 七、环境变量配置

### 7.1 系统基础

| 变量名 | 默认值 | 说明 |
|---|---|---|
| `APP_ENVIRONMENT` | `production` | 运行环境（development 时 Uvicorn 热重载） |
| `FLASK_PORT` | `8080` | 主应用监听端口（gunicorn.conf.py 固定 8000） |
| `TZ` | `Asia/Shanghai` | 容器时区 |
| `LOG_LEVEL` | `INFO` | 日志级别（DEBUG/INFO/WARNING/ERROR） |
| `LOG_FORMAT` | `text` | 日志格式（text=彩色终端 / json=结构化） |
| `LOG_DIR` | 空 | 日志文件目录（空=stdout，非空=写文件） |
| `CORS_ORIGIN` | `https://servicewechat.com` | 允许的跨域来源 |
| `SECURITY_FAIL_OPEN` | `false` | 安全接口异常时放行（生产必须 false） |
| `JWT_SECRET_KEY` | 必填 | JWT 签名密钥（物理透传，`secrets.token_hex(32)` 生成） |

### 7.2 数据库与缓存

| 变量名 | 默认值 / 示例 | 说明 |
|---|---|---|
| `MONGO_URI` | `mongodb://admin:pwd@hikari-mongodb:27017/hikari_nya?authSource=admin` | MongoDB 完整连接串 |
| `REDIS_URL` | `redis://:pwd@hikari-redis:6379/0` | Redis DSN（含密码） |
| `REDIS_ENCRYPTION_KEY` | 必填 | 对话历史 Fernet 加密密钥（物理透传） |
| `QDRANT_URL` | `http://hikari-qdrant:6333` | Qdrant 向量库地址 |
| `ES_HOST` | `http://hikari-es:9200` | Elasticsearch 地址（不配置时降级 2-Gram） |

### 7.3 LLM 与推理引擎

| 变量名 | 示例 | 说明 |
|---|---|---|
| `DOUBAO_API_KEY` | `sk-xxx` | 豆包 API Key（物理透传） |
| `DOUBAO_MAIN_EP` | `ep-20260214083658-grr2w` | 主对话 + 视觉 Endpoint ID |
| `LLM_EMBEDDING_MODEL` | `ep-20260305102042-6q9td` | 向量嵌入 Endpoint |
| `LLM_RERANK_MODEL` | `ep-20260313171928-cxz5f` | Rerank + 意图提取 Endpoint |
| `LLM_EXTRACT_MODEL` | `ep-20260313171928-cxz5f` | 事实/意图提取 Endpoint（同 Rerank） |
| `LLM_VISION_MODEL` | 与 MAIN_EP 相同 | 视觉分析 Endpoint |
| `SEEDREAM_EP` | `ep-20260315113222-46cpg` | Seedream 图生图 Endpoint |
| `LLM_MAX_OUTPUT_TOKENS` | `800` | LLM 最大输出 Token 数 |
| `LLM_TEMPERATURE` | `1.0` | 对话温度 |
| `LLM_TOP_P` | `0.7` | Top-P 采样（联网时强制 0.95） |
| `LLM_SESSION_CACHE` | `false` | Responses API 链式缓存（需后台开通） |
| `DIALOGUE_HISTORY_LIMIT` | `6` | Redis 对话历史保留轮数 |
| `DIALOGUE_RAG_LIMIT` | `8` | 2-Gram 检索候选上限 |
| `DIALOGUE_RERANK_TOP_K` | `5` | Rerank 精选上限 |
| `WEB_SEARCH_CONTEXT_SIZE` | `low` | 联网搜索返回内容量（low/medium/high） |
| `HISTORY_CONTENT_MAX_LEN` | `150` | 联网回复写入历史的截断长度 |
| `SESSION_TIMEOUT_SECONDS` | `604800` | Session 超时（7天） |

### 7.4 TTS 语音引擎

| 变量名 | 默认值 | 说明 |
|---|---|---|
| `TTS_ENGINE_HOST` | `http://host.docker.internal` | TTS 宿主机地址 |
| `TTS_ENGINE_PORTS` | `9880,9881,9882,9883` | 多端口 Failover（逗号分隔） |
| `MAX_TTS_CONCURRENCY` | 端口数量 | 最大并发 TTS 请求数 |
| `TTS_TIMEOUT` | `300` | TTS 请求超时（秒） |
| `DEFAULT_VOICE_CHARACTER` | `女性1_zh` | 默认声线 |
| `SAMPLE_ROOT` | `/app/assets/samples` | 容器内声线样本根目录 |
| `HOST_SAMPLE_ROOT` | Windows 路径 | 宿主机声线样本路径（供 TTS 引擎定位） |
| `TTS_TOP_K` | `15` | TTS 推理参数 |
| `TTS_TOP_P` | `0.8` | TTS 推理参数 |
| `TTS_TEMPERATURE` | `0.8` | TTS 推理参数 |
| `TTS_SPEED_FACTOR` | `1.0` | 语速倍率 |
| `TTS_SPLIT_METHOD` | `cut5` | 文本分句方式 |

### 7.5 访问控制与运营

| 变量名 | 默认值 | 说明 |
|---|---|---|
| `INVITE_ONLY` | `true` | 邀请制开关（false=公测模式） |
| `INVITE_CODE_EXPIRE_DAYS` | `7` | 邀请码有效期（天） |
| `DAILY_MSG_LIMIT` | `100` | 普通用户每日消息上限 |
| `DAILY_MSG_LIMIT_VIP` | `0` | VIP 每日限制（0=不限） |
| `VIP_LOCK_IMAGE_GEN` | `true` | 图生图 VIP 锁 |
| `VIP_LOCK_MAKEUP` | `true` | 妆容分析 VIP 锁 |
| `VIP_LOCK_COPYWRITING` | `true` | 文案生成 VIP 锁 |
| `IMAGE_GEN_QUOTA_VIP` | `30` | VIP 图生图初始配额 |
| `IMAGE_GEN_QUOTA_SVIP` | `60` | SVIP 图生图初始配额 |
| `PERSONA_ARCHIVE_THRESHOLD` | `5` | 触发画像归档的事实碎片阈值 |
| `MEMOIR_FRAGMENTS_PER_CAT` | `6` | 每类别最多导出碎片数 |

### 7.6 门店与商城

| 变量名 | 说明 |
|---|---|
| `STORE_ENABLED` | 门店模式总开关（true 时注册 /api/s/ 蓝图） |
| `STORE_SHOPS_FILE` | 多店配置 JSON 文件路径（最高优先级） |
| `STORE_TOKEN_EXPIRE` | 屏幕端 Token 有效期（分钟） |
| `STORE_INSTORE_TTL` | 在店状态有效期（分钟，默认 240） |
| `STORE_ORDER_AFFINITY_DELTA` | 下单触发好感度增量（默认 +30） |
| `SHOP_NAME` / `SHOP_SLOGAN` | 公开商城名称与标语 |
| `PUBLIC_BASE_URL` | 套餐图片公网地址前缀（物理透传） |
| `REGISTER_PUBLIC_API` | 主容器注册 /api/p/ 开关（本地开发用） |

### 7.7 Temporal 调度

| 变量名 | 默认值 | 说明 |
|---|---|---|
| `TEMPORAL_HOST` | `hikari-temporal:7233` | Temporal 服务地址 |
| `TEMPORAL_NAMESPACE` | `default` | Temporal 命名空间 |
| `TEMPORAL_TASK_QUEUE` | `hikari-main` | 任务队列名 |
| `ENABLE_WORKFLOW_IMAGEGEN` | `true` | 图生图 Workflow 开关 |
| `ENABLE_WORKFLOW_VOICE` | `true` | 语音 Workflow 开关 |
| `ENABLE_WORKFLOW_MAKEUP` | `true` | 妆容 Workflow 开关 |
| `ENABLE_WORKFLOW_COPYWRITING` | `true` | 文案 Workflow 开关 |
| `ENABLE_WORKFLOW_PUSH` | `true` | 推送 Workflow 开关 |
| `ENABLE_WORKFLOW_HEALTH` | `true` | 健康检查 Workflow 开关 |
| `ENABLE_WORKFLOW_DREAM` | `true` | AutoDream 记忆整合 Workflow 开关 |
| `PUSH_ENABLED` | `false` | 主动推送功能总开关 |
| `PUSH_CRON_EXPR` | `0 2 * * *` | 推送 Cron 表达式（UTC，02:00=北京10:00） |
| `DREAM_ENABLED` | `false` | AutoDream 整合 Cron 总开关（false=不提交 Cron，Workflow 已注册但不运行） |
| `DREAM_CRON_EXPR` | `0 3 * * *` | AutoDream Cron 表达式（UTC，03:00=北京11:00） |
| `AUTODREAM_THRESHOLD` | `30` | 触发记忆整合的活跃 fact 数阈值（每用户） |

### 7.8 第三方服务

| 变量名 | 说明 |
|---|---|
| `WX_APPID` / `WX_SECRET` | 微信小程序 AppID 和 Secret（物理透传） |
| `AMAP_KEY` | 高德地图 API Key（物理透传） |
| `AMAP_ENABLED` | 高德地图开关 |
| `FEISHU_MCP_URL` | 飞书 MCP 服务地址（默认 http://hikari-lark-mcp:3001） |
| `HIKARI_SKILL_KEY` | 知识库接口鉴权 Key（物理透传） |
| `ADMIN_API_KEY` | 系统管理接口 Key（物理透传） |

> **密钥脱敏原则**：所有真实密钥通过 Windows 系统环境变量物理透传（`${VAR_NAME}` 格式），`.env` 文件只作为变量映射模板，不含任何明文密钥。生产环境密钥与代码仓库物理隔离。

---

## 八、部署与编排架构

### 8.1 容器矩阵

| 容器名 | 镜像 | 内存限制 | 监听端口 | 启动命令 |
|---|---|---|---|---|
| hikari-ntp | busybox | 64m | - | ntpd 一次性时间同步 |
| hikari-init | busybox | - | - | mkdir/chmod 目录初始化 |
| hikari-mongodb | mongo:7.0.30 | 768m | 127.0.0.1:27018 | --wiredTigerCacheSizeGB=1.5 |
| hikari-redis | redis:7.0.14 | 128m | 内网 | requirepass + appendonly yes |
| hikari-postgres | postgres:16-alpine | 256m | 内网 | Temporal 持久化 |
| hikari-qdrant | qdrant:v1.9.0 | 512m | 内网 | - |
| hikari-es | hikari-es-ik:8.13.0 | 768m | 内网 | IK 分词插件内置 |
| hikari-temporal | temporalio/auto-setup:1.29.2 | 512m | 内网 | - |
| hikari-temporal-ui | temporalio/ui:2.44.0 | 128m | 127.0.0.1:8233 | - |
| **hikari-api** | Dockerfile | 768m | 8000 | gunicorn src.main:app -w 2 -k uvicorn.workers.UvicornWorker -b 0.0.0.0:8000 --timeout 120 |
| **hikari-worker** | Dockerfile | 1024m | - | python -m src.worker_main |
| hikari-shop | Dockerfile.shop | 256m | 8001 | gunicorn src.modules.public.app:app -w 2 |

> **Gunicorn 配置说明**：hikari-api 使用项目根目录的 `gunicorn.conf.py` 作为基础配置（gunicorn 自动读取），`docker-compose.yml` command 中的参数会覆盖配置文件中的同名项。`gunicorn.conf.py` 已启用 `preload_app = True`（Copy-on-Write 节省内存），`worker_connections = 2048`，`keepalive = 5`。
| hikari-imagegen | Dockerfile.imagegen | 512m | 8002 | gunicorn src.modules.tools.imagegen.api:app -w 1 --timeout 180 |
| hikari-makeup | Dockerfile.makeup | 512m | 8003 | gunicorn src.modules.tools.makeup.api:app -w 1 --timeout 120 |
| hikari-copywriting | Dockerfile.copywriting | 512m | 8004 | gunicorn src.modules.tools.copywriting.api:app -w 1 --timeout 120 |
| hikari-lark-mcp | node:22-alpine | 128m | 3001 | lark-openapi-mcp SSE 模式 |
| hikari-nginx | nginx:1.28.1-alpine | 128m | 80/443 | 反向代理 |

> 内存合计约 7.4GB，8GB 宿主机适用。makeup 和 copywriting 容器内存紧张时可暂不启动，待升级 16GB 后开启独立容器。

### 8.2 网关路由映射（Nginx 7 区域规则）

```
# 区域 1：WebSocket（最高优先级）
/api/u/ws/          → hikari-api:8000  （用户主 WS）
/api/s/ws/          → hikari-api:8000  （屏幕端 WS）

# 区域 2：工具容器路由（特定路径精确匹配，在粗路径之前）
/api/u/copywriting/ → hikari-copywriting:8004
/api/u/makeup/      → hikari-makeup:8003
/api/u/images       → hikari-imagegen:8002   （图片上传 = 精确）
/api/u/images/      → hikari-imagegen:8002   （图片文件 = 前缀）

# 区域 3：公开商城
/api/p/             → hikari-shop:8001

# 区域 4：主服务兜底（粗路径，必须在工具路由之后）
/api/               → hikari-api:8000

# 区域 5：TTS 引擎代理（宿主机直连）
/tts_api/           → host.docker.internal:9880

# 区域 6：静态资源挂载
/api/u/voices/      → nginx alias（/app/assets/voices/）  ← 语音文件直接由 nginx 静态服务，不经过 hikari-api
/api/p/images/      → nginx alias（/app/assets/packages/）
/store_screen.html  → nginx 直接返回 HTML 文件
/checkin            → nginx 直接返回签到 H5
/static/            → /usr/share/nginx/html/static/（Three.js / VRM 文件）
/live2d/            → /app/assets/live2d/
/live2d_background/ → /app/assets/live2d_background/

# 区域 7：健康检查（无鉴权）
/api/sys/health     → hikari-api:8000
```

**新工具扩展规则**：在"新工具插入点"注释上方添加 `location /api/u/{新资源}/ { proxy_pass http://hikari-{工具名}:{端口}; }`，端口从 8005 开始递增。删除功能时注释 location，不物理删除（保留恢复能力）。

### 8.3 Volume 挂载清单

| 宿主机路径 | 容器内路径 | 挂载容器 | 用途 |
|---|---|---|---|
| `./data/mongo/db` | `/data/db` | hikari-mongodb | MongoDB 数据持久化 |
| `./data/mongo/config` | `/data/configdb` | hikari-mongodb | MongoDB 配置持久化 |
| `./data/redis` | `/data` | hikari-redis | Redis AOF 持久化 |
| `./data/qdrant` | `/qdrant/storage` | hikari-qdrant | 向量数据持久化 |
| `./data/es` | `/usr/share/elasticsearch/data` | hikari-es | ES 索引持久化 |
| `./data/postgres` | `/var/lib/postgresql/data` | hikari-postgres | Temporal 状态持久化 |
| `./deployment/temporal/dynamicconfig` | `/etc/temporal/config/dynamicconfig` | hikari-temporal | Temporal 动态配置（只读） |
| `./assets/samples` | `/app/assets/samples` | api/worker | TTS 声线样本（只读） |
| `./assets/voices` | `/app/assets/voices` | api/worker | TTS 生成语音文件 |
| `./assets/uploads` | `/app/assets/uploads` | api/worker/tools | 用户上传图片 |
| `./assets/packages` | `/app/assets/packages` | api/worker/shop | 套餐图片 |
| `./assets/live2d` | `/app/assets/live2d` | nginx | Live2D 模型文件 |
| `./assets/live2d_background` | `/app/assets/live2d_background` | nginx | Live2D 背景图 |
| `./exports` | `/app/exports` | api/worker | 灵魂报告 + 买家画像导出文件 |
| `./logs` | `/app/logs` | api/worker | 应用日志文件 |
| `./deployment/store_shops.json` | `/app/deployment/store_shops.json` | api/worker | 多店铺配置文件（只读，精确文件挂载） |
| `./deployment/nginx.conf` | `/etc/nginx/nginx.conf` | nginx | Nginx 配置（只读） |
| `./deployment/store_screen.html` | `/usr/share/nginx/html/store_screen.html` | nginx | 屏幕端 HTML（只读） |
| `./deployment/checkin.html` | `/usr/share/nginx/html/checkin.html` | nginx | 签到 H5（只读） |
| `./deployment/static` | `/usr/share/nginx/html/static` | nginx | Three.js/VRM 静态文件（只读） |

### 8.4 架构扩展规则（三层契约）

系统以三层契约为核心设计原则，已通过代码审计验证全部符合（2026-04-01）。

#### 8.4.1 三层契约定义

| 层级 | 名称 | 职责 | 修改频率 |
|---|---|---|---|
| 第一层 | URL 层（路由契约） | 对前端/用户的承诺，蓝图前缀 `/api/{主体}/` | 发布后不改 |
| 第二层 | Nginx 层（部署契约） | 决定请求去哪个容器，`proxy_pass` 目标 | 按需修改，不影响 URL |
| 第三层 | 代码层（逻辑契约） | 功能具体实现，移动到新容器时只改 Nginx | 功能变更时修改 |

**核心规则**：
- 改第二层（Nginx）不影响第一层（URL）和第三层（代码）
- **增加功能** = 新容器 + Nginx 加 `location` + 可选新小程序，其他容器/代码/数据库完全不动
- **删除功能** = Nginx 注释 `location` + 停容器 + 关闭 Workflow 开关，不物理删除（保留恢复能力）

**当前路由主体（固定为 6 个，禁止新增主体）**：

| 主体 | 前缀 | 容器（默认） | 说明 |
|---|---|---|---|
| `u` | `/api/u/` | hikari-api:8000 | 用户域（含工具子路径分流至独立容器） |
| `m` | `/api/m/` | hikari-api:8000 | 管理域 |
| `s` | `/api/s/` | hikari-api:8000 | 到店域 |
| `k` | `/api/k/` | hikari-api:8000 | 知识库域 |
| `p` | `/api/p/` | hikari-shop:8001 | 公开商城域 |
| `sys` | `/api/sys/` | hikari-api:8000 | 系统紧急操作域 |

工具扩展时在 `/api/u/` 下新增子路径（如 `/api/u/voice_clone/`）并分流至新容器，**不新增顶级主体**。

#### 8.4.2 跨小程序用户数据打通规则

所有小程序必须注册在同一个微信开放平台主体下，确保 `unionid` 一致。

**`user_id` 生成规则**（单一入口：`auth_handler.handle_login()`）：

```
unionid 存在  → user_id = "wx_{unionid}"   （主推，跨小程序一致）
unionid 为空  → user_id = "wx_{openid}"    （内测期间可接受）
openid 也无   → user_id = cached_uid        （沿用前端缓存）
开发模式      → user_id = "dev_{hash16}"   （js_code hash，非生产）
完全离线      → user_id = "dev_guest"
```

同一用户在所有小程序里共用同一份数据：好感度、VIP 状态、配额、对话记忆、用户画像。
`user_id` 只在 `auth_handler.py` 一处生成，其他模块只读取，不生成。

#### 8.4.3 模块间通信规则

```
允许：同模块内直接 import（如 users/api.py → users/chat_handler.py）
允许：跨模块通过 services/ 层（共享服务）
允许：跨模块通过 utils/ 工具函数
允许：跨模块通过 Temporal Workflow（异步任务）
允许：modules 单向依赖 core/（只读调用）

禁止：跨模块直接 import 对方的 handler.py 或 api.py
禁止：core/ 层 import 任何 modules/ 层文件（单向依赖，core 只被调用）
禁止：tools/shared/ 被 tools/ 以外的模块 import
```

**当前审计结论（2026-04-01 全量验证）**：
- core/ 下 9 个文件全部无 `from src.modules` 语句 ✓
- `tools/shared/` 仅被 copywriting / imagegen / makeup 三个子模块引用 ✓
- 无跨模块 handler/api 直接交叉 import ✓

---

## 九、核心业务生命周期

### 9.1 用户首次登录流程

```
微信小程序 js_code
    → POST /api/u/auth/login {js_code}
    → auth_handler.handle_login()
        → GET https://api.weixin.qq.com/sns/jscode2session
        → 解析 unionid / openid
        → user_id = wx_{unionid}（优先 unionid，跨小程序一致）
        → db[core_users].find_one() → 不存在则 insert_one(get_default_user_schema())
        → session_manager.get_or_create_session()
        → create_access_token() + create_refresh_token()
    ← {access_token, refresh_token, user_id, user_name, vip_info}
```

### 9.2 普通对话完整链路

```
客户端 WebSocket 发送:
{
  "message": "今天漫展怎么去？",
  "image_id": "",
  "client_msg_id": "xxx"
}

1. api.py recv_loop()
    → _resolve_user_context()  # 身份 + 访问控制（邀请制/VIP/每日上限）
    → process_interaction_pipeline()

2. chat_handler.py
    → check_msg_safe()  # 微信内容安全检测
    → _AMAP_TRIGGERS 命中 "怎么去"
    → build_amap_context("今天漫展怎么去？")
        → LLM 提取 intent=transit
        → query_transit(漫展, 目的地, 城市)
        → 返回 "【高德实时路线数据】..."
    → orchestrator.handle_user_interaction(enable_amap=True)

3. orchestrator（11步管线，高德场景跳过历史写入和事实沉淀）
    → dialogue_engine.process_message(enable_amap=True, search_results=amap_data)
        → build_nya_prompt_split() + AMAP_OVERRIDE_PROMPT
        → Responses API 推理
        → 返回 "[neutral] 本喵帮你查了！从..." 

4. 解析 [neutral] 前缀，TTS 异步触发（高德场景 skip_tts=True）

5. WS 推送:
{
  "type": "text",
  "msg_id": "xxx",
  "content": "本喵帮你查了！从...",
  "affinity": 42,
  "mood": "neutral",
  "voice_status": "none"
}
```

### 9.3 图生图全链路

```
客户端发送:
{
  "message": "希卡莉启动pose替身",
  "image_id": "abc-uuid-xxx"
}

1. orchestrator 主流程（步骤 1-7 正常走完）
   → 步骤 2: get_visual_description() → "画面是一位穿白色兔耳cosplay的女生"
   → 步骤 5: inference_text = "希卡莉启动pose替身 (希卡莉看到画面内容: 白色兔耳cosplay女生)"
   → 步骤 6: is_image_gen = True → skip_tts = True
   → 步骤 7: 助手确认回复落盘（"好的喵！本喵帮你做四宫格！"）

2. 步骤 9: _dispatch_image_gen_task()
   → TemporalClient.start_workflow(ImageGenWorkflow, id="imagegen-{msg_id}")
   → Temporal Worker 执行:
       analyze_coser_image() → "角色：白色兔耳，设计4个动作：展翅/蹲坐/起跳/落地"
       generate_quad_image() → Seedream → gen_url（24h临时URL）
       save_gen_image() → /app/assets/uploads/{user_id}/gen_{uuid}.jpg
       push_image_gen_result()
           → db[core_messages].insert_one({gen_image_url, gen_image_status=ready})
           → socket_manager.send_to_user(session_id, {
               type: "image_gen_result",
               msg_id: new_msg_id,
               success: True,
               gen_image_url: "/api/u/images/{user_id}/gen_{uuid}.jpg"
           })

3. 客户端收到 image_gen_result 事件，展示四宫格图片
```

### 9.4 门店签到流程

```
客户到店扫码 → 扫码页 checkin.html
    → GET /api/s/token?store_key={key}
        → _find_shop_by_key() 时序安全比较
        → 签发包含 store_id 的短期 JWT（STORE_TOKEN_EXPIRE 分钟）
    ← {token, store_id}

客户小程序扫码登录
    → POST /api/u/auth/login {js_code}（正常登录流程）

客户确认在店
    → POST /api/s/checkin {store_id}
        → redis.setex(hikari:store:instore:{user_id}, STORE_INSTORE_TTL*60, store_id)
        → db[core_users].update(store_visit_count+1, last_store_visit)
        → affinity +ORDER_AFFINITY_DELTA（到店打卡）

屏幕端 WebSocket 连接
    → WS /api/s/ws/store_screen_{store_id}
        → socket_manager.connect(f"store_screen_{store_id}", ...)
        → 接收 live2d_motion / store_event / store_broadcast 事件

对话时 Live2D 联动
    → orchestrator 步骤11: push_live2d_motion(uid, emo)
        → socket_manager.send_to_user(f"store_screen_{store_id}", {
            type: "live2d_motion",
            motion: "happy",
            expression: "Joy1",
            priority: 2
          })
```

### 9.5 多租户知识库数据隔离

每条知识条目携带 `tenant_id` 字段（默认 `hikari_default`），不同品牌方/门店的知识库物理隔离。

用户文档的 `tenant_ids[]` 字段决定检索范围，orchestrator 在 RAG 检索时并发 gather 该用户所有 tenant 的结果后合并。Qdrant 检索时通过 `filter={"user_id": tenant_id}` 实现向量空间隔离（注：Qdrant 中 KB 向量的 user_id 存的是 tenant_id）。

---

## 十、权限、安全与防护设计

### 10.1 权限矩阵

| 访问层级 | 识别方式 | 豁免项 | 限制项 |
|---|---|---|---|
| 屏幕端设备 | session_id 前缀 `store_screen_` | 所有检查（邀请制/次数/VIP） | 只能接收推送事件 |
| 在店客户 | Redis `hikari:store:instore:{uid}` | 邀请制检查 | 每日次数限制仍有效 |
| 宽限期用户 | Redis `hikari:store:visited:{uid}` | 邀请制检查 | 每日次数限制仍有效 |
| 管理角色 | `core_users.manage_role != null` | 邀请制 + VIP 锁 + 次数限制 | 按角色限店铺操作范围 |
| VIP/SVIP | `vip_level >= 1` | 每日次数限制（DAILY_MSG_LIMIT_VIP=0） | 图生图配额（非无限） |
| 普通内测用户 | `has_access=true` | - | 每日消息上限 + VIP 功能锁 |
| 未激活用户 | `has_access=false` | - | 全部拒绝（返回激活提示） |

### 10.2 鉴权策略优先级

```
X-Admin-Key（时序安全比较）
    → /api/sys/ 系统紧急操作（curl 级别，Nginx 建议 IP 白名单）
    → 失败返回 401

X-Skill-Key（时序安全比较）
    → /api/k/ 知识库管理（B端 CI/CD 自动同步）

Bearer JWT（require_auth）
    → 所有用户域、管理域接口
    → 注入 g.user_id / g.vip_level / g.role

X-Store-Key（secrets.compare_digest 时序安全）
    → /api/s/ 到店域屏幕端接口（设备鉴权）

?token=（URL 参数 JWT 降级）
    → 历史消息图片 GET 请求（Bearer 无法附带时的 fallback）
```

### 10.3 物理防护方案

**越狱指令拦截（`dialogue_engine._sanitize_injection`）**：
正则匹配 6 种越狱模式（忽略之前指令/系统管理员/开发者模式等），命中时注入 `[⚠️ 系统警告]` 前缀后继续推理（不直接拒绝，让模型处理），并在 system prompt 末尾追加 `🛡️ [物理降级：越狱防御模式激活]`。

**UUID 物理过滤**：
- 视觉场景：`get_visual_description()` 输出后 `re.sub(r'[a-f0-9-]{32,}', '')` 洗白
- 所有 LLM 回复：`re.sub(r'[a-f0-9-]{32,}', '[秘密记录]', clean)` 过滤
- 推理文本构造：`inference_text = f"{message} (希卡莉看到画面内容: {visual_desc})"` 语义中继，不传原始 UUID

**NoSQL 注入防御（`database._sanitize_input`）**：
非 str/int/float 类型直接拒绝，移除 `$` 操作符前缀，保留 `.`（JWT 三段式结构依赖）。

**路径穿越防御（`vision_helper / public_api`）**：
`os.path.realpath()` + 前缀校验，确保文件路径在允许目录内，拒绝 `%2e%2e` 等编码绕过。

**微信内容安全（`wx_security`）**：
- 文字安全：`check_msg_safe()` 调用微信 `msg_sec_check` 接口
- 图片安全：`check_image_safe()` 调用微信 `img_sec_check` 接口
- access_token Redis 缓存（提前 5 分钟过期）
- `SECURITY_FAIL_OPEN=false`：接口异常时拒绝而非放行

**并发防护**：
- TTS 防重：`_active_msg_locks` 内存字典，同一 msg_id 只执行一次
- 图生图幂等：Temporal workflow_id = `imagegen-{msg_id}`，重复提交安全
- 画像归档并发：Redis NX 锁（`hikari:lock:archival:{uid}`）
- 里程碑重复：Redis NX 锁（`hikari:milestone:last:{sid}:{threshold}`）
- 弹幕分析互斥：Redis NX 锁（`hikari:store:danmaku_lock:{store_id}`）

**限流**：
- 每日消息上限：`hikari:daily_msg:{user_id}:{date}` 计数器，北京时间次日 0:05 过期
- 工具容器内部 `_rate_limit(key, limit, window_seconds)` 函数，Redis incr + expire

---

## 十一、API 层接口字典

### 11.1 鉴权体系总览

| 装饰器/中间件 | Header | 适用域 | 失败响应 |
|---|---|---|---|
| `@require_auth` | `Authorization: Bearer {JWT}` | /api/u/ /api/m/ | 401 |
| `@require_role("admin")` | 需先 @require_auth | /api/m/ 管理操作 | 403 |
| `@require_role("store_clerk")` | 需先 @require_auth | /api/m/ 店员操作 | 403 |
| `@require_admin_key` | `X-Admin-Key: {key}` | /api/sys/ | 401 |
| `@require_skill_key` | `X-Skill-Key: {key}` | /api/k/ | 401 |
| `_require_store_key` | `X-Store-Key: {key}` | /api/s/ 设备接口 | 403 |

### 11.2 用户域（/api/u/）

| 方法 | 路径 | 描述 | 鉴权 | 底层调用 |
|---|---|---|---|---|
| POST | /api/u/auth/login | 微信登录，返回双 Token | 无 | `handle_login()` |
| POST | /api/u/auth/refresh | 刷新 Access Token | 无（Refresh Token） | `handle_refresh()` |
| POST | /api/u/auth/logout | 登出，撤销 Refresh Token | require_auth | `handle_logout()` |
| POST | /api/u/invites/activate | 激活邀请码 | require_auth | `handle_activate_invite()` |
| POST | /api/u/invites/quit | 退出内测 | require_auth | `handle_quit()` |
| GET | /api/u/vip | 查询 VIP 状态和配额 | require_auth | `get_vip_status()` |
| WS | /api/u/ws/{session_id} | 主 WebSocket 连接 | Bearer JWT（首帧携带） | `process_interaction_pipeline()` |
| GET | /api/u/messages | 历史消息分页（游标分页） | require_auth | `orchestrator.get_user_history()` |
| POST | /api/u/images/upload | 图片上传（含微信安全检测） | require_auth | `vision_helper.process_user_image()` |
| GET | /api/u/images/{uid}/{fn} | 图片文件获取 | ?token= JWT | `_serve_image_file()` |
| GET | /api/u/voices/{path} | 语音文件获取 | require_auth | Nginx 静态文件 |
| POST | /api/u/memoirs/export | 触发灵魂报告生成 | require_auth | `memoir_exporter.export()` |
| GET | /api/u/memoirs/status | 报告冷却状态查询 | require_auth | - |
| GET | /api/u/memoirs/{path} | 报告文件下载 | require_auth | send_file |
| GET | /api/u/voice/library | 可用声线列表 | require_auth | `asset_service.get_voice_library()` |
| POST | /api/u/voice/preference | 设置用户声线偏好 | require_auth | `asset_service.set_user_voice_preference()` |

**WebSocket 消息上行格式**：
```json
{
  "action": "chat",
  "message": "用户文字消息",
  "image_ids": ["已上传图片UUID1", "已上传图片UUID2"],
  "image_id": "单图兼容字段（仅传单张时可用，优先使用image_ids）",
  "client_msg_id": "客户端去重ID",
  "character": "女性1_zh",
  "enable_search": false
}
```

### 11.3 工具容器域（/api/u/ 路由至独立容器）

| 方法 | 路径 | 容器 | 描述 |
|---|---|---|---|
| POST | /api/u/images | hikari-imagegen | 图片上传（→ Nginx → 8002） |
| POST | /api/u/images/generate | hikari-imagegen | 触发图生图 Workflow |
| POST | /api/u/makeup/analyze | hikari-makeup | 触发妆容分析 Workflow |
| POST | /api/u/copywriting/round1 | hikari-copywriting | 文案第一轮（发图+触发词） |
| POST | /api/u/copywriting/round2 | hikari-copywriting | 文案第二轮（主题或参考图） |

### 11.4 知识库域（/api/k/，X-Skill-Key）

| 方法 | 路径 | 描述 |
|---|---|---|
| POST | /api/k/entries | 写入知识条目 |
| DELETE | /api/k/entries/{id} | 软删除条目 |
| GET | /api/k/entries | 条目列表（支持 tenant_id 过滤） |
| POST | /api/k/sync/feishu | 从飞书多维表格增量同步 |
| POST | /api/k/sync/file | 从上传文件批量导入 |
| POST | /api/k/push | 手动触发主动推送 |

### 11.5 系统域（/api/sys/，X-Admin-Key）

| 方法 | 路径 | 描述 |
|---|---|---|
| GET | /api/sys/health | 健康检查（无鉴权，Nginx 探活） |
| POST | /api/sys/admin/invites | 批量生成邀请码 |
| POST | /api/sys/admin/vip/grant | 直接授予 VIP |
| POST | /api/sys/admin/users/kick | 紧急踢出用户 |
| GET | /api/sys/admin/stats | 系统统计数据 |

---

## 十二、待办与演进路线

### 12.1 已知技术债

| 优先级 | 位置 | 问题描述 | 影响 |
|---|---|---|---|
| 低 | `user.py schema` | `phone` 字段已播种但功能未实现（签到时写入手机号） | 待实现门店签到绑定手机号功能，需前后端联调 |
| 低 | `gunicorn.conf.py` | hikari-api 使用 `workers=2`，多容器水平扩容时不同容器间 WebSocket miss 问题仍存在（单容器内已通过 Redis Stream 解决）。根治方案：nginx sticky session 或 Redis Stream 路由（Stream 已落地，nginx 侧需配合） | 单容器内 miss 已解决；多容器扩容时需配置 nginx ip_hash |

**已解决（收录于 §13.1）**：
- `memory_storage.save_memory()` 去重无索引 → `hikari_memories_dedup_v1` 已在 `ensure_indexes` 中建立（v8.3）
- `core_facts` 缺少复合索引 → `hikari_facts_category_v1` / `hikari_facts_confidence_v1` / `hikari_facts_active_v1` 已在 `ensure_indexes` 中建立（v8.3）
- `copywriting_workflow` 双重失败无告警 → 已写入 `core_system_alerts`（v8.3）
- Redis PubSub 多实例 miss → 已改为 Redis Stream 定向推送（v8.3）
- `dialogue_engine` store import 解耦 → 经核查已通过 `store_interaction_activity.py` 桥接层合规，无需额外改造

### 12.2 下一版本开发计划

**功能扩展**：
- 手机号签到：实现 `phone` 字段写入，门店签到与手机号绑定（需前后端联调）
- 多轮记忆归档 v2：基于 `MEMOIR_FRAGMENTS_PER_CAT` 限制的画像摘要压缩，避免历史碎片无限增长
- 弹幕功能增强：弹幕历史记录持久化，管理后台可查

**架构演进**：
- 多容器水平扩容：hikari-api 扩容至多实例时，配置 nginx `ip_hash` sticky session，配合现有 Redis Stream 定向推送实现无 miss 多实例部署
- `core_system_alerts` 告警类型扩展：目前仅有健康检查和 copywriting 失败，可扩展至 TTS 连续失败、Qdrant 写入失败等场景

**跨团队对接**：
- GPT-SoVITS 团队：确认多端口 Failover 时 WAV 文件命名规范，避免并发写入冲突
- 微信小程序前端：`client_msg_id` 去重机制联调，确保断线重连后不重复提交消息
- 飞书 MCP 团队：`lark-openapi-mcp` SSE 模式下 `call-tool` 接口超时参数对齐（当前 `feishu_helper` 硬编码 30s）

---

## 十三、已知问题与运维复盘

### 13.1 Bug 墓碑区

**Bug10（已修复）**  
- 严重程度：中等（影响好感度显示错误）
- 问题文件：`orchestrator.py`
- 根本原因：`get_user_history()` 中读取用户情绪时使用了旧字段名 `current_mood`，但 `affinity_service` 写入的字段名为 `mood`（v3.0 重构后字段名变更）
- 修复手段：`user_doc.get("current_mood")` → `user_doc.get("mood")`
- 存量数据：无需清洗，`current_mood` 字段在用户文档中从未写入，原代码一直返回 `None` 降级为 `"neutral"`，修复后正常读取 `mood` 字段值

**Bug：核心数据库集合名字符串字面量散落（已修复）**
- 严重程度：高（重构期间多个文件使用不同字符串引用同一集合，导致部分查询打到错误集合）
- 根本原因：v2.x 时代集合名直接写字符串（`db["users"]`），无统一常量管理
- 修复手段：在 `database.py` 中统一定义 `COL_*` 常量，全库替换字符串字面量
- 存量数据：v3.0 迁移时旧集合名（`users`/`messages`/`memories`/`facts`）中数据通过 `$or` 三路查询兼容（见 `orchestrator.get_user_history()`），新数据写入新集合名（`core_users` 等）

**Bug：Responses API Session 缓存失效崩溃（已修复）**
- 严重程度：中等（部分用户对话请求 500）
- 问题文件：`dialogue_engine.py`
- 根本原因：`previous_response_id` 过期或非法时，Ark 返回错误而非降级，代码未捕获
- 修复手段：在 except 中检测 `"previous_response_id"` 关键字，清除 ID 并以无缓存模式重试
- 存量数据：无

**Bug：SocketManager 多进程 PubSub Miss**
- 严重程度：低（偶发，用户感受为消息延迟）
- 问题文件：`gunicorn.conf.py`（workers=2）+ `socket_manager.py`
- 根本原因：Gunicorn fork 出 2 个 Worker 进程，每个进程维护独立的 `active_queues`，PubSub 消息随机进入一个 Worker，若 WebSocket 连接在另一个 Worker，消息被 Bus-Miss
- 当前缓解：`preload_app=True` 已启用（节省内存）；`pending` 消息 Redis 持久化 10 分钟，用户重连后补发
- 根治方案（Roadmap）：迁移到单进程 uvicorn，或切换 sticky session

**Bug：历史图片懒加载计数器位置错误（v8.2 修复）**
- 严重程度：低（逻辑偏差，非崩溃）
- 问题文件：`dialogue_engine.py`（历史重建循环）
- 根本原因：`_img_restored += 1` 放在 `if hist_img_b64:` 成功分支内，图片读取失败时名额不消耗，后续带图历史仍会被尝试还原，不符合"只尝试最近 1 条"的意图
- 修复手段：将 `_img_restored += 1` 移至读取尝试前（进入 `_img_restored < 1` 分支即计数），读取失败时 fallback 到纯文本
- 存量数据：无

**Bug：AutoDream `create_task` 在 Temporal Activity 上下文不安全（v8.2 修复）**
- 严重程度：中等（向量化落盘静默失败）
- 问题文件：`fact_extractor.py`（`consolidate_facts` 的 Prune 阶段）
- 根本原因：`asyncio.create_task()` 抛出的任务在 Temporal Activity 函数返回后可能被 Temporal Worker 的清理机制中断，导致 `_vectorize_and_store_v3()` 静默不执行
- 修复手段：改为 `await self._vectorize_and_store_v3(...)` 直接等待完成
- 存量数据：无

---

以下为 `可能存在的问题.md` 完整排查清单处理结果（v8.2 统一收录）：

**#1【高】`res["base64_data"]` KeyError（已修复）**
- 问题文件：`modules/users/api.py`
- 根本原因：图片接口将 image_gen_service 返回值的字段名写错，生产被 nginx 拦截时不触发，本地开发必崩
- 修复手段：`res["base64_data"]` → `res["base64"]`，与 image_gen_service 实际返回字段一致
- 存量数据：无

**#4【中】copywriting Round1 HTTP 转发超时无感知（已修复）**
- 问题文件：`modules/users/api.py`（`_forward_tool` 辅助函数）
- 根本原因：HTTP 转发超时 10s 后抛出异常，主容器 try/except 吞掉错误，用户无任何提示
- 修复手段：`_forward_tool` 改为返回 `bool`，失败时通过 socket 向用户推送错误气泡提示
- 存量数据：无

**#6【中】`user_pref` 写入字段路径错误（已修复）**
- 问题文件：`core/fact_extractor.py`
- 根本原因：`else` 分支（非 preference 类别）先执行，`user_pref` 变量被误写到 `metadata.user_pref` 路径，而非 `preferences` 字段
- 修复手段：统一写入 `preferences` 字段，移除 `metadata.user_pref` 路径引用
- 存量数据：存量 `metadata.user_pref` 数据不影响正确读取，无需清洗

**#7【中】`affinity_levels` 中高好感度档永远不可达（已修复）**
- 问题文件：`core/soul_config.py`
- 根本原因：原配置 `stranger/acquaintance/friend/close/soulmate` 五档，上限为 100，`close`/`soulmate` 门槛超过 100 分，数学上不可达
- 修复手段：改为三档配置 `stranger(0-30) / friend(31-70) / soulmate(71+)`，门槛值均在 0-100 区间内
- 存量数据：存量用户 `affinity_score` 数值不变，下次调用 `get_affinity_level()` 自动映射到新档位

**#9【中】orchestrator 图生图路由——重新评估为正常代码**
- 问题文件：`core/orchestrator.py`
- 说明：`_dispatch_image_gen_task` 在 image_gen 微服务容器启动时正常路由，判断为死代码的前提（容器未启动）属于部署状态，非代码缺陷；保留现有逻辑，无需修改

**#10【中】`current_mood` → `mood` 字段名（已修复，收录于现有墓碑）**
- 详见上方 Bug10 条目

**#11【中】`get_store_token` 多店二维码 Key 不区分店铺（已修复）**
- 问题文件：`modules/store/api.py`
- 根本原因：全局 Redis Key 固定为 `hikari:store_token`，多店场景下后者覆盖前者，二维码显示错乱
- 修复手段：Key 改为 `hikari:store_token:{store_id}`，按店铺隔离缓存
- 存量数据：旧 Key `hikari:store_token` TTL 自然过期（30min），无需手动清理

**#12【低】`soul_config` 故障时未缓存降级值——已知问题，暂不修复**
- 问题文件：`core/soul_config.py`
- 说明：故障时每请求均需等待超时，高并发影响延迟。修复需引入降级值持久化逻辑，复杂度较高，当前业务量下影响可接受，列入 Roadmap

**#13【低】进程内锁不跨 Worker，事实重复提取（已修复）**
- 问题文件：`core/fact_extractor.py`
- 根本原因：`asyncio.Lock()` 仅在单进程内有效，Gunicorn 多 Worker 时锁失效，同一用户消息可能被多个 Worker 并发提取事实
- 修复手段：改用 Redis 分布式锁 `redis.lock(key, timeout=30, blocking_timeout=5)`，获取失败时降级跳过（非阻塞）
- 存量数据：无

**#14【高】Qdrant point ID 使用 MongoDB ObjectId 字符串，格式非法（已修复）**
- 问题文件：`services/rag_service.py`
- 根本原因：Qdrant point ID 只接受 unsigned int64 或 UUID 格式，直接传 ObjectId 字符串（24位十六进制）导致全部写入失败
- 修复手段：新增 `_fact_id_to_int()` 函数，将 ObjectId 字符串通过 `int(fact_id[:16], 16)` 转换为合法的 uint64
- 存量数据：历史向量 ID 格式非法，Qdrant 中实际无数据，修复后新写入正常；历史 MongoDB 事实记录无需清洗

**#15【低】`sync_file` 先全量读文件再校验大小（已修复）**
- 问题文件：`modules/knowledge/api.py`
- 根本原因：文件大小校验逻辑在 `await file.read()` 之后执行，超大文件已全量载入内存再拒绝，浪费内存
- 修复手段：改为先读 `file.size`（或 seek 到末尾获取大小），校验通过后再执行读取
- 存量数据：无

**#16【低】`_check_store_access` 死代码（已删除）**
- 问题文件：`modules/manage/api.py`
- 根本原因：函数依赖 `g.manage_store_id`，但该属性从未在任何中间件中注入，函数永远无法正确执行
- 修复手段：删除整个 `_check_store_access` 函数及其调用点，调用方直接使用 `g.store_id`
- 存量数据：无

**#17【高】push 在线检查永不过滤（已修复）**
- 问题文件：`workflows/push_workflow.py`
- 根本原因：`get_session_id_for_user()` 在用户不在线时仍返回非空字符串（返回 session prefix），导致在线检查逻辑永远通过，全量推送无论用户是否在线
- 修复手段：改用 `is_user_online(user_id)` 明确返回 `bool`，离线用户跳过推送
- 存量数据：无

**#18【中】`mood="friendly"` 不在合法值范围（已修复）**
- 问题文件：`workflows/push_workflow.py`
- 根本原因：push_workflow 硬编码 `mood="friendly"`，但合法值为 `neutral/happy/sad/excited/angry`，该值落入 else 分支触发默认降级
- 修复手段：`mood="friendly"` → `mood="happy"`
- 存量数据：无

**#19【中】`qdrant.upsert` 和 `qdrant.set_payload` 同步调用阻塞事件循环（已修复）**
- 问题文件：`services/rag_service.py`
- 根本原因：qdrant-client 同步方法在 async 函数中直接调用，阻塞整个事件循环，高并发时拖慢所有协程
- 修复手段：所有同步 Qdrant 调用改为 `await asyncio.to_thread(qc.upsert, ...)` 和 `await asyncio.to_thread(qc.set_payload, ...)`
- 存量数据：无

**#20【低】`get_user_vectors` 每次全量拉取 500 条向量（已修复）**
- 问题文件：`utils/qdrant_client.py`
- 根本原因：`get_user_vectors(with_vectors=True)` 默认拉取 500 条含向量数据，调用方通常只需 ID，产生大量无用传输
- 修复手段：将 `with_vectors` 默认值改为 `False`，需要向量数据的调用方显式传 `with_vectors=True`
- 存量数据：无

**#21【中】`VoiceGenWorkflow` 注册但从未被提交——重新评估为设计预留**
- 问题文件：`workflows/voice_gen_workflow.py`
- 说明：TTS 通过直接 HTTP 调用宿主机 GPT-SoVITS 实现（低延迟场景），`VoiceGenWorkflow` 为异步批量 TTS 预留（如离线语音合成），当前未启用属于有意设计；保留现有注册，无需修改

**#22【低】自定义 `persona_template` 时联网字数限制解除静默失效（已修复）**
- 问题文件：`core/dialogue_engine.py`
- 根本原因：`enable_search` 时联网字数上限为 300 字，自定义 `persona_template` 覆盖系统提示时该限制被替换掉，搜索结果注入超长无字数限制
- 修复手段：注入联网内容时先尝试 `str.replace()` 替换占位符，失败时 fallback 到末尾 append 并添加字数限制说明
- 存量数据：无

**#23【低】`users/api.py` 图片接口在生产被 nginx 拦截——已知架构限制**
- 问题文件：`modules/users/api.py`
- 说明：Nginx 配置中 `/api/u/image/*` 路由被代理到独立 image 服务，Flask 层该路由实际不可达（生产死代码）。本地开发正常可用，不影响功能，列入架构备忘

**#24【中】`core_facts` 字段名混用，索引建在 `importance` 上无效（已修复）**
- 问题文件：`deployment/init-mongo.js` + `core/fact_extractor.py`
- 根本原因：MongoDB 中 `core_facts` 集合实际写入字段为 `confidence`，但 `init-mongo.js` 中索引建在不存在的 `importance` 字段上，索引完全无效
- 修复手段：`init-mongo.js` 中将索引字段 `importance: -1` 改为 `confidence: -1`
- 存量数据：索引变更需重建，首次部署后执行 `db.core_facts.dropIndex("...")` 并重启触发 `ensure_indexes()` 重建；存量数据字段无需清洗

**#25【低】`_push_makeup_reject` 直接 import `socket_manager` 违反规范（已修复）**
- 问题文件：`modules/tools/makeup/handler.py`
- 根本原因：模块直接 `from src.utils.socket_manager import socket_manager` 而非通过统一的 `push_chat_bubble()` 抽象调用，违反项目「工具模块不直接操作 socket」规范
- 修复手段：改为调用 `push_chat_bubble(user_id, text)` 统一推送接口
- 存量数据：无

**Redis PubSub → Stream 定向推送（v8.3 重构）**
- 严重程度：中等（单容器多 Worker 时 WebSocket miss）
- 问题文件：`utils/socket_manager.py` + `main.py` + `worker_main.py`
- 根本原因：PubSub 广播到所有实例，但只有持有 WebSocket 的实例能处理，其余实例 Bus-Miss
- 修复手段：
  - `connect()` 写 `hikari:ws_owner:{session_id}` → `instance_id` 路由表
  - `send_to_user()` 查 owner 后定向 `xadd` 到 `hikari:ws_stream:{instance_id}`
  - `start_bus_listener()` → `start_stream_consumer()`，`XREAD BLOCK` 消费，处理后 `XDEL`
  - `worker_main.py` 删除 bus_listener 启动（worker 不持有 WebSocket）
- 存量数据：无

**`database.py ensure_indexes()` 补全 core_facts 复合索引 + core_memories 去重索引（v8.3）**
- 严重程度：低（性能问题，非功能缺陷）
- 问题文件：`utils/database.py`
- 根本原因：`core_facts` 仅有单字段 `user_id` 索引，画像聚合查询无索引覆盖；`core_memories` 缺去重联合索引，`save_memory()` 每次全表扫描
- 修复手段：`ensure_indexes()` 中新增 `hikari_facts_category_v1`、`hikari_facts_confidence_v1`、`hikari_facts_active_v1`、`hikari_memories_dedup_v1`
- 存量数据：索引后台建立，不影响读写

**`copywriting_workflow` 双重失败写告警（v8.3）**
- 严重程度：低（可观测性缺失）
- 问题文件：`workflows/copywriting_workflow.py`
- 根本原因：`generate_copywriting` 带参考图和去参考图双重重试均失败时，仅打 error 日志，无告警写入，health_workflow 无法感知
- 修复手段：在最终 `except` 块写入 `core_system_alerts`（type="copywriting_failure"），与健康检查告警共用同一集合
- 存量数据：无

**`rag_service.deactivate()` Qdrant soft-delete 无效（v8.4 修复）**
- 严重程度：高（软删除对向量库无效，知识条目停用后仍被检索命中）
- 问题文件：`services/rag_service.py`
- 根本原因：`deactivate()` 调用 `qdrant.set_payload(points=[entry_id])` 传入 MongoDB `_id` 字符串，但 Qdrant 中存储的 point ID 是经 `_fact_id_to_int()` 转换的 uint64，类型不匹配导致 `set_payload` 找不到任何点，`is_active=False` 从未写入 Qdrant
- 修复手段：改为 `points=[_fact_id_to_int(entry_id)]`，与写入时 ID 格式一致
- 存量数据：历史已停用条目的 Qdrant payload 未更新，Qdrant 中 `is_active` 仍为 True；MongoDB 侧 `is_active=False` 已正确。修复后新停用操作正常；如需补偿历史数据，可遍历 `core_kb` 中 `is_active=False` 的条目重跑一次 `deactivate()`

**`chat_handler` 图生图配额无条件扣减（v8.4 修复）**
- 严重程度：中（`VIP_LOCK_IMAGE_GEN=false` 时非 VIP 用户每次触发图生图均被扣配额，而此时根本没有配额体系）
- 问题文件：`modules/users/chat_handler.py`
- 根本原因：`decrement_image_gen_quota()` 在进入图生图分支后无条件执行，未判断功能锁开关。`VIP_LOCK_IMAGE_GEN=true` 时行为正确；`VIP_LOCK_IMAGE_GEN=false` 时非 VIP 用户 `image_gen_quota` 字段被 `$inc -1` 至负数
- 修复手段：扣配额改为 `if VIP_LOCK_IMAGE_GEN and vip_info["is_vip"]:` 条件下执行
- 存量数据：若生产环境 `VIP_LOCK_IMAGE_GEN=false` 且有用户触发过图生图，其 `image_gen_quota` 字段可能为负值；可执行 `db.core_users.updateMany({image_gen_quota:{$lt:0}},{$set:{image_gen_quota:0}})` 修复

### 13.2 代码质量备忘

- `fact_extractor.py`：`audit_buffer` 存储在 `core_sessions` 文档中，该字段随 session 文档频繁更新，理论上应独立存储（如 Redis List），但目前业务量下无明显性能问题
- `amap_service.py`：`_build_amap_context_regex` 正则降级方案较为简单，城市列表硬编码 42 个，海外用户或小城市用户可能无法正确提取城市
- `copywriting_service.py`：网格图拼接使用 PIL，多图拼接时内存占用与图片数量线性增长，建议限制单次上传图片数量
- `soul_config.py`：60s 内存缓存在多 Worker 场景下，SOUL 配置更新后最多需要 60 × Worker 数秒才能全部生效（每个 Worker 独立缓存）
- `nekos_service.py`：NekosBest 为第三方公开 API，无 SLA 保障，目前任何失败静默降级（不发 GIF），建议增加可用性监控

### 13.3 运维避坑指南

**时区问题**：
- 容器全部设置 `TZ=Asia/Shanghai`，但 MongoDB 存储时间统一为 UTC（Python `datetime.now(timezone.utc)`）
- `hikari:daily_msg:{user_id}:{date}` 的 date 是北京时间日期（代码中计算 `now_bj = now + timedelta(hours=8)`），TTL 过期时间也以北京时间 0:05 为准，需要注意跨天边界

**Temporal 假死**：
- Temporal 不可用时，新任务通过 `asyncio.create_task` 降级执行，但已提交到 Temporal 的历史任务会积压在 Temporal queue 中，Temporal 恢复后会批量重放——注意 Activity 幂等性
- `health_workflow` 的 `check_system_health` 不检测 Temporal 自身（自引用死锁），需独立监控

**Qdrant 冷启动**：
- Qdrant 首次启动时 `hikari_memories` 集合不存在，`main.py` 的 `startup` 钩子调用 `ensure_collection()` 自动创建，但需要 Qdrant 已 healthy 才能启动 api 容器（`depends_on: service_healthy`）
- Qdrant 版本锁定 v1.9.0，勿随意升级（qdrant-client SDK 版本约束 `>=1.9.0,<1.11.0`）

**ES IK 分词插件**：
- `Dockerfile.es` 中已内置 IK 分词插件，使用自定义镜像 `hikari-es-ik:8.13.0`，首次需本地 build
- ES 内存配置：`ES_JAVA_OPTS="-Xms512m -Xmx512m"`，内存不足时 GC 压力大，建议不低于 1GB

**TTS 宿主机依赖**：
- GPT-SoVITS 运行在 Windows 宿主机（非容器），通过 `host.docker.internal` 访问
- 宿主机重启后 TTS 需手动启动，`health_workflow` 的 `check_system_health` 会检测 TTS 端口，结果写入 `core_system_alerts`，可通过 `/api/m/system/alerts` 查询

**MongoDB 认证**：
- `init-mongo.js` 在首次启动时以 `initdb` 用户执行，需要 `MONGO_INITDB_ROOT_USERNAME/PASSWORD` 环境变量正确配置
- 后续连接使用 `MONGO_URI` 中的 `hikari_admin` 用户（`authSource=admin`），两个用户的密码可以不同

**Redis 内存上限**：
- Redis 无配置 `maxmemory`，建议在生产环境添加 `--maxmemory 100mb --maxmemory-policy allkeys-lru`，防止 pending 消息 List 无限增长

