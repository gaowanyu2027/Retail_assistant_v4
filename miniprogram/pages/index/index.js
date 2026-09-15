// 问答页 — 对接 /api/queries（模板毫秒级 + Agent 分析）
const api = require('../../utils/api');

Page({
  data: {
    question: '',
    answer: '',
    intent: '',
    confidence: '',
    time: '',
    loading: false,
    structured: [],
    quickQuestions: [
      '1号货架现在客流怎么样',
      '今天有没有异常告警',
      '顾客情绪怎么样',
      '帮我看看门店整体运营情况',
      '为什么销量差？帮我分析一下'
    ]
  },

  onLoad() {
    let sid = wx.getStorageSync('mp_session_id');
    if (!sid) {
      sid = 'mp_' + Date.now() + '_' + Math.floor(Math.random() * 100000);
      wx.setStorageSync('mp_session_id', sid);
    }
    this.sessionId = sid;
  },

  onInput(e) {
    this.setData({ question: e.detail.value });
  },

  onQuick(e) {
    this.setData({ question: e.currentTarget.dataset.item });
    this.send();
  },

  send() {
    const q = (this.data.question || '').trim();
    if (!q || this.data.loading) return;
    this.setData({ loading: true, answer: '', intent: '', confidence: '' });
    this._streamAsk(q);
  },

  // 流式回答：POST /api/queries/stream（SSE），onChunkReceived 逐 token 打字机渲染
  _streamAsk(q) {
    const app = getApp();
    const task = wx.request({
      url: app.globalData.apiBase + '/queries/stream',
      method: 'POST',
      data: { question: q, session_id: this.sessionId },
      // 鉴权：SSE 也走 Bearer（小程序不携带 Cookie）
      header: api.authHeader(),
      enableChunked: true,
      success: (res) => {
        if (res.statusCode === 401) {
          getApp().clearAuth();
          this.setData({ loading: false });
          wx.reLaunch({ url: '/pages/login/login' });
          return;
        }
        if (res.statusCode !== 200) {
          this.setData({ loading: false });
          wx.showToast({ title: '请求失败：HTTP ' + res.statusCode, icon: 'none' });
          return;
        }
        this.setData({ loading: false, time: this._now() });
      },
      fail: (err) => {
        this.setData({ loading: false });
        wx.showToast({ title: '请求失败：' + String(err.errMsg || '').slice(0, 20), icon: 'none' });
      }
    });
    let buf = '', full = '';
    task.onChunkReceived((res) => {
      let chunk;
      try {
        chunk = new TextDecoder().decode(new Uint8Array(res.data));
      } catch (e) {
        chunk = String.fromCharCode.apply(null, new Uint8Array(res.data));
      }
      buf += chunk;
      let idx;
      // SSE 帧以 \n\n 分隔，取 "data: xxx" 里的内容累积
      while ((idx = buf.indexOf('\n\n')) !== -1) {
        const frame = buf.slice(0, idx);
        buf = buf.slice(idx + 2);
        for (const line of frame.split('\n')) {
          if (line.indexOf('data: ') === 0) {
            const d = line.slice(6);
            if (d === '[DONE]') {
              this.setData({ loading: false, answer: full, time: this._now() });
            } else if (d && d !== 'null') {
              full += d;
              this.setData({ answer: full });
            }
          }
        }
      }
    });
  },

  _now() {
    const d = new Date();
    const p = (n) => (n < 10 ? '0' + n : '' + n);
    return p(d.getHours()) + ':' + p(d.getMinutes()) + ':' + p(d.getSeconds());
  },

  // 把响应的结构化数据转成 label/value 行（常见字段，缺省不展示）
  _extract(data) {
    const rows = [];
    const ranking = data.ranking || [];
    if (ranking.length) {
      const top = ranking[0];
      rows.push({ label: '最热区域', value: (top.label || top.zone_id || '') });
      rows.push({ label: '到访人次', value: String(top.visit_count != null ? top.visit_count : top.count || 0) });
      if (top.heat_score != null) rows.push({ label: '热度分', value: String(top.heat_score) });
    } else if (data.top_zone) {
      rows.push({ label: '最热区域', value: data.top_zone });
    }
    if (data.total_visitors != null) {
      rows.push({ label: '总到访', value: String(data.total_visitors) });
    }
    return rows;
  }
});
