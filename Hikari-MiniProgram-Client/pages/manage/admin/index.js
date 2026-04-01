/**
 * pages/manage/admin/index.js — 管理员主界面
 * 展示统计概览 + 各功能模块入口
 */

const app = getApp();
const { manageRequest } = require('../api');

Page({
  data: {
    stats: null,
    statsLoading: true,
    menus: [
      { key: 'users',    icon: '👤', title: '用户管理',   desc: '搜索用户、授VIP、踢人' },
      { key: 'invites',  icon: '🎫', title: '邀请码',     desc: '生成/查看/作废邀请码' },
      { key: 'packages', icon: '📦', title: '套餐管理',   desc: '上下架套餐、新增修改' },
      { key: 'orders',   icon: '🧾', title: '订单记录',   desc: '查看所有订单流水' },
      { key: 'store',    icon: '📺', title: '店铺管理',   desc: '屏幕链接、重置短码' },
      { key: 'roles',    icon: '🔑', title: '角色授权',   desc: '管理员/店员权限分配' },
    ],
  },

  onLoad: function() {
    this._loadStats();
  },

  onShow: function() {
    this._loadStats();
  },

  _loadStats: function() {
    this.setData({ statsLoading: true });
    manageRequest('/system/stats', 'GET')
      .then(res => {
        if (res && res.code === 200) {
          this.setData({ stats: res.data });
        } else {
          console.warn('[Admin] stats 接口返回异常:', res);
        }
      })
      .catch(err => {
        // 请求失败不崩溃，只清除 loading，stats 保持 null 显示空状态
        console.error('[Admin] stats 加载失败:', err);
      })
      .finally(() => this.setData({ statsLoading: false }));
  },

  goMenu: function(e) {
    const key = e.currentTarget.dataset.key;
    wx.navigateTo({ url: `/pages/manage/admin/${key}` });
  },

  // 店员下单（admin 也可以用店员界面下到店套餐）
  goStore: function() {
    // 从后端拉店铺列表，让 admin 选择目标店铺
    manageRequest('/screen-codes', 'GET')
      .then(res => {
        const raw = Array.isArray(res.data) ? res.data
          : (res.data && Array.isArray(res.data.list) ? res.data.list
          : (res.data && Array.isArray(res.data.stores) ? res.data.stores : []));
        if (!res || res.code !== 200 || !raw.length) {
          wx.showToast({ title: '暂无店铺', icon: 'none' });
          return;
        }
        const stores = raw;
        if (stores.length === 1) {
          // 只有一家店直接跳转
          wx.navigateTo({ url: `/pages/manage/store/index?store_id=${stores[0].store_id}&store_name=${encodeURIComponent(stores[0].store_name)}` });
          return;
        }
        // 多店时弹选择器
        wx.showActionSheet({
          itemList: stores.map(s => s.store_name),
          success: (e) => {
            const store = stores[e.tapIndex];
            wx.navigateTo({ url: `/pages/manage/store/index?store_id=${store.store_id}&store_name=${encodeURIComponent(store.store_name)}` });
          }
        });
      })
      .catch(() => {
        wx.showToast({ title: '加载店铺失败', icon: 'none' });
      });
  },

  goBack: function() {
    wx.navigateBack();
  },
});
