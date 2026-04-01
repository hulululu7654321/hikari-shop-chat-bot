# 希卡莉 Bot 前端开发文档 v1.3
> 本文档描述截至 2026-04-01 的微信小程序前端完整架构、文件职责、接口对齐方式与状态机说明。
> v1.0：首次建立前端文档体系，覆盖全量页面逻辑、全局状态管理、WebSocket 协议、接口调用规范与已知问题记录。
> v1.1：新增公开商城页面（pages/shop/）、VIP 图生图配额 UI（imageGenQuota 字段 + quota_exceeded 错误处理）、前端频闪根本原因修复（loadHistory 多处 setData 优化）、invite.js 激活后 WS 建立修复。
> v1.2：安全修复（JWT token 移出图片 URL）、refresh token 无限循环修复（isRetry 标记）、onShow needsRefresh 读取时序修复、goShop 页面栈修复（redirectTo）、admin/index.js + admin/store.js 数组格式兼容、image_id/image_ids 互斥发送修复、死代码清理（D1–D6）。
> v1.3：hasAccess 持久化修复（重启/重编译后邀请态丢失）。
> 用于新对话上下文恢复，与后端开发文档配套使用。

---

## 一、项目概述

希卡莉小程序前端基于微信原生小程序框架（JS/WXML/WXSS）开发，是用户与希卡莉 AI 交互的主要入口。整体定位：

- **C 端**：聊天界面 + 语音播放 + 图生图 + 灵魂画像报告
- **B 端**：到店签到 + 店员/管理员后台（套餐下单、用户管理、角色授权）
- **邀请制**：内测期间通过邀请码控制访问权限，支持 VIP 功能分级

**版本历史**
- v7.x：基础聊天功能（文字/图片/语音/灵魂画像）
- v9.0 新增：邀请制访问控制、VIP 状态展示、error 类型 WS 消息处理
- v9.1 新增：管理员小程序页面（manage 目录）、到店宽限期 UI、manageRole 权限入口
- v1.1 新增：公开商城页面（pages/shop/）、VIP 图生图配额展示与 quota_exceeded 处理、前端高频 setData 频闪修复、invite.js 激活后 WS 建立修复

**技术栈**
- 框架：微信原生小程序（无第三方 UI 库）
- 语言：JavaScript（ES6+）/ WXML / WXSS
- 通信：`wx.request`（HTTP）+ `wx.connectSocket`（WebSocket，通过 `app.js` 统一管理）
- 存储：`wx.getStorageSync` / `wx.setStorageSync`（token、uid、currentVoice、current_report_md）

---

## 二、目录结构

```
miniprogram/
├── app.js                          # 全局入口：鉴权、WebSocket、全局数据
├── app.json                        # 页面注册、窗口配置
├── app.wxss                        # 全局样式
├── assets/
│   └── images/
│       ├── hikari.png              # 希卡莉头像（assistant 气泡左侧）
│       └── user.png                # 用户头像（user 气泡右侧）
└── pages/
    ├── index/                      # 启动页（身份验证 + 路由分发）
    │   ├── index.js
    │   ├── index.wxml
    │   └── index.wxss
    ├── chat/                       # 主聊天页（核心页面）
    │   ├── chat.js
    │   ├── chat.wxml
    │   ├── chat.wxss
    │   └── chat.json
    ├── checkin/                    # 到店签到页（扫码跳转）
    │   ├── checkin.js
    │   ├── checkin.wxml
    │   ├── checkin.wxss
    │   └── checkin.json
    ├── invite/                     # 邀请码激活页
    │   ├── invite.js
    │   ├── invite.wxml
    │   ├── invite.wxss
    │   └── invite.json
    ├── report/                     # 灵魂画像报告渲染页
    │   ├── report.js
    │   ├── report.wxml
    │   ├── report.wxss
    │   └── report.json
    ├── shop/                       # ★ v1.1 新增：公开商城页（无需登录）
    │   ├── shop.js                 # 套餐列表加载、分类 Tab 本地过滤、详情浮层、联系管理员
    │   ├── shop.wxml
    │   ├── shop.wxss
    │   └── shop.json               # enablePullDownRefresh=true
    └── manage/                     # 管理后台（v9.1 新增）
        ├── api.js                  # manage 公共请求工具（自动鉴权 + 401/403 处理）
        ├── manage.js               # 管理入口（whoami → 角色路由）
        ├── manage.wxml
        ├── manage.wxss
        ├── manage.json
        ├── admin/                  # 管理员专属页面
        │   ├── index.js            # 管理员主界面（统计概览 + 功能菜单）
        │   ├── index.wxml
        │   ├── index.json
        │   ├── index.wxss
        │   ├── users.js            # 用户管理（搜索 / 授VIP / 踢人）
        │   ├── users.wxml
        │   ├── users.json
        │   ├── users.wxss
        │   ├── invites.js          # 邀请码管理（生成 / 查看 / 作废）
        │   ├── invites.wxml
        │   ├── invites.json
        │   ├── invites.wxss
        │   ├── packages.js         # 套餐管理（新增 / 编辑 / 上下架）
        │   ├── packages.wxml
        │   ├── packages.json
        │   ├── packages.wxss
        │   ├── orders.js           # 订单记录（按日期 / 店铺过滤）
        │   ├── orders.wxml
        │   ├── orders.json
        │   ├── orders.wxss
        │   ├── roles.js            # 角色授权（admin / store_clerk）
        │   ├── roles.wxml
        │   ├── roles.json
        │   ├── roles.wxss
        │   ├── store.js            # 店铺管理（屏幕链接 / 重置短码）
        │   ├── store.wxml
        │   ├── store.json
        │   └── store.wxss
        └── store/                  # 店员专属页面
            ├── index.js            # 店员主界面（在店客户 / 下单 / 今日订单）
            ├── index.wxml
            ├── index.json
            └── index.wxss
```

---

## 三、全局状态管理（app.js）

### 3.1 globalData 字段说明

| 字段 | 类型 | 来源 | 说明 |
|------|------|------|------|
| `BASE_URL` | string | `_getBaseUrl()` | API 根地址，开发版走局域网，正式版走公网 |
| `API_PREFIX` | string | 硬编码 `/api/mp` | 小程序接口前缀 |
| `token` | string\|null | Storage / silentLogin | JWT Bearer Token，持久化到 Storage |
| `sessionId` | string\|null | silentLogin | 会话 ID，用于 WS 连接 URL |
| `userInfo` | object\|null | silentLogin | 登录返回的完整用户信息对象 |
| `hasAccess` | bool | silentLogin | 用户是否有访问权限（邀请制） |
| `inviteOnly` | bool | silentLogin | 系统是否开启邀请制，默认 false（热启动兜底） |
| `vipLevel` | number | silentLogin | VIP 等级，0=普通用户 |
| `isVip` | bool | silentLogin | `vipLevel > 0` 的语法糖 |
| `msgRemaining` | number | silentLogin / syncVipStatus | 今日剩余消息次数，-1=不限 |
| `dailyLimit` | number | silentLogin / syncVipStatus | 今日消息上限 |
| `imageGenQuota` | number | chat.js syncVipStatus | ★ v1.1 图生图剩余配额；-1=不受限（VIP_LOCK_IMAGE_GEN=false）；>=0=剩余次数 |
| `manageRole` | string\|null | silentLogin / manage.js whoami | 管理角色：null / "admin" / "store_clerk" |
| `manageStoreId` | string\|null | manage.js whoami | 店员绑定的店铺 ID |
| `manageStoreName` | string | manage.js whoami | 店铺显示名称 |
| `visitedExpiresAt` | number\|null | silentLogin | 宽限期到期 UTC 时间戳（秒），null=无宽限期 |
| `socketStatus` | string | connectWebSocket | WS 连接状态：`closed` / `connecting` / `connected` |
| `chatContext` | object\|null | chat.js onLoad/onShow | 当前 chat 页实例引用，供 WS 消息回调使用 |
| `_authCallbacks` | array | onAuthReady | 等待登录完成的回调队列 |

### 3.2 核心方法

**`_getBaseUrl()`**

