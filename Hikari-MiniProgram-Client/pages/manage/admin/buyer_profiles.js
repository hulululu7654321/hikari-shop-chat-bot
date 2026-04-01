/**
 * pages/manage/admin/buyer_profiles.js — 套餐买家画像页
 *
 * 下载安全策略：token 放 Authorization header，不出现在 URL。
 * 本地缓存策略：导出成功后自动下载并保存到小程序永久存储，
 *               后续点「打开文件」直接读本地，不再请求后端。
 */

const { manageRequest } = require('../api');
const app = getApp();
const fs = wx.getFileSystemManager();

// 本地永久存储根目录
const LOCAL_DIR = `${wx.env.USER_DATA_PATH}/buyer_portraits`;

Page({
  data: {
    packageId: '', packageName: '', packageInfo: null,
    buyers: [], total: 0, page: 1, limit: 50, hasMore: false, loading: false,
    exporting:     false,
    exportedFile:  '',   // 后端生成的文件名
    localFilePath: '',   // 本地永久路径，有值时显示「打开文件」按钮
    saving:        false,
  },

  onLoad: function(options) {
    const packageId   = options.package_id || '';
    const packageName = decodeURIComponent(options.package_name || '未知套餐');
    this.setData({ packageId, packageName });
    if (!packageId) { wx.showToast({ title: '缺少套餐参数', icon: 'none' }); return; }

    // 确保本地目录存在
    try { fs.mkdirSync(LOCAL_DIR, true); } catch(e) {}

    // 检查本地是否已有该套餐的缓存文件
    this._checkLocalCache(packageId);
    this._loadBuyers(1);
  },

  // 检查本地缓存（同一套餐上次导出的文件）
  _checkLocalCache: function(packageId) {
    try {
      const metaPath = `${LOCAL_DIR}/meta_${packageId}.json`;
      const meta = JSON.parse(fs.readFileSync(metaPath, 'utf8'));
      if (meta && meta.localPath && meta.filename) {
        // 验证文件还在
        fs.accessSync(meta.localPath);
        this.setData({ localFilePath: meta.localPath, exportedFile: meta.filename });
      }
    } catch(e) {
      // 无缓存或文件已失效，忽略
    }
  },

  // 保存 meta 映射
  _saveMeta: function(packageId, filename, localPath) {
    try {
      const metaPath = `${LOCAL_DIR}/meta_${packageId}.json`;
      fs.writeFileSync(metaPath, JSON.stringify({ filename, localPath }), 'utf8');
    } catch(e) {}
  },

  _loadBuyers: function(page) {
    const { packageId, limit } = this.data;
    this.setData({ loading: true });
    manageRequest(
      `/users/by-package?package_id=${packageId}&page=${page}&limit=${limit}`,
      'GET'
    ).then(res => {
      if (res && res.code === 200) {
        const d = res.data;
        const existing = page === 1 ? [] : this.data.buyers;
        this.setData({
          packageInfo: d.package || null,
          buyers:  existing.concat(d.list || []),
          total:   d.total    || 0,
          page:    d.page     || page,
          hasMore: d.has_more || false,
        });
      } else {
        wx.showToast({ title: (res && res.msg) || '加载失败', icon: 'none' });
      }
    }).catch(() => {
      wx.showToast({ title: '网络异常', icon: 'none' });
    }).finally(() => {
      this.setData({ loading: false });
      wx.stopPullDownRefresh();
    });
  },

  onPullDownRefresh: function() {
    this.setData({ buyers: [], exportedFile: '', localFilePath: '' });
    this._loadBuyers(1);
  },

  onReachBottom: function() {
    if (this.data.hasMore && !this.data.loading) this._loadBuyers(this.data.page + 1);
  },

  // ── 导出 ──

  exportPortrait: function() {
    const { packageId, packageName, exporting, buyers } = this.data;
    if (exporting) return;
    if (buyers.length === 0) { wx.showToast({ title: '暂无买家数据', icon: 'none' }); return; }
    wx.showModal({
      title: '确认导出',
      content: `导出「${packageName}」全部 ${this.data.total} 位买家的原始画像 MD 文件？`,
      confirmText: '导出', confirmColor: '#6366F1',
      success: (r) => { if (r.confirm) this._doExport(packageId); }
    });
  },

  _doExport: function(packageId) {
    this.setData({ exporting: true, exportedFile: '', localFilePath: '' });
    wx.showLoading({ title: '生成中…', mask: true });

    manageRequest('/users/by-package/export', 'POST', { package_id: packageId })
      .then(res => {
        if (res && res.code === 200) {
          const filename = res.data.filename || '';
          const relPath  = res.data.rel_path || '';
          this.setData({ exportedFile: filename });
          wx.hideLoading();
          // 生成成功后立即自动下载保存到本地
          this._downloadAndSave(relPath, filename, packageId);
        } else {
          wx.hideLoading();
          wx.showModal({
            title: '导出失败',
            content: (res && res.msg) || '请稍后重试',
            showCancel: false
          });
        }
      }).catch(() => {
        wx.hideLoading();
        wx.showToast({ title: '网络异常', icon: 'none' });
      }).finally(() => {
        this.setData({ exporting: false });
      });
  },

  // 下载并保存到本地永久存储
  _downloadAndSave: function(relPath, filename, packageId) {
    const base  = app.globalData.BASE_URL || '';
    const token = app.globalData.token    || '';
    this.setData({ saving: true });
    wx.showLoading({ title: '保存到本地…', mask: true });

    wx.downloadFile({
      url:    `${base}/api/m/files/buyer-portraits/${filename}`,
      header: { 'Authorization': `Bearer ${token}` },
      success: (dlRes) => {
        if (dlRes.statusCode === 200) {
          // 保存到小程序永久目录
          // 本地存为 .md.doc，微信 openDocument 用 fileType:doc 才能预览纯文本
          const localPath = `${LOCAL_DIR}/${filename}.doc`;
          try {
            // 如果旧文件存在先删除
            try { fs.unlinkSync(localPath); } catch(e) {}
            fs.saveFileSync(dlRes.tempFilePath, localPath);
            this._saveMeta(packageId, filename, localPath);
            this.setData({ localFilePath: localPath });
            wx.hideLoading();
            wx.showToast({ title: '已保存到本地', icon: 'success' });
          } catch(saveErr) {
            wx.hideLoading();
            console.error('[BuyerPortrait] 保存失败:', saveErr);
            wx.showModal({
              title: '保存失败',
              content: '文件下载成功但保存到本地失败，可尝试重新导出',
              showCancel: false
            });
          }
        } else {
          wx.hideLoading();
          wx.showToast({ title: '下载失败，请重试', icon: 'none' });
        }
      },
      fail: () => {
        wx.hideLoading();
        wx.showToast({ title: '网络异常', icon: 'none' });
      },
      complete: () => {
        this.setData({ saving: false });
      }
    });
  },

  // 打开本地文件（不请求后端）
  openLocalFile: function() {
    const { localFilePath, exportedFile, packageId } = this.data;
    if (!localFilePath) return;

    // 先验证本地文件是否还在（可能被微信清理）
    try {
      fs.accessSync(localFilePath);
    } catch(e) {
      // 文件已不存在，清除缓存状态，提示用户重新导出
      try { fs.unlinkSync(`${LOCAL_DIR}/meta_${packageId}.json`); } catch(e2) {}
      this.setData({ localFilePath: '', exportedFile: '' });
      wx.showModal({
        title: '本地文件已失效',
        content: '缓存文件已被系统清理，请重新导出',
        confirmText: '重新导出',
        cancelText: '取消',
        success: (r) => {
          if (r.confirm) this._doExport(packageId);
        }
      });
      return;
    }

    wx.openDocument({
      filePath: localFilePath,
      fileType: 'doc',
      showMenu: true,
      fail: (e) => {
        console.error('[BuyerPortrait] openDocument 失败:', e);
        wx.showModal({
          title: '无法预览',
          content: '当前环境不支持预览 .md 文件，文件已保存在小程序本地存储中',
          showCancel: false
        });
      }
    });
  },

  // 清除本地缓存（重新导出前可手动清除）
  clearLocalCache: function() {
    const { packageId, localFilePath, exportedFile } = this.data;
    wx.showModal({
      title: '清除本地缓存',
      content: '删除本地保存的画像文件？不影响服务器上的文件',
      confirmColor: '#EF4444',
      success: (r) => {
        if (!r.confirm) return;
        try {
          if (localFilePath) fs.unlinkSync(localFilePath);
          const metaPath = `${LOCAL_DIR}/meta_${packageId}.json`;
          try { fs.unlinkSync(metaPath); } catch(e) {}
          this.setData({ localFilePath: '', exportedFile: '' });
          wx.showToast({ title: '已清除', icon: 'success' });
        } catch(e) {
          wx.showToast({ title: '清除失败', icon: 'none' });
        }
      }
    });
  },
});
