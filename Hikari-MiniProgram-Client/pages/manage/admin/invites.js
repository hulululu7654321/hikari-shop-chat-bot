const { manageRequest, formatBJTime } = require('../api');

Page({
  data: {
    list: [], loading: false,
    statusFilter: 'all',
    statusOptions: ['all','unused','used','revoked'],
    statusLabels: ['全部','未使用','已使用','已作废'],
    statusIndex: 0,
    generating: false,
    genCount: '5', genNote: '',
  },

  onLoad: function() { this._load(); },

  _load: function() {
    this.setData({ loading: true });
    manageRequest(`/invites?status=${this.data.statusFilter}&limit=100`, 'GET')
      .then(res => {
        if (res && res.code === 200) {
          this.setData({ list: (res.data.list || []).map(d => ({
            ...d,
            created_at_fmt: formatBJTime(d.created_at),
            expires_at_fmt: d.expires_at ? formatBJTime(d.expires_at) : '不过期',
            used_at_fmt:    d.used_at    ? formatBJTime(d.used_at)    : '',
          }))});
        }
      }).finally(() => this.setData({ loading: false }));
  },

  onFilterChange: function(e) {
    const idx = parseInt(e.detail.value);
    const opts = ['all','unused','used','revoked'];
    this.setData({ statusIndex: idx, statusFilter: opts[idx] });
    this._load();
  },

  onCountInput: function(e) { this.setData({ genCount: e.detail.value }); },
  onNoteInput:  function(e) { this.setData({ genNote:  e.detail.value }); },

  generate: function() {
    const count = parseInt(this.data.genCount) || 1;
    if (count < 1 || count > 50) { wx.showToast({ title: '数量1-50之间', icon: 'none' }); return; }
    this.setData({ generating: true });
    manageRequest('/invites', 'POST', { count, expire_days: 30 })
      .then(res => {
        if (res && res.code === 200) {
          const codes = (res.data.codes || []).map(c => c.code).join('\n');
          wx.showModal({ title: `已生成 ${res.data.count} 个`, content: codes, showCancel: false });
          this._load();
        }
      }).finally(() => this.setData({ generating: false }));
  },

  revoke: function(e) {
    const code = e.currentTarget.dataset.code;
    wx.showModal({
      title: '作废邀请码', content: `作废 ${code}？`,
      confirmColor: '#EF4444',
      success: (r) => {
        if (!r.confirm) return;
        manageRequest(`/invites/${code}/revoke`, 'POST', {})
          .then(res => { if (res && res.code === 200) { wx.showToast({ title: '已作废', icon: 'success' }); this._load(); } });
      }
    });
  },

  copyCode: function(e) {
    wx.setClipboardData({ data: e.currentTarget.dataset.code,
      success: () => wx.showToast({ title: '已复制', icon: 'success' }) });
  },
});
