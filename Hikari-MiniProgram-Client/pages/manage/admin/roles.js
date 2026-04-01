const { manageRequest, formatBJTime } = require('../api');

Page({
  data: {
    list: [], loading: false,
    // 授权表单
    showModal: false,
    form: { user_id: '', role: 'store_clerk', store_id: '' },
    roleOptions: ['store_clerk', 'admin'],
    roleLabels: ['店员', '管理员'],
    roleIndex: 0,
    submitting: false,
    // 搜索先找 user_id
    searchKw: '', searching: false, searchResults: [],
  },

  onLoad: function() { this._load(); },

  _load: function() {
    this.setData({ loading: true });
    manageRequest('/roles', 'GET')
      .then(res => {
        if (res && res.code === 200) {
          this.setData({ list: (res.data.list || []).map(u => ({
            ...u,
            granted_at_fmt: formatBJTime(u.manage_granted_at),
          }))});
        }
      }).finally(() => this.setData({ loading: false }));
  },

  // 搜索用户（复用 users/search）
  onSearchInput: function(e) { this.setData({ searchKw: e.detail.value }); },
  searchUser: function() {
    const q = this.data.searchKw.trim();
    if (q.length < 1) { wx.showToast({ title: '请输入搜索内容', icon: 'none' }); return; }
    this.setData({ searching: true });
    manageRequest(`/users?q=${encodeURIComponent(q)}&limit=10`, 'GET')
      .then(res => {
        if (res && res.code === 200) this.setData({ searchResults: res.data.list || [] });
      }).finally(() => this.setData({ searching: false }));
  },

  pickUser: function(e) {
    const uid = e.currentTarget.dataset.uid;
    const name = e.currentTarget.dataset.name;
    this.setData({
      'form.user_id': uid,
      searchKw: name,
      searchResults: [],
      showModal: true,
    });
  },

  closeModal: function() { this.setData({ showModal: false, searchResults: [] }); },

  onRoleChange: function(e) {
    const idx = parseInt(e.detail.value);
    const roles = ['store_clerk', 'admin'];
    this.setData({ roleIndex: idx, 'form.role': roles[idx] });
  },

  onStoreIdInput: function(e) { this.setData({ 'form.store_id': e.detail.value }); },

  grantRole: function() {
    const { form } = this.data;
    if (!form.user_id) { wx.showToast({ title: '请先搜索并选择用户', icon: 'none' }); return; }
    if (form.role === 'store_clerk' && !form.store_id.trim()) {
      wx.showToast({ title: '店员必须填写店铺ID', icon: 'none' }); return;
    }
    this.setData({ submitting: true });
    manageRequest('/roles/grant', 'POST', {
      user_id: form.user_id,
      role:     form.role,
      store_id: form.store_id.trim(),
    }).then(res => {
      if (res && res.code === 200) {
        wx.showToast({ title: '授权成功', icon: 'success' });
        this.setData({ showModal: false });
        this._load();
      } else {
        wx.showModal({ title: '失败', content: res && res.msg || '请重试', showCancel: false });
      }
    }).finally(() => this.setData({ submitting: false }));
  },

  revokeRole: function(e) {
    const uid  = e.currentTarget.dataset.uid;
    const name = e.currentTarget.dataset.name;
    wx.showModal({
      title: '撤销权限',
      content: `撤销「${name}」的管理权限？`,
      confirmColor: '#EF4444',
      success: (r) => {
        if (!r.confirm) return;
        manageRequest('/roles/revoke', 'POST', { user_id: uid })
          .then(res => {
            if (res && res.code === 200) {
              wx.showToast({ title: '已撤销', icon: 'success' });
              this._load();
            }
          });
      }
    });
  },
});
