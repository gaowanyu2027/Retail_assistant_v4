// 摄像头管理 — 识别/添加摄像头 + 按需勾选模块（显性按钮加载）+ 绑定视频 + 开关
const api = require('../../utils/api');

const TYPE_CN = {
  'indoor_shelf': '店内货架',
  'entrance': '店门口',
  'checkout': '收银台'
};

Page({
  data: {
    loading: true,
    error: '',
    cameras: [],          // 摄像头列表（每镜头含 enabled_modules + candidates）
    modules: [],          // 所有已注册模块
    typeCandidates: {},   // 标签 → 候选模块池
    activeCamera: ''
  },

  onLoad() {
    this.loadAll();
  },

  onPullDownRefresh() {
    this.loadAll().finally(() => wx.stopPullDownRefresh());
  },

  loadAll() {
    this.setData({ loading: true, error: '' });
    return Promise.all([api.getCameras(), api.getModules(), api.scanCameras()])
      .then(([cams, mods, scan]) => {
        const typeCN = {};
        for (const c of cams.cameras) typeCN[c.id] = TYPE_CN[c.type] || c.type;
        this.setData({
          loading: false,
          cameras: cams.cameras || [],
          modules: mods.modules || [],
          typeCandidates: mods.type_candidates || {},
          typeCN: typeCN
        });
      })
      .catch((err) => this.setData({ loading: false, error: err.message || '加载失败' }));
  },

  // 添加摄像头（弹表单）
  onAddTap() {
    wx.showModal({
      title: '添加摄像头',
      editable: true,
      placeholderText: '摄像头名称，如 3号收银台',
      success: (res) => {
        if (!res.confirm || !res.content) return;
        const name = res.content;
        wx.showActionSheet({
          itemList: ['店内货架(indoor)', '店门口(entrance)', '收银台(checkout)'],
          success: (r) => {
            const type = ['indoor_shelf', 'entrance', 'checkout'][r.tapIndex];
            this._doAdd(name, type);
          }
        });
      }
    });
  },

  _doAdd(name, type) {
    api.addCamera({ name: name, type: type, source: 'webcam', modules: [] })
      .then((res) => {
        wx.showToast({ title: '已添加 ' + (res.camera_id || ''), icon: 'none' });
        this.loadAll();
      })
      .catch((err) => wx.showToast({ title: err.message, icon: 'none' }));
  },

  // 勾选/卸载模块（显性按钮）
  onToggleModule(e) {
    const { cam, mod } = e.currentTarget.dataset;
    const camObj = this.data.cameras.find((c) => c.id === cam);
    const enabled = camObj && camObj.enabled_modules.includes(mod);
    const p = enabled
      ? api.unloadModule(cam, mod)
      : api.loadModule(cam, mod);
    p.then(() => {
      wx.showToast({ title: (enabled ? '卸载 ' : '加载 ') + mod, icon: 'none' });
      this.loadAll();
    }).catch((err) => wx.showToast({ title: err.message, icon: 'none' }));
  },

  // 开关模块
  onToggleEnabled(e) {
    const { cam, mod } = e.currentTarget.dataset;
    const camObj = this.data.cameras.find((c) => c.id === cam);
    const enabled = camObj && camObj.enabled_modules.includes(mod);
    api.setModuleEnabled(cam, mod, !enabled)
      .then(() => this.loadAll())
      .catch((err) => wx.showToast({ title: err.message, icon: 'none' }));
  },

  // 绑定视频输出到摄像头
  onBind(e) {
    const cam = e.currentTarget.dataset.cam;
    api.setActiveCamera(cam)
      .then((res) => {
        this.setData({ activeCamera: res.active_camera });
        wx.showToast({ title: '视频已绑定 ' + cam, icon: 'none' });
      })
      .catch((err) => wx.showToast({ title: err.message, icon: 'none' }));
  },

  // 删除摄像头
  onDelete(e) {
    const cam = e.currentTarget.dataset.cam;
    wx.showModal({
      title: '删除摄像头',
      content: '确定删除 ' + cam + '？',
      success: (res) => {
        if (!res.confirm) return;
        api.deleteCamera(cam)
          .then(() => { wx.showToast({ title: '已删除', icon: 'none' }); this.loadAll(); })
          .catch((err) => wx.showToast({ title: err.message, icon: 'none' }));
      }
    });
  },
});
