<template>
  <div class="app-root">
    <header class="topbar">
      <h1>智能零售分析系统</h1>
      <div class="topbar-right">
        <span id="status-indicator" class="status-dot offline"></span>
        <span id="status-text">未连接</span>
        <select id="camera-select" class="btn btn-secondary" style="display:none;padding:6px 10px"></select>
        <button id="btn-webcam" class="btn btn-primary" style="display:none" @click="startServerCamera">服务器摄像头</button>
        <button id="btn-local-cam" class="btn btn-primary" style="display:none" @click="startLocalCamera">本机摄像头</button>
        <div class="camera-menu-wrap" @click.stop>
          <button id="btn-camera-menu" class="btn btn-primary" @click.stop="toggleCameraMenu">
            {{ cameraActive ? '切换摄像头' : '摄像头' }}
          </button>
          <div v-show="cameraMenuOpen" id="camera-menu" class="camera-menu">
            <button type="button" @click="selectCamera('webcam')">服务器摄像头</button>
            <button type="button" @click="selectCamera('local')">本机摄像头</button>
          </div>
        </div>
        <button id="btn-upload" class="btn btn-secondary" :disabled="cameraActive" @click="openFilePicker">上传视频</button>
        <input type="file" id="file-input" accept="video/*" style="display:none" @change="onFileSelected">
        <button id="btn-stop" class="btn btn-danger" :disabled="!cameraActive" @click="stopVideo">停止</button>
        <button id="btn-voice" class="btn btn-success">🎤 语音输入</button>
        <button id="btn-transcribe-test" class="btn btn-secondary">转文字测试</button>
        <button id="btn-voice-reply" class="btn btn-secondary">语音回复: 开</button>
        <span class="fps-display">FPS: <strong id="fps-value">--</strong></span>
        <span class="user-chip" :title="'当前登录：' + userLabel">
          {{ userLabel }}
        </span>
        <button id="btn-logout" class="btn btn-secondary" @click="logout">退出</button>
      </div>
    </header>

    <button id="btn-open-sessions" class="session-toggle-btn" @click="openDrawer">会话记录</button>
    <aside id="session-drawer" class="session-drawer collapsed" :class="{ open: drawerOpen }">
      <div class="session-drawer-header">
        <h2>会话记录</h2>
        <button class="btn btn-outline" @click="closeDrawer">收起</button>
      </div>
      <div class="session-search-row">
        <input id="session-search" v-model="searchInput" type="text" placeholder="搜索问题、回答或标题" autocomplete="off" @keyup.enter="searchSessions">
        <button class="btn btn-primary" @click="searchSessions">搜索</button>
      </div>
      <div class="session-drawer-actions">
        <button class="btn btn-primary" @click="createSession">新建会话</button>
        <button class="btn btn-outline" @click="saveSession">保存会话</button>
      </div>
      <div id="session-list" class="session-list">
        <div v-if="!sessions.length" class="session-item">暂无会话</div>
        <div
          v-for="session in sessions"
          :key="session.session_id"
          class="session-item"
          :class="{ active: session.session_id === currentSessionId }"
          :title="session.question || session.title || ''"
          @click="switchSession(session.session_id)"
        >
          <span class="session-item-title">{{ session.title || session.session_id }}</span>
          <span class="session-item-meta">{{ sessionSearchMode ? '#' + (session.seq_no || 1) : (session.message_count || 0) + '条' }}</span>
          <button class="session-rename-btn" @click.stop="renameSession(session.session_id)">重命名</button>
          <button class="session-delete-btn" @click.stop="deleteSession(session.session_id)">删除</button>
        </div>
      </div>
    </aside>

    <div class="mode-switcher">
      <div class="mode-tab" :class="{ active: currentMode === 'retail' }" @click="switchMode('retail')">
        <span class="mode-icon">📊</span><span>零售视频分析（货架摄像头）</span>
      </div>
      <div class="mode-tab" :class="{ active: currentMode === 'emotion' }" @click="switchMode('emotion')">
        <span class="mode-icon">😀</span><span>门店人脸表情分析（出入口摄像头）</span>
      </div>
    </div>

    <div class="voice-bar" id="voice-bar">
      <span id="voice-status" class="voice-status">点击后说“小零 + 指令”，例如“小零打开摄像头”</span>
      <span id="voice-heard" class="voice-heard"></span>
      <span id="voice-result" class="voice-result"></span>
    </div>

    <div class="main-container retail-mode" id="retail-container" v-show="currentMode === 'retail'">
      <section class="panel video-panel">
        <h2>实时监控画面 — 货架摄像头</h2>
        <div class="video-container" id="video-container" title="点击放大画面">
          <canvas id="video-canvas" width="1280" height="720"></canvas>
          <div id="video-placeholder" class="video-placeholder" style="pointer-events:none">
            <p>📷</p><p>点击「摄像头」启动实时画面<br>或「上传视频」播放文件</p>
          </div>
        </div>
        <div class="video-info">
          <span>帧号: <strong id="frame-id">--</strong></span>
          <span>活跃轨迹: <strong id="active-tracks">0</strong></span>
          <span>总访客: <strong id="total-visitors">0</strong></span>
        </div>
      </section>
      <section class="panel dash-panel">
        <h2>货架热度排行</h2>
        <div id="chart-heat" class="chart-box"></div>
        <div id="ranking-list" class="ranking-list">
          <p class="placeholder-text">等待数据...</p>
        </div>
        <h2 style="margin-top:14px">热度 vs 销量 <span style="font-size:0.7em;color:var(--text-secondary)">(转化诊断)</span></h2>
        <div id="sales-compare-box" style="font-size:0.78em;line-height:1.6">
          <p class="placeholder-text">暂无比对数据 —— <button @click="simulateSales" style="background:var(--accent-blue);color:#fff;border:none;border-radius:4px;padding:2px 10px;cursor:pointer">生成演示数据</button></p>
        </div>
      </section>
      <section class="panel alert-panel">
        <h2>实时告警</h2>
        <div id="emo-box" style="background:var(--bg-secondary);border-radius:6px;padding:6px 10px;margin-bottom:8px;font-size:0.8em;display:flex;justify-content:space-around;text-align:center">
          <span>😊正向 <b id="emo-pos" style="color:#4caf84">--</b></span>
          <span>😟负向 <b id="emo-neg" style="color:#f05050">--</b></span>
          <span>📊总计 <b id="emo-tot" style="color:#8899aa">--</b></span>
        </div>
        <div id="chart-emotion" style="width:100%;height:160px;margin-bottom:8px"></div>
        <div id="heat-report-bar" style="display:none;background:var(--bg-secondary);border:1px solid var(--border);border-radius:6px;padding:8px 10px;margin-bottom:8px;font-size:0.8em;line-height:1.5">
          📋 <span id="heat-report-text"></span>
        </div>
        <div id="alert-status" class="alert-status safe">🟢 无异常告警</div>
        <div id="alert-list" class="alert-list">
          <p class="placeholder-text">监控正常运行中</p>
        </div>
        <div id="suspicious-tracks" class="suspicious-list"></div>
      </section>
    </div>

    <div class="main-container emotion-mode" id="emotion-container" v-show="currentMode === 'emotion'">
      <section class="panel">
        <h2>实时监控画面 — 出入口摄像头</h2>
        <div class="camera-control">
          <div id="emo-status-dot" class="status-dot offline"></div>
          <span>状态：<strong id="emo-status-text">已关闭</strong></span>
          <span style="margin-left:auto;color:var(--text-secondary);font-size:0.85em">
            检测到 <strong id="emo-face-count" style="color:var(--accent-blue)">0</strong> 张人脸
          </span>
        </div>
        <div class="video-container" id="video-container-emo" title="点击放大画面">
          <canvas id="video-canvas-emo" width="1280" height="720"></canvas>
          <div id="video-placeholder-emo" class="video-placeholder" style="pointer-events:none">
            <p>📷</p>
            <p>选择上方摄像头/上传视频开始表情分析<br>系统将自动检测人脸并识别7种表情</p>
          </div>
        </div>
        <div class="video-info">
          <span>帧号: <strong id="emo-frame-id">--</strong></span>
          <span>人脸数: <strong id="emo-active-faces">0</strong></span>
          <span>数据库记录: <strong id="emo-db-count">0</strong></span>
        </div>
      </section>
      <section class="panel">
        <h2>表情统计分析</h2>
        <div class="emotion-stats-grid">
          <div class="stat-card total"><div class="stat-value" id="stat-total">0</div><div class="stat-label">识别记录</div></div>
          <div class="stat-card positive"><div class="stat-value" id="stat-positive">0</div><div class="stat-label">正面情绪</div></div>
          <div class="stat-card negative"><div class="stat-value" id="stat-negative">0</div><div class="stat-label">负面情绪</div></div>
        </div>
        <div id="emotion-pie-chart" style="width:100%;height:180px;margin-bottom:12px"></div>
        <div class="emotion-bar-chart" id="emotion-bar-chart"><p class="placeholder-text">暂无识别数据</p></div>
        <h2 style="margin-top:16px">最近识别记录</h2>
        <table class="records-table">
          <thead><tr><th>时间</th><th>表情</th><th>置信度</th></tr></thead>
          <tbody id="emo-records-body"><tr><td colspan="3" style="color:var(--text-secondary)">暂无数据</td></tr></tbody>
        </table>
        <div class="analysis-report" id="analysis-report">
          <h3>📋 本次采集表情分段分析报告</h3>
          <div id="analysis-content"></div>
        </div>
      </section>
    </div>

    <section class="query-section" id="query-section" v-show="currentMode === 'retail'">
      <h2>自然语言查询</h2>
      <div id="query-result" class="query-result">
        <div class="result-header">
          <span class="result-label">🤖 AI 分析结果</span>
          <span id="result-intent" class="badge"></span>
          <span id="result-confidence" class="confidence"></span>
        </div>
        <div id="answer-text" class="answer-text" v-html="answerHtml"></div>
        <div id="suggestions" class="suggestions"></div>
      </div>
      <div class="query-input-row" id="query-input-row">
        <input id="query-input" v-model="queryInput" type="text" placeholder="例如：哪个货架最受欢迎？有没有异常行为？今天整体情况怎么样？" autocomplete="off" @keyup.enter="sendQuery">
        <button id="btn-query" class="btn btn-primary" @click="sendQuery">查询</button>
        <button id="btn-quick-overview" class="btn btn-outline" @click="quickQuery('今天整体情况怎么样？')">整体概况</button>
        <button id="btn-quick-heat" class="btn btn-outline" @click="quickQuery('哪个货架最受欢迎？')">热度排行</button>
        <button id="btn-quick-alert" class="btn btn-outline" @click="quickQuery('有没有异常行为需要关注？')">异常检查</button>
      </div>
    </section>
  </div>
