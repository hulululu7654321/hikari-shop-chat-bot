/**
 * index.js - 启动页 (v6.0.0 极简信号版 + v9.0 邀请制跳转)
 * 物理执行准则：
 * 1. 绝对信任：拆除所有前端防御拦截器，完全信任 app.js 传来的打捞结果。
 * 2. 信号驱动：死等 onAuthReady 信号，拿到名字立刻展示并跳转。
 * 3. 大道至简：不再瞎猜内存状态，只专注 UI 渲染和转场。
 *
 * v9.0 新增：
 * - syncUI 里检查 has_access，未激活邀请码时跳 invite 页而非 chat 页
 */

const app = getApp();

Page({
  data: {
    statusText: '正在找到你喵，马上就来~',
    isNavigating: false
  },

  onLoad: function() {
    console.log("🚀 [Index] 启动页加载，静候真实身份归位...");

    // [Fix-F4/F6] onAuthReady 已统一为异步，单一注册即可，无需 if 兜底
    app.onAuthReady((info) => {
      console.log("🔔 [Index] 接收到身份确认信号，档案名：", info.user_name);
      this.syncUI(info);
    });
  },

  syncUI: function(info) {
    // 物理防抖：防止 app.js 抽风发两次信号导致重复跳转
    if (this.data.isNavigating) return;

    const displayName = info.user_name || '未知生命体';

    // ★ v9.0 邀请制判断：invite_only=true 且 has_access=false → 跳邀请码激活页
    // 用 globalData 里的值（由 silentLogin 写入），比 info 对象更可靠
    const inviteOnly  = app.globalData.inviteOnly;
    const hasAccess   = app.globalData.hasAccess;

    if (inviteOnly && !hasAccess) {
      console.log("🔒 [Index] 邀请制生效，用户未激活，跳转邀请码页");
      this.setData({ isNavigating: true });
      wx.redirectTo({ url: '/pages/invite/invite' });
      return;
    }

    // 正常流程：进聊天室
    this.setData({
      statusText: `本喵认出你了——${displayName} 喵~`,
      isNavigating: true
    });

    console.log(`✅ [Index] UI 渲染完毕，当前真身：${displayName}`);
    this.enterTerminal();
  },

  enterTerminal: function() {
    setTimeout(() => {
      console.log("🚀 [Route] 身份验证彻底闭环，正在穿透至交互界面...");
      wx.redirectTo({
        url: '/pages/chat/chat',
        fail: (err) => {
          console.error("❌ [Route-Collapse] 界面跳转失败:", err);
          this.setData({ statusText: '传送失败了喵……刷新试试？' });
        }
      });
    }, 1200);
  }
})
