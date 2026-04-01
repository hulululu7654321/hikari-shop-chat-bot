/**
 * pages/invite/invite.js
 * 邀请码激活页
 * 从 index.js 跳转过来（invite_only=true 且 has_access=false）
 * 激活成功后跳到 chat 页
 */

const app = getApp();

Page({
  data: {
    inputCode: '',
    isSubmitting: false,
    errMsg: '',
    // ★ v9.1 宽限期到期状态
    isVisitExpired: false,
    subTitle: '当前为邀请制内测，请输入邀请码继续',
  },

  onLoad: function(options) {
    // 如果已经有访问权限（比如用户手动导航到此页），直接跳 chat
    if (app.globalData.hasAccess) {
      wx.redirectTo({ url: '/pages/chat/chat' });
      return;
    }

    // ★ v9.1 宽限期到期：显示提示文案，隐藏输入框，引导联系管理员
    if (options.reason === 'visit_expired') {
      this.setData({
        isVisitExpired: true,
        subTitle: '到店体验宽限期已到期，请联系管理员获取邀请码继续使用喵',
      });
    }
  },

  onInput: function(e) {
    this.setData({ inputCode: e.detail.value, errMsg: '' });
  },

  // 激活邀请码
  activate: function() {
    const code = this.data.inputCode.trim();
    if (!code) {
      this.setData({ errMsg: '请输入邀请码喵' });
      return;
    }
    if (this.data.isSubmitting) return;

    this.setData({ isSubmitting: true, errMsg: '' });

    wx.request({
      url: `${app.globalData.BASE_URL}/api/u/invites/activate`,
      method: 'POST',
      data: { code },
      header: {
        'Content-Type': 'application/json',
        'Authorization': `Bearer ${app.globalData.token}`
      },
      success: (res) => {
        const body = res.data || {};
        if (res.statusCode === 200 && body.code === 200) {
          // 激活成功：更新 globalData，建立 WS，进入聊天室
          app.globalData.hasAccess = true;
          wx.setStorageSync('hikari_has_access', '1');
          wx.showToast({ title: body.msg || '激活成功喵！', icon: 'success', duration: 1500 });
          // 主动建立 WS：redirectTo 不触发 App.onShow，不能依赖它自动连接
          // socketStatus 若已是 connected/connecting（极少数情况）则 connectWebSocket 内部会跳过
          app.connectWebSocket();
          setTimeout(() => {
            wx.redirectTo({ url: '/pages/chat/chat' });
          }, 1500);
        } else {
          this.setData({ errMsg: body.msg || '激活失败，请检查邀请码是否正确' });
        }
      },
      fail: () => {
        this.setData({ errMsg: '网络异常，稍后再试喵' });
      },
      complete: () => {
        this.setData({ isSubmitting: false });
      }
    });
  },
});