根据小程序运行环境自动切换 API 地址：
- `develop`（开发/体验版）→ 局域网地址（如 `http://192.168.31.150`）
- 其他 → 生产域名

**`onAuthReady(cb)`**

统一的登录就绪回调注册入口。

```
若 token + userInfo 均已存在 → setTimeout(0) 异步执行 cb（统一异步，消灭双触发）
若未就绪 → 将 cb 推入 _authCallbacks 队列，等 silentLogin 完成后批量执行
```

所有页面只需调用一次 `app.onAuthReady(cb)`，不需要额外的 `if token` 兜底分支。

**`silentLogin()`**

完整登录流程：

```
1. wx.login() 获取临时 code
2. POST /api/mp/login → 返回 token / session_id / userInfo / 邀请制状态 / VIP 状态
3. 写入所有 globalData 字段
4. 若 invite_only=true 且 has_access=false → 执行 _authCallbacks（不建立 WS）
5. 正常流程 → connectWebSocket() → 执行 _authCallbacks
```

**`connectWebSocket()`**

防重入守卫：`socketStatus === 'connected'` 时直接返回。

WS URL 格式：`ws(s)://{BASE_URL}/api/mp/ws/{session_id}?token={token}`

心跳：每 25 秒发送 `{ action: "ping" }`，收到 `{ action: "pong" }` 静默处理。

断线重连：`onClose` 后 5 秒自动重连。`onClose` 触发时若检测到 `needsRefresh`（`isGenerating=true` 或 `isMemoirProcessing=true` 或有 `voice_status=loading` 的消息）则 500ms 后调 `chatContext.loadHistory(true)` 补漏消息；否则跳过，避免用户预览图片/查看报告返回时因 WS 短暂断开触发不必要的重绘。

**`onShow()`**

热启动（小程序切回前台）时，若 `socketStatus === 'closed'` 则自动重连 WS，不重新登录。

**`request(endpoint, method, data, isRetry)`**

封装 `wx.request`，自动拼接 `BASE_URL + API_PREFIX`，自动附加 `Authorization: Bearer {token}` 头。返回 Promise，成功（HTTP 200）resolve `res.data`，失败 reject。

收到 HTTP 401 时：若 `!_isRefreshing && !isRetry` → 调 `_refreshAndRetry()`；若 `isRetry=true`（已经是重发请求）→ 直接 reject `{ code: 401 }`，**不再重新触发 refresh，防止 401 无限循环**。

**`_refreshAndRetry(endpoint, method, data)`**

refresh token 刷新流程：
1. 若 `_isRefreshing=true`（其他请求正在刷新）→ 将当前请求推入 `_refreshQueue`，等待统一重发
2. 刷新成功 → 更新 `token`，批量重发 `_refreshQueue` 中所有请求（均传 `isRetry=true`），再重发本次请求（传 `isRetry=true`）
3. 刷新失败 → 清空队列，`redirectTo` 到登录页

**`formatMsgTime(utcStr)`**

UTC 时间字符串 → 本地北京时间显示规则：
- 今天 → `HH:MM`
- 今年其他日期 → `MM/DD HH:MM`
- 跨年 → `YYYY/MM/DD`

---

## 四、页面文件职责详解

### 4.1 `pages/index/` — 启动页

**版本**：v6.0.0

**职责**：小程序启动后的第一个页面，负责等待身份验证完成并路由分发，自身不承载任何业务逻辑。

**执行流程**：

```
onLoad
  └── app.onAuthReady(cb)
        ├── 收到信号 → syncUI(info)
        │     ├── inviteOnly=true 且 hasAccess=false
        │     │     └── wx.redirectTo → /pages/invite/invite
        │     └── 正常 → setData statusText → setTimeout(1200ms) → enterTerminal()
        │                                                            └── wx.redirectTo → /pages/chat/chat
```

**data 字段**

| 字段 | 默认值 | 说明 |
|------|--------|------|
| `statusText` | `'正在找到你喵，马上就来~'` | 加载状态文字 |
| `isNavigating` | `false` | 防抖锁，防止 syncUI 重复执行跳转 |

**注意**：`isNavigating` 初始值为 false，进入 `syncUI` 后置为 true，加载环在 `isNavigating=false` 时显示（`wx:if="{{!isNavigating}}"`）。

---

### 4.2 `pages/chat/` — 主聊天页

**版本**：v7.0.2（物理装甲修复版）+ v9.0 邀请制/VIP + v9.1 管理角色

**职责**：核心交互页面，承载全量功能：多模态对话、历史消息管理、语音播放、图生图、灵魂画像、VIP 状态展示、在店状态管理。

#### 4.2.1 data 字段完整说明

**消息与输入**

| 字段 | 类型 | 说明 |
|------|------|------|
| `messages` | array | 消息列表，每条包含 id / role / content / image_url / voice_url / voice_status / sticker_url / gen_image_url / is_report 等 |
| `inputValue` | string | 输入框当前文字 |
| `scrollToView` | string | scroll-view 的 scroll-into-view 目标，格式 `msg_{index}` |

**用户状态**

| 字段 | 类型 | 说明 |
|------|------|------|
| `affinity` | number | 好感度（0-100），驱动顶栏进度条 |
| `mood` | string | 情绪标签：`neutral` / `happy` / `angry` / `sad` |
| `moodText` | string | mood 的中文映射：平静 / 愉悦 / 微愠 / 失落 |
| `userInfo` | object\|null | 当前用户信息 |
| `isVip` | bool | VIP 状态，控制次数显示区域 |
| `msgRemaining` | number | 今日剩余次数，-1=不限 |
| `dailyLimit` | number | 今日上限 |
| `imageGenQuota` | number | ★ v1.1 图生图剩余配额；-1=不受限；>=0=剩余次数；来自 syncVipStatus 的 image_gen_quota_remaining 字段；≤3 时顶栏显示红色警告 |
| `manageRole` | string\|null | 管理角色，控制管理入口 ⚙️ 显示 |
| `isInstore` | bool | 当前是否在店，控制"在店"标识显示 |

**图片资产状态机**

| 字段 | 类型 | 说明 |
|------|------|------|
| `tempImagePath` | string | 单图预览的本地临时路径 |
| `currentImageId` | string | 单图上传成功后的 image_id |
| `isUploading` | bool | 上传进行中，锁定发送和再次选图 |
| `pendingImageIds` | array | 多图上传成功的 image_id 列表（文案/美妆功能） |
| `pendingImagePaths` | array | 多图本地临时路径列表，用于预览 |
| `uploadProgress` | string | 多图上传进度文字，如"上传中 2/3" |

**语音**

| 字段 | 类型 | 说明 |
|------|------|------|
| `voiceLibrary` | array | 可用声线列表，从 `/voice/library` 接口拉取 |
| `currentVoice` | string | 当前选中声线，持久化到 Storage |
| `voiceIndex` | number | picker 选中位置 |
| `playingMsgId` | string\|null | 当前正在播放的消息 id |

**引擎状态**

| 字段 | 类型 | 说明 |
|------|------|------|
| `isReady` | bool | initPageData 是否已执行，防重入守卫 |
| `isGenerating` | bool | AI 回复生成中，锁定发送按钮 |
| `isFetchingHistory` | — | ★ v1.1 已改为实例变量 `this._isFetchingHistory`，不再在 data 中，不触发渲染；功能不变（防洪锁） |
| `pollingTimer` | number\|null | setInterval 定时器 ID |
| `isUserScrollingUp` | bool | 用户上拉浏览历史中，不自动滚底 |

**灵魂画像**

| 字段 | 类型 | 说明 |
|------|------|------|
| `isMemoirProcessing` | bool | 报告生成中 |
| `memoirCooldown` | number | 冷却剩余秒数，>0 时显示倒计时 |

**历史记录分页**

| 字段 | 类型 | 说明 |
|------|------|------|
| `hasMoreHistory` | bool | 是否还有更早的历史消息 |
| `isLoadingMore` | bool | 加载更多历史中 |
| `oldestMsgId` | string\|null | 当前已加载的最早一条消息 id，用于 `before_id` 分页 |

