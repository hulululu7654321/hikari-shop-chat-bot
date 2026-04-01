/**
 * 希卡莉 (Hikari) - MongoDB 索引初始化脚本
 * 基于：重构参考文档 MASTER v3.0 第四节（MongoDB 集合名常量）
 *
 * 集合名对照（COL_* 常量 → 实际集合名）：
 *   COL_USERS        = "core_users"
 *   COL_SESSIONS     = "core_sessions"
 *   COL_MESSAGES     = "core_messages"
 *   COL_MEMORIES     = "core_memories"
 *   COL_FACTS        = "core_facts"
 *   COL_KB           = "core_kb"
 *   COL_CONFIG       = "core_config"
 *   COL_ALERTS       = "core_system_alerts"
 *   COL_PACKAGES     = "shop_packages"
 *   COL_ORDERS       = "store_orders"
 *   COL_INVITE_CODES = "auth_invite_codes"
 *
 * 已废弃：core_vector_memory（向量数据迁移至 Qdrant，不再创建索引）
 * 幂等：重复执行安全，已存在的索引不会报错
 */

var dbName = 'hikari';
db = db.getSiblingDB(dbName);

print(">>> [INIT] 开始部署希卡莉 MongoDB 索引...");

// --- 辅助：安全删除指定集合的所有 TEXT 索引（保证幂等重建）---
function dropTextIndexes(collectionName) {
    var col = db.getCollection(collectionName);
    col.getIndexes().forEach(function(idx) {
        var isText = Object.values(idx.key).some(function(v) { return v === "text"; });
        if (isText) {
            try { col.dropIndex(idx.name); } catch(e) {}
        }
    });
}

// ======================================================================
// core_users — 用户主表
// ======================================================================
print(">>> core_users...");

// 主键：user_id（格式 wx_{unionid}）
db.core_users.createIndex(
    { "user_id": 1 },
    { unique: true, name: "idx_user_id_unique" }
);

// VIP 查询（管理后台列表筛选）
db.core_users.createIndex(
    { "vip_level": 1, "created_at": -1 },
    { name: "idx_vip_level_created" }
);

// 好感度里程碑查询
db.core_users.createIndex(
    { "affinity": 1 },
    { name: "idx_affinity" }
);

// push_worker 读取最近活跃用户
db.core_users.createIndex(
    { "last_active_at": -1 },
    { name: "idx_last_active" }
);

// manage_api 全文搜索用户名
dropTextIndexes('core_users');
db.core_users.createIndex(
    { "user_name": "text" },
    { name: "idx_user_name_text" }
);

// ======================================================================
// core_sessions — 会话表
// ======================================================================
print(">>> core_sessions...");

// session_id 唯一索引（session_manager 查询主键）
db.core_sessions.createIndex(
    { "session_id": 1 },
    { unique: true, name: "idx_session_id_unique" }
);

// user_id 查询（login 时找已有 session）
db.core_sessions.createIndex(
    { "user_id": 1 },
    { name: "idx_session_user_id" }
);

// TTL：7天无活动自动清理
db.core_sessions.createIndex(
    { "updated_at": 1 },
    { expireAfterSeconds: 604800, name: "idx_session_ttl" }
);

// ======================================================================
// core_messages — 消息表
// ======================================================================
print(">>> core_messages...");

// 主查询：按 user_id 翻页（get_user_history 三路 $or 兜底）
db.core_messages.createIndex(
    { "user_id": 1, "created_at": -1 },
    { name: "idx_msg_user_timeline" }
);

// session_id 维度（WS 会话内查询）
db.core_messages.createIndex(
    { "session_id": 1, "created_at": -1 },
    { name: "idx_msg_session_timeline" }
);

// voice_status 轮询（前端喇叭按钮）
db.core_messages.createIndex(
    { "user_id": 1, "voice_status": 1 },
    { name: "idx_msg_voice_status" }
);

// image_id 查找（get_user_history 图片打捞）
db.core_messages.createIndex(
    { "image_id": 1 },
    { sparse: true, name: "idx_msg_image_id" }
);

// ======================================================================
// core_memories — 对话记忆片段（2-Gram 关键词检索）
// ======================================================================
print(">>> core_memories...");

db.core_memories.createIndex(
    { "user_id": 1, "created_at": -1 },
    { name: "idx_memory_user_timeline" }
);