</template>

<script>
// Vue3 实现（v4 起为唯一浏览器前端；交互约定沿用早期原生版）
const EMO_CN = { happy: '开心', neutral: '平静', surprise: '惊讶',
                 sad: '悲伤', angry: '愤怒', fear: '害怕', disgust: '厌恶' }
const EMO_COLORS = { happy: '#f0c040', neutral: '#8899aa', surprise: '#c084fc',
                     sad: '#4da6ff', angry: '#f05050', fear: '#f08030', disgust: '#4caf84' }
const POSITIVE = ['happy', 'neutral']
const NEGATIVE = ['angry', 'disgust', 'fear', 'sad']

export default {
  data() {
    return {
      currentMode: 'retail',
      drawerOpen: false,
      cameraMenuOpen: false,
      cameraActive: false,
      sourceType: null,
      sessions: [],
      sessionSearchMode: false,
      currentSessionId: '',
      queryInput: '',
      searchInput: '',
      answerHtml: '<p class="placeholder-text">在下方输入问题或点击快捷按钮开始查询</p>',
      localStream: null,
      localVideoEl: null,
      localCaptureTimer: null,
      localPreviewRaf: null,
      pollTimer: null,
      reportTimer: null,
      // 主动汇报 WS 订阅（实时推送；REST 轮询作为兜底保留）
      reportWs: null,
      reportWsReconnectTimer: null,
      reportWsKeepaliveTimer: null,
      reportWsAttempts: 0,
      reportWsClosed: false,
      // 当前登录用户（由 Root.vue 登录门禁写入；App 挂载时已就绪）
      currentUser: (typeof window !== 'undefined' && window.__currentUser) || null,
      userLabel: (() => {
        const w = (typeof window !== 'undefined' && window.__currentUser) || null
        if (!w) return ''
        const role = w.role === 'root' ? '管理员' : '平台账户'
        return (w.display_name || w.username) + ' · ' + role
      })(),
      emoPieChart: null,
      retailEmoChart: null,
    }
  },
  mounted() {
    // 供 stream.js / voice.js 调用的全局函数（与原生 app.js 一致）
    window.updateStatus = (status, text) => {
      const dot = document.getElementById('status-indicator')
      const label = document.getElementById('status-text')
      if (dot) dot.className = 'status-dot ' + status
      if (label) label.textContent = text
    }
    window.updateEmotionFaces = (faces) => {
      const faceCount = faces.length
      const el1 = document.getElementById('emo-face-count')
      const el2 = document.getElementById('emo-active-faces')
      if (el1) el1.textContent = faceCount
      if (el2) el2.textContent = faceCount
    }
    window.renderAnalysisReport = (seg) => this.renderAnalysisReport(seg)

    StreamManager.init('video-canvas')
    StreamManager.connect()
    ChartManager.init('chart-heat')
    // 挂载瞬间容器可能尚未完成布局（宽度为 0，图表会塌缩成默认 100px），
    // 等下一帧布局稳定后校正一次图表尺寸，保证与下方排行列表对齐
    this.$nextTick(() => {
      requestAnimationFrame(() => {
        requestAnimationFrame(() => {
          ChartManager.resize()
        })
      })
    })

    this.scanCameras()
    this.loadSessions()
    this.fetchPopularityReport()
    this.fetchAnomalyReport()
    this.fetchEmotionStats()
    this.fetchHotVsSales()

    // 5 秒轮询（按模式选择数据源，与原生一致）
    this.pollTimer = setInterval(() => {
      if (this.currentMode === 'retail') {
        this.fetchPopularityReport()
        this.fetchAnomalyReport()
        this.fetchEmotionStats()
        this.fetchHotVsSales()
      } else {
        this.fetchEmotionSummary()
        this.fetchEmotionRecords()
        this.updateEmotionDbCount()
      }
    }, 5000)

    // 最新运营汇报（主动汇报 Agent 生成）
    // 双通道：WS 订阅实时推送（优先），REST 轮询兜底（WS 未连上/断线期间仍有数据）
    this.fetchLatestReport()
    this.reportTimer = setInterval(() => this.fetchLatestReport(), 15000)
    this.connectReportWs()

    // 服务端帧到达后停止本地预览；同时处理实时告警/热度事件（对齐原生版行为）
    StreamManager.onFrame((msg) => {
      if (this.localPreviewRaf) {
        cancelAnimationFrame(this.localPreviewRaf)
        this.localPreviewRaf = null
      }
      if (msg && msg.anomaly_alerts && msg.anomaly_alerts.length) {
        this.updateAlertsPanel(msg.anomaly_alerts, msg.active_suspicious || [])
      }
      if (msg && msg.popularity_events) {
        this.fetchPopularityReport()
      }
    })

    // 点击画面全屏放大
    ;['video-container', 'video-container-emo'].forEach(id => {
      const el = document.getElementById(id)
      if (el) {
        el.style.cursor = 'pointer'
        el.addEventListener('click', () => {
          if (document.fullscreenElement) {
            document.exitFullscreen()
          } else {
            el.requestFullscreen()
          }
        })
      }
    })

    // 点击其他区域：关闭摄像头菜单；抽屉展开时点击外部自动收回
    this._docClickHandler = (e) => {
      this.cameraMenuOpen = false
      if (!this.drawerOpen) return
      const drawer = document.getElementById('session-drawer')
      const btn = document.getElementById('btn-open-sessions')
      if (drawer && drawer.contains(e.target)) return
      if (btn && btn.contains(e.target)) return
      this.closeDrawer()
    }
    document.addEventListener('click', this._docClickHandler)

    // 窗口尺寸变化时重绘表情饼图
    this._resizeHandler = () => {
      if (this.emoPieChart) this.emoPieChart.resize()
      if (this.retailEmoChart) this.retailEmoChart.resize()
    }
    window.addEventListener('resize', this._resizeHandler)
  },
  beforeUnmount() {
    clearInterval(this.pollTimer)
    clearInterval(this.reportTimer)
    // 主动汇报 WS：置关闭标记（阻止重连）+ 清定时器 + 断连
    this.reportWsClosed = true
    this._stopReportKeepalive()
    if (this.reportWsReconnectTimer) {
      clearTimeout(this.reportWsReconnectTimer)
      this.reportWsReconnectTimer = null
    }
    if (this.reportWs) {
      try { this.reportWs.close() } catch (e) {}
      this.reportWs = null
    }
    clearTimeout(this.localCaptureTimer)
    if (this.localPreviewRaf) cancelAnimationFrame(this.localPreviewRaf)
    if (this._docClickHandler) document.removeEventListener('click', this._docClickHandler)
    if (this._resizeHandler) window.removeEventListener('resize', this._resizeHandler)
    if (this.localStream) {
      this.localStream.getTracks().forEach(t => t.stop())
      this.localStream = null
    }
    // 释放 StreamManager：关闭 WS/心跳/重连定时器并清空回调，避免组件卸载后继续运行
    if (window.StreamManager) {
      try {
        StreamManager.disconnect && StreamManager.disconnect()
        StreamManager.clearCallbacks && StreamManager.clearCallbacks()
      } catch (e) { /* 忽略清理异常 */ }
    }
  },
  methods: {
    // 退出登录：吊销服务端会话并回登录页（整页重载以彻底清掉仪表盘状态/WS 连接）
    async logout() {
      if (!window.confirm('确认退出登录？')) return
      try {
        await fetch('/api/auth/logout', { method: 'POST' })
      } catch (e) { /* 网络异常也继续本地清理 */ }
      try {
        window.__currentUser = null
        window.__currentPerms = []
      } catch (e) { /* ignore */ }
      location.reload()
    },
    escapeHtml(value) {
      return String(value || '')
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#39;')
    },
    getSessionId() {
      let sid = sessionStorage.getItem('chat_session_id')
      if (!sid) {
        sid = 'sess_' + Date.now() + '_' + Math.random().toString(36).slice(2, 8)
        sessionStorage.setItem('chat_session_id', sid)
      }
      this.currentSessionId = sid
      return sid
    },
    openDrawer() {
      this.drawerOpen = true
      const btn = document.getElementById('btn-open-sessions')
      if (btn) btn.style.display = 'none'
    },
    closeDrawer() {
      this.drawerOpen = false
      const btn = document.getElementById('btn-open-sessions')
      if (btn) btn.style.display = ''
    },
    toggleCameraMenu() {
      this.cameraMenuOpen = !this.cameraMenuOpen
    },
    async selectCamera(kind) {
      this.cameraMenuOpen = false
      if (this.cameraActive && this.sourceType === kind) return
      if (this.cameraActive) {
        this.stopVideo()
      }
      if (kind === 'webcam') {
        this.startServerCamera()
      } else if (kind === 'local') {
        this.startLocalCamera()
      }
    },
    // ==================== 摄像头扫描 ====================
    async scanCameras() {
      const cameraSelect = document.getElementById('camera-select')
      if (!cameraSelect) return
      try {
        const resp = await fetch('/api/cameras')
        const data = await resp.json()
        cameraSelect.innerHTML = ''
        if (data.cameras && data.cameras.length > 0) {
          data.cameras.forEach(c => {
            const opt = document.createElement('option')
            opt.value = c.id
            opt.textContent = `摄像头 ${c.id} (${c.resolution})`
            if (c.id === data.default) opt.selected = true
            cameraSelect.appendChild(opt)
          })
        } else {
          cameraSelect.innerHTML = '<option value="">无可用摄像头</option>'
        }
      } catch (e) {
        cameraSelect.innerHTML = '<option value="">扫描失败</option>'
      }
    },
    // ==================== 视频控制 ====================
    startServerCamera() {
      const cameraSelect = document.getElementById('camera-select')
      const camId = parseInt(cameraSelect && cameraSelect.value) || 0
      StreamManager.startWebcam(camId)
      this.cameraActive = true
      this.sourceType = 'webcam'
      const modeLabel = this.currentMode === 'retail' ? '货架' : '出入口'
      window.updateStatus('online', `${modeLabel}摄像头 #${camId} 运行中`)
      if (this.currentMode === 'emotion') {
        this.updateEmoStatus(true)
      }
    },
    async startLocalCamera() {
      try {
        const devices = await navigator.mediaDevices.enumerateDevices()
        const cameras = devices.filter(d => d.kind === 'videoinput')
        let deviceId = undefined
        if (cameras.length > 1) {
          const labels = cameras.map((c, i) => {
            const name = c.label || ('摄像头 ' + (i + 1))
            return `${i}: ${name}`
          })
          const choice = prompt('选择本机摄像头：\n' + labels.join('\n'), '0')
          if (choice === null) return
          const idx = parseInt(choice) || 0
          deviceId = cameras[Math.min(idx, cameras.length - 1)]?.deviceId
        }
        const videoConstraints = {
          width: { ideal: 640 },
          height: { ideal: 480 },
          aspectRatio: { ideal: 4 / 3 },
          frameRate: { ideal: 30 },
        }
        if (deviceId) videoConstraints.deviceId = { exact: deviceId }
        else videoConstraints.facingMode = 'environment'
        const stream = await navigator.mediaDevices.getUserMedia({ video: videoConstraints })
        this.localStream = stream
        const videoEl = document.createElement('video')
        videoEl.srcObject = stream
        videoEl.setAttribute('playsinline', '')
        videoEl.setAttribute('autoplay', '')
        videoEl.setAttribute('muted', '')
        videoEl.style.cssText = 'position:fixed;top:-9999px;left:-9999px'
        document.body.appendChild(videoEl)
        await videoEl.play()
        for (let i = 0; i < 20 && !(videoEl.videoWidth && videoEl.videoHeight); i++) {
          await new Promise(resolve => setTimeout(resolve, 50))
        }
        this.localVideoEl = videoEl

        // 本地预览（服务端帧到达后由 onFrame 回调取消）
        const drawLocalPreview = () => {
          if (!this.cameraActive || this.sourceType !== 'local') return
          const previewCanvas = this.currentMode === 'retail'
            ? document.getElementById('video-canvas')
            : document.getElementById('video-canvas-emo')
          const previewCtx = previewCanvas && previewCanvas.getContext('2d')
          if (!previewCtx) return
          const pW = previewCanvas.width || 960
          const pH = previewCanvas.height || 540
          const srcWPreview = videoEl.videoWidth || 480
          const srcHPreview = videoEl.videoHeight || 360
          const previewScale = Math.max(pW / srcWPreview, pH / srcHPreview)
          const dw = srcWPreview * previewScale
          const dh = srcHPreview * previewScale
          const dx = (pW - dw) / 2
          const dy = (pH - dh) / 2
          try {
            previewCtx.fillStyle = '#000'
            previewCtx.fillRect(0, 0, pW, pH)
            // 镜像模式：水平翻转（自拍视角，画面与手势同向）
            previewCtx.save()
            previewCtx.translate(pW, 0)
            previewCtx.scale(-1, 1)
            previewCtx.drawImage(videoEl, dx, dy, dw, dh)
            previewCtx.restore()
          } catch (e) {}
          this.localPreviewRaf = requestAnimationFrame(drawLocalPreview)
        }

        StreamManager.startClientCamera()
        this.cameraActive = true
        this.sourceType = 'local'
        drawLocalPreview()
        window.updateStatus('online', '本机摄像头运行中')
        if (this.currentMode === 'emotion') {
          this.updateEmoStatus(true)
        }

        // 低延迟：降分辨率(320x240) + 降JPEG质量(0.4) + 固定帧率(~15fps)，不锁步
        // （原实现锁步"等上一帧发完才发下一帧"，toBlob/网络慢会卡到秒级；改连续推，卡则丢帧保流畅）
        const vw = videoEl.videoWidth || 480
        const vh = videoEl.videoHeight || 360
        const outW = 320
        const outH = 240
        const FRAME_INTERVAL = 66   // ~15fps
        const canvas = document.createElement('canvas')
        canvas.width = outW
        canvas.height = outH
        const ctx = canvas.getContext('2d')

        let srcX = 0
        let srcY = 0
        let srcW = vw
        let srcH = vh
        if (vw / vh > outW / outH) {
          srcW = vh * outW / outH
          srcX = (vw - srcW) / 2
        } else if (vw / vh < outW / outH) {
          srcH = vw * outH / outW
          srcY = (vh - srcH) / 2
        }

        const captureAndSend = () => {
          if (!this.cameraActive) {
            stream.getTracks().forEach(t => t.stop())
            return
          }
          try {
            // 与预览一致的镜像翻转：发送帧也水平翻转，标注方向一致
            ctx.save()
            ctx.translate(outW, 0)
            ctx.scale(-1, 1)
            ctx.drawImage(videoEl, srcX, srcY, srcW, srcH, 0, 0, outW, outH)
            ctx.restore()
            // 始终按固定帧率连续推（不再锁步等待），toBlob 完成后立即发（卡则丢帧保流畅）
            canvas.toBlob((blob) => {
              try {
                if (blob) {
                  StreamManager.sendClientFrame(blob)
                }
              } catch (e) {}
            }, 'image/jpeg', 0.4)
          } catch (e) {
            console.error('[Camera] 截图错误:', e)
          }
          this.localCaptureTimer = setTimeout(captureAndSend, FRAME_INTERVAL)
        }
        captureAndSend()
      } catch (e) {
        if (this.currentMode === 'retail') {
          this.answerHtml = `<p style="color:var(--accent-red)">无法访问摄像头: ${this.escapeHtml(e.message)}</p>`
        }
        console.error('[Camera] 错误:', e)
      }
    },
    openFilePicker() {
      const fileInput = document.getElementById('file-input')
      if (fileInput) fileInput.click()
    },
    async onFileSelected(e) {
      const file = e.target.files[0]
      if (!file) return
      const formData = new FormData()
      formData.append('file', file)
      if (this.currentMode === 'retail') {
        this.answerHtml = '<p class="placeholder-text">正在上传视频文件...</p>'
      }
      try {
        const resp = await fetch('/api/videos', { method: 'POST', body: formData })
        if (!resp.ok) {
          const err = await resp.json()
          if (this.currentMode === 'retail') {
            this.answerHtml = `<p style="color:var(--accent-red)">上传失败: ${this.escapeHtml(err.detail)}</p>`
          }
          e.target.value = ''
          return
        }
        const result = await resp.json()
        StreamManager.startFile(result.path)
        this.cameraActive = true
        this.sourceType = 'file'
        window.updateStatus('online', '视频播放中')
        if (this.currentMode === 'emotion') this.updateEmoStatus(true)
      } catch (err) {
        console.error('[Upload] 错误:', err)
      }
      e.target.value = ''
    },
    async stopVideo() {
      // 记录发起停止时的模式：await 期间 currentMode 可能已被 switchMode 修改
      const stopMode = this.currentMode

      if (this.localPreviewRaf) {
        cancelAnimationFrame(this.localPreviewRaf)
        this.localPreviewRaf = null
      }
      clearTimeout(this.localCaptureTimer)
      this.localCaptureTimer = null

      // 如果是表情模式，先调用stop接口获取分段分析并等待完成，再发WS停止，
      // 避免 REST 与 WS 竞争导致分段报告丢失
      if (stopMode === 'emotion' && this.cameraActive) {
        await this.stopEmotionCamera()
      }

      StreamManager.stop()
      this.cameraActive = false
      this.sourceType = null

      // 停止本地摄像头流
      if (this.localStream) {
        this.localStream.getTracks().forEach(t => t.stop())
        this.localStream = null
      }
      if (this.localVideoEl) {
        this.localVideoEl.srcObject = null
        this.localVideoEl.remove()
        this.localVideoEl = null
      }

      window.updateStatus('online', '等待指令')

      // 清空Canvas
      const canvasId = stopMode === 'retail' ? 'video-canvas' : 'video-canvas-emo'
      const canvas = document.getElementById(canvasId)
      if (canvas) {
        const ctx = canvas.getContext('2d')
        ctx.clearRect(0, 0, canvas.width, canvas.height)
      }
      const phId = stopMode === 'retail' ? 'video-placeholder' : 'video-placeholder-emo'
      const ph = document.getElementById(phId)
      if (ph) ph.style.display = 'flex'

      if (stopMode === 'emotion') {
        this.updateEmoStatus(false)
      }
    },
    async switchMode(mode) {
      if (mode === this.currentMode) return

      // 如果视频正在运行，await 停止完成（否则旧 stop 的异步 DELETE/WS 关闭
      // 会与新模式启动竞态，误停刚启动的新视频）
      if (this.cameraActive) {
        await this.stopVideo()
      }

      this.currentMode = mode
      StreamManager.setMode(mode)

      if (mode === 'retail') {
        // 切换Canvas
        const canvas = document.getElementById('video-canvas')
        StreamManager.canvas = canvas
        StreamManager.ctx = canvas ? canvas.getContext('2d') : null
        // 刷新零售数据
        this.fetchPopularityReport()
        this.fetchAnomalyReport()
        this.fetchEmotionStats()
      } else {
        StreamManager.init('video-canvas-emo')
        // 调用启动接口
        fetch('/api/emotion-cameras', { method: 'POST' }).then(r => r.json()).then(d => {
          console.log('[Emotion] 摄像头就绪:', d.msg)
        }).catch(() => {})
        // 刷新表情数据
        this.fetchEmotionSummary()
        this.fetchEmotionRecords()
        this.updateEmotionDbCount()
      }

      console.log('[Mode] 切换到', mode === 'retail' ? '零售分析' : '表情分析')
    },
    // ==================== 表情模式：状态与数据 ====================
    updateEmoStatus(running) {
      const dot = document.getElementById('emo-status-dot')
      const text = document.getElementById('emo-status-text')
      if (running) {
        if (dot) dot.className = 'status-dot online'
        if (text) {
          text.textContent = '运行中'
          text.style.color = 'var(--accent-green)'
        }
      } else {
        if (dot) dot.className = 'status-dot offline'
        if (text) {
          text.textContent = '已关闭'
          text.style.color = 'var(--text-secondary)'
        }
      }
    },
    async stopEmotionCamera() {
      try {
        const resp = await fetch('/api/emotion-cameras', { method: 'DELETE' })
        const data = await resp.json()
        if (data.code === 0 && data.segment_analysis) {
          this.renderAnalysisReport(data.segment_analysis)
        }
      } catch (e) {
        console.error('[Emotion] 停止失败:', e)
      }
    },
    renderAnalysisReport(seg) {
      const report = document.getElementById('analysis-report')
      const content = document.getElementById('analysis-content')
      if (!report || !content || !seg) return
      const fmt = arr => arr.map(([k, v]) => `${EMO_CN[k] || k}: ${v}`).join(', ') || '无数据'
      content.innerHTML = `
        <table class="analysis-table">
          <tr><th>阶段</th><th>表情分布</th></tr>
          <tr><td>前半段（采集前期）</td><td>${fmt(seg['前半段(采集前期)'])}</td></tr>
          <tr><td>后半段（采集后期）</td><td>${fmt(seg['后半段(采集后期)'])}</td></tr>
        </table>
        <div class="analysis-conclusion">${this.escapeHtml(seg['分析结论'])}</div>
      `
      report.classList.add('active')
    },
    // ==================== 零售模式：自然语言查询 ====================
    async sendQuery() {
      const question = this.queryInput.trim()
      if (!question) {
        this.answerHtml = '<p style="color:var(--accent-yellow)">请输入查询内容</p>'
        return
      }
      this.queryInput = ''
      if (window.prepareVoiceReply) window.prepareVoiceReply()
      this.answerHtml = '<p class="placeholder-text">分析中...</p>'
      const intentEl = document.getElementById('result-intent')
      const confEl = document.getElementById('result-confidence')
      const suggEl = document.getElementById('suggestions')
      if (intentEl) intentEl.textContent = ''
      if (confEl) confEl.textContent = ''
      if (suggEl) suggEl.innerHTML = ''
      try {
        const resp = await fetch('/api/queries/stream', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ question, session_id: this.getSessionId() })
        })
        if (!resp.ok) {
          const err = await resp.json()
          this.answerHtml = `<p style="color:var(--accent-red)">查询失败: ${this.escapeHtml(err.detail)}</p>`
          return
        }
        const reader = resp.body.getReader()
        const decoder = new TextDecoder()
        let fullText = ''
        let buffer = ''  // SSE 行缓冲：跨 chunk 被拆开的半行保留到下一个 chunk，避免丢字
        this.answerHtml = '<p></p>'
        while (true) {
          const { done, value } = await reader.read()
          if (done) break
          buffer += decoder.decode(value, { stream: true })
          const lines = buffer.split('\n')
          buffer = lines.pop()  // 末段可能是不完整行，留到下一 chunk
          for (const line of lines) {
            if (line.startsWith('data: ')) {
              const token = line.slice(6)
              if (token === '[DONE]') continue
              fullText += token
              let html = this.escapeHtml(fullText)
                .replace(/\*\*(.*?)\*\*/g, '<strong>$1</strong>')
                .replace(/\n/g, '<br>')
              this.answerHtml = `<p>${html}</p>`
            }
          }
        }
        // 处理结尾残留（可能是不带换行的最后一行）
        if (buffer.startsWith('data: ')) {
          const token = buffer.slice(6)
          if (token !== '[DONE]') {
            fullText += token
            let html = this.escapeHtml(fullText)
              .replace(/\*\*(.*?)\*\*/g, '<strong>$1</strong>')
              .replace(/\n/g, '<br>')
            this.answerHtml = `<p>${html}</p>`
          }
        }
        this.fetchDashboardData()
        if (window.playVoiceReply && fullText.trim()) {
          window.playVoiceReply(fullText)
        }
        this.loadSessions()
        requestAnimationFrame(() => {
          const target = document.getElementById('query-input-row') ||
            document.getElementById('query-section')
          if (target) {
            target.scrollIntoView({ behavior: 'smooth', block: 'end' })
          }
        })
      } catch (err) {
        this.answerHtml = `<p style="color:var(--accent-red)">网络错误: ${this.escapeHtml(err.message)}</p>`
      }
    },
    quickQuery(text) {
      this.queryInput = text
      this.sendQuery()
    },
    async fetchDashboardData() {
      try {
        const resp = await fetch('/api/reports/dashboard')
        if (resp.ok) {
          const data = await resp.json()
          if (data.popularity) {
            // /report/dashboard 的 popularity 只含 ranking 摘要，
            // 转成与 /report/popularity 相同的 zones 结构再喂给图表
            const ranking = data.popularity.ranking || []
            const zones = {}
            ranking.forEach(r => {
              zones[r.zone_id] = {
                zone_id: r.zone_id,
                zone_label: r.label || r.zone_id,
                visit_count: r.visit_count || 0,
                heat_score: r.heat_score || 0,
                total_dwell_seconds: r.total_dwell_seconds || 0,
                staff_count: r.staff_count || 0,
              }
            })
            const stats = {
              zones,
              top_zone: data.popularity.top_zone,
              total_visitors: data.popularity.total_visitors || 0,
            }
            ChartManager.updateFromStats(stats)
            this.updateRankingList(stats)
          }
        }
      } catch (e) {}
    },
    // ==================== 零售模式：仪表盘数据刷新 ====================
    async fetchPopularityReport() {
      try {
        const resp = await fetch('/api/reports/popularity')
        if (resp.ok) {
          const data = await resp.json()
          ChartManager.updateFromStats(data)
          this.updateRankingList(data)
          const tv = document.getElementById('total-visitors')
          if (tv) tv.textContent = data.total_visitors || 0
        }
      } catch (e) {}
    },
    updateRankingList(data) {
      const zones = data.zones || data
      const items = Object.values(zones).filter(z => z.heat_score !== undefined || z.visit_count !== undefined)
      items.sort((a, b) => (b.heat_score || 0) - (a.heat_score || 0))
      const list = document.getElementById('ranking-list')
      if (!list) return
      if (!items.length) {
        list.innerHTML = '<p class="placeholder-text">等待数据...</p>'
        return
      }
      list.innerHTML = items.slice(0, 8).map((z, i) => `
        <div class="ranking-item">
          <span class="rank">#${i + 1}</span>
          <span class="label">${this.escapeHtml(z.zone_label || z.zone_id)}</span>
          <span class="count">热度 ${Number(z.heat_score || 0).toFixed(0)}</span>
          <span class="dwell">${z.visit_count || 0}次/${Number(z.total_dwell_seconds || 0).toFixed(0)}s</span>
          ${z.staff_count > 0 ? `<span style="color:var(--accent-yellow);font-size:0.75em">店员${z.staff_count}</span>` : ''}
        </div>
      `).join('')
    },
    // ===== 热度 vs 销量比对（转化诊断） =====
    async fetchHotVsSales() {
      const box = document.getElementById('sales-compare-box')
      if (!box) return
      try {
        const resp = await fetch('/api/analytics/hot-vs-sales')
        if (!resp.ok) return
        const data = await resp.json()
        const zones = data.zones || []
        if (!zones.length) {
          box.innerHTML = '<p class="placeholder-text">暂无比对数据 —— <button id="btn-sales-simulate-vue" style="background:var(--accent-blue);color:#fff;border:none;border-radius:4px;padding:2px 10px;cursor:pointer">生成演示数据</button></p>'
          this.bindSalesSimulate()
          return
        }
        const quad = {
          healthy: '🟢 健康', high_heat_low_sales: '🔴 高热度低销量',
          low_heat_high_sales: '🟡 低热度高销量', high_heat_no_sales: '⚪ 有热度无销量',
          low_heat_low_sales: '⚫ 双低', cold: '⚪ 冷区',
        }
        const rows = zones.map(z => `
          <div style="display:flex;justify-content:space-between;padding:4px 6px;border-bottom:1px solid var(--border)">
            <span style="min-width:110px">${this.escapeHtml(z.zone_label)}</span>
            <span>客流 <b>${z.visit_count}</b></span>
            <span>销量 <b>${z.sold_count}</b></span>
            <span>转化 <b>${z.conversion_rate}%</b></span>
            <span title="${this.escapeHtml(z.diagnosis)}">${quad[z.quadrant] || z.quadrant}</span>
          </div>`).join('')
        box.innerHTML = `
          <div style="margin-bottom:6px;color:var(--text-secondary)">${this.escapeHtml(data.summary || '')}</div>
          ${rows}
          <div style="margin-top:6px"><button id="btn-sales-simulate-vue" style="background:var(--accent-blue);color:#fff;border:none;border-radius:4px;padding:2px 10px;cursor:pointer;font-size:0.9em">重新生成演示数据</button></div>`
        this.bindSalesSimulate()
      } catch (e) {}
    },
    bindSalesSimulate() {
      const btn = document.getElementById('btn-sales-simulate-vue')
      if (btn && !btn._bound) {
        btn._bound = true
        btn.addEventListener('click', async () => {
          try {
            await fetch('/api/analytics/sales-simulate', { method: 'POST' })
            this.fetchHotVsSales()
          } catch (e) {}
        })
      }
    },
    async fetchAnomalyReport() {
      try {
        const resp = await fetch('/api/reports/anomaly')
        if (resp.ok) {
          const data = await resp.json()
          this.updateAlertsFromQuery((data.high_risk || []).concat(data.watch_list || []))
        }
      } catch (e) {}
    },
    // 汇报栏渲染（REST 轮询与 WS 推送共用，避免两处逻辑漂移）
    updateReportBar(summary, type, timeStr) {
      const bar = document.getElementById('heat-report-bar')
      const text = document.getElementById('heat-report-text')
      if (!bar || !text || !summary) return
      text.textContent = (timeStr ? '[' + timeStr + '] ' : '') + summary
      bar.style.borderColor = type === 'surge' ? 'var(--accent-red)' : 'var(--border)'
      bar.style.display = 'block'
    },
    // 最新运营汇报（主动汇报 Agent 生成）— REST 兜底通道
    async fetchLatestReport() {
      try {
        const resp = await fetch('/api/reports/heat-reports?limit=1')
        if (!resp.ok) return
        const data = await resp.json()
        const reports = data.reports || []
        if (!reports.length) return
        const r = reports[0]
        const type = (r.data && typeof r.data === 'object') ? r.data.type : null
        this.updateReportBar(r.summary, type, r.report_time)
      } catch (e) {}
    },
    // 主动汇报 WS 订阅 — 实时推送通道（后台汇报 Agent 广播 /api/ws/reports）
    // 断线自动退避重连；失败不影响主链路（REST 轮询仍在跑）
    connectReportWs() {
      if (this.reportWsClosed) return
      const protocol = location.protocol === 'https:' ? 'wss:' : 'ws:'
      const url = `${protocol}//${location.host}/api/ws/reports`
      let ws
      try {
        ws = new WebSocket(url)
      } catch (e) {
        this.scheduleReportWsReconnect()
        return
      }
      this.reportWs = ws

      ws.onopen = () => {
        this.reportWsAttempts = 0
        this._startReportKeepalive()
      }
      ws.onmessage = (ev) => {
        try {
          const msg = JSON.parse(ev.data)
          if (!msg || msg.type !== 'report' || !msg.summary) return
          const inner = (msg.data && typeof msg.data === 'object') ? msg.data : {}
          const rawTs = msg.ts || inner.timestamp || ''
          const timeStr = rawTs ? String(rawTs).replace('T', ' ').slice(0, 19) : ''
          this.updateReportBar(msg.summary, inner.type, timeStr)
        } catch (e) {}
      }
      ws.onclose = () => {
        this._stopReportKeepalive()
        if (this.reportWs === ws) this.reportWs = null
        this.scheduleReportWsReconnect()
      }
      ws.onerror = () => {
        // 交给 onclose 统一处理重连，这里只确保连接被关闭
        try { ws.close() } catch (e) {}
      }
    },
    scheduleReportWsReconnect() {
      if (this.reportWsClosed || this.reportWsReconnectTimer) return
      this.reportWsAttempts = Math.min(this.reportWsAttempts + 1, 10)
      const delay = Math.min(3000 * this.reportWsAttempts, 30000)  // 3s 起，最长 30s
      this.reportWsReconnectTimer = setTimeout(() => {
        this.reportWsReconnectTimer = null
        this.connectReportWs()
      }, delay)
    },
    // 保活：汇报间隔 10 分钟，长时间空闲可能被中间代理（如内网穿透/反代）断开
    _startReportKeepalive() {
      this._stopReportKeepalive()
      this.reportWsKeepaliveTimer = setInterval(() => {
        const ws = this.reportWs
        if (ws && ws.readyState === WebSocket.OPEN) {
          try { ws.send(JSON.stringify({ type: 'ping' })) } catch (e) {}
        }
      }, 60000)
    },
    _stopReportKeepalive() {
      if (this.reportWsKeepaliveTimer) {
        clearInterval(this.reportWsKeepaliveTimer)
        this.reportWsKeepaliveTimer = null
      }
    },
    updateAlertsFromQuery(alerts) {
      const status = document.getElementById('alert-status')
      const list = document.getElementById('alert-list')
      if (!status || !list) return
      if (!alerts.length) {
        status.className = 'alert-status safe'
        status.innerHTML = '🟢 无异常告警'
        list.innerHTML = '<p class="placeholder-text">监控正常运行中</p>'
        return
      }
      const hasHigh = alerts.some(a => a.level === 'high')
      status.className = 'alert-status ' + (hasHigh ? 'danger' : 'warning')
      status.innerHTML = hasHigh
        ? `${alerts.length} 起告警（含高风险）`
        : `${alerts.length} 起关注告警`
      list.innerHTML = alerts.slice(0, 10).map(a => `
        <div class="alert-item ${a.level}">
          <div class="alert-title">
            ${a.level === 'high' ? '🔴' : '🟡'}
            人员 #${a.person_id}
            <span style="float:right">评分: ${a.score}/100</span>
          </div>
          <div class="alert-detail">
            ${(a.reasons || []).join('；')}
            ${a.frame_id ? ` | 帧: ${a.frame_id}` : ''}
          </div>
        </div>
      `).join('')
    },
    // 实时告警面板（WS 帧消息触发，对齐原生版 updateAlertsPanel）
    updateAlertsPanel(newAlerts, suspiciousTracks) {
      const status = document.getElementById('alert-status')
      const list = document.getElementById('alert-list')
      if (!status || !list) return
      if (!newAlerts.length && !(suspiciousTracks && suspiciousTracks.length)) {
        status.className = 'alert-status safe'
        status.innerHTML = '🟢 无异常告警'
        list.innerHTML = '<p class="placeholder-text">监控正常运行中</p>'
        return
      }
      const hasHigh = newAlerts.some(a => a.level === 'high')
      status.className = 'alert-status ' + (hasHigh ? 'danger' : 'warning')
      status.innerHTML = hasHigh
        ? `${newAlerts.length} 起高风险告警`
        : `${newAlerts.length} 起需关注告警`
      const items = newAlerts.slice(0, 10).map(a => `
        <div class="alert-item ${a.level}">
          <div class="alert-title">
            ${a.level === 'high' ? '🔴' : '🟡'}
            人员 #${a.person_id}
            <span style="float:right">评分: ${a.score}/100</span>
          </div>
          <div class="alert-detail">${(a.reasons || []).join('；')}</div>
        </div>
      `)
      const sus = (suspiciousTracks || []).slice(0, 5).map(t => `
        <div class="alert-item watch">
          <div class="alert-title">⚠️ 轨迹 #${t.track_id || t.person_id || '?'}
            <span style="float:right">评分: ${t.score || 0}/100</span>
          </div>
        </div>
      `)
      list.innerHTML = items.concat(sus).join('') || '<p class="placeholder-text">暂无明细</p>'
    },
    // 零售模式表情统计（内存版）
    async fetchEmotionStats() {
      try {
        const resp = await fetch('/api/emotions/stats')
        if (!resp.ok) return
        const d = await resp.json()
        const pos = document.getElementById('emo-pos')
        const neg = document.getElementById('emo-neg')
        const tot = document.getElementById('emo-tot')
        if (pos) pos.textContent = d.positive_count || '0'
        if (neg) neg.textContent = d.negative_count || '0'
        if (tot) tot.textContent = d.total_faces || '0'

        if (typeof echarts === 'undefined') return
        if (!this.retailEmoChart) {
          const el = document.getElementById('chart-emotion')
          if (!el) return
          this.retailEmoChart = echarts.init(el)
        }
        const dist = d.distribution || {}
        const data = Object.entries(EMO_CN)
          .filter(([k]) => dist[k] > 0)
          .map(([k, v]) => ({ name: v, value: dist[k], itemStyle: { color: EMO_COLORS[k] } }))
        if (!data.length) data.push({ name: '暂无', value: 1, itemStyle: { color: '#333' } })
        this.retailEmoChart.setOption({
          tooltip: { trigger: 'item', formatter: '{b}: {c}次 ({d}%)' },
          series: [{ type: 'pie', radius: ['45%', '70%'], center: ['50%', '50%'],
            label: { color: '#8899aa', fontSize: 10, formatter: '{b}\n{d}%' },
            labelLine: { lineStyle: { color: '#2a3f55' } },
            data: data }]
        }, true)
        setTimeout(() => {
          if (this.retailEmoChart) this.retailEmoChart.resize()
        }, 0)
      } catch (e) {}
    },
    // ==================== 表情模式：统计数据 ====================
    async fetchEmotionSummary() {
      try {
        const resp = await fetch('/api/emotion-records/summary?camera_id=camera_entrance&hours=1')
        if (!resp.ok) return
        const data = await resp.json()
        const total = data.total || 0
        const st = document.getElementById('stat-total')
        if (st) st.textContent = total

        let pos = 0
        let neg = 0
        const dist = {}
        ;(data.distribution || []).forEach(([emotion, count]) => {
          dist[emotion] = count
          if (POSITIVE.includes(emotion)) pos += count
          if (NEGATIVE.includes(emotion)) neg += count
        })
        const sp = document.getElementById('stat-positive')
        const sn = document.getElementById('stat-negative')
        if (sp) sp.textContent = pos
        if (sn) sn.textContent = neg

        this.renderEmotionBarChart(dist, total)
        this.renderEmotionPieChart(dist)
      } catch (e) {}
    },
    renderEmotionBarChart(dist, total) {
      const chart = document.getElementById('emotion-bar-chart')
      if (!chart) return
      if (!total) {
        chart.innerHTML = '<p class="placeholder-text">暂无识别数据</p>'
        return
      }
      const allEmotions = ['happy', 'neutral', 'surprise', 'sad', 'angry', 'fear', 'disgust']
      let html = ''
      allEmotions.forEach(emo => {
        const count = dist[emo] || 0
        const pct = total ? Math.round(count / total * 100) : 0
        html += `
          <div class="bar-row">
            <div class="bar-label">${EMO_CN[emo]}</div>
            <div class="bar-track">
              <div class="bar-fill" style="width:${pct}%;background:${EMO_COLORS[emo]}"></div>
            </div>
            <div class="bar-value">${count}</div>
          </div>
        `
      })
      chart.innerHTML = html
    },
    renderEmotionPieChart(dist) {
      if (typeof echarts === 'undefined') return
      if (!this.emoPieChart) {
        const el = document.getElementById('emotion-pie-chart')
        if (!el) return
        this.emoPieChart = echarts.init(el)
      }
      const data = Object.entries(EMO_CN)
        .filter(([k]) => dist[k] > 0)
        .map(([k, v]) => ({ name: v, value: dist[k], itemStyle: { color: EMO_COLORS[k] } }))
      if (!data.length) data.push({ name: '暂无', value: 1, itemStyle: { color: '#333' } })

      this.emoPieChart.setOption({
        tooltip: { trigger: 'item', formatter: '{b}: {c}次 ({d}%)' },
        series: [{ type: 'pie', radius: ['40%', '65%'], center: ['50%', '50%'],
          label: { color: '#8899aa', fontSize: 10, formatter: '{b}\n{d}%' },
          labelLine: { lineStyle: { color: '#2a3f55' } },
          data: data }]
      }, true)
      setTimeout(() => {
        if (this.emoPieChart) this.emoPieChart.resize()
      }, 0)
    },
    async fetchEmotionRecords() {
      try {
        const resp = await fetch('/api/emotion-records?camera_id=camera_entrance&limit=10')
        if (!resp.ok) return
        const data = await resp.json()
        const tbody = document.getElementById('emo-records-body')
        if (!tbody) return
        if (!data.records || data.records.length === 0) {
          tbody.innerHTML = '<tr><td colspan="3" style="color:var(--text-secondary)">暂无数据</td></tr>'
          return
        }
        tbody.innerHTML = data.records.map(r => {
          let cls = 'badge-neutral'
          if (POSITIVE.includes(r.emotion)) cls = 'badge-happy'
          else if (NEGATIVE.includes(r.emotion)) cls = 'badge-negative'
          else cls = 'badge-surprise'
          return `
            <tr>
              <td>${this.escapeHtml(r.time)}</td>
              <td><span class="emotion-badge ${cls}">${EMO_CN[r.emotion] || this.escapeHtml(r.emotion)}</span></td>
              <td>${(r.conf * 100).toFixed(1)}%</td>
            </tr>
          `
        }).join('')
      } catch (e) {}
    },
    async updateEmotionDbCount() {
      try {
        const resp = await fetch('/api/emotion-records/summary?camera_id=camera_entrance&hours=24')
        if (!resp.ok) return
        const data = await resp.json()
        const el = document.getElementById('emo-db-count')
        if (el) el.textContent = data.total || 0
      } catch (e) {}
    },
    // ==================== 会话记录 ====================
    async loadSessions() {
      try {
        const resp = await fetch('/api/chat/sessions')
        if (!resp.ok) throw new Error('load sessions failed')
        const data = await resp.json()
        this.sessions = data.sessions || []
        this.getSessionId()
      } catch (e) {
        this.sessions = []
      }
    },
    async searchSessions() {
      const keyword = (this.searchInput || '').trim()
      if (!keyword) {
        this.sessionSearchMode = false
        await this.loadSessions()
        return
      }
      try {
        const resp = await fetch('/api/chat/search?q=' + encodeURIComponent(keyword))
        if (!resp.ok) throw new Error('search failed')
        const data = await resp.json()
        this.sessionSearchMode = true
        this.sessions = (data.results || []).map(r => ({
          session_id: r.session_id,
          title: r.title || r.session_id,
          seq_no: r.seq_no || 1,
          question: r.question || '',
          message_count: 1,
        }))
      } catch (e) {
        this.sessions = []
      }
    },
    async createSession() {
      try {
        const resp = await fetch('/api/chat/sessions', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ title: '新会话' })
        })
        if (!resp.ok) throw new Error('create failed')
        const data = await resp.json()
        sessionStorage.setItem('chat_session_id', data.session_id)
        this.currentSessionId = data.session_id
        this.sessionSearchMode = false
        this.answerHtml = '<p class="placeholder-text">当前会话暂无记录</p>'
        const intentEl = document.getElementById('result-intent')
        const suggEl = document.getElementById('suggestions')
        if (intentEl) intentEl.textContent = ''
        if (suggEl) suggEl.innerHTML = ''
        await this.loadSessions()
      } catch (e) {
        console.error('[Session] 新建会话失败:', e)
      }
    },
    async saveSession() {
      try {
        await fetch(`/api/chat/sessions/${encodeURIComponent(this.getSessionId())}/save`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({})
        })
        await this.loadSessions()
      } catch (e) {
        console.error('[Session] 保存会话失败:', e)
      }
    },
    async switchSession(sessionId) {
      sessionStorage.setItem('chat_session_id', sessionId)
      this.currentSessionId = sessionId
      this.sessionSearchMode = false
      this.closeDrawer()
      try {
        const resp = await fetch(`/api/chat/sessions/${encodeURIComponent(sessionId)}/messages`)
        if (!resp.ok) throw new Error('load messages failed')
        const data = await resp.json()
        const messages = data.messages || []
        if (!messages.length) {
          this.answerHtml = '<p class="placeholder-text">当前会话暂无记录</p>'
        } else {
          this.answerHtml = messages.map(msg => `
            <div class="chat-bubble-row question-row">
              <div class="chat-bubble question">${this.escapeHtml(msg.question)}
                <div class="chat-time">${this.escapeHtml(msg.created_at || '')}</div>
              </div>
            </div>
            <div class="chat-bubble-row answer-row">
              <div class="chat-bubble answer">${this.escapeHtml(msg.answer || '无回答')}
                <div class="chat-time">${this.escapeHtml(msg.created_at || '')}</div>
              </div>
            </div>
          `).join('')
        }
        const intentEl = document.getElementById('result-intent')
        const suggEl = document.getElementById('suggestions')
        if (intentEl) intentEl.textContent = '会话记录'
        if (suggEl) suggEl.innerHTML = ''
        this.scrollToLatestMessage()
      } catch (e) {
        this.answerHtml = '<p class="placeholder-text">当前会话暂无记录</p>'
      }
      await this.loadSessions()
    },
    scrollToLatestMessage() {
      requestAnimationFrame(() => {
        const target = document.getElementById('query-input-row') ||
          document.getElementById('query-section')
        if (target) {
          target.scrollIntoView({ behavior: 'smooth', block: 'end' })
        }
      })
    },
    async renameSession(sessionId) {
      const session = this.sessions.find(s => s.session_id === sessionId)
      const oldTitle = session ? session.title : ''
      const title = prompt('重命名会话', oldTitle || '新会话')
      if (!title || !title.trim()) return
      try {
        await fetch(`/api/chat/sessions/${encodeURIComponent(sessionId)}/rename`, {
          method: 'PUT',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ title: title.trim() })
        })
        await this.loadSessions()
      } catch (e) {
        console.error('[Session] 重命名失败:', e)
      }
    },
    async deleteSession(sessionId) {
      if (!confirm('确定删除该会话及其全部记录？')) return
      try {
        await fetch(`/api/chat/sessions/${encodeURIComponent(sessionId)}`, { method: 'DELETE' })
        if (this.currentSessionId === sessionId) {
          await this.createSession()
        } else {
          await this.loadSessions()
        }
      } catch (e) {
        console.error('[Session] 删除失败:', e)
      }
    }
  }
}
</script>
