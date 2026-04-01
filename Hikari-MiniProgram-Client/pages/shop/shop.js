/**
 * pages/shop/shop.js — 希卡莉商城首页
 * 公开页面，无需登录，展示所有上架套餐
 * 分类 Tab + 套餐卡片 + 详情浮层 + 联系/聊天入口
 */

const app = getApp();

// 与后端 SHOP_CATEGORIES 保持一致
const CATEGORIES = [
  { key: 'all',         label: '全部' },
  { key: 'photography', label: '摄影' },
  { key: 'costume',     label: '服装' },
  { key: 'makeup',      label: '妆娘' },
  { key: 'studio',      label: '场地' },
  { key: 'activity',    label: '活动' },
  { key: 'online',      label: '线上VIP' },
  { key: 'other',       label: '其他' },
];



Page({
  data: {
    // 分类
    categories: CATEGORIES,
    currentCategory: 'all',
    categoryIndex: 0,

    // 套餐列表
    packages: [],
    allPackages: [],  // 全量缓存，切 Tab 时本地过滤，不重复请求
    loading: false,

    // Banner 轮播
    bannerHeight: 360,  // rpx

    // 详情浮层
    showDetail: false,
    detailPkg: null,
    detailImgIndex: 0,  // 详情里图片轮播当前页

    // 底部安全区
    bottomPad: 0,

    baseUrl: '',
    contactWechat: '',   // 从 /shop/config 接口拉取
    contactNote: '',
  },

  onLoad: function() {
    const baseUrl = app.globalData.BASE_URL || '';
    this.setData({ baseUrl });
    this._loadPackages();
    this._loadConfig();

    // 底部安全区适配
    const info = wx.getSystemInfoSync();
    this.setData({ bottomPad: info.safeArea ? info.screenHeight - info.safeArea.bottom : 0 });
  },

  onShow: function() {
    // 从聊天页返回时不重新请求，保持当前状态
  },

  // ── 配置加载 ──

  _loadConfig: function() {
    wx.request({
      url: `${this.data.baseUrl}/api/p/shop`,
      method: 'GET',
      success: (res) => {
        if (res.statusCode === 200 && res.data && res.data.code === 200) {
          const d = res.data.data;
          this.setData({
            contactWechat: d.contact_wechat || '',
            contactNote:   d.contact_note   || '',
          });
        }
      },
      fail: () => {}  // 静默失败，不影响主功能
    });
  },

  // ── 数据加载 ──

  _loadPackages: function() {
    this.setData({ loading: true });
    const base = this.data.baseUrl;

    wx.request({
      url: `${base}/api/p/packages`,
      method: 'GET',
      success: (res) => {
        if (res.statusCode === 200 && res.data && res.data.code === 200) {
          const list = (res.data.data.list || []).map(pkg => {
            const images = (pkg.images || []).map(p =>
              p.startsWith('http') ? p : `${base}${p}`
            );
            return {
              ...pkg,
              images,
              cover: images[0] || '',
              categoryLabel: CATEGORIES.find(c => c.key === pkg.category)?.label || '其他',
              store_name: pkg.store_name || '',
            };
          });

          this.setData({
            allPackages: list,
            packages: list,
          });
        } else {
          wx.showToast({ title: '加载失败，请稍后再试', icon: 'none' });
        }
      },
      fail: () => {
        wx.showToast({ title: '网络异常，请检查网络', icon: 'none' });
      },
      complete: () => {
        this.setData({ loading: false });
      }
    });
  },

  // ── 分类切换 ──

  switchCategory: function(e) {
    const key = e.currentTarget.dataset.key;
    const idx = parseInt(e.currentTarget.dataset.index);
    const filtered = key === 'all'
      ? this.data.allPackages
      : this.data.allPackages.filter(p => p.category === key);
    this.setData({
      currentCategory: key,
      categoryIndex: idx,
      packages: filtered,
      showDetail: false,
    });
  },

  // ── 套餐详情浮层 ──

  openDetail: function(e) {
    const pkg = e.currentTarget.dataset.pkg;
    this.setData({ showDetail: true, detailPkg: pkg, detailImgIndex: 0 });
  },

  closeDetail: function() {
    this.setData({ showDetail: false, detailPkg: null });
  },

  onDetailImgChange: function(e) {
    this.setData({ detailImgIndex: e.detail.current });
  },

  previewImg: function(e) {
    const { urls, current } = e.currentTarget.dataset;
    if (!urls || !urls.length) return;
    wx.previewImage({ urls, current: current || urls[0] });
  },

  // ── 联系管理员 ──

  contactAdmin: function() {
    const { contactWechat, contactNote } = this.data;
    if (!contactWechat) {
      wx.showToast({ title: '请联系管理员获取微信号', icon: 'none', duration: 2000 });
      return;
    }
    wx.setClipboardData({
      data: contactWechat,
      success: () => {
        const tip = contactNote ? `微信号已复制，${contactNote}` : '微信号已复制，请添加好友';
        wx.showToast({ title: tip, icon: 'success', duration: 2500 });
      }
    });
  },

  // ── 跳转聊天 ──

  goChat: function() {
    // 统一跳 index，让 index 判断鉴权状态
    wx.navigateTo({ url: '/pages/index/index' });
  },

  // 下拉刷新
  onPullDownRefresh: function() {
    this._loadPackages();
    this._loadConfig();
    wx.stopPullDownRefresh();
  },
});
