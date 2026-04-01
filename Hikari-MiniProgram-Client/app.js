App({
  // [Fix-F1] 环境配置——上线前将 isDev 改为 false
  // 或根据 wx.getAccountInfoSync().miniProgram.envVersion 自动判断
  _getBaseUrl: function() {
    const env = wx.getAccountInfoSync().miniProgram.envVersion;
    if (env === 'develop') {
      // 真机调试需要 HTTPS，局域网 HTTP 无法加载图片
      // 如需本地调试改回 http://192.168.31.150
      return 'http://192.168.31.150';
    }
    return 'https://poster-favour-alternate-poultry.trycloudflare.com'; // 正式版：替换为真实域名
  },
  onLaunch() {
    wx.onNeedPrivacyAuthorization(resolve => { /* 留空——微信自动弹出官方隐私弹窗 */ });
    console.log("🚀 [System] v7.1.2 物理直连纯净版启动...");
    this.globalData.BASE_URL = this._getBaseUrl();
    this.checkSession();
  },

  onShow() {
    console.log("🔄 [Lifecycle] 捕获到热启动，检查连接状态...");
    if (this.globalData.token && this.globalData.sessionId) {
      if (this.globalData.socketStatus === 'closed') {
        this.connectWebSocket();
      }
    }
  },

  globalData: {
    BASE_URL: '',
    API_PREFIX: '/api/u',

    userInfo: null,
    token: wx.getStorageSync('hikari_token') || null,
    refreshToken: wx.getStorageSync('hikari_refresh_token') || null,
    sessionId: null,
    
    // ★ v9.0 邀请制/VIP 状态，登录后由 silentLogin 写入
    // inviteOnly 默认 false：热启动极速兜底分支可能在 silentLogin 回调前触发
    // 若默认 true 会导致已登录用户热启动时被误判跳到邀请码页
    hasAccess: wx.getStorageSync('hikari_has_access') === '1',
    inviteOnly: false,
    vipLevel: 0,
    isVip: false,
    msgRemaining: -1,   // -1=不限（VIP），>=0=今日剩余次数
    dailyLimit: 100,
    // ★ v9.1 管理角色：null=无权限，"admin"=管理员，"store_clerk"=店员
    // 只用于前端决定是否渲染管理入口，权限校验全在后端
    manageRole: null,
    // ★ v9.1 到店宽限期到期 UTC 时间戳（秒），null=无宽限期
    // 用于 invite 页显示"宽限期已过期，请联系管理员"
    visitedExpiresAt: null,

    socketStatus: 'closed',
    chatContext: null,
    userAvatarPath: wx.getStorageSync('userAvatarPath') || '',
    _authCallbacks: []
  },

  // UTC 时间字符串 → 本地时间显示
  formatMsgTime: function(utcStr) {
    if (!utcStr) return '';
    const d = new Date(utcStr.endsWith('Z') ? utcStr : utcStr + 'Z');
    if (isNaN(d.getTime())) return '';
    const now = new Date();
    const pad = n => String(n).padStart(2, '0');
    const hhmm = `${pad(d.getHours())}:${pad(d.getMinutes())}`;
    if (d.toDateString() === now.toDateString()) return hhmm;
    if (d.getFullYear() === now.getFullYear()) {
      return `${pad(d.getMonth() + 1)}/${pad(d.getDate())} ${hhmm}`;
    }
    return `${d.getFullYear()}/${pad(d.getMonth() + 1)}/${pad(d.getDate())}`;
  },

  onAuthReady: function(cb) {
    if (this.globalData.token && this.globalData.userInfo) {
      // [Fix-F4/F6] 统一走异步，避免调用方因同步/异步不确定而加 if 兜底导致双触发
      // setTimeout(0) 延迟约 0-4ms，用户完全无感知
      setTimeout(() => cb(this.globalData.userInfo), 0);
    } else {
      this.globalData._authCallbacks.push(cb);
    }
  },

  request: function(endpoint, method = 'POST', data = {}, isRetry = false) {
    const url = `${this.globalData.BASE_URL}${this.globalData.API_PREFIX}${endpoint}`;

    return new Promise((resolve, reject) => {
      wx.request({
        url: url,
        method: method,
        data: data,
        header: {
          'Content-Type': 'application/json',
          'Authorization': this.globalData.token ? `Bearer ${this.globalData.token}` : ''
        },
        success: (res) => {
          if (res.statusCode === 401 && !this._isRefreshing && !isRetry) {
            this._refreshAndRetry(endpoint, method, data).then(resolve).catch(reject);
          } else if (res.statusCode === 401) {
            reject({ code: 401 });
          } else if (res.statusCode === 200) {
            resolve(res.data);
          } else {
            reject(res);
          }
        },
        fail: (err) => reject(err)
      });
    });
  },

  _refreshAndRetry: function(endpoint, method, data) {
    if (this._isRefreshing) {
      return new Promise((resolve, reject) => {
        this._refreshQueue = this._refreshQueue || [];
        this._refreshQueue.push({ endpoint, method, data, resolve, reject });
      });
    }
    this._isRefreshing = true;
    const refreshToken = this.globalData.refreshToken;
    if (!refreshToken) {
      this._isRefreshing = false;
      return Promise.reject({ code: 401 });
    }
    return new Promise((resolve, reject) => {
      wx.request({
        url: `${this.globalData.BASE_URL}/api/u/auth/refresh`,
        method: 'POST',
        data: { refresh_token: refreshToken },
        header: { 'Content-Type': 'application/json' },
        success: (res) => {
          this._isRefreshing = false;
          if (res.statusCode === 200 && res.data && res.data.code === 200) {
            const newToken = res.data.data.access_token;
            this.globalData.token = newToken;
            wx.setStorageSync('hikari_token', newToken);
            // 重发原请求
            (this._refreshQueue || []).forEach(q => {
              this.request(q.endpoint, q.method, q.data, true).then(q.resolve).catch(q.reject);
            });
            this._refreshQueue = [];
            this.request(endpoint, method, data, true).then(resolve).catch(reject);
          } else {
            this._refreshQueue = [];
            wx.redirectTo({ url: '/pages/index/index' });
            reject({ code: 401 });
          }
        },
        fail: () => {
          this._isRefreshing = false;
          this._refreshQueue = [];
          reject({ code: -1 });
        }
      });
    });
  },

  checkSession() {
    this.silentLogin();
  },

  silentLogin() {
    const cachedUid = wx.getStorageSync('hikari_uid');

    wx.login({
      success: (res) => {
        if (!res.code) return;

        this.request('/auth/login', 'POST', { 
          code: res.code,
          user_id: cachedUid 
        }).then(response => {
          if (response.code === 200) {
            const d = response.data;
            
            this.globalData.userInfo = d;
            this.globalData.token = d.access_token;
            this.globalData.refreshToken = d.refresh_token;
            this.globalData.sessionId = d.session_id;

            wx.setStorageSync('hikari_uid', d.user_id);
            wx.setStorageSync('hikari_token', d.access_token);
            wx.setStorageSync('hikari_refresh_token', d.refresh_token);
            wx.setStorageSync('hikari_has_access', d.has_access ? '1' : '0');

            // ★ v9.0 写入邀请制/VIP 状态到 globalData
            this.globalData.hasAccess  = d.has_access  ?? true;
            this.globalData.inviteOnly = d.invite_only ?? false;
            this.globalData.vipLevel   = d.vip_level   ?? 0;
            this.globalData.isVip      = (d.vip_level ?? 0) > 0;

            // ★ v9.1 写入管理角色和宽限期
            this.globalData.manageRole       = d.manage_role        ?? null;
            this.globalData.visitedExpiresAt = d.visited_expires_at ?? null;

            console.log(`✅ [Identity] 身份锚定成功：${d.user_name} | access=${d.has_access} | vip=${d.vip_level} | role=${d.manage_role ?? 'user'}`);

            // ★ v9.0/v9.1 邀请制跳转判断
            // invite_only=true 且 has_access=false → 跳邀请码页，不进聊天室，不建 WS
            // 注意：管理角色和宽限期用户的 has_access 由后端直接返回 true，无需前端额外判断
            if (d.invite_only && !d.has_access) {
              console.log("🔒 [Invite] 未激活邀请码，跳转激活页");
              this.globalData._authCallbacks.forEach(cb => cb(d));
              this.globalData._authCallbacks = [];
              return; // 不 connectWebSocket，不进聊天室
            }

            // 正常流程：建立 WS，通知各页面
            this.connectWebSocket();
            this.globalData._authCallbacks.forEach(cb => cb(d));
            this.globalData._authCallbacks = [];
          }
        }).catch(err => {
          console.error("❌ [Login-Failed] 物理链路中断:", err);
        });
      }
    });
  },

  connectWebSocket() {
    const { token, sessionId, BASE_URL, API_PREFIX } = this.globalData;
    if (!token || !sessionId || this.globalData.socketStatus === 'connected') return;

    this.globalData.socketStatus = 'connecting';
    
    const wsBaseUrl = BASE_URL.replace(/^http/, 'ws');
    const wsUrl = `${wsBaseUrl}${API_PREFIX}/ws/${sessionId}?token=${token}`;

    this.socketTask = wx.connectSocket({ url: wsUrl });

    this.socketTask.onOpen(() => {
      this.globalData.socketStatus = 'connected';
      console.log("✨ [WS] 神经链路已连通");
      if (this.heartTimer) clearInterval(this.heartTimer);
      this.heartTimer = setInterval(() => {
        this.sendSocketMessage({ action: 'ping' });
      }, 25000);
    });

    this.socketTask.onMessage((res) => {
      console.log('[WS-Recv]', res.data); 
      let payload = res.data;
      try {
        while (typeof payload === 'string') {
          const parsed = JSON.parse(payload);
          if (typeof parsed !== 'object' || parsed === null) {
            payload = parsed; break; 
          }
          payload = parsed;
        }
        
        if (payload.action === 'pong') return;
        
        if (payload.type === 'system' && payload.action === 'update_profile') {
          console.log(`👑 [WS] 接收到大模型赐名圣旨，新名字：${payload.user_name}`);
          
          if (this.globalData.userInfo) {
            this.globalData.userInfo.user_name = payload.user_name;
          }
          
          if (this.globalData.chatContext && typeof this.globalData.chatContext.onProfileUpdate === 'function') {
            this.globalData.chatContext.onProfileUpdate(payload.user_name);
          }
          return; 
        }

        if (this.globalData.chatContext && typeof this.globalData.chatContext.handleReceiveMessage === 'function') {
          this.globalData.chatContext.handleReceiveMessage(payload);
        }
      } catch (e) {
        console.warn("⚠️ [WS] 报文格式非标");
      }
    });

    this.socketTask.onClose(() => {
      this.globalData.socketStatus = 'closed';
      if (this.heartTimer) { clearInterval(this.heartTimer); this.heartTimer = null; }
      const ctx = this.globalData.chatContext;
      const needsRefresh = ctx && ctx.data && (
        ctx.data.isGenerating ||
        ctx.data.isMemoirProcessing ||
        (ctx.data.messages || []).some(m => m.role === 'assistant' && m.voice_status === 'loading')
      );
      if (needsRefresh && ctx && typeof ctx.loadHistory === 'function') {
        setTimeout(() => { this.globalData.chatContext.loadHistory(true); }, 500);
      }
      setTimeout(() => {
        if (this.globalData.token && this.globalData.sessionId) {
          this.connectWebSocket();
        }
      }, 5000);
    });
  },

  sendSocketMessage(data) {
    if (this.globalData.socketStatus === 'connected' && this.socketTask) {
      this.socketTask.send({ data: JSON.stringify(data) });
    }
  }
})
