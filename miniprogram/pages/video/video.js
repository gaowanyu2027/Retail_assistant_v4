// 视频监控页 — wx.connectSocket 接 /api/ws/stream，canvas 2d 渲染，切换摄像头看模块数据
// 鉴权：WS 无法可靠携带自定义请求头，因此**不放会话令牌**，
//       改为每次连接前用 api.getTicket() 签发一次性票据（?ticket=，用后即焚）
const api = require('../../utils/api');

// 模块统计字段 → 中文标签（避免英文 key 直接展示）
const MODULE_KEY_CN = {
  status: '状态', top_zone: '最热区域', total_visitors: '到访人次',
  total_visits: '到访次数', total_staff: '疑似店员', timestamp: '时间',
  total_alerts: '总告警', high_risk_count: '高风险', watch_count: '关注',
  total_entry: '进店', total_exit: '外出', net_visit: '净客流',
  total_faces: '识别人脸', positive_rate: '正向占比', dominant_emotion: '主导情绪',
  needs_restock: '需补货', recent_visits: '近访问'
};

// 从模块统计中提取可平铺展示的字段（过滤嵌套 zones/hourly 等）
function _flattenModuleStats(res) {
  const rows = [];
  if (!res || typeof res !== 'object') return rows;
  for (const k of Object.keys(res)) {
    const v = res[k];
    if (k === 'zones' || k === 'hourly' || k === 'distribution' || typeof v === 'object') continue;
    if (typeof v === 'number' || typeof v === 'string') {
      rows.push({ label: MODULE_KEY_CN[k] || k, value: String(v) });
    }
  }
  return rows;
}

Page({
  data: {
    cameras: [],
    activeCamera: '',
    enabledForActive: [],   // 当前摄像头启用的模块（模块数据 tab）
    videoReady: false,
    wsStatus: '未连接',
    frameId: 0,
    moduleRows: [],   // 当前模块统计（已转中文 label/value）
    moduleName: 'shelf_heat',
    error: ''
  },

  onLoad() {
    this.canvas = null;
    this.ctx = null;
    this.img = null;
    this.sock = null;
    this.reconnectTimer = null;
    this.initCanvas();
    this.loadCameras();
  },

  onUnload() {
    if (this.sock) this.sock.close();
    if (this.reconnectTimer) clearTimeout(this.reconnectTimer);
  },

  initCanvas() {
    wx.createSelectorQuery()
      .select('#video')
      .fields({ node: true, size: true })
      .exec((res) => {
        if (!res || !res[0] || !res[0].node) return;
        this.canvas = res[0].node;
        this.ctx = this.canvas.getContext('2d');
        const image = this.canvas.createImage();
        image.onload = () => {
          // contain 模式：完整显示画面、保持原始宽高比，不拉伸、不裁切（黑边为正常）
          const cw = this.canvas.width, ch = this.canvas.height;
          const scale = Math.min(cw / image.width, ch / image.height);
          const dw = image.width * scale, dh = image.height * scale;
          const dx = (cw - dw) / 2, dy = (ch - dh) / 2;
          this.ctx.clearRect(0, 0, cw, ch);
          this.ctx.drawImage(image, dx, dy, dw, dh);
        };
        this.img = image;
        this.connect();
      });
  },

  loadCameras() {
    api.getCameras().then((res) => {
      const cams = res.cameras || [];
      const first = cams[0];
      const enabledForActive = first && first.enabled_modules ? first.enabled_modules : [];
      this.setData({
        cameras: cams,
        activeCamera: first ? first.id : '',
        enabledForActive: enabledForActive,
        moduleName: enabledForActive.length ? enabledForActive[0] : ''
      });
      this.loadModuleData();
    }).catch((e) => this.setData({ error: e.message }));
  },

  onSwitchCamera(e) {
    const cam = e.currentTarget.dataset.cam;
    const c = this.data.cameras.find((x) => x.id === cam);
    const enabledForActive = c && c.enabled_modules ? c.enabled_modules : [];
    this.setData({
      activeCamera: cam,
      enabledForActive: enabledForActive,
      moduleName: enabledForActive.length ? enabledForActive[0] : ''
    });
    api.setActiveCamera(cam).then(() => this.loadModuleData()).catch(() => {});
  },

  onPickModule(e) {
    const mod = e.currentTarget.dataset.mod;
    this.setData({ moduleName: mod });
    this.loadModuleData();
  },

  loadModuleData() {
    const { activeCamera, moduleName } = this.data;
    if (!activeCamera || !moduleName) return;
    api.moduleStats(activeCamera, moduleName)
      .then((res) => this.setData({ moduleRows: _flattenModuleStats(res) }))
      .catch(() => this.setData({ moduleRows: [] }));
  },

  connect() {
    if (this.sock) { try { this.sock.close(); } catch (e) {} }
    this.setData({ wsStatus: '连接中…' });
    // 票据是一次性的 → 每次连接（含重连）都重新签发；
    // 不在 URL 里放会话令牌（会进访问日志，后端也已默认拒绝 ?token=）
    api.getTicket('miniprogram-video').then((ticket) => {
      this.sock = wx.connectSocket({
        url: api.wsUrl('/api/ws/stream', ticket),
        timeout: 10000
      });
      this.sock.onOpen(() => {
        this.setData({ wsStatus: '已连接', videoReady: true });
        // ⚠ 字段名必须是 action，不是 type：后端只解析 msg["action"]
        // （api/routes/stream.py 的 `action = msg.get("action", "")`，仅特判 type=="ping"）。
        // 此前发的是 {type:'start_webcam'}，消息被当成未知指令**静默忽略** ——
        // 表现为：WS 显示"已连接"、但画布永远空白、帧号恒为 0，无从排查。
        // 浏览器端 public/js/stream.js 发的就是 {action, mode, ...}，这里对齐即可。
        this.sock.send({ data: JSON.stringify({ action: 'start_webcam', camera_id: 0, mode: 'retail' }) });
      });
      this.sock.onMessage((res) => {
        if (typeof res.data === 'string') {
          try { this._handleMeta(JSON.parse(res.data)); } catch (e) {}
        } else {
          this._handleBinary(res.data);
        }
      });
      this.sock.onClose(() => {
        this.setData({ wsStatus: '已断开', videoReady: false });
        this.reconnectTimer = setTimeout(() => this.connect(), 3000);
      });
      this.sock.onError(() => this.setData({ wsStatus: '连接错误' }));
    }).catch((e) => {
      // 票据签发失败（如登录过期）→ 由 api 层触发回登录页，这里仅提示并稍后重试
      this.setData({ wsStatus: '鉴权失败', videoReady: false });
      this.reconnectTimer = setTimeout(() => this.connect(), 5000);
    });
  },

  _handleMeta(msg) {
    if (msg.frame_id) this.setData({ frameId: msg.frame_id });
    if (msg.type === 'status') { /* 状态消息 */ }
  },

  _handleBinary(buffer) {
    const view = new DataView(buffer);
    const frameId = view.getUint32(0);
    const jpegBytes = new Uint8Array(buffer, 4);
    if (this.img && jpegBytes.length > 2) {
      this.img.src = 'data:image/jpeg;base64,' + wx.arrayBufferToBase64(jpegBytes.buffer.slice(jpegBytes.byteOffset, jpegBytes.byteOffset + jpegBytes.byteLength));
    }
    this.setData({ frameId: frameId });
  },
});
