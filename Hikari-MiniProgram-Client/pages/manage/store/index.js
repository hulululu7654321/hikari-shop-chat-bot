/**
 * pages/manage/store/index.js — 店员主界面
 * 三个 Tab：在店客户 / 确认下单 / 今日订单
 */

const app = getApp();
const { manageRequest, formatBJTime, todayBJ } = require('../api');

Page({
  data: {
    tab: 'customers',       // customers | order | orders
    storeName: '',
    storeId: '',            // admin 跳转时传入，store_clerk 从 globalData 取

    // 在店客户列表
    customers: [],
    customersLoading: false,

    // 下单流程
    selectedUser: null,     // 选中的客户 {user_id, display_name}
    packages: [],
    selectedPkg: null,
    paymentMethod: 'wechat',
    orderNote: '',
    orderSubmitting: false,

    // 今日订单
    orders: [],
    ordersLoading: false,

    payMethods: ['wechat', 'alipay', 'cash', 'other'],
    payMethodLabels: ['微信', '支付宝', '现金', '其他'],
    payMethodIndex: 0,
  },

  onLoad: function(options) {
    // admin 从快捷入口跳过来时会带 store_id / store_name 参数
    const storeId   = options.store_id   || app.globalData.manageStoreId   || '';
    const storeName = options.store_name
      ? decodeURIComponent(options.store_name)
      : (app.globalData.manageStoreName || '店铺');
    // 先把 storeId 存起来，再直接传参调用，不依赖 setData 异步更新
    this.setData({ storeId, storeName });
    this._loadCustomers();
    this._loadPackages(storeId);
    this._loadOrders();
  },

  onShow: function() {
    if (this.data.tab === 'customers') this._loadCustomers();
    if (this.data.tab === 'orders')    this._loadOrders();
  },

  switchTab: function(e) {
    const tab = e.currentTarget.dataset.tab;
    this.setData({ tab });
    if (tab === 'customers') this._loadCustomers();
    if (tab === 'orders')    this._loadOrders();
  },

  // ── 在店客户 ──────────────────────────────

  _loadCustomers: function() {
    this.setData({ customersLoading: true });
    manageRequest('/store/customers', 'GET')
      .then(res => {
        if (res && res.code === 200) {
          this.setData({ customers: res.data.list || [] });
        }
      })
      .finally(() => this.setData({ customersLoading: false }));
  },

  onRefreshCustomers: function() {
    this._loadCustomers();
  },

  // 点击客户 → 进入下单 tab，预填客户
  selectCustomer: function(e) {
    const customer = e.currentTarget.dataset.customer;
    this.setData({
      tab: 'order',
      selectedUser: { user_id: customer.user_id, display_name: customer.display_name },
      selectedPkg: null,
    });
  },

  // ── 套餐列表（store 类型） ──────────────────

  _loadPackages: function(storeId) {
    // storeId 优先用传入参数，fallback 到 data 里存的值
    const sid = storeId || this.data.storeId || '';
    const qs = sid ? `active_only=1&store_id=${sid}` : 'active_only=1';
    console.log('[Packages] 请求套餐', qs);
    manageRequest(`/packages?${qs}`, 'GET')
      .then(res => {
        console.log('[Packages] 返回', JSON.stringify(res && res.data));
        if (res && res.code === 200) {
          // 后端 admin 视图返回 groups 格式，store_clerk 返回 list 格式
          let packages = [];
          if (res.data.list) {
            packages = res.data.list;
          } else if (res.data.groups) {
            res.data.groups.forEach(g => { packages = packages.concat(g.packages || []); });
          }
          this.setData({ packages });
        }
      });
  },

  selectPackage: function(e) {
    this.setData({ selectedPkg: e.currentTarget.dataset.pkg });
  },

  onPayMethodChange: function(e) {
    const idx = parseInt(e.detail.value);
    const methods = ['wechat', 'alipay', 'cash', 'other'];
    this.setData({ payMethodIndex: idx, paymentMethod: methods[idx] });
  },

  onNoteInput: function(e) {
    this.setData({ orderNote: e.detail.value });
  },

  // ── 确认下单 ──────────────────────────────

  submitOrder: function() {
    const { selectedUser, selectedPkg, paymentMethod, orderNote } = this.data;
    if (!selectedUser) {
      wx.showToast({ title: '请先选择客户', icon: 'none' }); return;
    }
    if (!selectedPkg) {
      wx.showToast({ title: '请选择套餐', icon: 'none' }); return;
    }

    wx.showModal({
      title: '确认下单',
      content: `为 ${selectedUser.display_name} 下单「${selectedPkg.name}」¥${selectedPkg.price}？`,
      confirmText: '确认',
      confirmColor: '#07C160',
      success: (r) => {
        if (!r.confirm) return;
        this.setData({ orderSubmitting: true });
        manageRequest('/orders', 'POST', {
          user_id:        selectedUser.user_id,
          package_id:     selectedPkg.package_id,
          payment_method: paymentMethod,
          note:           orderNote,
        }).then(res => {
          if (res && res.code === 200) {
            wx.showToast({ title: '下单成功喵！', icon: 'success' });
            this.setData({
              tab: 'orders',
              selectedUser: null,
              selectedPkg: null,
              orderNote: '',
              payMethodIndex: 0,
              paymentMethod: 'wechat',
            });
            this._loadOrders();
          } else {
            wx.showModal({
              title: '下单失败',
              content: (res && res.msg) || '请重试喵',
              showCancel: false
            });
          }
        }).catch(() => {}).finally(() => this.setData({ orderSubmitting: false }));
      }
    });
  },

  clearUser: function() {
    this.setData({ selectedUser: null, selectedPkg: null });
  },

  // ── 今日订单 ──────────────────────────────

  _loadOrders: function() {
    this.setData({ ordersLoading: true });
    manageRequest(`/orders?date=${todayBJ()}`, 'GET')
      .then(res => {
        if (res && res.code === 200) {
          const list = (res.data.list || []).map(o => ({
            ...o,
            created_at_fmt: formatBJTime(o.created_at),
          }));
          this.setData({ orders: list });
        }
      })
      .finally(() => this.setData({ ordersLoading: false }));
  },

  goBack: function() {
    wx.navigateBack();
  },
});
