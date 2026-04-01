/**
 * pages/manage/admin/packages.js — 套餐管理
 * 分组展示所有套餐，支持新增/修改/上下架
 * [Shop] 新增：商城分类选择 + 套餐图片上传/删除
 */

const { manageRequest } = require('../api');
const app = getApp();

// [Shop] 分类枚举，与后端 SHOP_CATEGORIES 保持一致
const CATEGORY_OPTIONS = ['photography', 'costume', 'makeup', 'studio', 'activity', 'online', 'other'];
const CATEGORY_LABELS  = ['摄影', '服装', '妆娘', '场地', '活动', '线上VIP', '其他'];

Page({
  data: {
    groups: [], loading: false,
    inactiveExpanded: {},  // 各 group 的已下架折叠状态 { store_id: bool }
    showModal: false,
    editing: null,   // null=新增，object=编辑中的套餐完整对象（含 images）

    form: {
      type: 'store', store_id: '', name: '', price: '',
      description: '', screen_minutes: '0',
      vip_level: 1, vip_days: '30', image_gen_quota: '0', affinity_delta: '30',
      category: 'photography',
    },

    // 套餐类型 picker
    typeOptions: ['store', 'online'],
    typeLabels: ['到店套餐', '线上VIP套餐'],
    typeIndex: 0,

    // 分类 picker
    categoryOptions: CATEGORY_OPTIONS,
    categoryLabels:  CATEGORY_LABELS,
    categoryIndex: 0,

    // 店铺 picker
    storeList: [],
    storeLabels: [],
    storeIndex: 0,
    storesLoading: false,

    // 图片管理（编辑模式）
    editImages: [],       // 当前编辑套餐的图片列表（相对路径）
    imgUploading: false,  // 图片上传中

    submitting: false,
    baseUrl: '',  // 图片 URL 前缀
  },

  onLoad: function() {
    this.setData({ baseUrl: app.globalData.BASE_URL || '' });
    this._load();
    this._loadStores();
  },

  _load: function() {
    this.setData({ loading: true });
    manageRequest('/packages', 'GET')
      .then(res => {
        if (res && res.code === 200) {
          // 每个 group 的 packages 按 is_active 拆成 active / inactive 两组
          const groups = (res.data.groups || []).map(g => ({
            ...g,
            active:   (g.packages || []).filter(p => p.is_active),
            inactive: (g.packages || []).filter(p => !p.is_active),
          }));
          this.setData({ groups });
        }
      }).finally(() => this.setData({ loading: false }));
  },

  // ── 店铺列表 ──

  _loadStores: function() {
    if (this.data.storeList.length > 0) return;
    this.setData({ storesLoading: true });
    manageRequest('/screen-codes', 'GET')
      .then(res => {
        if (res && res.code === 200) {
          let stores = [];
          if (Array.isArray(res.data)) {
            stores = res.data;
          } else if (res.data && Array.isArray(res.data.list)) {
            stores = res.data.list;
          } else if (res.data && Array.isArray(res.data.stores)) {
            stores = res.data.stores;
          }
          this.setData({
            storeList:   stores,
            storeLabels: stores.map(s => s.store_name),
          });
        }
      })
      .finally(() => this.setData({ storesLoading: false }));
  },

  onStoreChange: function(e) {
    const idx = parseInt(e.detail.value);
    const store = this.data.storeList[idx];
    this.setData({ storeIndex: idx, 'form.store_id': store ? store.store_id : '' });
  },

  // ── 分类 picker ──

  onCategoryChange: function(e) {
    const idx = parseInt(e.detail.value);
    this.setData({ categoryIndex: idx, 'form.category': CATEGORY_OPTIONS[idx] });
  },

  onVipLevelChange: function(e) {
    const levels = [1, 2, 0];  // picker顺序：普通VIP=1，SVIP=2，纯配额包=0（不动等级）
    const vipLevel = levels[parseInt(e.detail.value)];
    const updates = { 'form.vip_level': vipLevel };
    // 纯配额包自动清零 vip_days，避免出现 level=0 但 days>0 的矛盾
    if (vipLevel === 0) updates['form.vip_days'] = '0';
    this.setData(updates);
  },

  // ── 新增/编辑弹窗 ──

  openNew: function() {
    const firstStore = this.data.storeList[0];
    this.setData({
      showModal: true, editing: null,
      typeIndex: 0, storeIndex: 0, categoryIndex: 0,
      editImages: [],
      form: {
        type: 'store',
        store_id: firstStore ? firstStore.store_id : '',
        name: '', price: '', description: '',
        screen_minutes: '0', vip_level: 1, vip_days: '30', image_gen_quota: '0', affinity_delta: '30',
        category: CATEGORY_OPTIONS[0],
      },
    });
  },

  openEdit: function(e) {
    const pkg = e.currentTarget.dataset.pkg;
    const typeIndex = pkg.type === 'online' ? 1 : 0;
    const storeIndex = Math.max(0, this.data.storeList.findIndex(s => s.store_id === pkg.store_id));
    const catIdx = Math.max(0, CATEGORY_OPTIONS.indexOf(pkg.category || 'other'));
    this.setData({
      showModal: true, editing: pkg, typeIndex, storeIndex,
      categoryIndex: catIdx,
      // editImages 只存相对路径，显示时通过 baseUrl 拼接
      editImages: (pkg.images || []).map(p =>
        p.startsWith('http') ? p.replace(/^.*\/api\/mp\/shop\/images\//, '') : p
      ),
      form: {
        type:           pkg.type || 'store',
        store_id:       pkg.store_id || '',
        name:           pkg.name,
        price:          String(pkg.price),
        description:    pkg.description || '',
        screen_minutes: String(pkg.screen_minutes || 0),
        vip_level:      pkg.vip_level || 1,
        vip_days:       String(pkg.vip_days || 30),
        image_gen_quota: String(pkg.image_gen_quota || 0),
        affinity_delta: String(pkg.affinity_delta || 30),
        category:       pkg.category || 'other',
      },
    });
  },

  closeModal: function() { this.setData({ showModal: false, editImages: [] }); },

  onTypeChange: function(e) {
    const idx = parseInt(e.detail.value);
    if (idx === 1) {
      this.setData({
        typeIndex: idx, 'form.type': 'online',
        'form.store_id': '', 'form.screen_minutes': '0',
        'form.name': '', 'form.price': '39',
        'form.vip_days': '30', 'form.image_gen_quota': '0',
        storeIndex: 0,
        categoryIndex: CATEGORY_OPTIONS.indexOf('online'),
        'form.category': 'online',
      });
    } else {
      this.setData({
        typeIndex: idx, 'form.type': 'store',
        'form.vip_level': 1, 'form.vip_days': '30', 'form.image_gen_quota': '0',
        'form.name': '', 'form.price': '',
        categoryIndex: 0,
        'form.category': CATEGORY_OPTIONS[0],
      });
    }
  },

  onFormInput: function(e) {
    const key = e.currentTarget.dataset.key;
    this.setData({ [`form.${key}`]: e.detail.value });
  },

  submitForm: function() {
    const { form, editing } = this.data;
    if (!form.name.trim()) { wx.showToast({ title: '请填写套餐名', icon: 'none' }); return; }
    if (!form.price)       { wx.showToast({ title: '请填写价格',   icon: 'none' }); return; }

    const body = {
      name:           form.name.trim(),
      price:          parseFloat(form.price),
      description:    form.description.trim(),
      affinity_delta: parseInt(form.affinity_delta) || 30,
      category:       form.category,
    };

    if (!editing) {
      body.type = form.type;
      if (form.type === 'store') {
        body.store_id       = form.store_id;
        body.screen_minutes = parseInt(form.screen_minutes) || 0;
      } else {
        body.vip_level       = parseInt(form.vip_level) || 0;
        body.vip_days        = parseInt(form.vip_days) || 0;
        body.image_gen_quota = parseInt(form.image_gen_quota) || 0;
      }
    } else {
      // 编辑模式：线上套餐三个维度都要传，否则后端不更新
      if (form.type === 'online' || (editing && editing.type === 'online')) {
        body.vip_level       = parseInt(form.vip_level) || 0;
        body.vip_days        = parseInt(form.vip_days) || 0;
        body.image_gen_quota = parseInt(form.image_gen_quota) || 0;
      }
    }

    this.setData({ submitting: true });
    const req = editing
      ? manageRequest(`/packages/${editing.package_id}`, 'PATCH', body)
      : manageRequest('/packages', 'POST', body);

    req.then(res => {
      if (res && res.code === 200) {
        wx.showToast({ title: editing ? '已更新' : '已新增', icon: 'success' });
        this.setData({ showModal: false, editImages: [] });
        this._load();
      } else {
        wx.showModal({ title: '失败', content: (res && res.msg) || '请重试', showCancel: false });
      }
    }).finally(() => this.setData({ submitting: false }));
  },

  toggleActive: function(e) {
    const pkg = e.currentTarget.dataset.pkg;
    const action = pkg.is_active ? '下架' : '上架';
    wx.showModal({
      title: `确认${action}`,
      content: `${action}套餐「${pkg.name}」？`,
      confirmColor: pkg.is_active ? '#EF4444' : '#07C160',
      success: (r) => {
        if (!r.confirm) return;
        manageRequest(`/packages/${pkg.package_id}`, 'PATCH', { is_active: !pkg.is_active })
          .then(res => {
            if (res && res.code === 200) {
              wx.showToast({ title: `已${action}`, icon: 'success' });
              this._load();
            }
          });
      }
    });
  },

  // ── [Shop] 图片管理 ──

  // 上传图片（编辑模式，套餐已存在时才能上传）
  uploadImage: function() {
    const { editing, editImages, imgUploading } = this.data;
    if (!editing) {
      wx.showToast({ title: '请先保存套餐再上传图片', icon: 'none' });
      return;
    }
    if (imgUploading) return;
    if (editImages.length >= 9) {
      wx.showToast({ title: '最多上传 9 张图片', icon: 'none' });
      return;
    }

    // [Fix] 在选图前固定 pkg_id，避免异步回调时 this.data.editing 已变化
    const pkg_id = editing.package_id;
    if (!pkg_id) {
      wx.showToast({ title: '套餐ID获取失败，请关闭后重试', icon: 'none' });
      return;
    }

    wx.chooseMedia({
      count: 9 - editImages.length,
      mediaType: ['image'],
      sourceType: ['album', 'camera'],
      success: (res) => {
        this._uploadImages(res.tempFiles, 0, pkg_id);
      },
    });
  },

  // [Fix] pkg_id 作为参数传入，不依赖 this.data.editing（异步回调中可能已变化）
  _uploadImages: function(files, idx, pkg_id) {
    if (idx >= files.length) {
      this.setData({ imgUploading: false });
      wx.hideLoading();
      wx.showToast({ title: '上传完成', icon: 'success' });
      this._load();
      return;
    }

    wx.showLoading({ title: `上传 ${idx + 1}/${files.length}…`, mask: true });
    this.setData({ imgUploading: true });

    wx.uploadFile({
      url: `${app.globalData.BASE_URL}/api/m/packages/${pkg_id}/images`,
      filePath: files[idx].tempFilePath,
      name: 'image',
      header: { 'Authorization': `Bearer ${app.globalData.token}` },
      success: (r) => {
        try {
          const data = JSON.parse(r.data);
          if (r.statusCode === 200 && data.code === 200) {
            // data.data.path 是相对路径，直接存
            const newImages = [...this.data.editImages, data.data.path];
            this.setData({ editImages: newImages, 'editing.images': newImages });
          } else {
            wx.showToast({ title: data.msg || `第${idx+1}张上传失败`, icon: 'none', duration: 2000 });
          }
        } catch(e) {
          console.error('[Upload] 解析响应失败:', e, r.data);
          wx.showToast({ title: '响应解析失败', icon: 'none' });
        }
      },
      fail: (err) => {
        console.error('[Upload] wx.uploadFile fail:', JSON.stringify(err));
        wx.showToast({ title: '网络异常，上传失败', icon: 'none' });
      },
      complete: () => {
        wx.hideLoading();
        this._uploadImages(files, idx + 1, pkg_id);
      }
    });
  },

  // 删除图片（长按触发）
  deleteImage: function(e) {
    const path = e.currentTarget.dataset.path;
    const pkg_id = this.data.editing && this.data.editing.package_id;
    if (!pkg_id || !path) return;

    wx.showModal({
      title: '删除图片',
      content: '确认删除这张图片？',
      confirmColor: '#EF4444',
      success: (r) => {
        if (!r.confirm) return;
        manageRequest(`/packages/${pkg_id}/images`, 'DELETE', { path })
          .then(res => {
            if (res && res.code === 200) {
              const newImages = this.data.editImages.filter(p => p !== path);
              this.setData({ editImages: newImages, 'editing.images': newImages });
              wx.showToast({ title: '已删除', icon: 'success' });
            }
          });
      }
    });
  },

  // 预览图片
  // 切换已下架折叠展开
  toggleInactive: function(e) {
    const storeId = e.currentTarget.dataset.storeId;
    const expanded = { ...this.data.inactiveExpanded };
    expanded[storeId] = !expanded[storeId];
    this.setData({ inactiveExpanded: expanded });
  },

  noop: function() {},  // catchtap 阻止冒泡用

  // ── [BuyerPortrait] 跳转买家画像页 ──
  openBuyerProfiles: function(e) {
    const pkg = e.currentTarget.dataset.pkg;
    wx.navigateTo({
      url: `/pages/manage/admin/buyer_profiles?package_id=${pkg.package_id}&package_name=${encodeURIComponent(pkg.name)}`,
    });
  },

  previewImage: function(e) {
    const path = e.currentTarget.dataset.path;
    const base = app.globalData.BASE_URL || '';
    // path 可能已经是完整 URL（来自列表页），也可能是相对路径（来自编辑弹窗）
    const toFull = p => p.startsWith('http') ? p : `${base}/api/p/images/${p}`;
    const urls = this.data.editImages.map(toFull);
    const current = toFull(path);
    wx.previewImage({ current, urls });
  },
});
