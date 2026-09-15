// 语音问答 — 录音(PCM) → /api/asr/ws 识别 → /api/queries 问答 → /api/tts 播放
// 鉴权：WS 与音频 URL 都无法可靠自定义请求头 → **不放会话令牌**，
//       改为用 api.getTicket() 签发一次性票据（?ticket=，60 秒有效、用后即焚）
const api = require('../../utils/api');

Page({
  data: {
    status: '按住说话',
    recording: false,
    interim: '',
    answer: '',
    playing: false
  },

  onLoad() {
    this.recorder = wx.getRecorderManager();
    this.sock = null;
    this.audio = wx.createInnerAudioContext();
    this.audio.onEnded(() => this.setData({ playing: false }));
    this.recorder.onFrameRecorded((res) => this._pushPcm(res));
    this.recorder.onStop(() => this._onStop());
  },

  onUnload() {
    if (this.sock) this.sock.close();
    this.audio.destroy();
    this.recorder.stop();
  },

  _ws() {
    if (this.sock) return this.sock;
    // 票据一次性：每次建立连接都重新签发
    api.getTicket('miniprogram-asr').then((ticket) => {
      this.sock = wx.connectSocket({ url: api.wsUrl('/api/asr/ws', ticket), timeout: 30000 });
      this.sock.onMessage((res) => {
        if (typeof res.data !== 'string') return;
        try {
          const msg = JSON.parse(res.data);
          if (msg.event === 'interim' && msg.text) this.setData({ interim: msg.text });
          if (msg.event === 'final' && msg.text) this._ask(msg.text);
        } catch (e) {}
      });
      this.sock.onClose(() => { this.sock = null; });
    }).catch((e) => {
      this.setData({ status: '连接失败：' + (e.message || '') });
    });
    return this.sock;
  },

  _pushPcm(res) {
    if (!this.data.recording || !this.sock) return;
    // 微信 PCM 帧是 Int16；后端 ASR 要 Float32 → 转换
    const int16 = new Int16Array(res.frameBuffer);
    const float32 = new Float32Array(int16.length);
    for (let i = 0; i < int16.length; i++) float32[i] = int16[i] / 32768;
    this.sock.send({ data: float32.buffer, binary: true });
  },

  onRecordTap() {
    if (this.data.recording) { this.recorder.stop(); return; }
    this.setData({ recording: true, status: '松开结束', interim: '', answer: '' });
    this._ws();
    this.recorder.start({
      duration: 60000,
      sampleRate: 16000,
      numberOfChannels: 1,
      encodeBitRate: 128000,
      format: 'pcm'
    });
  },

  _onStop() {
    this.setData({ recording: false, status: '识别中…' });
    // 稍等待最终识别结果推送
    setTimeout(() => this.setData({ status: '按住说话' }), 500);
  },

  _ask(text) {
    this.setData({ interim: '', status: 'AI 回答…' });
    api.query(text, 'voice_' + Date.now())
      .then((res) => this._speak(res.answer || '（无回答）'))
      .catch((e) => this.setData({ status: '出错：' + e.message }));
  },

  _speak(text) {
    this.setData({ answer: text, status: '按住说话' });
    // 音频播放器无法设置请求头 → 用一次性票据走 URL（不暴露会话令牌）
    api.getTicket('miniprogram-tts').then((ticket) => {
      this.audio.src = api.ticketUrl('/tts?text=' + encodeURIComponent(text), ticket);
      this.audio.play();
      this.setData({ playing: true });
    }).catch((e) => {
      this.setData({ status: '语音播报失败：' + (e.message || '') });
    });
  }
});
