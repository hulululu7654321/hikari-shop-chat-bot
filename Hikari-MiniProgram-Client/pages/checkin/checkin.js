/**
 * pages/checkin/checkin.js
 * 店铺签到页 — 从二维码 URL 参数解析 store_token，调用签到接口
 *
 * 二维码 URL 格式（由后端 _build_qr_url 生成）：
 *   https://your-domain.com/store/checkin?store_token=xxx
 * 微信小程序扫码后跳转到此页：
 *   /pages/checkin/checkin?store_token=xxx
 */

const app = getApp();

Page({
  data: {
    status: 'loading',      // loading | success | new | error
    statusText: '',
    welcomeText: '',
    displayName: '',
    errorMsg: '',
    storeToken: '',
  },

  onLoad: function(options) {
    const storeToken = (options.store_token || '').trim();
    if (!storeToken) {
      wx.redirectTo({ url: '/pages/chat/chat' });
      return;
    }
    this.setData({ storeToken });
    // [Fix-F4] onAuthReady 已统一为异步，单一注册即可，消灭热启动下的双重 POST
    app.onAuthReady(() => { this.doCheckin(storeToken); });
  },

  doCheckin: function(storeToken) {
    if (!app.globalData.token) {
      this.setData({ status: 'error', errorMsg: '登录状态异常，请重启小程序喵' });
      return;
    }

    wx.request({
      url: `${app.globalData.BASE_URL}/api/s/checkin`,
      method: 'POST',
      data: { store_token: storeToken },
      header: {
        'Content-Type': 'application/json',
        'Authorization': `Bearer ${app.globalData.token}`
      },
      success: (res) => {
        const body = res.data || {};
        if (res.statusCode === 200 && body.code === 200) {
          const d = body.data || {};
          // 更新全局在店状态
          if (app.globalData.userInfo) {
            app.globalData.userInfo.is_instore = true;
          }
          this.setData({
            status: d.is_new_customer ? 'new' : 'success',
            welcomeText: d.welcome_text || '欢迎光临喵！',
            displayName: d.display_name || '神秘冒险者',
          });
          // 1.5秒后自动跳转到聊天页
          setTimeout(() => { this.goToChat(); }, 1800);
        } else if (res.statusCode === 403) {
          this.setData({
            status: 'error',
            errorMsg: body.msg || '二维码已过期喵，请扫最新的～'
          });
        } else if (res.statusCode === 429) {
          this.setData({
            status: 'error',
            errorMsg: '操作太频繁了喵，稍等一下再试～'
          });
        } else {
          this.setData({
            status: 'error',
            errorMsg: body.msg || '签到失败喵，请重试～'
          });
        }
      },
      fail: () => {
        this.setData({
          status: 'error',
          errorMsg: '网络断了喵，检查一下网络再扫～'
        });
      }
    });
  },

  goToChat: function() {
    // 跳转到聊天页，用 redirectTo 防止返回到签到页
    wx.redirectTo({ url: '/pages/chat/chat' });
  },

  goBack: function() {
    wx.navigateBack({ delta: 1 });
  }
});
