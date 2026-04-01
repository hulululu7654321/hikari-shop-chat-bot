/**
 * pages/manage/manage.js — 管理页面入口
 * 调 /api/manage/whoami 确认角色，自动跳转到对应界面
 * 安全说明：角色判断只影响跳转目标，后端每个接口都有独立鉴权
 */

const app = getApp();

Page({
  data: {
    loading: true,
    errMsg: '',
  },

  onLoad: function() {
    this._checkRole();
  },

  onShow: function() {
    // 从子页面返回时重新检查（防止权限被撤销后还停留在此页）
    if (!this.data.loading) {
      this._checkRole();
    }
  },

  _checkRole: function() {
    this.setData({ loading: true, errMsg: '' });

    const baseUrl = app.globalData.BASE_URL || '';
    const token   = app.globalData.token   || '';

    // 防御：token 未就绪时不请求（冷启动极短窗口期）
    if (!baseUrl || !token) {
      console.warn('[Manage] BASE_URL 或 token 未就绪，延迟 500ms 重试');
      setTimeout(() => this._checkRole(), 500);
      return;
    }

    console.log('[Manage] whoami 请求中…', baseUrl);

    wx.request({
      url: `${baseUrl}/api/m/whoami`,
      method: 'GET',
      header: { 'Authorization': `Bearer ${token}` },
      success: (res) => {
        const body = res.data || {};
        console.log('[Manage] whoami 响应:', res.statusCode, body);

        if (res.statusCode !== 200 || body.code !== 200) {
          this.setData({
            loading: false,
            errMsg: res.statusCode === 403 ? '没有管理权限喵' : `加载失败(${res.statusCode})，请重试`,
          });
          return;
        }

        const { manage_role, manage_store_id, store_name } = body.data;

        if (!manage_role) {
          this.setData({ loading: false, errMsg: '没有管理权限喵' });
          return;
        }

        // 把角色信息存到 globalData，子页面直接读，不用重复请求
        // [M5 备注] silentLogin 已从 login 接口写入一次 manageRole，此处 whoami 覆写与其来源一致，无副作用
        app.globalData.manageRole      = manage_role;
        app.globalData.manageStoreId   = manage_store_id || null;
        app.globalData.manageStoreName = store_name || '';

        console.log('[Manage] 跳转到', manage_role === 'admin' ? 'admin/index' : 'store/index');

        if (manage_role === 'admin') {
          wx.redirectTo({ url: '/pages/manage/admin/index' });
        } else {
          wx.redirectTo({ url: '/pages/manage/store/index' });
        }
      },
      fail: (err) => {
        console.error('[Manage] whoami 请求失败:', err);
        this.setData({ loading: false, errMsg: '网络异常，请重试' });
      }
    });
  },

  goBack: function() {
    wx.navigateBack();
  },
});
