/**
 * pages/manage/admin/users.js — 用户管理
 * 搜索用户 → 查详情 → 授VIP / 撤VIP / 踢人
 */

const { manageRequest, formatBJTime } = require('../api');

Page({
  data: {
    keyword: '',
    searching: false,
    results: [],
    selected: null,   // 当前查看的用户详情
    detailLoading: false,

    // [Fix] 激活状态筛选器：'all' | 'true' | 'false'
    accessFilter: 'all',
    accessFilterLabels: ['全部', '已激活', '未激活'],
    accessFilterOptions: ['all', 'true', 'false'],
    accessFilterIndex: 0,

    // VIP 授权表单
    showVipModal: false,
    vipDays: '30',
    vipLevel: 1,
  },

  onLoad: function() {
    // 进页面时自动加载全部用户（按注册时间倒序）
    this.search();
  },

  onInput: function(e) {
    this.setData({ keyword: e.detail.value });
  },

  search: function() {
    const q = this.data.keyword.trim();
    const { accessFilter } = this.data;
    this.setData({ searching: true, results: [], selected: null });
    // [Fix] 拼接 has_access 过滤参数
    let qs = q ? `q=${encodeURIComponent(q)}&limit=50` : `q=&limit=50`;
    if (accessFilter !== 'all') qs += `&has_access=${accessFilter}`;
    manageRequest(`/users?${qs}`, 'GET')
      .then(res => {
        if (res && res.code === 200) {
          const list = (res.data.list || []).map(u => ({
            ...u,
            user_id_tail: (u.user_id || '').slice(-8),
          }));
          this.setData({ results: list });
          if (list.length === 0) {
            wx.showToast({ title: '没有找到用户', icon: 'none' });
          }
        }
      })
      .finally(() => this.setData({ searching: false }));
  },

  // [Fix] 激活状态筛选器切换，切换即刷新
  onAccessFilterChange: function(e) {
    const idx = parseInt(e.detail.value);
    const opts = ['all', 'true', 'false'];
    this.setData({ accessFilterIndex: idx, accessFilter: opts[idx] });
    this.search();
  },

  selectUser: function(e) {
    const uid = e.currentTarget.dataset.uid;
    this.setData({ detailLoading: true, selected: null });
    manageRequest(`/users/${uid}`, 'GET')
      .then(res => {
        if (res && res.code === 200) {
          const d = res.data;
          // 格式化时间
          if (d.user.created_at)      d.user.created_at      = formatBJTime(d.user.created_at);
          if (d.user.vip_expires_at)  d.user.vip_expires_at  = formatBJTime(d.user.vip_expires_at);
          if (d.user.access_granted_at) d.user.access_granted_at = formatBJTime(d.user.access_granted_at);
          // 订单时间转北京时间
          if (Array.isArray(d.recent_orders)) {
            d.recent_orders = d.recent_orders.map(o => ({
              ...o,
              created_at: o.created_at ? formatBJTime(o.created_at) : 'N/A',
            }));
          }
          this.setData({ selected: d });
        }
      })
      .finally(() => this.setData({ detailLoading: false }));
  },

  // ── VIP 操作 ──
  openVipModal: function() {
    this.setData({ showVipModal: true, vipDays: '30', vipLevel: 1 });
  },
  closeVipModal: function() {
    this.setData({ showVipModal: false });
  },
  onVipDaysInput: function(e) {
    this.setData({ vipDays: e.detail.value });
  },
  onVipLevelChange: function(e) {
    this.setData({ vipLevel: parseInt(e.detail.value) === 0 ? 1 : 2 });
  },

  grantVip: function() {
    const uid  = this.data.selected.user.user_id;
    const days = parseInt(this.data.vipDays) || 0;
    const level = this.data.vipLevel;
    const label = days === 0 ? '永久VIP' : `VIP ${days}天`;
    wx.showModal({
      title: '确认授予',
      content: `授予该用户 ${label}？`,
      confirmColor: '#07C160',
      success: (r) => {
        if (!r.confirm) return;
        manageRequest(`/users/${uid}/vip`, 'POST', {
          vip_level: level, days
        }).then(res => {
          if (res && res.code === 200) {
            wx.showToast({ title: '授权成功', icon: 'success' });
            this.setData({ showVipModal: false });
            this.selectUser({ currentTarget: { dataset: { uid } } });
          }
        });
      }
    });
  },

  revokeVip: function() {
    const uid = this.data.selected.user.user_id;
    wx.showModal({
      title: '确认撤销',
      content: '撤销该用户的 VIP 权限？',
      confirmColor: '#EF4444',
      success: (r) => {
        if (!r.confirm) return;
        manageRequest(`/users/${uid}/vip`, 'POST', { vip_level: 0, days: 0 })
          .then(res => {
            if (res && res.code === 200) {
              wx.showToast({ title: '已撤销', icon: 'success' });
              this.selectUser({ currentTarget: { dataset: { uid } } });
            }
          });
      }
    });
  },

  // ── 踢人 ──
  kickUser: function() {
    const uid = this.data.selected.user.user_id;
    const name = this.data.selected.user.user_name;
    wx.showModal({
      title: '⚠️ 确认踢出',
      content: `踢出「${name}」？该用户邀请码将永久作废，需重新申请。`,
      confirmText: '踢出',
      confirmColor: '#EF4444',
      success: (r) => {
        if (!r.confirm) return;
        manageRequest(`/users/${uid}/kick`, 'POST', {})
          .then(res => {
            if (res && res.code === 200) {
              wx.showToast({ title: '已踢出', icon: 'success' });
              this.setData({ selected: null, results: [] });
            }
          });
      }
    });
  },

  clearSelected: function() {
    this.setData({ selected: null });
  },
});
