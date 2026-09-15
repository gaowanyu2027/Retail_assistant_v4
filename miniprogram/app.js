// 智能零售分析系统 — 小程序端
App({
  globalData: {
    // 局域网真机预览：电脑局域网 IP（手机与电脑同一 Wi-Fi，最稳；电脑 IP 会变需及时更新）
    // 公网扫码：花生壳域名（https://12750ag6gw456.vicp.fun/api），需花生壳隧道在线
    apiBase: 'http://192.168.10.6:8000/api',
    // 登录态：后端会话令牌。小程序不携带 Cookie，统一走 Authorization: Bearer
    token: '',
    user: null,
    permissions: []
  },

  onLaunch() {
    this._restoreAuth()
  },

  // ==================== 登录态 ====================

  _restoreAuth() {
    try {
      this.globalData.token = wx.getStorageSync('auth_token') || ''
      const u = wx.getStorageSync('auth_user') || null
      this.globalData.user = u
      this.globalData.permissions = (u && u.permissions) || []
    } catch (e) {
      this.globalData.token = ''
      this.globalData.user = null
      this.globalData.permissions = []
    }
  },

  /** 登录成功后写入登录态 */
  setAuth(payload) {
    const u = (payload && payload.user) || null
    this.globalData.token = (payload && payload.token) || ''
    this.globalData.user = u
    this.globalData.permissions = (payload && payload.permissions) || []
    try {
      wx.setStorageSync('auth_token', this.globalData.token)
      wx.setStorageSync('auth_user', u)
    } catch (e) { /* 存储失败不影响本次会话 */ }
  },

  /** 清除登录态（登出 / 401 时调用） */
  clearAuth() {
    this.globalData.token = ''
    this.globalData.user = null
    this.globalData.permissions = []
    try {
      wx.removeStorageSync('auth_token')
      wx.removeStorageSync('auth_user')
    } catch (e) { /* ignore */ }
  },

  isLoggedIn() {
    return !!this.globalData.token
  },

  /** 是否具备某权限（root 与 platform 的能力差异由此驱动 UI 显隐） */
  hasPerm(perm) {
    return (this.globalData.permissions || []).indexOf(perm) !== -1
  }
})