#### 4.2.2 生命周期与初始化流程

**冷启动（首次进入）**

```
onLoad
  ├── 初始化 audioCtx
  ├── 设置 app.globalData.chatContext = this
  └── app.onAuthReady(() => initPageData())
        └── silentLogin 完成后触发
              └── initPageData()
                    ├── isReady=true（防重入）
                    ├── setData：userInfo / isVip / manageRole / currentVoice
                    ├── loadHistory()         拉取最新消息列表
                    ├── loadVoiceLibrary()    拉取可用声线
                    ├── syncMemoirCooldown()  同步冷却状态
                    └── syncVipStatus()       同步今日剩余次数

onShow（冷启动时 isReady=false）
  └── 跳过 loadHistory（等 initPageData 执行）
```

**热启动（从其他页面返回）**

```
onShow（isReady=true）
  ├── stillProcessing = this.data.isMemoirProcessing（setData 前读取，保留真实值）
  ├── setData({ isMemoirProcessing: false, ... })
  ├── needsRefresh 判断：isGenerating || stillProcessing || 有 voice_status=loading 的消息
  │     ├── true  → loadHistory(isSilent=true) + checkAndStartPollingIfNeeded()
  │     └── false → 跳过 loadHistory（用户预览图片/查看报告返回时静默，避免重绘）★ v1.1
  └── syncInstoreStatus()    同步在店状态
```

> ⚠️ **注意**：`needsRefresh` 必须用 `stillProcessing`（setData 前读取的变量），不能用 `this.data.isMemoirProcessing`。原因：小程序 `setData` 对 `this.data` 是同步更新，setData 之后立刻读 `this.data.isMemoirProcessing` 已经是 `false`，导致 memoir 处理期间切换页面返回后轮询不恢复。（★ v1.2 修复 B2）

#### 4.2.3 消息发送流程

```
sendMessage()
  ├── 前置校验：isReady / 内容非空 / !isGenerating / !isUploading / 发送间隔 >1200ms
  ├── 本地乐观渲染：pushMessage({ id: clientMsgId, role:'user', ... })
  ├── 清空输入框和图片状态
  ├── isGenerating = true
  ├── app.sendSocketMessage({ action:'chat', message, image_id, image_ids, enable_search, client_msg_id })
  └── startPolling()    1500ms 轮询 loadHistory
```

**发送时字段说明**

| 字段 | 说明 |
|------|------|
| `action` | 固定 `'chat'` |
| `message` | 用户文本（已过滤 HTML 标签） |
| `image_id` | 纯单图时的 image_id，**有多图时为 undefined**（★ v1.2 互斥修复） |
| `image_ids` | 多图 image_id 数组（文案/美妆场景），**有多图时才发，单图时为 undefined**（★ v1.2 互斥修复） |
| `enable_search` | 是否开启联网搜索 |
| `client_msg_id` | 格式 `c_{timestamp}_{random5}` 去重标识 |

> ⚠️ **注意**：`image_id` 与 `image_ids` 两字段互斥——有多图（`pendingImageIds.length > 0`）时只发 `image_ids`，纯单图时只发 `image_id`。两字段均使用 `sendMessage` 函数作用域内的局部变量 `imageIds`（不能用 `this.data.pendingImageIds`，因为 setData 已将其清空）。

#### 4.2.4 WebSocket 消息处理（handleReceiveMessage）

所有 WS 消息统一经此函数处理：

| payload.type | 处理逻辑 |
|------|------|
| `system` | 直接 return，不渲染（`update_profile` 由 app.js 处理） |
| `error` | 先解锁 isGenerating + stopPolling，再按 error_code 分支处理 |
| `error` / `no_access` | showModal + redirectTo `/invite/invite` |
| `error` / `vip_required` | showModal 提示 VIP 专属 |
| `error` / `quota_exceeded` | ★ v1.1 showModal 提示配额用完 + 去商城按钮 + syncVipStatus 刷新剩余次数 |
| `error` / `daily_limit` | showModal + syncVipStatus 重新拉次数 |
| `error` / `visit_expired` | redirectTo `/invite/invite?reason=visit_expired` |
| `error` / 其他 | showToast 兜底 |
| `text` | pushMessage 渲染气泡，乐观递减 msgRemaining |
| `voice` | applyVoiceToMessage 更新语音状态 |
| `image_gen_result` | isGenerating=false + loadHistory |
| `copywriting_result` | isGenerating=false + loadHistory（成功时） |
| `makeup_result` | isGenerating=false + loadHistory（成功时） |

**error_code 完整枚举**

| error_code | 含义 | 前端处理 |
|------|------|------|
| `no_access` | 邀请码未激活或被踢 | Modal + 跳邀请页 |
| `vip_required` | 功能需要 VIP | Modal 提示 |
| `quota_exceeded` | ★ v1.1 图生图配额用完 | Modal + 去商城按钮 + syncVipStatus |
| `daily_limit` | 今日次数用完 | Modal + 重新拉次数 |
| `visit_expired` | 到店宽限期到期 | 跳邀请页并传 reason |

#### 4.2.5 loadHistory 合并算法

防洪锁 `this._isFetchingHistory`（★ v1.1 改为实例变量，不再是 data 字段，避免每次轮询因 `isFetchingHistory: true/false` 两次 setData 触发渲染）。

**合并策略（防消息防吞）**：

```
1. 拉取服务端最新消息列表 serverHistory
2. 过滤本地 messages 中以 'c_' 开头的乐观气泡（pendingLocal）
3. 匹配：serverHistory 中的 user 消息通过 client_msg_id 找到对应乐观气泡，继承 temp_image_url
4. pendingLocal = 本地乐观气泡中 client_msg_id 不在 serverHistory 中的（仍在飞行中）
5. finalMessages = [...serverHistory, ...pendingLocal]
6. ★ v1.1 本地路径继承：从 this.data.messages 按 id 查找，将已下载的 wxfile:// / http://tmp 路径
   继承到 finalMessages 对应条目，避免每次轮询重新发起下载
7. ★ v1.1 messages diff：比较 id / voice_status / gen_image_status / voice_url / gen_image_url /
   sticker_url / content 七个字段，全部一致则跳过 setData({ messages })，不触发列表重绘
8. ★ v1.1 downloadToLocal 批量合并：只对还未本地化的路径发起下载，Promise.all 全部回调后
   一次 setData 统一写入，不再逐条触发
9. ★ v1.1 hasMoreHistory / oldestMsgId 加 diff 保护，值未变不 setData
```

**voice_status 处理规则**：

| 条件 | voice_status |
|------|------|
| `m.voice_url` 存在 | `'ready'` |
| 无 voice_url 且 voice_status=loading 且消息超过 3 分钟 | ★ v1.1 强制改为 `'none'`（防止历史中僵尸 loading 消息永久触发轮询） |
| `m.voice_status` 存在（未超时） | 使用原值 |
| role=assistant 且无以上字段 | `'loading'` |
| role=user | `'none'` |

> ⚠️ **僵尸 loading 消息是前端频闪的根本原因**：历史消息中语音生成失败导致 `voice_status` 永久卡在 `loading`，`checkAndStopPolling` 因存在 loading 消息而不停轮询，`onShow` 的 `needsRefresh` 也持续为 true，导致轮询 + onShow 双重触发频繁 setData。3 分钟超时强制降级为 none 彻底解决此问题。

#### 4.2.6 轮询机制

| 函数 | 说明 |
|------|------|
| `startPolling()` | 启动 1500ms 间隔的 `loadHistory(true)` 定时器，有防重入 |
| `stopPolling()` | 清除定时器 |
| `checkAndStopPolling()` | 当 `!isGenerating && !isMemoirProcessing && !hasLoadingVoice` 时停止轮询 |
| `checkAndStartPollingIfNeeded()` | 有 loading 语音或 isGenerating 时启动轮询（onShow 时调用） |

**★ v1.1 onShow / onClose 的 needsRefresh 条件**

`chat.js onShow` 和 `app.js onClose` 中触发 `loadHistory` 前均检查：

