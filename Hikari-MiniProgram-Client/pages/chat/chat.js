/**
 * 希卡莉喵 前端核心逻辑 (v7.0.2 物理装甲修复版 + v9.0 邀请制/VIP)
 * =====================================================================
 * 物理执行准则 [cite: 2026-03-09]：
 * 1. 防洪装甲：维持 isFetchingHistory 物理锁，阻断并发导致的管道瘫痪。
 * 2. 消息防吞：【核心修复】重构 loadHistory 合并算法，强制保留本地 u_ 气泡直到后端确认。
 * 3. 绿灯常驻：【核心修复】剥离心情更新门槛，实现心情状态与好感度分数的解耦同步。
 * 4. 生产自适应：维持局域网 127.0.0.1 映射，支持线上/线下无缝物理对位。
 * 5. 全量交付：禁止省略，确保所有跑通功能（10MB/灵魂画像/语音）全量通电。
 *
 * v9.0 新增：
 * - handleReceiveMessage 加 error type 分支，处理 no_access/vip_required/daily_limit
 *   error 到达时必须解锁 isGenerating 和 stopPolling，否则发送按钮永久锁死
 * - initPageData 里调一次 /vip/status，把剩余次数写入 data，供顶栏展示
 * - data 里加 isVip / msgRemaining / dailyLimit 三个字段驱动 WXML
 * =====================================================================
 */

const app = getApp();

// [Fix-F9] 统一 URL 处理：相对路径补全 BASE_URL，绝对路径直接返回
function resolveUrl(url) {
  if (!url) return '';
  if (url.startsWith('http')) return url;
  return `${app.globalData.BASE_URL}${url}`;
}

// 真机兼容：下载图片到本地临时路径，解决 <image> 组件无法显示外部 URL 的问题
function downloadToLocal(url) {
  return new Promise((resolve) => {
    if (!url) { resolve(''); return; }
    // 已是本地路径直接返回
    if (url.startsWith('wxfile://') || url.startsWith('http://tmp')) {
      resolve(url); return;
    }
    wx.downloadFile({
      url: url,
      header: {
        'Authorization': app.globalData.token ? `Bearer ${app.globalData.token}` : ''
      },
      success: (res) => {
        console.log('[IMG] download status:', res.statusCode, url.substring(0, 60));
        if (res.statusCode === 200) resolve(res.tempFilePath);
        else resolve(url);
      },
      fail: (err) => {
        console.log('[IMG] download fail:', err, url.substring(0, 60));
        resolve(url);
      }
    });
  });
}

