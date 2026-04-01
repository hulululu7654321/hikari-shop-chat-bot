const { manageRequest, formatBJTime, todayBJ } = require('../api');

Page({
  data: {
    orders: [], loading: false,
    dateFilter: '', storeFilter: '',
    storeNameMap: {},  // store_id → store_name 映射
    // 店铺 picker
    storeList:   [],   // [{ store_id, store_name }]，首项为"全部"
    storeLabels: [],
    storeIndex:  0,
  },

  onLoad: function() {
    this.setData({ dateFilter: todayBJ() });
    this._loadStoreMap().then(() => this._load());
  },

  _loadStoreMap: function() {
    return manageRequest('/screen-codes', 'GET')
      .then(res => {
        if (res && res.code === 200) {
          const raw = Array.isArray(res.data) ? res.data
            : (res.data && Array.isArray(res.data.list) ? res.data.list
            : (res.data && Array.isArray(res.data.stores) ? res.data.stores : []));

          // 建 store_id → name map（含线上兜底）
          const map = { hikari_global: '线上VIP' };
          raw.forEach(s => { if (s.store_id) map[s.store_id] = s.store_name || s.store_id; });

          // picker 数据：第一项"全部"，后面跟各店 + 线上VIP
          const storeList = [
            { store_id: '', store_name: '全部' },
            ...raw.map(s => ({ store_id: s.store_id, store_name: s.store_name || s.store_id })),
            { store_id: 'hikari_global', store_name: '线上VIP' },
          ];
          this.setData({
            storeNameMap:  map,
            storeList,
            storeLabels:   storeList.map(s => s.store_name),
            storeIndex:    0,
            storeFilter:   '',
          });
        }
      }).catch(() => {});
  },

  onStorePickerChange: function(e) {
    const idx   = parseInt(e.detail.value);
    const store = this.data.storeList[idx];
    this.setData({
      storeIndex:  idx,
      storeFilter: store ? store.store_id : '',
    });
    this._load();
  },

  _load: function() {
    this.setData({ loading: true });
    let qs = `limit=100`;
    if (this.data.dateFilter)  qs += `&date=${this.data.dateFilter}`;
    if (this.data.storeFilter) qs += `&store_id=${this.data.storeFilter}`;
    manageRequest(`/orders?${qs}`, 'GET')
      .then(res => {
        if (res && res.code === 200) {
          const map = this.data.storeNameMap;
          this.setData({ orders: (res.data.list || []).map(o => ({
            ...o,
            created_at_fmt: formatBJTime(o.created_at),
            store_name: map[o.store_id] || o.store_id,
          }))});
        }
      }).finally(() => this.setData({ loading: false }));
  },

  onDateInput: function(e) {
    this.setData({ dateFilter: e.detail.value });
    // [Fix-M6] 防抖 300ms，连续输入只触发一次请求
    if (this._dateTimer) clearTimeout(this._dateTimer);
    this._dateTimer = setTimeout(() => { this._load(); }, 300);
  },

  doSearch: function() { this._load(); },
  clearDate: function() { this.setData({ dateFilter: '' }); this._load(); },
});