```javascript
const needsRefresh = isGenerating || isMemoirProcessing ||
  messages.some(m => m.role === 'assistant' && m.voice_status === 'loading');
```

`needsRefresh=false` 时跳过 `loadHistory`，消除用户预览图片/查看报告/从其他页面返回时因 WS 短暂断开触发的不必要渲染。

**★ v1.1 onScroll setData 优化**

`isUserScrollingUp` 加值比较，仅在 `false→true` 或 `true→false` 时才 setData，消除每次滚动事件必写的开销。3 秒超时恢复也加了判断，已经是 false 就不写。

#### 4.2.7 图片上传流程

**单图**（对话场景）：

```
chooseMedia(count=1) → 单文件
  └── uploadVisionImage(path)
        └── wx.uploadFile → POST /api/mp/upload/vision
              ├── 成功 → currentImageId = data.image_id
              └── 失败 → 清空 tempImagePath / currentImageId
```

**多图**（文案/美妆场景，count=9）：

```
chooseMedia(count=9) → 多文件
  └── _uploadMultiImages(files)
        ├── 并行 wx.uploadFile × N
        ├── 每完成一张更新 uploadProgress
        └── 全部完成 → pendingImageIds / pendingImagePaths
```

大小限制：单张 ≤ 10MB，超过弹 Modal 提示。

#### 4.2.8 灵魂画像功能

```
generateMemoir()
  ├── 防重入：isMemoirProcessing || memoirCooldown > 0 → return
  ├── POST /export/memoir
  ├── 成功（code=200）→ isMemoirProcessing=true + startPolling
  ├── 429（冷却中）→ syncMemoirCooldown()
  └── 失败 → isMemoirProcessing=false

loadHistory 轮询时检测报告状态：
  ├── 服务端 memoir_processing=true → 设置 expectedReportCount
  ├── 报告条数 >= expectedReportCount → startMemoirCooldown(300s)
  └── 冷却倒计时由 cdTimer 每秒递减
```

**冷却同步**（进页面时）：

```
syncMemoirCooldown()
  └── GET /memoir/status → res.data.cooldown > 0 → startMemoirCooldown(cooldown)
```

**★ v1.1 冷却倒计时降频**：`startMemoirCooldown` 内部用 `this._memoirCdValue` 每秒递减，但每 5 秒才调一次 `setData({ memoirCooldown })`，消除 1 秒一次 setData 与 1.5 秒轮询交错导致的渲染撕裂。倒计时显示精度从每秒变为每 5 秒，肉眼无感知差异。

#### 4.2.9 onProfileUpdate（v1.0 新增）

接收 app.js 转发的 WS `system + update_profile` 回调，实时更新用户名：

```javascript
onProfileUpdate(newName)
  ├── setData({ 'userInfo.user_name': newName })
  └── app.globalData.userInfo.user_name = newName
```

---

### 4.3 `pages/checkin/` — 到店签到页

**职责**：处理扫码签到，从 URL 参数解析 `store_token`，调用签到接口，展示签到结果并自动跳转聊天页。

**进入方式**：微信扫描店铺二维码，URL 格式 `/pages/checkin/checkin?store_token=xxx`

**执行流程**：

```
onLoad(options)
  ├── 无 store_token → wx.redirectTo('/chat')（非扫码进入，静默跳转）
  └── 有 store_token → app.onAuthReady(() => doCheckin(storeToken))

doCheckin(storeToken)
  ├── token 未就绪 → status='error'
  └── POST /api/store/checkin { store_token }
        ├── 200 → status='success'|'new' + welcomeText + displayName
        │          更新 globalData.userInfo.is_instore=true
        │          1800ms 后自动 redirectTo chat
        ├── 403 → status='error'（二维码过期）
        ├── 429 → status='error'（频率限制）
        └── 其他 → status='error'
```

**data 状态机**

| status | 展示内容 |
|------|------|
| `loading` | 加载动画 + "正在确认二维码喵…" |
| `success` | 成功图标 + welcomeText + displayName + "在店中"徽章 |
| `new` | 新客户欢迎 + 引导告知名字 |
| `error` | 错误图标 + errorMsg + 返回按钮 |

---

### 4.4 `pages/invite/` — 邀请码激活页

**进入方式**：
1. index.js 判断 `inviteOnly=true && hasAccess=false` 时路由
2. chat.js 收到 `error.no_access` WS 消息时路由
3. chat.js 收到 `error.visit_expired` 时路由（携带 `?reason=visit_expired`）

**执行流程**：

```
onLoad(options)
  ├── hasAccess=true → wx.redirectTo('/chat')（已有权限直接跳过）
  └── options.reason='visit_expired'
        └── isVisitExpired=true → 展示到期提示，隐藏输入框

activate()
  ├── 防重入：isSubmitting → return
  └── POST /api/mp/invite/activate { code }
        ├── 成功 → globalData.hasAccess=true + showToast + 1500ms → redirectTo('/chat')
        └── 失败 → errMsg = body.msg
```

**宽限期到期 UI**（`isVisitExpired=true`）：隐藏输入框和激活按钮，仅展示"请联系管理员获取邀请码"提示。

---

### 4.5 `pages/report/` — 灵魂画像报告页

**版本**：v1.2.0

**进入方式**：chat.js 点击报告卡片时调用 `viewReport()`，通过 URL 参数传入报告文件路径。

```javascript
// chat.js
viewReport(e) {
  const fullApiUrl = resolveUrl(`/api/mp/files/${url}`);
  wx.navigateTo({ url: `/pages/report/report?url=${encodeURIComponent(fullApiUrl)}` });
}
```

**两种加载模式**：

| 模式 | 触发条件 | 说明 |
|------|------|------|
| 模式一：内存透传 | `options.source='mem'` | 从 Storage `current_report_md` 读取，秒开（写入端待实现） |
| 模式二：API 拉取 | `options.url` 存在 | 带 JWT 的网络请求，当前实际工作路径 |
| 模式三：本地兜底 | 无参数 | 读取 Storage 最近一份，显示"已物理挥发"兜底文案 |

**当前状态**：模式二是唯一工作的路径，模式一的写入端（WS 推送后存入 Storage）尚未实现。

**功能**：`copyReport()` 将 Markdown 原文复制到剪贴板。

**已知问题**：报告内容以 `<text>` 组件直接渲染，Markdown 语法符号（`##`、`**`、`-`）会原样显示，未做 Markdown 解析。

---

### 4.6 `pages/shop/` — 公开商城页（★ v1.1 新增）

**版本**：v1.0

**职责**：展示所有上架套餐，无需登录即可浏览。支持分类 Tab 筛选、套餐详情浮层、联系管理员（复制微信号）、跳转聊天入口。

**进入方式**：`chat.js goShop()` 调用 `wx.redirectTo({ url: '/pages/shop/shop' })`（`redirectTo` 替换当前页面，不销毁整个页面栈，但也不叠加新层）。★ v1.2 从 `wx.reLaunch` 改为 `wx.redirectTo`，原因：`reLaunch` 会清空整个栈（包括底部的 index/chat），导致从商城页点"去聊天"时无法正常返回；`redirectTo` 替换当前 chat 页，栈深度不增加，同时保留 index 页在栈底。

**数据加载**：

```
onLoad
  ├── _loadPackages()  → GET /api/mp/shop/packages（无需鉴权）
  │     ├── 全量加载存入 allPackages（本地缓存，切 Tab 不重复请求）
  │     ├── 图片 URL 处理：后端已拼好完整路径（含 /api/mp/shop/images/...）
  │     │   若 PUBLIC_BASE_URL 未配，后端返回相对路径（/api/mp/shop/images/pkg_xxx/img.jpg）
  │     │   前端只补 base 域名：p.startsWith('http') ? p : `${base}${p}`
  │     │   ⚠️ 不能再额外拼 /api/mp/shop/images/，否则路径重复变成双倍
  │     └── packages = allPackages（默认 all 分类）
  └── _loadConfig()   → GET /api/mp/shop/config（无需鉴权）
        └── 写入 contactWechat / contactNote
```

