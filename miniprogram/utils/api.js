// API 请求封装 — 对接现有 FastAPI 后端
//
// 鉴权说明：后端默认封启（/api/* 需登录）。浏览器端靠 HttpOnly Cookie，
// 但小程序不携带 Cookie，因此这里统一用 Authorization: Bearer <token>。
// 401 → 清除本地登录态并回到登录页；403 → 提示权限不足（角色差异）。
const app = getApp();

function authHeader() {
  const h = { 'Content-Type': 'application/json' };
  const t = (app.globalData && app.globalData.token) || '';
  if (t) h['Authorization'] = 'Bearer ' + t;
  return h;
}

function toLogin() {
  app.clearAuth();
  wx.reLaunch({ url: '/pages/login/login' });
}

function request(method, path, data) {
  return new Promise((resolve, reject) => {
    wx.request({
      url: app.globalData.apiBase + path,
      method: method,
      data: data || {},
      timeout: 60000,
      header: authHeader(),
      success(res) {
        if (res.statusCode === 200) {
          resolve(res.data);
        } else if (res.statusCode === 401) {
          toLogin();
          reject(new Error('登录已过期，请重新登录'));
        } else if (res.statusCode === 403) {
          reject(new Error((res.data && res.data.detail) || '权限不足'));
        } else {
          reject(new Error('HTTP ' + res.statusCode + ': ' + JSON.stringify(res.data || '').slice(0, 80)));
        }
      },
      fail(err) {
        reject(new Error(err.errMsg || '网络请求失败，请确认后端已启动'));
      }
    });
  });
}

// ==================== 登录相关（登录接口本身不能走 401 跳转逻辑） ====================

function login(username, password) {
  return new Promise((resolve, reject) => {
    wx.request({
      url: app.globalData.apiBase + '/auth/login',
      method: 'POST',
      data: { username: username, password: password },
      timeout: 20000,
      header: { 'Content-Type': 'application/json' },
      success(res) {
        if (res.statusCode === 200) {
          resolve(res.data);
        } else if (res.statusCode === 401) {
          reject(new Error('用户名或密码错误，或账号已被停用'));
        } else {
          reject(new Error('登录失败（HTTP ' + res.statusCode + '）'));
        }
      },
      fail(err) {
        reject(new Error(err.errMsg || '网络请求失败，请确认后端已启动'));
      }
    });
  });
}

function logout() {
  return new Promise((resolve) => {
    wx.request({
      url: app.globalData.apiBase + '/auth/logout',
      method: 'POST',
      header: authHeader(),
      complete() {
        app.clearAuth();
        resolve();
      }
    });
  });
}

/**
 * 签发**一次性短时效票据**（用于 WS / 音频等无法自定义请求头的场景）。
 *
 * 为什么不直接把会话令牌放 URL：URL 会进访问日志（uvicorn / 反向代理 / 内网穿透），
 * 会话令牌一旦落日志即等同泄露（CWE-598）。票据 60 秒有效、**用后即焚**，
 * 即便落进日志也已失效。后端默认拒绝 URL 里的主会话令牌（?token=）。
 *
 * 注意：票据一次性 → **每次连接（含重连）都要重新签发**。
 */
function getTicket(purpose) {
  return request('POST', '/auth/ws-ticket', { purpose: purpose || '' })
    .then((res) => (res && res.ticket) || '');
}

/**
 * WebSocket 地址（附带一次性票据）。
 * @param {string} path 以 /api 开头，如 '/api/ws/stream'
 * @param {string} ticket getTicket() 签发的票据
 */
function wsUrl(path, ticket) {
  const base = (app.globalData.apiBase || '').replace(/\/api$/, '');
  let url = base.replace(/^http/, 'ws') + path;
  if (ticket) {
    url += (url.indexOf('?') === -1 ? '?' : '&') + 'ticket=' + encodeURIComponent(ticket);
  }
  return url;
}

/**
 * 可直接交给音频播放器的 URL（附带一次性票据）。
 * 音频元素无法设置请求头 → 只能用 URL 传凭据，因此同样走票据而非会话令牌。
 */
function ticketUrl(path, ticket) {
  const url = app.globalData.apiBase + path;
  if (!ticket) return url;
  return url + (url.indexOf('?') === -1 ? '?' : '&') + 'ticket=' + encodeURIComponent(ticket);
}

module.exports = {
  authHeader: authHeader,
  wsUrl: wsUrl,
  ticketUrl: ticketUrl,
  getTicket: getTicket,
  // ===== 鉴权 =====
  login: login,
  logout: logout,
  me: () => request('GET', '/auth/me'),
  // 自然语言问答（模板快路径 + Agent 慢路径）
  query: (question, sessionId) => request('POST', '/queries', { question: question, session_id: sessionId }),
  // 仪表盘全局快照（热度排行 + 告警汇总）
  getDashboard: () => request('GET', '/reports/dashboard'),
  // 热度 × 销量转化率四象限
  getHotVsSales: (hours) => request('GET', '/analytics/hot-vs-sales?hours=' + (hours || 1)),
  // 定期热度汇报
  getHeatReports: (limit) => request('GET', '/reports/heat-reports?limit=' + (limit || 5)),
  // 表情统计
  getEmotionStats: () => request('GET', '/emotions/stats'),
  // ===== 摄像头管理（模块化按需加载） =====
  getCameras: () => request('GET', '/cameras'),
  scanCameras: () => request('GET', '/cameras/scan'),
  getModules: () => request('GET', '/cameras/modules'),
  addCamera: (data) => request('POST', '/cameras', data),
  deleteCamera: (id) => request('DELETE', '/cameras/' + id),
  loadModule: (id, module) => request('POST', '/cameras/' + id + '/modules', { module: module }),
  unloadModule: (id, module) => request('DELETE', '/cameras/' + id + '/modules/' + module),
  setModuleEnabled: (id, module, enabled) => request('PUT', '/cameras/' + id + '/modules/' + module + '/enabled', { enabled: enabled }),
  moduleStats: (id, module) => request('GET', '/cameras/' + id + '/modules/' + module + '/stats'),
  setActiveCamera: (id) => request('POST', '/cameras/active', { cam_id: id }),
  // ===== 多会话管理 =====
  listSessions: () => request('GET', '/chat/sessions'),
  createSession: (title) => request('POST', '/chat/sessions', { title: title || '新会话' }),
  renameSession: (id, title) => request('PUT', '/chat/sessions/' + id + '/rename', { title: title }),
  deleteSession: (id) => request('DELETE', '/chat/sessions/' + id),
  searchSessions: (q) => request('GET', '/chat/search?q=' + encodeURIComponent(q)),
  getMessages: (id) => request('GET', '/chat/sessions/' + id + '/messages')
};
