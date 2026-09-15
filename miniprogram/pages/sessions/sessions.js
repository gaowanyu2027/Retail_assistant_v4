// 会话管理 — 新建/切换/搜索/删除会话
const api = require('../../utils/api');

Page({
  data: {
    sessions: [],
    keyword: '',
    searchResults: null,
    currentSession: '',
    history: [],        // 当前查看的会话历史消息
    historySession: '', // 正在查看历史的会话 id
    loading: true,
    error: ''
  },

  onLoad() {
    this.currentSession = wx.getStorageSync('mp_session_id') || '';
    this.load();
  },

  onPullDownRefresh() { this.load().finally(() => wx.stopPullDownRefresh()); },

  load() {
    this.setData({ loading: true, error: '' });
    return api.listSessions()
      .then((res) => this.setData({ loading: false, sessions: res.sessions || [] }))
      .catch((e) => this.setData({ loading: false, error: e.message }));
  },

  // 新建会话
  onCreate() {
    wx.showModal({
      title: '新建会话',
      editable: true,
      placeholderText: '会话标题',
      success: (res) => {
        if (!res.confirm) return;
        api.createSession(res.content || '新会话')
          .then((r) => {
            this._useSession(r.session_id, r.title);
            wx.showToast({ title: '已新建', icon: 'none' });
          })
          .catch((e) => wx.showToast({ title: e.message, icon: 'none' }));
      }
    });
  },

  // 切换会话（存为当前，回到问答页）
  onUse(e) {
    const { id, title } = e.currentTarget.dataset;
    this._useSession(id, title);
  },

  _useSession(id, title) {
    wx.setStorageSync('mp_session_id', id);
    this.setData({ currentSession: id });
    wx.showToast({ title: '已切换: ' + (title || id), icon: 'none' });
    setTimeout(() => wx.navigateTo({ url: '/pages/index/index' }), 600);
  },

  // 删除会话
  onDelete(e) {
    const id = e.currentTarget.dataset.id;
    wx.showModal({
      title: '删除会话',
      content: '确定删除？',
      success: (res) => {
        if (!res.confirm) return;
        api.deleteSession(id)
          .then(() => { if (this.currentSession === id) wx.removeStorageSync('mp_session_id'); this.load(); })
          .catch((e) => wx.showToast({ title: e.message, icon: 'none' }));
      }
    });
  },

  onSearchInput(e) { this.setData({ keyword: e.detail.value }); },

  // 搜索历史问答
  onSearch() {
    const q = (this.data.keyword || '').trim();
    if (!q) { this.setData({ searchResults: null }); return; }
    api.searchSessions(q)
      .then((res) => {
        const results = (res.results || []).map((r) => ({
          question: r.question || r.title || '',
          answer: r.answer || r.summary || '',
          session_id: r.session_id || ''
        }));
        this.setData({ searchResults: results });
      })
      .catch((e) => wx.showToast({ title: e.message, icon: 'none' }));
  },

  // 查看某会话的历史消息（Q/A 对话）
  onViewHistory(e) {
    const id = e.currentTarget.dataset.id;
    api.getMessages(id)
      .then((res) => this.setData({ history: res.messages || [], historySession: id }))
      .catch((err) => wx.showToast({ title: err.message, icon: 'none' }));
  },

  onCloseHistory() {
    this.setData({ history: [], historySession: '' });
  }
});