**分类 Tab 切换**（`switchCategory`）：本地过滤 `allPackages`，不发网络请求，同时关闭详情浮层。

**套餐详情浮层**：点击套餐卡片弹出（`showDetail=true`），展示图片轮播（`detailImgIndex` 跟踪当前页）、价格、描述、类型标签。`closeDetail` 收起。

**联系管理员**（`contactAdmin`）：`contactWechat` 复制到剪贴板，Toast 提示（含 `contactNote`）。无联系微信时提示找管理员。

**跳转聊天**（`goChat`）：`wx.navigateTo({ url: '/pages/index/index' })`，让 index 做鉴权判断。

**下拉刷新**：`onPullDownRefresh` 同时调 `_loadPackages()` 和 `_loadConfig()`。

**data 字段**

| 字段 | 类型 | 说明 |
|------|------|------|
| `categories` | array | 分类列表，与后端 `SHOP_CATEGORIES` 保持一致 |
| `currentCategory` | string | 当前选中分类 key |
| `packages` | array | 当前展示的套餐列表（过滤后） |
| `allPackages` | array | 全量套餐缓存，切 Tab 时本地过滤 |
| `loading` | bool | 数据加载中 |
| `showDetail` | bool | 详情浮层是否展开 |
| `detailPkg` | object\|null | 当前展示的套餐详情对象 |
| `detailImgIndex` | number | 详情图片轮播当前页 |
| `contactWechat` | string | 联系微信号（从 /shop/config 拉取） |
| `contactNote` | string | 添加微信备注提示 |
| `baseUrl` | string | `app.globalData.BASE_URL` 副本 |

---

### 4.7 `pages/manage/` — 管理后台

#### 4.7.1 `manage.js` — 管理入口

**职责**：调用 `/api/manage/whoami` 确认当前用户的管理角色，自动路由到对应界面。

```
onLoad / onShow
  └── _checkRole()
        ├── token 未就绪 → setTimeout(500ms) 重试
        └── GET /api/manage/whoami
              ├── manage_role='admin' → wx.redirectTo('/manage/admin/index')
              ├── manage_role='store_clerk' → wx.redirectTo('/manage/store/index')
              └── 无权限 / 接口失败 → 展示错误信息
```

`whoami` 返回的 `manage_role`、`manage_store_id`、`store_name` 写入 `globalData`，子页面直接读取，无需重复请求。

#### 4.7.2 `manage/api.js` — 公共请求工具

所有 `/api/manage/*` 接口统一通过此模块请求：

- 自动附加 `Authorization: Bearer {token}`
- HTTP 401 → showToast"登录已过期"+ reject
- HTTP 403 → showToast"没有操作权限喵"+ reject
- 网络失败 → showToast"网络异常"+ reject

注意：`getApp()` 必须在函数内调用，不能在模块顶层调用（模块被 require 时 App 可能未初始化）。

**工具函数**

| 函数 | 说明 |
|------|------|
| `manageRequest(path, method, data)` | 返回 Promise，resolve `res.data` |
| `formatBJTime(isoStr)` | UTC → 北京时间 `YYYY-MM-DD HH:MM` |
| `todayBJ()` | 今日北京日期字符串 `YYYYMMDD` |

#### 4.7.3 `admin/index.js` — 管理员主界面

**功能菜单**

| key | 标题 | 跳转页面 |
|------|------|------|
| `users` | 用户管理 | `admin/users` |
| `invites` | 邀请码 | `admin/invites` |
| `packages` | 套餐管理 | `admin/packages` |
| `orders` | 订单记录 | `admin/orders` |
| `store` | 店铺管理 | `admin/store` |
| `roles` | 角色授权 | `admin/roles` |

统计概览：`GET /api/manage/admin/stats`，返回用户总数/已激活/VIP/今日订单/可用邀请码。

**多店跳转**（`goStore`）：调用 `/api/manage/screen-codes` 拉取店铺列表，返回格式兼容数组直返、`{ list: [] }` 和 `{ stores: [] }` 三种格式（★ v1.2 修复 B3）。单店直跳，多店弹 ActionSheet 选择，跳转 `store/index` 时携带 `store_id` 和 `store_name`。

#### 4.7.4 `admin/users.js` — 用户管理

```
进入页面 → search()（拉全量用户列表，空关键词返回最近50条）

搜索：GET /api/manage/admin/users/search?q={keyword}&limit=50
查详情：GET /api/manage/admin/users/info?user_id={uid}
授VIP：POST /api/manage/admin/vip/grant { user_id, vip_level, days }
撤VIP：POST /api/manage/admin/vip/revoke { user_id }
踢人：POST /api/manage/admin/users/kick { user_id, reason:'admin_kick' }
```

VIP 授权弹窗支持选择天数（0=永久）和等级（1=VIP / 2=SVIP）。

#### 4.7.5 `admin/invites.js` — 邀请码管理

```
列表：GET /api/manage/admin/invites?status={filter}&limit=100
生成：POST /api/manage/admin/invites { count, note }（1-50个）
作废：POST /api/manage/admin/invites/revoke { code }
```

筛选器：全部 / 未使用 / 已使用 / 已作废，切换即刷新。

生成成功后弹 Modal 展示所有新码，支持一键复制单个邀请码。

#### 4.7.6 `admin/packages.js` — 套餐管理

**套餐类型**

| type | 说明 | 专属字段 |
|------|------|------|
| `store` | 到店套餐 | store_id / screen_minutes |
| `online` | 线上VIP套餐 | vip_level / vip_days |

```
列表：GET /api/manage/store/packages（返回 groups 格式，按店铺分组）
新增：POST /api/manage/store/packages { type, store_id, name, price, ... }
编辑：PATCH /api/manage/store/packages/{id} { name, price, ... }
上下架：PATCH /api/manage/store/packages/{id} { is_active: bool }
```

店铺选择器：进页面时预加载 `GET /api/manage/admin/screen_codes`，新增/编辑时以 picker 而非手填 store_id。编辑时根据套餐的 store_id 回填 picker 选中位置。

切换套餐类型时自动重置对方类型的专属字段，防止残留数据提交。

#### 4.7.7 `admin/orders.js` — 订单记录

```
列表：GET /api/manage/store/orders?date={YYYYMMDD}&store_id={可选}&limit=100
```

进页面默认填入今日北京日期，日期输入框有 300ms 防抖，输入完成后自动刷新。

#### 4.7.8 `admin/roles.js` — 角色授权

```
角色列表：GET /api/manage/admin/role/list
搜索用户：GET /api/manage/admin/users/search?q={kw}&limit=10
授权：POST /api/manage/admin/role/grant { user_id, role, store_id }
撤销：POST /api/manage/admin/role/revoke { user_id }
```

**流程**：搜索用户 → 点击搜索结果（`pickUser`）→ 弹出授权弹窗，同时触发 `_loadStores()` 拉取店铺列表。

店铺选择器：`_loadStores()` 调用 `GET /api/manage/admin/screen_codes`，结果缓存在 `storeList`（已拉过不重复请求）。店员角色时展示店铺 picker，管理员角色时隐藏。

注意：弹窗的唯一入口是 `pickUser`（从搜索结果点击进入）。（★ v1.2 已删除无绑定的死代码 `openModal` 函数）

#### 4.7.9 `admin/store.js` — 店铺管理

```
列表：GET /api/manage/screen-codes
重置短码：POST /api/manage/screen-codes/reset { store_id }
```

列表接口返回格式兼容三路（数组 / `{ list }` / `{ stores }`），统一提取为数组后渲染（★ v1.2 修复 B4）。重置后弹 Modal 展示新链接，同时刷新列表。复制链接按钮写入剪贴板。

#### 4.7.10 `store/index.js` — 店员主界面

**三 Tab 设计**

| tab | 内容 | 接口 |
|------|------|------|
| `customers` | 在店客户列表 | `GET /api/manage/store/customers` |
| `order` | 确认下单 | `POST /api/manage/store/orders` |
| `orders` | 今日订单 | `GET /api/manage/store/orders?date={today}` |

