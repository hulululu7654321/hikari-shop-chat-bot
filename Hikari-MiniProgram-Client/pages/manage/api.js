/**
 * pages/manage/api.js — 管理页面公共请求工具
 * 所有 /api/manage/* 接口统一走这里，自动带 token、处理 401/403
 */

// getApp() 不能在模块顶层调用，必须在函数内部调用
// 原因：模块被 require 时 App 可能还未初始化，顶层调用会返回 undefined

function manageRequest(path, method, data) {
  method = method || 'GET';
  return new Promise((resolve, reject) => {
    const app     = getApp();
    const baseUrl = (app && app.globalData.BASE_URL) || '';
    const token   = (app && app.globalData.token)   || '';
    if (!baseUrl || !token) {
      console.error('[ManageAPI] BASE_URL 或 token 未就绪，请重新进入');
      wx.showToast({ title: '登录信息未就绪，请返回重试', icon: 'none' });
      reject({ code: -1, msg: 'not_ready' });
      return;
    }
    wx.request({
      url: `${baseUrl}/api/m${path}`,
      method: method,
      data: data || {},
      header: {
        'Content-Type': 'application/json',
        'Authorization': `Bearer ${token}`,
      },
      success: (res) => {
        if (res.statusCode === 401) {
          wx.showToast({ title: '登录已过期，请重新进入', icon: 'none' });
          reject({ code: 401 });
          return;
        }
        if (res.statusCode === 403) {
          wx.showToast({ title: '没有操作权限喵', icon: 'none' });
          reject({ code: 403 });
          return;
        }
        resolve(res.data);
      },
      fail: (err) => {
        wx.showToast({ title: '网络异常，请稍后再试', icon: 'none' });
        reject(err);
      }
    });
  });
}

// 格式化北京时间，供订单/邀请码列表用
function formatBJTime(isoStr) {
  if (!isoStr) return '';
  const d = new Date(isoStr.endsWith('Z') ? isoStr : isoStr + 'Z');
  if (isNaN(d.getTime())) return isoStr;
  const pad = n => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${pad(d.getMonth()+1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

// 今日北京日期字符串 YYYYMMDD
function todayBJ() {
  const now = new Date();
  const bj = new Date(now.getTime() + 8 * 3600 * 1000);
  const pad = n => String(n).padStart(2, '0');
  return `${bj.getUTCFullYear()}${pad(bj.getUTCMonth()+1)}${pad(bj.getUTCDate())}`;
}

module.exports = { manageRequest, formatBJTime, todayBJ };
