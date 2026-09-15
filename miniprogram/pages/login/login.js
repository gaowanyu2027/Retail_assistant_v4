// 登录页 — 账号密码登录（无验证码/手机号/微信）
const api = require('../../utils/api');

Page({
  data: {
    username: '',
    password: '',
    loading: false,
    error: ''
  },

  onLoad() {
    // 已有本地令牌：先向后端校验是否仍有效，有效则直接进主页
    const app = getApp();
    if (app.isLoggedIn()) {
      api.me().then(() => {
        wx.switchTab({ url: '/pages/index/index' });
      }).catch(() => {
        // 令牌已失效（后端已吊销/过期），停留在登录页
      });
    }
  },

  onInputUsername(e) {
    this.setData({ username: e.detail.value, error: '' });
  },

  onInputPassword(e) {
    this.setData({ password: e.detail.value, error: '' });
  },

  onSubmit() {
    const that = this;
    const { username, password, loading } = this.data;
    if (loading) return;
    if (!username || !password) {
      this.setData({ error: '请输入用户名和密码' });
      return;
    }
    this.setData({ loading: true, error: '' });

    api.login(username, password).then((res) => {
      getApp().setAuth(res);
      that.setData({ password: '', loading: false });
      wx.switchTab({ url: '/pages/index/index' });
    }).catch((err) => {
      that.setData({ loading: false, error: err.message || '登录失败' });
    });
  }
});