**进入方式**：
- 店员直接进入：`store_id` / `store_name` 从 `globalData.manageStoreId/Name` 读取
- admin 快捷跳转：URL 携带 `?store_id=xxx&store_name=xxx`

**下单流程**：

```
selectCustomer(客户) → 切换到 order tab，预填客户
selectPackage(套餐) → 选中套餐
onPayMethodChange(支付方式) → wechat/alipay/cash/other
submitOrder()
  └── POST /api/manage/store/orders { user_id, package_id, payment_method, note }
        ├── 成功 → showToast + 切换到 orders tab + _loadOrders() + _loadCustomers()
        └── 失败 → showModal 展示错误
```

**套餐格式兼容**：后端 admin 视图返回 `groups` 格式，store_clerk 视图返回 `list` 格式，`_loadPackages` 自动判断并统一转为扁平数组。

---

## 五、接口调用汇总

### 5.1 `app.request()` 封装接口（前缀 `/api/mp`）

| 接口 | 方法 | 调用位置 | 说明 |
|------|------|------|------|
| `/login` | POST | app.js silentLogin | 微信登录，返回 token + 全量用户状态 |
| `/history` | GET | chat.js loadHistory | 拉取最新消息列表，返回 list / has_more / profile |
| `/history?before_id={id}` | GET | chat.js loadMoreHistory | 分页拉取更早历史 |
| `/export/memoir` | POST | chat.js generateMemoir | 触发灵魂画像报告生成 |
| `/memoir/status` | GET | chat.js syncMemoirCooldown | 查询报告冷却状态，返回 `data.cooldown` |
| `/vip/status` | GET | chat.js syncVipStatus | 查询 VIP 和今日剩余次数，★ v1.1 新增 `image_gen_quota_remaining` 字段 |
| `/voice/library` | GET | chat.js loadVoiceLibrary | 拉取可用声线列表 |
| `/voice/preference` | POST | chat.js onVoiceChange | 保存声线偏好 |
| `/invite/activate` | POST | invite.js activate | 激活邀请码 |
| `/shop/config` | GET | ★ v1.1 shop.js _loadConfig | 商城公开配置（无需鉴权，shop.js 直接调 wx.request） |
| `/shop/packages` | GET | ★ v1.1 shop.js _loadPackages | 公开套餐列表（无需鉴权）；返回的 images 路径已由后端拼好，前端只补 base 域名，不再拼 /api/mp/shop/images/ |

### 5.2 直接 `wx.request` 调用接口

| 接口 | 方法 | 调用位置 | 说明 |
|------|------|------|------|
| `POST /api/mp/upload/vision` | POST | chat.js uploadVisionImage / _uploadMultiImages | 图片上传（multipart/form-data） |
| `GET /api/mp/images/{uid}/{filename}` | GET | chat.js loadHistory（构造图片 URL） | 图片服务，鉴权通过 `Authorization: Bearer` header，**URL 中不再附加 `?token=`**（★ v1.2 安全修复 S1） |
| `GET /api/mp/files/{url}` | GET | report.js loadFromRemote | 报告文件拉取（带 JWT） |
| `GET /api/store/instore` | GET | chat.js syncInstoreStatus | 查询是否在店 |
| `POST /api/store/checkin` | POST | checkin.js doCheckin | 到店签到 |
| `POST /api/store/checkout` | POST | chat.js doCheckout | 主动离店 |

### 5.3 WebSocket 连接

**URL**：`ws(s)://{BASE_URL}/api/mp/ws/{session_id}?token={token}`

**前端→后端消息**

| action | 参数 | 说明 |
|------|------|------|
| `chat` | `message, image_id?, image_ids?, enable_search, client_msg_id` | 发送对话消息 |
| `ping` | — | 心跳保活，每 25 秒发送一次 |

**后端→前端消息**（详见第六节 WebSocket 协议）

### 5.4 `manageRequest()` 封装接口（前缀 `/api/manage`）

| 接口 | 方法 | 调用位置 |
|------|------|------|
| `GET /whoami` | GET | manage.js |
| `GET /admin/stats` | GET | admin/index.js |
| `GET /admin/users/search` | GET | admin/users.js, admin/roles.js |
| `GET /admin/users/info` | GET | admin/users.js |
| `POST /admin/users/kick` | POST | admin/users.js |
| `POST /admin/vip/grant` | POST | admin/users.js |
| `POST /admin/vip/revoke` | POST | admin/users.js |
| `GET /admin/invites` | GET | admin/invites.js |
| `POST /admin/invites` | POST | admin/invites.js |
| `POST /admin/invites/revoke` | POST | admin/invites.js |
| `GET /store/packages` | GET | admin/packages.js, store/index.js |
| `POST /store/packages` | POST | admin/packages.js |
| `PATCH /store/packages/{id}` | PATCH | admin/packages.js |
| `POST /store/packages/{id}/images` | POST | ★ v1.1 admin/packages.js（套餐图片上传） |
| `DELETE /store/packages/{id}/images` | DELETE | ★ v1.1 admin/packages.js（套餐图片删除） |
| `GET /store/orders` | GET | admin/orders.js, store/index.js |
| `POST /store/orders` | POST | store/index.js |
| `GET /store/customers` | GET | store/index.js |
| `GET /admin/screen_codes` | GET | admin/store.js, admin/packages.js, admin/roles.js, admin/index.js |
| `POST /admin/screen_code/reset` | POST | admin/store.js |
| `GET /admin/role/list` | GET | admin/roles.js |
| `POST /admin/role/grant` | POST | admin/roles.js |
| `POST /admin/role/revoke` | POST | admin/roles.js |

---

## 六、WebSocket 消息协议（小程序端）

### 6.1 后端 → 前端

| type | 关键字段 | 前端处理位置 |
|------|------|------|
| `text` | `msg_id, content, affinity, mood, voice_status, gen_pending, sticker_url, char_text` | chat.js handleReceiveMessage |
| `voice` | `msg_id, voice_status, voice_url` | chat.js handleReceiveMessage → applyVoiceToMessage |
| `image_gen_result` | `msg_id, status, char_text, gen_image_url` | chat.js handleReceiveMessage → loadHistory |
| `makeup_result` | `msg_id, success, content` | chat.js handleReceiveMessage → loadHistory |
| `copywriting_result` | `msg_id, success, content` | chat.js handleReceiveMessage → loadHistory |
| `memoir_ready` | `content, file_url` | chat.js handleReceiveMessage（当前经由轮询检测） |
| `error` | `content, error_code` | chat.js handleReceiveMessage（先解锁状态机再处理）；★ v1.1 新增 `quota_exceeded` 分支 |
| `system` + `action:update_profile` | `user_name` | app.js onMessage → chatContext.onProfileUpdate |
| `system` + 其他 | — | chat.js handleReceiveMessage return |

### 6.2 voice_status 枚举

| 值 | 含义 | WXML 表现 |
|------|------|------|
| `loading` | TTS 生成中 | 显示"希卡莉正在准备声线喵..." 加载动画 |
| `ready` | 语音就绪 | 显示播放/暂停按钮 |
| `none` | 本条无语音 | 不显示语音区域 |

### 6.3 gen_image_status 枚举

| 值 | 含义 | WXML 表现 |
|------|------|------|
| `loading` | 图生图生成中 | "Seedream 四宫格生成中喵…"加载动画 |
| `ready` | 图片就绪 | 展示 char_text + gen_image_url |
| `failed` | 生成失败 | "❌ 生成失败喵，稍后再试" |

---

## 七、Storage 持久化字段

| Key | 类型 | 写入位置 | 读取位置 | 说明 |
|------|------|------|------|------|
| `hikari_token` | string | app.js silentLogin | app.js globalData 初始化 | JWT Access Token |
| `hikari_refresh_token` | string | app.js silentLogin | app.js _refreshAndRetry | JWT Refresh Token |
| `hikari_uid` | string | app.js silentLogin | app.js silentLogin（登录时传入） | 用户 ID，用于跨 session 识别同一用户 |
| `hikari_has_access` | string `'1'`/`'0'` | app.js silentLogin、invite.js activate | app.js globalData 初始化 | ★ v1.3 邀请制访问权限持久化；启动时恢复到 `globalData.hasAccess`，防止重启/重编译后误跳邀请页 |
| `currentVoice` | string | chat.js onVoiceChange | chat.js initPageData | 声线偏好 |
| `current_report_md` | string | 待实现 | report.js loadFromMemory | 最新报告 MD 全文（模式一秒开） |