// 内容去重查询（save_memory 幂等写入）
// 注：此处补充联合索引，解决 v11 代码质量问题 Q1（全表扫描）
db.core_memories.createIndex(
    { "user_id": 1, "role": 1, "content": 1 },
    { name: "idx_memory_dedup" }
);

// 全文检索（memory_storage.search_memories 2-Gram 关键词路径）
dropTextIndexes('core_memories');
db.core_memories.createIndex(
    { "user_id": 1, "content": "text" },
    { weights: { content: 1 }, name: "idx_memory_content_text" }
);

// ======================================================================
// core_facts — 用户画像事实（fact_extractor，向量检索由 Qdrant 接管）
// ======================================================================
print(">>> core_facts...");

// 主查询：按 user_id + category
db.core_facts.createIndex(
    { "user_id": 1, "category": 1 },
    { name: "idx_fact_user_category" }
);

// 置信度排序（memoir_exporter 聚合）
db.core_facts.createIndex(
    { "user_id": 1, "confidence": -1, "updated_at": -1 },
    { name: "idx_fact_confidence" }
);

// is_active 过滤（只读取有效事实）
db.core_facts.createIndex(
    { "user_id": 1, "is_active": 1 },
    { name: "idx_fact_active" }
);

// 全文检索（retrieve_relevant_facts 关键词路径兜底）
dropTextIndexes('core_facts');
db.core_facts.createIndex(
    { "user_id": 1, "content": "text" },
    { name: "idx_fact_content_text" }
);

// ======================================================================
// core_kb — B端知识库（rag_service / knowledge_api）
// ======================================================================
print(">>> core_kb...");

db.core_kb.createIndex(
    { "tenant_id": 1, "created_at": -1 },
    { name: "idx_kb_tenant_timeline" }
);

// 全文检索（RAG 关键词路径）
dropTextIndexes('core_kb');
db.core_kb.createIndex(
    { "tenant_id": 1, "content": "text", "title": "text" },
    { weights: { title: 10, content: 5 }, name: "idx_kb_content_text" }
);

// ======================================================================
// core_config — 系统配置（soul_config.py SOUL 热配置）
// ======================================================================
print(">>> core_config...");

db.core_config.createIndex(
    { "key": 1 },
    { unique: true, name: "idx_config_key_unique" }
);

// ======================================================================
// core_system_alerts — 系统健康告警（health_workflow 写入）
// ======================================================================
print(">>> core_system_alerts...");

db.core_system_alerts.createIndex(
    { "created_at": -1 },
    { name: "idx_alert_timeline" }
);

// TTL：保留 30 天
db.core_system_alerts.createIndex(
    { "created_at": 1 },
    { expireAfterSeconds: 2592000, name: "idx_alert_ttl" }
);

// ======================================================================
// shop_packages — 公开商城套餐（manage_api 管理 + public api 展示）
// ======================================================================
print(">>> shop_packages...");

db.shop_packages.createIndex(
    { "is_active": 1, "sort_order": 1 },
    { name: "idx_package_active_sort" }
);

// ======================================================================
// store_orders — 到店订单
// ======================================================================
print(">>> store_orders...");

db.store_orders.createIndex(
    { "user_id": 1, "created_at": -1 },
    { name: "idx_order_user_timeline" }
);

db.store_orders.createIndex(
    { "store_id": 1, "created_at": -1 },
    { name: "idx_order_store_timeline" }
);

// manage_api 订单列表（店员视图）
db.store_orders.createIndex(
    { "store_id": 1, "status": 1, "created_at": -1 },
    { name: "idx_order_store_status" }
);

// ======================================================================
// auth_invite_codes — 邀请码
// ======================================================================
print(">>> auth_invite_codes...");

db.auth_invite_codes.createIndex(
    { "code": 1 },
    { unique: true, name: "idx_invite_code_unique" }
);

db.auth_invite_codes.createIndex(
    { "used_by": 1 },
    { sparse: true, name: "idx_invite_used_by" }
);

// TTL：邀请码过期自动清理（INVITE_CODE_EXPIRE_DAYS=7，以秒计）
db.auth_invite_codes.createIndex(
    { "expires_at": 1 },
    { expireAfterSeconds: 0, name: "idx_invite_expires_ttl" }
);

print(">>> [SUCCESS] 希卡莉 MongoDB 索引部署完成。");
