const { manageRequest } = require('../api');

Page({
  data: { stores: [], loading: false, resetting: '' },

  onLoad: function() { this._load(); },

  _load: function() {
    this.setData({ loading: true });
    manageRequest('/screen-codes', 'GET')
      .then(res => {
        if (res && res.code === 200) {
          const raw = Array.isArray(res.data) ? res.data
            : (res.data && Array.isArray(res.data.list) ? res.data.list
            : (res.data && Array.isArray(res.data.stores) ? res.data.stores : []));
          this.setData({ stores: raw });
        }
      }).finally(() => this.setData({ loading: false }));
  },

  copyUrl: function(e) {
    wx.setClipboardData({ data: e.currentTarget.dataset.url,
      success: () => wx.showToast({ title: '链接已复制', icon: 'success' }) });
  },

  resetCode: function(e) {
    const store = e.currentTarget.dataset.store;
    wx.showModal({
      title: '⚠️ 确认重置',
      content: `重置「${store.store_name}」的屏幕短码？屏幕端会自动刷新页面。`,
      confirmText: '重置',
      confirmColor: '#EF4444',
      success: (r) => {
        if (!r.confirm) return;
        this.setData({ resetting: store.store_id });
        manageRequest('/screen-codes/reset', 'POST', { store_id: store.store_id })
          .then(res => {
            if (res && res.code === 200) {
              wx.showModal({
                title: '重置成功',
                content: `新链接：${res.data.url}`,
                showCancel: false,
              });
              this._load();
            }
          }).finally(() => this.setData({ resetting: '' }));
      }
    });
  },
});