> ⚠️ **hasAccess 持久化规则**：后端 `silentLogin` 返回的 `has_access` 始终覆盖 Storage（被踢/撤权时会写 `'0'`，下次重启自动生效）。激活邀请码成功后立即写 `'1'`，不依赖下次登录。Storage 被清（开发工具重编译）时 `getStorageSync` 返回空字符串，`'' === '1'` 为 false，`hasAccess` 初始化为 false，等 silentLogin 回调后由后端返回值覆盖。

---

## 八、页面导航关系

```
index
  ├── → chat（正常登录）
  └── → invite（邀请制未激活）

invite
  ├── → chat（激活成功）
  └── → invite?reason=visit_expired（宽限期到期，展示特殊 UI）

chat
  ├── → checkin（扫码，由微信路由）
  ├── → report（点击报告卡片）
  ├── → shop（点击 🛍️ 商城按钮，wx.redirectTo 替换当前页，栈深度不增加）★ v1.2
  └── → manage（点击 ⚙️ 管理入口，manageRole 非 null 时显示）

shop
  └── → index（点击"去聊天"，wx.navigateTo 保留页面栈供返回）★ v1.1

checkin
  └── → chat（签到完成后 1800ms 自动跳转）

manage
  ├── → manage/admin/index（manage_role='admin'）
  └── → manage/store/index（manage_role='store_clerk'）

manage/admin/index
  ├── → manage/admin/users
  ├── → manage/admin/invites
  ├── → manage/admin/packages
  ├── → manage/admin/orders
  ├── → manage/admin/store
  ├── → manage/admin/roles
  └── → manage/store/index?store_id=...（快捷下单，多店弹选择器）
```

---

## 九、已知问题记录

### 9.1 已修复（含 v1.0、v1.1、v1.2）

| # | 位置 | 问题描述 | 修复方案 |
|---|------|---------|---------|
| F1 | chat.js syncMemoirCooldown | 冷却时间读 `res.cooldown`，应为 `res.data.cooldown`，导致冷却计时器永远不恢复 | 修正字段路径，补加 `res.data &&` 空值守卫 |
| F2 | chat.js handleReceiveMessage | `visit_expired` error_code 无分支处理，宽限期到期用户只看到 Toast | 补加 `visit_expired` 分支，redirectTo invite?reason=visit_expired |
| F3 | index.wxml | loading-ring 条件绑定 `{{isConnecting}}` 但变量未定义，首屏无加载动画 | 改为 `{{!isNavigating}}` |
| F4 | chat.js / index.js / checkin.js | onLoad 中 `app.onAuthReady` + `if token` 双触发结构，热启动可能双重初始化 | app.js onAuthReady 改为 setTimeout(0) 统一异步，各页面删除 if 兜底块 |
| F5 | checkin.js | doCheckin 可能被双重调用（onAuthReady + if token 两路） | 同 F4，统一为单路 onAuthReady |
| F6 | invite.js | 激活成功后手动调 connectWebSocket，可能与 app.onShow 自动重连竞态 | 删除手动调用，依赖 app.onShow 自动处理 |
| F7 | chat.js | 缺少 onProfileUpdate 函数，WS update_profile 消息到达后 app.js 无法回调，用户名不实时更新 | 新增 onProfileUpdate 函数 |
| F8 | invite.js | ★ v1.1 激活邀请码成功后未主动建立 WS，chat 页 `isReady=true` 但消息无法收发；根本原因：`wx.redirectTo` 不触发 `App.onShow`，原注释"由 onShow 自动处理"的假设不成立 | 激活成功后主动调 `app.connectWebSocket()`，不依赖 onShow |
| F9 | chat.js / app.js | ★ v1.1 轮询期间高频 setData 导致页面频闪；根本原因链：①历史消息中语音生成失败留下僵尸 loading → 轮询不停 + onShow/onClose 持续触发 loadHistory；②loadHistory 每次全量写 messages + 逐条 downloadToLocal + isFetchingHistory 两次 setData + hasMoreHistory 无 diff；③memoirCooldown 每秒 setData 与轮询交错 | 修复汇总见 10.7 |
| F10 | shop.js _loadPackages | ★ v1.1 商城图片路径双重拼接，显示 404；根本原因：`PUBLIC_BASE_URL` 未配时后端返回相对路径 `/api/mp/shop/images/pkg_xxx/img.jpg`，前端误判为相对路径又拼一次 `/api/mp/shop/images/`，变成双倍路径 | 前端改为 `p.startsWith('http') ? p : \`${base}${p}\``，只补域名不补路径，与后端返回格式解耦 |
| F11 | admin/packages.js submitForm | ★ v1.1 编辑套餐时修改 vip_level 或 vip_days 保存后无效，自动还原；根本原因：编辑模式的 body 只传了 image_gen_quota，未传 vip_level 和 vip_days，后端 PATCH 收不到字段不更新 | 编辑模式线上套餐三个字段（vip_level / vip_days / image_gen_quota）统一传入 |
| M1 | admin/admin_index.js + admin_index.wxml | 废弃文件，goStore 逻辑为旧版（直接跳转不传 store_id），与 index.js 新版重复 | 删除废弃文件，保留 index.js / index.wxml |
| M2 | admin/roles.js | store_id 手填输入框，容易输错 | 改为从 /admin/screen_codes 接口拉取店铺 picker |
| M3 | admin/roles.js | pickUser 是弹窗实际入口但未调用 _loadStores，导致店铺列表永远为空 | pickUser 中补加 _loadStores() 调用 |
| M4 | admin/packages.js | _loadStores 和 onStoreChange 被错误插入 data 对象内，JS 语法错误 | 重写文件，正确放置于 Page 方法层 |
| M5 | store/index.js | 下单成功后未刷新在店客户列表 | submitOrder 成功后补加 _loadCustomers() |
| M6 | admin/orders.js | 日期输入需手动点查询，与其他筛选器行为不一致 | onDateInput 加 300ms 防抖自动刷新 |
| M7 | admin/packages.wxml | 店铺 ID 手填 textarea | 改为店铺 picker，三态显示（加载中/无店铺/正常选择） |
| M8 | admin/roles.wxml | 店铺 ID 手填 textarea | 同 M7 |
| B1 | app.js _refreshAndRetry | ★ v1.2 refresh token 刷新成功后重发原请求，若再次 401 因 `_isRefreshing=false` 会再次触发 refresh，导致死循环 | `request()` 加第四参数 `isRetry`，重发请求传 `true`，收到 401 且 `isRetry=true` 时直接 reject |
| B2 | chat.js onShow | ★ v1.2 `needsRefresh` 读 `this.data.isMemoirProcessing`，但此时 setData 已将其同步置为 false，memoir 处理期间返回聊天页轮询不恢复 | 改用 setData 前读取的 `stillProcessing` 局部变量 |
| B3 | admin/index.js goStore | ★ v1.2 `/screen-codes` 返回对象格式时 `res.data.length` 为 undefined，有店铺也显示"暂无" | 三路格式兼容（数组 / `data.list` / `data.stores`） |
| B4 | admin/store.js _load | ★ v1.2 `/screen-codes` 返回对象格式时 `res.data \|\| []` 赋为对象，列表渲染 key 而非数组 | 同 B3 三路兼容 |
| C1 | chat.js goShop | ★ v1.2 `navigateTo` 叠加页面栈，反复 chat→shop 触达上限10层后导航失效 | 改为 `redirectTo`（替换当前页，不增加栈深度） |
| C2 | chat.js sendMessage | ★ v1.2 单图时 `image_id` 和 `image_ids` 同时发送同一 id（重复）；多图时 `image_id` 取首元素与 `image_ids[0]` 重复 | 两字段互斥：有多图只发 `image_ids`，纯单图只发 `image_id`；使用 setData 前读取的局部变量 `imageIds`（而非 `this.data.pendingImageIds`，已被清空） |
| S1 | chat.js loadHistory/loadMoreHistory | ★ v1.2 图片 URL 中携带 `?token={JWT}`，出现在微信网络日志和开发工具抓包记录中 | 去掉 URL 中的 token，鉴权仅通过 `downloadToLocal` 的 `Authorization: Bearer` header 传递 |
| D1–D6 | 多文件 | ★ v1.2 死代码清理 | 删除 `beforeId` 常量、`openModal` 函数、`const self=this`、`expectedReplyCount` 字段、`applyGenImageToMessage` 函数、`buyer_profiles.js` 中未使用的 `files` 变量 |
| P1 | app.js globalData + silentLogin / invite.js activate | ★ v1.3 小程序重启或开发工具重编译后 `hasAccess` 丢失，`index.js` 误判为未激活跳邀请页；根本原因：`globalData.hasAccess` 初始值硬编码为 `false`，未从 Storage 恢复 | `globalData.hasAccess` 初始化改为 `wx.getStorageSync('hikari_has_access') === '1'`；`silentLogin` 成功后写 `hikari_has_access`（始终用后端值覆盖）；`invite.js` 激活成功后立即写 `'1'` |