Page({
  data: {
    messages: [],
    inputValue: '',
    scrollToView: '',
    
    // 物理状态属性
    affinity: 10,
    mood: 'neutral',
    moodText: '平静',
    userInfo: null,
    
    // 视觉资产状态机
    tempImagePath: '',    
    currentImageId: '',   
    isUploading: false,   
    // 多图状态（文案功能）
    pendingImageIds: [],
    pendingImagePaths: [],
    uploadProgress: '',
    
    // 语音配置
    voiceLibrary: ['女性1_zh'], 
    currentVoice: '女性1_zh',
    voiceIndex: 0,
    
    // 引擎运行状态
    isReady: false, 
    isGenerating: false,
    // isFetchingHistory 不放 data，改用实例变量 this._isFetchingHistory，不触发渲染
    
    // 灵魂画像状态机
    isMemoirProcessing: false,
    memoirCooldown: 0, 
    
    playingMsgId: null,
    isUserScrollingUp: false, 
    
    // 计时器矩阵
    pollingTimer: null,
    cdTimer: null, 
    expectedReportCount: 0,
    lastSendTime: 0,
    enableSearch: false,
    hasMoreHistory: false,
    isLoadingMore: false,
    oldestMsgId: null,
    isInstore: false,

    // ★ v9.0 邀请制/VIP 状态
    isVip: false,
    msgRemaining: -1,       // -1=不限（VIP），>=0=今日剩余次数
    dailyLimit: 100,
    imageGenQuota: -1,      // 图生图剩余配额，-1=不受限（VIP_LOCK_IMAGE_GEN=false），>=0=剩余次数

    // ★ v9.1 管理角色（仅控制 UI 显示，后端独立鉴权）
    // null=无权限（不显示按钮），"admin"=管理员，"store_clerk"=店员
    manageRole: null,
  },

  // ==========================================
  // 🚀 [生命周期：物理初始化]
  // ==========================================

  onLoad: function() {
    this.audioCtx = wx.createInnerAudioContext();
    this.audioCtx.onEnded(() => this.setData({ playingMsgId: null }));
    this.audioCtx.onError(() => this.setData({ playingMsgId: null }));
    
    app.globalData.chatContext = this; 

    // [Fix-F4/F6] onAuthReady 已统一为异步，单一注册即可，无需 if 兜底
    app.onAuthReady(() => { this.initPageData(); });
  },

  onShow: function() {
    app.globalData.chatContext = this;
    const latestAvatar = app.globalData.userAvatarPath || wx.getStorageSync('userAvatarPath') || '';
    const syncCd = (this._memoirCdValue > 0) ? this._memoirCdValue : this.data.memoirCooldown;
    const stillProcessing = this.data.isMemoirProcessing;
    this.setData({ isUploading: false, uploadProgress: '', userAvatarPath: latestAvatar, memoirCooldown: syncCd, isMemoirProcessing: false });
    if (stillProcessing) setTimeout(() => this.setData({ isMemoirProcessing: true }), 50);
    if (this.data.isReady) {
      const needsRefresh = this.data.isGenerating || stillProcessing ||
        this.data.messages.some(m => m.role === 'assistant' && m.voice_status === 'loading');
      if (needsRefresh) {
        this.loadHistory(true, () => { this.checkAndStartPollingIfNeeded(); });
      }
    }
    this.syncInstoreStatus();
  },

  syncInstoreStatus: function() {
    if (!app.globalData.token) return;
    wx.request({
      url: `${app.globalData.BASE_URL}/api/s/instore`,
      method: 'GET',
      header: { 'Authorization': `Bearer ${app.globalData.token}` },
      success: (res) => {
        if (res.statusCode === 200 && res.data && res.data.code === 200) {
          this.setData({ isInstore: res.data.data.is_instore || false });
        }
      },
      fail: () => {}
    });
  },

  doCheckout: function() {
    wx.showModal({
      title: '确认离店',
      content: '确定要离店吗？希卡莉会想你的喵～',
      confirmText: '离店',
      cancelText: '再待会',
      confirmColor: '#EF4444',
      success: (res) => {
        if (!res.confirm) return;
        wx.request({
          url: `${app.globalData.BASE_URL}/api/s/checkout`,
          method: 'POST',
          header: {
            'Content-Type': 'application/json',
            'Authorization': `Bearer ${app.globalData.token}`
          },
          success: (r) => {
            if (r.statusCode === 200) {
              this.setData({ isInstore: false });
              wx.showToast({ title: '再见喵～欢迎下次再来！', icon: 'none', duration: 2000 });
            }
          },
          fail: () => {
            wx.showToast({ title: '网络异常，稍后再试喵', icon: 'none' });
          }
        });
      }
    });
  },

  onUnload: function() {
    this.stopPolling();
    this.stopCDTimer();
    if (this.audioCtx) this.audioCtx.destroy();
    if (app.globalData.chatContext === this) { app.globalData.chatContext = null; }
  },

  initPageData: function() {
    if (this.data.isReady) return;
    const cachedVoice  = wx.getStorageSync('currentVoice');
    const userAvatar   = app.globalData.userAvatarPath || wx.getStorageSync('userAvatarPath') || '';
    const initAffinity = parseInt(app.globalData.userInfo?.affinity);
    const safeAffinity = (!isNaN(initAffinity) && initAffinity >= 0) ? initAffinity : 10;
    
    this.setData({
      isReady: true,
      userInfo: app.globalData.userInfo,
      userAvatarPath: userAvatar,
      affinity: safeAffinity,
      currentVoice: cachedVoice || '女性1_zh',
      isUploading: false,
      isGenerating: false,
      tempImagePath: '',
      currentImageId: '',
      pendingImageIds: [],
      pendingImagePaths: [],
      uploadProgress: '',
      // ★ v9.0 从 globalData 读入登录时已拿到的 VIP 状态（避免多发一次请求）
      isVip: app.globalData.isVip || false,
      // ★ v9.1 管理角色：只用于显示/隐藏管理入口，安全由后端保证
      manageRole: app.globalData.manageRole || null,
    });
    
    this.loadHistory();
    this.loadVoiceLibrary();
    this.syncMemoirCooldown();
    // ★ v9.0 拉一次最新的 vip/status，同步今日剩余次数
    this.syncVipStatus();
  },

  // ==========================================
  // ★ v9.0 VIP 状态同步
  // ==========================================

  syncVipStatus: function() {
    app.request('/vip', 'GET').then(res => {
      if (res && res.code === 200) {
        const d = res.data;
        app.globalData.isVip        = (d.vip_level ?? 0) > 0;
        app.globalData.vipLevel     = d.vip_level;
        app.globalData.msgRemaining = d.msg_remaining;
        this.setData({
          isVip:         (d.vip_level ?? 0) > 0,
          msgRemaining:  d.msg_remaining,
          imageGenQuota: d.image_gen_quota_remaining ?? -1,
        });
      }
    }).catch(() => {});
  },

  // ==========================================
  // 🚀 [视觉管线：物理采集与报错穿透]
  // ==========================================

  chooseImage: function() {
    if (this.data.isUploading || this.data.isGenerating) return;
    this._doChooseImage();
  },

  _doChooseImage: function() {
    wx.chooseMedia({
      count: 9,
      mediaType: ['image'],
      sourceType: ['album'],
      success: (res) => {
        const files = res.tempFiles;

        const MAX_SIZE = 10 * 1024 * 1024;
        const oversized = files.find(f => f.size > MAX_SIZE);
        if (oversized) {
          wx.showModal({
            title: '有图太大了喵！',
            content: `有张图超过 10MB，本喵抱不动，换小一点的？`,
            showCancel: false
          });
          return;
        }

        if (files.length === 1 && this.data.pendingImageIds.length === 0) {
          this.setData({ tempImagePath: files[0].tempFilePath });
          this.uploadVisionImage(files[0].tempFilePath);
          return;
        }

        this._uploadMultiImages(files);
      },
      fail: (err) => {
        if (err.errno === 103 || err.errno === 104) {
          wx.showToast({ title: '需要同意隐私协议才能上传图片', icon: 'none', duration: 2000 });
        }
      }
    });
  },

  _uploadMultiImages: function(files) {
    const total = files.length;
    let done = 0;
    let failed = 0;
    const newIds = [...this.data.pendingImageIds];
    const newPaths = [...this.data.pendingImagePaths];

    this.setData({ isUploading: true, uploadProgress: `上传中 0/${total}` });

    files.forEach(file => {
      wx.uploadFile({
        url: `${app.globalData.BASE_URL}/api/u/images`,
        filePath: file.tempFilePath,
        name: 'image',
        header: { 'Authorization': `Bearer ${app.globalData.token}` },
        success: (res) => {
          try {
            const data = JSON.parse(res.data);
            if (res.statusCode === 200 && data.code === 200) {
              newIds.push(data.data.image_id);
              newPaths.push(file.tempFilePath);
            } else {
              failed++;
            }
          } catch(e) { failed++; }
        },
        fail: () => { failed++; },
        complete: () => {
          done++;
          this.setData({ uploadProgress: `上传中 ${done}/${total}` });
          if (done === total) {
            this.setData({
              isUploading: false,
              uploadProgress: '',
              pendingImageIds: newIds,
              pendingImagePaths: newPaths,
              tempImagePath: newPaths[0] || '',
              currentImageId: newIds[0] || ''
            });
            if (failed > 0) {
              wx.showToast({ title: `${failed} 张上传失败了喵`, icon: 'none' });
            }
          }
        }
      });
    });
  },

  uploadVisionImage: function(path) {
    this.setData({ isUploading: true });
    wx.showLoading({ title: '本喵看看这是什么…' });

    wx.uploadFile({
      url: `${app.globalData.BASE_URL}/api/u/images`,
      filePath: path,
      name: 'image',
      header: { 'Authorization': `Bearer ${app.globalData.token}` },
      success: (res) => {
        try {
          const data = JSON.parse(res.data);
          if (res.statusCode === 200 && data.code === 200) {
            this.setData({ currentImageId: data.data.image_id });
          } else {
            this.setData({ tempImagePath: '', currentImageId: '' });
            wx.showModal({ title: '识别失败喵', content: data.msg || '这张图本喵看不太懂……换一张试试？', showCancel: false });
          }
        } catch(e) {
          this.setData({ tempImagePath: '', currentImageId: '' });
        }
      },
      fail: () => {
        this.setData({ tempImagePath: '', currentImageId: '' });
        wx.showToast({ title: '网络断了喵，稍后再试~', icon: 'none' });
      },
      complete: () => {
        this.setData({ isUploading: false });
        wx.hideLoading();
      }
    });
  },

  removeImage: function() {
    this.setData({
      tempImagePath: '',
      currentImageId: '',
      pendingImageIds: [],
      pendingImagePaths: [],
      uploadProgress: ''
    });
  },

  // ==========================================
  // 🚀 [业务核心：多模态对话管线]
  // ==========================================

  generateMemoir: function() {
    if (this.data.isMemoirProcessing || this.data.memoirCooldown > 0) return;
    this.setData({ isMemoirProcessing: true, expectedReportCount: this.data.messages.filter(m => m.is_report).length + 1 });
    app.request('/memoirs/export', 'POST').then(res => {
      if (res && res.code === 200) {
        wx.showToast({ title: '本喵正在认真观察你…', icon: 'none' });
        this.startPolling();
      } else if (res && res.code === 429) {
        this.setData({ isMemoirProcessing: false });
        this.syncMemoirCooldown();
      } else {
        this.setData({ isMemoirProcessing: false });
      }
    }).catch(() => {
      this.setData({ isMemoirProcessing: false });
    });
  },

  syncMemoirCooldown: function() {
    app.request('/memoirs/status', 'GET').then(res => {
      // [Fix-F1] 冷却时间在 res.data.cooldown，原来读 res.cooldown 永远是 undefined
      if (res && res.code === 200 && res.data && res.data.cooldown > 0) {
        this.startMemoirCooldown(res.data.cooldown);
      }
    }).catch(() => {});
  },

  sendMessage: function() {
    const now = Date.now();
    const text = this.data.inputValue.trim();
    const imageId = this.data.currentImageId;
    
    if (!this.data.isReady) {
      wx.showToast({ title: '还没连上喵，等一下再说话~', icon: 'none' });
      return;
    }

    if ((!text && !imageId && this.data.pendingImageIds.length === 0) || this.data.isGenerating || this.data.isUploading) return;
    
    if (now - this.data.lastSendTime < 1200) {
      wx.showToast({ title: '慢点喵，本喵还没反应过来~', icon: 'none' });
      return;
    }

    const cleanText = text.replace(/<[^>]*>?/gm, '');
    const clientMsgId = `c_${now}_${Math.random().toString(36).slice(2,7)}`;

    const imageIds = this.data.pendingImageIds.length > 0
      ? this.data.pendingImageIds
      : (imageId ? [imageId] : []);

    const newMessages = this.data.messages.concat([{
      id: clientMsgId,
      role: 'user',
      content: cleanText,
      image_id: imageId,
      temp_image_url: this.data.tempImagePath,
      temp_image_count: imageIds.length > 1 ? imageIds.length : 0,
      client_msg_id: clientMsgId
    }]);

    this.setData({
      messages: newMessages,
      inputValue: '',
      tempImagePath: '',
      currentImageId: '',
      pendingImageIds: [],
      pendingImagePaths: [],
      isGenerating: true,
      isUserScrollingUp: false,
      lastSendTime: now,
      scrollToView: `msg_${newMessages.length - 1}`,
    });

    app.sendSocketMessage({
      action: 'chat',
      message: cleanText,
      image_id: imageIds.length === 0 ? (imageId || undefined) : undefined,
      image_ids: imageIds.length > 0 ? imageIds : undefined,
      enable_search: this.data.enableSearch,
      client_msg_id: clientMsgId
    });

    this.startPolling();
  },

  handleReceiveMessage: function(payload) {
    this.syncNeuralState(payload);

    if (payload.type === 'system') return;

    // typing 事件：插入临时"正在输入"气泡
    if (payload.type === 'typing') {
      this._showTypingBubble();
      return;
    }

    // memoir_ready：报告生成完成，立即拉取最新历史显示报告气泡
    if (payload.type === 'memoir_ready') {
      this.loadHistory(false);
      return;
    }

    // ★ v9.0 error 分支：必须先解锁 isGenerating 和停轮询，否则发送按钮永久锁死
    if (payload.type === 'error') {
      this._removeTypingBubble();
      // 无论什么 error_code，先把状态机复位
      this.setData({ isGenerating: false });
      this.stopPolling();

      const code = payload.error_code;
      if (code === 'no_access') {
        // 邀请码失效或被踢出，跳回邀请码页
        wx.showModal({
          title: '需要邀请码',
          content: payload.content || '需要邀请码才能使用喵',
          showCancel: false,
          confirmText: '去激活',
          success: () => {
            wx.redirectTo({ url: '/pages/invite/invite' });
          }
        });
      } else if (code === 'vip_required') {
        // VIP 专属功能，弹提示即可，不跳页
        wx.showModal({
          title: '✨ VIP 专属功能',
          content: payload.content || '此功能需要 VIP 才能使用喵',
          showCancel: false,
          confirmText: '知道了'
        });
      } else if (code === 'quota_exceeded') {
        // 图生图配额用完，弹提示并刷新剩余次数显示
        wx.showModal({
          title: '图生图次数已用完',
          content: payload.content || '购买套餐可以补充次数哦～',
          showCancel: true,
          cancelText: '知道了',
          confirmText: '去商城',
          success: (modalRes) => {
            if (modalRes.confirm) this.goShop();
          }
        });
        this.syncVipStatus();  // 刷新剩余次数显示
      } else if (code === 'daily_limit') {
        // 今日次数用完，弹提示并从服务端重新拉一次次数，避免本地和服务端不一致
        wx.showModal({
          title: '今日次数已用完',
          content: payload.content || '明天再来喵～',
          showCancel: false,
          confirmText: '知道了'
        });
        this.syncVipStatus();
      } else if (code === 'visit_expired') {
        // [Fix-F2] 宽限期到期：跳转邀请码页并传入 reason，触发到期专属 UI
        wx.redirectTo({ url: '/pages/invite/invite?reason=visit_expired' });
      } else {
        // 兜底：其他未知错误用 Toast 提示
        wx.showToast({ title: payload.content || '出错了喵，稍后再试', icon: 'none', duration: 2500 });
      }
      return;
    }

    if (payload.type === 'text' || payload.reply_text) {
      const vsFromServer = (payload.voice_status != null) ? payload.voice_status : 'loading';
      const fullContent  = payload.content || payload.reply_text || '';
      const msgId        = payload.msg_id || `bot_${Date.now()}`;

      // 把 typing 气泡原地升级为正式消息气泡（id 替换 + is_typing 关闭）
      const typingIdx = this.data.messages.findIndex(m => m.id === '__typing__');
      if (typingIdx !== -1) {
        const firstChar = fullContent.length > 0 ? fullContent[0] : ' ';
        const pendingStickerUrl    = payload.sticker_url || '';
        const pendingGenImgStatus  = payload.gen_pending ? 'loading' : (payload.gen_image_status || '');
        this.setData({
          [`messages[${typingIdx}].id`]:               msgId,
          [`messages[${typingIdx}].is_typing`]:         false,
          [`messages[${typingIdx}].content`]:           firstChar,
          [`messages[${typingIdx}].voice_status`]:      'none',
          [`messages[${typingIdx}].gen_image_status`]:  '',
          [`messages[${typingIdx}].char_text`]:         payload.char_text || '',
          [`messages[${typingIdx}].sticker_url`]:       '',
          scrollToView:                                 `msg_${typingIdx}`,
          isGenerating:                                 !!payload.gen_pending,
          ...(!this.data.isVip && this.data.msgRemaining > 0 ? { msgRemaining: this.data.msgRemaining - 1 } : {}),
        }, () => {
          this._typewriterAnimate(msgId, fullContent, 1, () => {
            const idx2 = this.data.messages.findIndex(m => m.id === msgId);
            if (idx2 !== -1) {
              const patch = {};
              // 打字机结束后一次性写入所有会撑高气泡的字段
              patch[`messages[${idx2}].voice_status`] = vsFromServer;
              if (pendingStickerUrl)   patch[`messages[${idx2}].sticker_url`]     = pendingStickerUrl;
              if (pendingGenImgStatus) patch[`messages[${idx2}].gen_image_status`] = pendingGenImgStatus;
              this.setData(patch);
            }
          });
        });
      } else {
        // 没有 typing 气泡（断线重连等情况），退回插入新气泡
        this.pushMessage({
          id: msgId, role: 'assistant', content: '',
          voice_status: vsFromServer,
          gen_image_status: payload.gen_pending ? 'loading' : (payload.gen_image_status || ''),
          char_text: payload.char_text || '', sticker_url: payload.sticker_url || '',
        });
        this._typewriterAnimate(msgId, fullContent, 0);
        if (!payload.gen_pending) this.setData({ isGenerating: false });
      }

      if (vsFromServer === 'none' && !payload.gen_pending) {
        this.stopPolling();
      }
    }
    else if (payload.type === 'voice') {
      const vs = (payload.voice_status != null) ? payload.voice_status : 'ready';
      this.applyVoiceToMessage(payload.msg_id, payload.voice_url, vs);
      this.setData({ isGenerating: false });
      this.stopPolling();
    }
    else if (payload.type === 'image_gen_result') {
      this.setData({ isGenerating: false });
      this.stopPolling();
      this.loadHistory(false);
      this.syncVipStatus();
    }
    else if (payload.type === 'copywriting_result') {
      this.setData({ isGenerating: false });
      this.stopPolling();
      if (payload.success) {
        this.loadHistory(false);
      }
    }
    else if (payload.type === 'makeup_result') {
      this.setData({ isGenerating: false });
      this.stopPolling();
      if (payload.success) {
        this.loadHistory(false);
      }
    }
  },

  // ==========================================
  // 🚀 [资产打捞：全量自愈防洪逻辑]
  // ==========================================

  loadMoreHistory: function() {
    if (!this.data.hasMoreHistory || this.data.isLoadingMore || !this.data.oldestMsgId) return;
    this.setData({ isLoadingMore: true });

    app.request(`/messages?before_id=${this.data.oldestMsgId}&limit=50`, 'GET').then(res => {
      this.setData({ isLoadingMore: false });
      if (res && res.code === 200) {
        const olderMsgs = (res.data.list || []).map(m => {
          const genImageUrl = m.gen_image_url ? resolveUrl(m.gen_image_url) : '';
          const effectiveImageId = m.image_id || (m.image_ids && m.image_ids[0]) || '';
          let imageUrl = '';
          if (effectiveImageId) {
            imageUrl = resolveUrl(m.image_url || `/api/u/images/${app.globalData.userInfo.user_id}/${effectiveImageId}.jpg`);
          }
          const olderMsgCreatedAt = m.created_at ? new Date(m.created_at).getTime() : 0;
          const isOlderVoiceStale = !m.voice_url && m.voice_status === 'loading' &&
            olderMsgCreatedAt && (Date.now() - olderMsgCreatedAt > 3 * 60 * 1000);
          const olderResolvedVoiceStatus = m.voice_url ? 'ready'
            : isOlderVoiceStale ? 'none'
            : (m.voice_status || (m.role === 'assistant' ? 'loading' : 'none'));
          return {
            id: m.id || m.msg_id,
            role: m.role === 'user' ? 'user' : 'assistant',
            content: m.content || '',
            image_id: m.image_id || '',
            image_url: imageUrl,
            voice_url: m.voice_url || '',
            voice_status: olderResolvedVoiceStatus,
            is_report: !!(m.is_report || m.report_url),
            report_url: m.report_url || '',
            gen_image_url: genImageUrl,
            gen_image_status: m.gen_image_status || (genImageUrl ? 'ready' : ''),
            char_text: m.char_text || '',
            sticker_url: m.sticker_url || ''
          };
        });

        const existingIds = new Set(this.data.messages.map(m => m.id));
        const newOlder = olderMsgs.filter(m => !existingIds.has(m.id));

        if (newOlder.length > 0) {
          const merged = [...newOlder, ...this.data.messages];
          this.setData({
            messages: merged,
            hasMoreHistory: res.data.has_more || false,
            oldestMsgId: merged[0].id,
            scrollToView: `msg_${newOlder.length}`
          });
        }
      }
    }).catch(() => { this.setData({ isLoadingMore: false }); });
  },

  loadHistory: function(isSilent = false, onDone = null) {
    if (this._isFetchingHistory) return;
    this._isFetchingHistory = true;

    app.request('/messages?limit=50', 'GET').then(res => {
      this._isFetchingHistory = false;

      if (res && res.code === 200) {
        const serverHistory = (res.data.list || []).map(m => {
          const effectiveImageId = m.image_id || (m.image_ids && m.image_ids[0]) || '';
          let imageUrl = '';
          if (effectiveImageId) {
            imageUrl = resolveUrl(m.image_url || `/api/u/images/${app.globalData.userInfo.user_id}/${effectiveImageId}.jpg`);
          }
          const genImageUrl = m.gen_image_url ? resolveUrl(m.gen_image_url) : '';
          const msgCreatedAt = m.created_at ? new Date(m.created_at).getTime() : 0;
          // 语音状态：有 url 就是 ready；没有 url 且服务端是 loading 时，
          // 超过 3 分钟的消息强制改成 none（语音生成早已结束，前端没收到回调），
          // 防止历史消息中卡住的 loading 状态永久触发轮询
          const isVoiceStale = !m.voice_url && m.voice_status === 'loading' &&
            msgCreatedAt && (Date.now() - msgCreatedAt > 3 * 60 * 1000);
          const resolvedVoiceStatus = m.voice_url ? 'ready'
            : isVoiceStale ? 'none'
            : (m.voice_status || (m.role === 'assistant' ? 'loading' : 'none'));
          return {
            id: m.id || m.msg_id,
            role: m.role === 'user' ? 'user' : 'assistant',
            content: m.content || '',
            image_id: m.image_id || '',
            image_url: imageUrl, 
            voice_url: m.voice_url || '',
            voice_status: resolvedVoiceStatus,
            is_report: !!(m.is_report || m.report_url),
            report_url: m.report_url || '',
            gen_image_url: genImageUrl,
            gen_image_status: m.gen_image_status || (genImageUrl ? 'ready' : ''),
            char_text: m.char_text || '',
            client_msg_id: m.client_msg_id || '',
            created_at: m.created_at || '',
            display_time: app.formatMsgTime(m.created_at || ''),
            sticker_url: m.sticker_url || ''
          };
        });

        serverHistory.forEach(sm => {
          const existing = this.data.messages.find(m => m.id === sm.id);
          if (!sm.sticker_url && existing && existing.sticker_url) {
            sm.sticker_url = existing.sticker_url;
          }
        });

        const localMsgs = this.data.messages.filter(m => String(m.id).startsWith('c_'));

        serverHistory.forEach(sm => {
          if (sm.role !== 'user') return;
          const match = localMsgs.find(lm => lm.client_msg_id && lm.client_msg_id === sm.client_msg_id);
          if (match) {
            sm.temp_image_url = match.temp_image_url || '';
          }
        });

        const pendingLocal = localMsgs.filter(lm => {
          return !serverHistory.some(sm => sm.client_msg_id && sm.client_msg_id === lm.client_msg_id);
        });

        // 保留 typing 气泡（__typing__），避免轮询期间被服务端历史覆盖清掉
        const typingBubble = this.data.messages.find(m => m.id === '__typing__');
        const finalMessages = typingBubble
          ? [...serverHistory, ...pendingLocal, typingBubble]
          : [...serverHistory, ...pendingLocal];
        
        const oldLength = this.data.messages.length;

        // ── 先继承已下载的本地路径，再做 diff ──
        // 避免 image_url 从远程 URL → wxfile 被误判为内容变化
        const prevById = {};
        this.data.messages.forEach(m => { prevById[m.id] = m; });
        finalMessages.forEach(m => {
          const prev = prevById[m.id];
          if (prev) {
            if (prev.image_url && (prev.image_url.startsWith('wxfile://') || prev.image_url.startsWith('http://tmp'))) {
              m.image_url = prev.image_url;
            }
            if (prev.gen_image_url && (prev.gen_image_url.startsWith('wxfile://') || prev.gen_image_url.startsWith('http://tmp'))) {
              m.gen_image_url = prev.gen_image_url;
            }
          }
        });

        // ── messages diff：只有内容真正变化才 setData，避免轮询期间无新消息也触发重绘 ──
        const prevMessages = this.data.messages;
        const hasTyping = !!typingBubble;
        const isSameMessages = (
          finalMessages.length === prevMessages.length &&
          finalMessages.every((m, i) => {
            const p = prevMessages[i];
            return p && m.id === p.id &&
              m.voice_status === p.voice_status &&
              m.gen_image_status === p.gen_image_status &&
              m.voice_url === p.voice_url &&
              m.gen_image_url === p.gen_image_url &&
              m.sticker_url === p.sticker_url &&
              m.content === p.content;
          })
        );
        if (!isSameMessages) {
          if (hasTyping) {
            // typing 气泡存在期间不整体替换 messages，避免触发 scroll-view 重算位置
            // 只更新非 typing 条目中真正变化的字段
            const patch = {};
            finalMessages.forEach((m) => {
              if (m.id === '__typing__') return;
              const pi = prevMessages.findIndex(p => p.id === m.id || (p.client_msg_id && p.client_msg_id === m.client_msg_id));
              if (pi === -1) return;
              const p = prevMessages[pi];
              if (m.voice_status !== p.voice_status)  patch[`messages[${pi}].voice_status`]     = m.voice_status;
              if (m.voice_url !== p.voice_url)         patch[`messages[${pi}].voice_url`]        = m.voice_url;
              if (m.content !== p.content)             patch[`messages[${pi}].content`]          = m.content;
              if (m.sticker_url !== p.sticker_url)     patch[`messages[${pi}].sticker_url`]      = m.sticker_url;
              if (m.gen_image_status !== p.gen_image_status) patch[`messages[${pi}].gen_image_status`] = m.gen_image_status;
              if (m.gen_image_url !== p.gen_image_url) patch[`messages[${pi}].gen_image_url`]   = m.gen_image_url;
              // c_xxx → 服务端 id 确认
              if (m.id !== p.id)                       patch[`messages[${pi}].id`]               = m.id;
            });
            if (Object.keys(patch).length > 0) this.setData(patch);
          } else {
            this.setData({ messages: finalMessages });
          }
        }

        this.syncNeuralState(res.data);

        // ── downloadToLocal 批量合并：只对还没本地化的路径发起下载 ──
        const downloadTasks = [];
        finalMessages.forEach((msg, idx) => {
          if (msg.image_url) console.log('[IMG-URL]', idx, msg.image_url.substring(0, 80));
          if (msg.image_url && !msg.image_url.startsWith('wxfile://') && !msg.image_url.startsWith('http://tmp')) {
            downloadTasks.push(
              downloadToLocal(msg.image_url).then(local => ({ key: `messages[${idx}].image_url`, val: local, orig: msg.image_url }))
            );
          }
          if (msg.gen_image_url && !msg.gen_image_url.startsWith('wxfile://') && !msg.gen_image_url.startsWith('http://tmp')) {
            downloadTasks.push(
              downloadToLocal(msg.gen_image_url).then(local => ({ key: `messages[${idx}].gen_image_url`, val: local, orig: msg.gen_image_url }))
            );
          }
        });
        if (downloadTasks.length > 0) {
          Promise.all(downloadTasks).then(results => {
            const patch = {};
            results.forEach(r => { if (r.val && r.val !== r.orig) patch[r.key] = r.val; });
            if (Object.keys(patch).length > 0) this.setData(patch);
          });
        }

        const newHasMore = res.data.has_more || false;
        const newOldestId = finalMessages.length > 0 ? finalMessages[0].id : null;
        if (newHasMore !== this.data.hasMoreHistory || newOldestId !== this.data.oldestMsgId) {
          this.setData({ hasMoreHistory: newHasMore, oldestMsgId: newOldestId });
        }

        if (finalMessages.length > 0) {
          const lastMsg = finalMessages[finalMessages.length - 1];
          if (lastMsg.role === 'assistant') {
            this.setData({ isGenerating: false });
          } else if (lastMsg.role === 'user') {
            const msgTime = lastMsg.created_at ? new Date(lastMsg.created_at).getTime() : 0;
            const isTimedOut = msgTime && (Date.now() - msgTime > 2 * 60 * 1000);
            if (isTimedOut) {
              this.setData({ isGenerating: false });
            } else {
              // 最后一条是 user 消息且未超时，只在有待生成的图生图气泡时才锁住。
              // 若没有 gen_pending 气泡，说明图生图已结束（成功/失败/被拦截），
              // 维持 handleReceiveMessage 已设置的状态，不强制重新锁住（防频闪）。
              const hasGenPending = finalMessages.some(m => m.gen_image_status === 'loading');
              if (hasGenPending) {
                this.setData({ isGenerating: true });
              }
            }
          }
        }

        if (!this.data.isMemoirProcessing && res.data.memoir_processing) {
          const reportCount = serverHistory.filter(m => m.is_report).length;
          this.setData({ isMemoirProcessing: true, expectedReportCount: reportCount + 1 });
        }

        if (this.data.isMemoirProcessing) {
          const reportCount = serverHistory.filter(m => m.is_report).length;
          if (reportCount >= this.data.expectedReportCount) {
            this.startMemoirCooldown(300);
          }
        }
        
        const hasTypingBubble = finalMessages.some(m => m.id === '__typing__');
        if (!hasTypingBubble && (!isSilent || (!this.data.isUserScrollingUp && !isSameMessages && finalMessages.length > oldLength))) { this.scrollToBottom(); }
        this.checkAndStopPolling();
        if (onDone) onDone();
      }
    }).catch(() => { 
      this._isFetchingHistory = false;
      if (isSilent) this.stopPolling(); 
    });
  },

  // ── typing 气泡辅助方法 ──────────────────────────────────────
  _showTypingBubble: function() {
    if (this.data.messages.some(m => m.id === '__typing__')) return;
    const msgs = this.data.messages.concat([{
      id: '__typing__',
      role: 'assistant',
      content: '',
      is_typing: true,
      voice_status: 'none',
    }]);
    // 不更新 scrollToView：发消息时已滚到底，不需要再触发滚动动画
    this.setData({ messages: msgs });
  },

  _removeTypingBubble: function() {
    const msgs = this.data.messages.filter(m => m.id !== '__typing__');
    if (msgs.length !== this.data.messages.length) {
      this.setData({ messages: msgs });
    }
  },

  // 打字机逐字渲染：content 已在 messages 里，逐步更新 content 字段
  // 每次追加一个字符，间隔 INTERVAL ms；超过 MAX_INSTANT 字时直接显示全文
  _typewriterAnimate: function(msgId, fullText, startIdx, onDone) {
    const INTERVAL    = 30;
    const MAX_INSTANT = 200;

    if (fullText.length > MAX_INSTANT) {
      const idx = this.data.messages.findIndex(m => m.id === msgId);
      if (idx !== -1) this.setData({ [`messages[${idx}].content`]: fullText });
      if (onDone) onDone();
      return;
    }

    if (startIdx >= fullText.length) {
      if (onDone) onDone();
      return;
    }

    const idx = this.data.messages.findIndex(m => m.id === msgId);
    if (idx === -1) return;

    this.setData(
      { [`messages[${idx}].content`]: fullText.slice(0, startIdx + 1) },
      () => {
        setTimeout(() => {
          this._typewriterAnimate(msgId, fullText, startIdx + 1, onDone);
        }, INTERVAL);
      }
    );
  },

  syncNeuralState: function(data) {
    if (!data) return;

    const moodMap = { 'happy': '愉悦', 'angry': '微愠', 'sad': '失落', 'neutral': '平静' };
    const updates = {};

    const score = (data.affinity != null) ? data.affinity : (data.profile && data.profile.affinity);
    if (score != null) {
      const newAffinity = parseInt(score);
      if (!isNaN(newAffinity) && newAffinity !== this.data.affinity) updates.affinity = newAffinity;
    }

    const rawMood = data.mood || (data.profile && data.profile.mood) || 'neutral';
    if (rawMood !== this.data.mood) {
      updates.mood = rawMood;
      updates.moodText = moodMap[rawMood] || '平静';
    }

    const voiceChar = data.profile && data.profile.voice_character;
    if (voiceChar && this.data.voiceLibrary && this.data.voiceLibrary.length > 0) {
      const idx = this.data.voiceLibrary.indexOf(voiceChar);
      if (idx !== -1 && voiceChar !== this.data.currentVoice) {
        updates.currentVoice = voiceChar;
        updates.voiceIndex = idx;
      }
    }

    if (Object.keys(updates).length > 0) this.setData(updates);
  },

  // ==========================================
  // 🚀 [底层辅助组件：全量保留]
  // ==========================================

  pushMessage: function(msg) {
    const list = this.data.messages;
    const existingIdx = list.findIndex(m => m.id === msg.id);
    if (existingIdx !== -1) {
      if (msg.sticker_url && !list[existingIdx].sticker_url) {
        this.setData({ [`messages[${existingIdx}].sticker_url`]: msg.sticker_url });
      }
      return;
    }
    list.push(msg);
    this.setData({ messages: list });
    this.scrollToBottom();
  },

  scrollToBottom: function() {
    this.setData({ scrollToView: `msg_${this.data.messages.length - 1}` });
  },

  previewImage: function(e) {
    const url = e.currentTarget.dataset.url;
    if (url) wx.previewImage({ current: url, urls: [url] });
  },

  saveSticker: function(e) {
    const url = e.currentTarget.dataset.url;
    if (!url) return;
    wx.showActionSheet({
      itemList: ['保存到相册'],
      success: (res) => {
        if (res.tapIndex === 0) {
          wx.authorize({
            scope: 'scope.writePhotosAlbum',
            success: () => { this._doSaveSticker(url); },
            fail: () => {
              wx.showModal({
                title: '需要相册权限',
                content: '保存图片需要访问相册权限，是否前往设置开启？',
                confirmText: '去开启',
                success: (modalRes) => {
                  if (modalRes.confirm) wx.openSetting();
                }
              });
            }
          });
        }
      }
    });
  },

  _doSaveSticker: function(url) {
    wx.downloadFile({
      url: url,
      success: (dlRes) => {
        if (dlRes.statusCode === 200) {
          wx.saveImageToPhotosAlbum({
            filePath: dlRes.tempFilePath,
            success: () => wx.showToast({ title: '已保存到相册', icon: 'success' }),
            fail: () => wx.showToast({ title: '保存失败喵', icon: 'none' })
          });
        }
      },
      fail: () => wx.showToast({ title: '下载失败喵', icon: 'none' })
    });
  },

  startPolling: function() {
    if (this.data.pollingTimer) return;
    this.setData({ pollingTimer: setInterval(() => { this.loadHistory(true); }, 1500) });
  },

  stopPolling: function() {
    if (this.data.pollingTimer) { clearInterval(this.data.pollingTimer); this.setData({ pollingTimer: null }); }
  },

  checkAndStopPolling: function() {
    const hasLoading = this.data.messages.some(m => m.role === 'assistant' && m.voice_status === 'loading');
    if (!this.data.isGenerating && !this.data.isMemoirProcessing && !hasLoading) this.stopPolling();
  },

  checkAndStartPollingIfNeeded: function() {
    const hasLoading = this.data.messages.some(m => m.role === 'assistant' && m.voice_status === 'loading');
    if (hasLoading || this.data.isGenerating) this.startPolling();
  },

  applyVoiceToMessage: function(id, url, status = 'ready') {
    const list = this.data.messages;
    const idx = list.findIndex(m => m.id === id);
    if (idx !== -1) { 
      this.setData({ [`messages[${idx}].voice_url`]: url, [`messages[${idx}].voice_status`]: status }); 
    }
  },

  playVoice: function(e) {
    const { url, id } = e.currentTarget.dataset;
    if (!url) return;
    if (this.data.playingMsgId === id) { this.audioCtx.stop(); this.setData({ playingMsgId: null }); return; }
    
    const fullUrl = resolveUrl(url);
    this.setData({ playingMsgId: id });
    this.audioCtx.src = fullUrl;
    this.audioCtx.play();
  },

  viewReport: function(e) {
    const { url } = e.currentTarget.dataset;
    if (!url) return;
    const fullApiUrl = resolveUrl(`/api/u/memoirs/${url}`);
    wx.navigateTo({ url: `/pages/report/report?url=${encodeURIComponent(fullApiUrl)}` });
  },

  // [Shop] 打开商城页
  goShop: function() {
    wx.redirectTo({ url: '/pages/shop/shop' });
  },

  // ★ v9.1 打开管理页面
  openManage: function() {
    wx.navigateTo({ url: '/pages/manage/manage' });
  },

  // ★ app.js WS update_profile 回调：后端识别到用户名后实时更新 UI
  onProfileUpdate: function(newName) {
    if (!newName) return;
    console.log('[Chat] 收到赐名圣旨，更新用户名:', newName);
    // 同步更新本地 userInfo，避免下次 initPageData 读到旧名字
    if (this.data.userInfo) {
      this.setData({ 'userInfo.user_name': newName });
    }
    // 更新 globalData，保持和 app.js 一致
    if (app.globalData.userInfo) {
      app.globalData.userInfo.user_name = newName;
    }
  },

  onVoiceChange: function(e) {
    const idx = e.detail.value;
    const character = this.data.voiceLibrary[idx];
    this.setData({ voiceIndex: idx, currentVoice: character });
    app.request('/voice/preference', 'POST', { character });  },

  loadVoiceLibrary: function() {
    app.request('/voice/library', 'GET').then(res => {
      if (res && res.code === 200) {
        const library = res.data || [];
        let idx = library.indexOf(this.data.currentVoice);
        this.setData({ voiceLibrary: library, voiceIndex: idx === -1 ? 0 : idx });
      }
    });
  },

  onScroll: function(e) {
    const scrollTop = e.detail.scrollTop;
    const isUp = scrollTop > 50;
    if (isUp !== this.data.isUserScrollingUp) {
      this.setData({ isUserScrollingUp: isUp });
    }
    if (scrollTop < 80 && this.data.hasMoreHistory && !this.data.isLoadingMore) {
      this.loadMoreHistory();
    }
    if (this.scrollTimer) clearTimeout(this.scrollTimer);
    this.scrollTimer = setTimeout(() => {
      if (this.data.isUserScrollingUp) this.setData({ isUserScrollingUp: false });
    }, 3000);
  },

  startMemoirCooldown: function(seconds = 300) {
    this.setData({ isMemoirProcessing: false, memoirCooldown: seconds });
    this.stopCDTimer();
    // 内部计数器：每秒递减，但只每 5 秒 setData 一次，避免高频渲染和轮询交错闪烁
    this._memoirCdValue = seconds;
    this.data.cdTimer = setInterval(() => {
      this._memoirCdValue -= 1;
      if (this._memoirCdValue <= 0) {
        this.stopCDTimer();
        this.setData({ memoirCooldown: 0 });
      } else if (this._memoirCdValue % 5 === 0) {
        // 每 5 秒同步一次 UI，倒计时显示精度 5 秒，肉眼无感知但消除高频渲染
        this.setData({ memoirCooldown: this._memoirCdValue });
      }
    }, 1000);
  },

  stopCDTimer: function() { if (this.data.cdTimer) { clearInterval(this.data.cdTimer); this.data.cdTimer = null; } },

  onAvatarLongPress: function(e) {
    if (e.currentTarget.dataset.role !== 'user') return;
    this.changeAvatar();
  },

  changeAvatar: function() {
    const app = getApp();
    wx.showActionSheet({
      itemList: ['从相册选择', '恢复默认头像'],
      success: (res) => {
        if (res.tapIndex === 0) {
          wx.chooseMedia({
            count: 1,
            mediaType: ['image'],
            sourceType: ['album', 'camera'],
            sizeType: ['compressed'],
            success: (media) => {
              const tempPath = media.tempFiles[0].tempFilePath;
              const fs = wx.getFileSystemManager();
              const localPath = `${wx.env.USER_DATA_PATH}/user_avatar_${Date.now()}.jpg`;
              try {
                const oldPath = wx.getStorageSync('userAvatarPath');
                if (oldPath) { try { fs.unlinkSync(oldPath); } catch(e) {} }
                fs.saveFileSync(tempPath, localPath);
                wx.setStorageSync('userAvatarPath', localPath);
                app.globalData.userAvatarPath = localPath;
                this.setData({ userAvatarPath: '' });
                setTimeout(() => this.setData({ userAvatarPath: localPath }), 50);
                wx.showToast({ title: '头像已更新', icon: 'success' });
              } catch(e) {
                wx.showToast({ title: '保存失败，请重试', icon: 'none' });
              }
            },
            fail: () => {}
          });
        } else if (res.tapIndex === 1) {
          wx.removeStorageSync('userAvatarPath');
          app.globalData.userAvatarPath = '';
          this.setData({ userAvatarPath: '' });
          wx.showToast({ title: '已恢复默认头像', icon: 'success' });
        }
      },
      fail: () => {}
    });
  },

  onInput: function(e) { this.setData({ inputValue: e.detail.value }); }
});
