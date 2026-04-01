/**
 * 希卡莉喵 (Hikari-Nya) - 报告渲染引擎 (v1.2.0 内存贯通版)
 * 物理执行准则：
 * 1. 内存优先：【核心修复】优先读取 'current_report_md' 缓存，实现秒开且不丢 MD 排版。
 * 2. 鉴权回溯：若从历史记录进入，则通过 URL 执行带 JWT 的物理拉取。
 * 3. 容错增强：物理隔离不同来源的数据，确保渲染引擎不空转。
 */

const app = getApp();

Page({
  data: { 
    reportContent: '', 
    isLoading: true,
    errorMsg: '',
    source: '' // 追踪数据来源：'mem' 或 'api'
  },

  onLoad: function(options) {
    console.log("🚀 [Report-Engine] 渲染引擎唤醒，参数:", options);
    
    // 🚀 模式一：内存透传（秒开模式）
    if (options && options.source === 'mem') {
      this.loadFromMemory();
      return;
    }

    // 🚀 模式二：物理拉取（历史回溯模式）
    if (options && options.url) {
      const targetUrl = decodeURIComponent(options.url);
      this.loadFromRemote(targetUrl);
      return;
    }

    // 模式三：本地兜底（直接读取最近一份）
    this.loadFromMemory(true);
  },

  /**
   * 🛠️ 物理逻辑：从本地缓存瞬时提取 MD 全文
   */
  loadFromMemory: function(isFallback = false) {
    console.log("📂 [Storage] 正在执行内存打捞...");
    try {
      const content = wx.getStorageSync('current_report_md');
      if (content && content.length > 0) {
        this.setData({ 
          reportContent: content,
          source: 'memory',
          isLoading: false 
        });
        console.log("✅ [Storage] 内存打捞成功，字节数:", content.length);
      } else {
        this.setData({ 
          reportContent: isFallback ? '报告数据已物理挥发，请重新炼制喵...' : '读取中...',
          isLoading: false 
        });
      }
    } catch (e) {
      console.error("❌ [Storage-Error] 内存打捞坍塌:", e);
      this.setData({ errorMsg: '本地链路异常', isLoading: false });
    }
  },

  /**
   * 🛠️ 物理逻辑：带鉴权的网络拉取（针对旧版或历史数据）
   */
  loadFromRemote: function(url) {
    const that = this;
    console.log("🌐 [Network] 正在执行物理拉取:", url);
    
    wx.request({
      url: url,
      method: 'GET',
      header: {
        'Authorization': 'Bearer ' + (app.globalData.token || '')
      },
      success: function(res) {
        if (res.statusCode === 200) {
          that.setData({ 
            reportContent: res.data,
            source: 'api',
            isLoading: false 
          });
          console.log("✅ [Network] 物理拉取成功");
        } else {
          that.setData({ 
            reportContent: `拉取失败 (HTTP ${res.statusCode})喵，可能是权限已过期。`,
            isLoading: false 
          });
        }
      },
      fail: function() {
        that.setData({ 
          reportContent: '网络链路抖动，无法连接至炼丹炉...',
          isLoading: false 
        });
      }
    });
  },

  /**
   * 💡 物理动作：同步至系统剪贴板（保留 MD 格式）
   */
  copyReport: function() {
    if (!this.data.reportContent || this.data.reportContent.length < 10) {
      wx.showToast({ title: '内容不足', icon: 'none' });
      return;
    }
    wx.setClipboardData({
      data: this.data.reportContent,
      success: () => wx.showToast({ title: '已复制 MD 原文', icon: 'success' })
    });
  },

  /**
   * 💡 物理动作：返回终端
   */
  goBack: function() { 
    wx.navigateBack(); 
  }
});