### 9.2 已知��处理问题（非 bug，功能层面）

| # | 位置 | 问题描述 | 影响 |
|---|------|---------|------|
| Q1 | report.wxml | Markdown 内容用 `<text>` 直接渲染，语法符号原样显示 | 用户看到 `##`、`**` 等符号，排版体验差 |
| Q2 | report.js 模式一 | current_report_md 写入端未实现，秒开路径不可用 | 每次进报告页都发网络请求，模式二兜底正常 |
| Q4 | chat.js startMemoirCooldown | ~~`cdTimer` 直接写 `this.data.cdTimer` 绕过 setData，与 pollingTimer 风格不一致~~ ★ v1.1 已重构为 `this.data.cdTimer`（实例变量），降频至 5 秒更新一次 UI，风格不一致问题同步解决 | 已修复 |
| Q5 | report.json | 缺少 navigationBarTitleText，导航栏显示空白或继承上页标题 | 视觉小瑕疵 |
| Q6 | invite.wxml | isVisitExpired 判断写了两次（输入框和按钮各判断一次） | 无功能影响，可合并 |
| Q7 | chat.js sendMessage | ★ v1.2 已修复：`image_id`/`image_ids` 互斥发送（见 C2） | — |
| Q8 | manage/admin/index.js | ★ v1.2 已修复：`const self = this` 死代码（见 D3） | — |
| Q9 | 16.3（后端文档） | 在店用户 login 返回 has_access=false，前端未拦截，等 WS error.no_access 才触发跳转 | 在店用户首次进入不受影响（_resolve_user_context 守卫��放行），UX 略有瑕疵 |

---

## 十、开发规范与注意事项

### 10.1 双触发防护

所有页面的 `onLoad` 中统一只调用一次 `app.onAuthReady(cb)`，不再添加 `if (app.globalData.token)` 兜底分支。`app.js` 的 `onAuthReady` 已统一为 `setTimeout(0)` 异步执行，保证回调只触发一次。

### 10.2 isGenerating 状态机规范

`isGenerating` 是发送按钮的物理锁，以下情况必须确保正确解锁：
- WS 收到 `text` 消息且 `gen_pending=false` → 解锁
- WS 收到任何 `error` 消息 → **先解锁**，再处理错误（否则发送按钮永久锁死）
- WS 收到 `voice` / `image_gen_result` / `copywriting_result` / `makeup_result` → 解锁
- `loadHistory` 检测到最后一条消息是 assistant → 解锁
- `loadHistory` 检测到最后一条消息是 user 且超过 2 分钟 → 超时解锁

### 10.3 图片 URL 处理

所有来自服务端的图片路径必须经过 `resolveUrl()` 处理（相对路径补全 BASE_URL，绝对路径直接返回），再通过 `downloadToLocal()` 下载到本地临时路径，解决真机 `<image>` 组件无法显示外部 URL 的问题。

### 10.4 manageRequest 的 getApp() 时机

`manage/api.js` 中 `getApp()` 必须在 `manageRequest` 函数内部调用，不能在模块顶层调用。原因：模块被 `require` 时 App 实例可能尚未初始化。

### 10.5 多图与文案/美妆功能的字段对齐

`sendMessage` 发送时 `image_id` 与 `image_ids` 互斥（★ v1.2 修复）：
- 有多图（`pendingImageIds.length > 0`）→ 只发 `image_ids`，`image_id` 为 undefined
- 纯单图 → 只发 `image_id`，`image_ids` 为 undefined

两字段均从 `sendMessage` 函数内的局部变量 `imageIds` 读取，**不能用 `this.data.pendingImageIds`**，因为 `setData` 在 `sendSocketMessage` 前执行，已将 `pendingImageIds` 同步清空为 `[]`。

后端 orchestrator 主对话管线只读 `image_id`，`image_ids` 由 skill_api（文案/美妆）消费。若后续需要主对话支持多图，需后端 orchestrator 同步改造。

### 10.6 store/index.js 套餐列表格式

`_loadPackages` 需兼容两种返回格式：
- admin 视角：`res.data.groups`（按店铺分组的嵌套结构）
- store_clerk 视角：`res.data.list`（扁平列表）

统一转为扁平数组后绑定到 `data.packages`。

### 10.7 ★ v1.1 频闪修复规范（setData 高频化防护）

轮询期间高频 setData 是频闪的根本原因，以下规范必须遵守：

**防洪锁用实例变量而非 data**：`isFetchingHistory` 改为 `this._isFetchingHistory`，不触发渲染。所有仅用于逻辑控制、不需要驱动 WXML 的布尔锁，均应使用实例变量而非 `setData`。

**全量 setData 必须做 diff**：对 `messages` 等大数组，先比对关键字段（id / voice_status / gen_image_status / voice_url / gen_image_url / sticker_url / content），全部一致则跳过 `setData`，不触发列表重绘。

**批量合并异步回调**：多个异步操作（如 `downloadToLocal`）产生的 setData，必须用 `Promise.all` 收集后一次性写入，不能逐条回调各自 setData。

**本地路径继承**：`loadHistory` 中从服务端拿到新数据后，先将当前 `this.data.messages` 中已下载的本地路径（`wxfile://` / `http://tmp`）继承到 `finalMessages` 对应条目，避免每次轮询重复下载。继承步骤必须在 diff 之前完成。

**onShow / onClose 加 needsRefresh 守卫**：触发 `loadHistory` 前必须检查 `isGenerating || isMemoirProcessing || hasLoadingVoice`，静止状态下不触发。

**倒计时降频**：`memoirCooldown` 每秒递减但每 5 秒才 setData 一次，消除与轮询的交错冲突。

**值比较再写**：所有频繁变化的单字段（`isUserScrollingUp`、`hasMoreHistory`、`oldestMsgId`、`affinity`、`mood` 等），写入前对比当前值，相同则跳过 setData。

**僵尸 loading 消息超时降级**：`loadHistory` 中 `voice_status=loading` 且超过 3 分钟无 `voice_url` 的历史消息，强制改为 `'none'`，防止永久触发轮询。

---

## 十一、环境配置

| 配置项 | 位置 | 说明 |
|------|------|------|
| 开发环境 API 地址 | app.js `_getBaseUrl()` | 局域网 IP，如 `http://192.168.31.150` |
| 生产环境 API 地址 | app.js `_getBaseUrl()` | 生产域名，上线前替换 |
| 环境判断 | `wx.getAccountInfoSync().miniProgram.envVersion` | `develop`=开发/体验版，其余=正式版 |

**Storage 持久化**

- `hikari_token`：跨启动保持登录态，避免每次冷启动都走 wx.login
- `hikari_uid`：传给后端 `/login` 接口，后端用于查找历史用户文档
- `currentVoice`：声线偏好本地持久化，不依赖服务端
