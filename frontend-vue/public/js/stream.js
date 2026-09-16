/**
 * WebSocket 视频流处理模块（双模式）
 * 负责连接 /api/ws/stream，接收并渲染视频帧
 * 支持 retail(零售分析) 和 emotion(表情分析) 两种模式
 */
const StreamManager = {
    ws: null,
    clientWs: null,
    canvas: null,
    ctx: null,
    img: null,
    connected: false,
    shouldReconnect: true,
    manualDisconnect: false,
    reconnectAttempts: 0,
    reconnectTimer: null,
    heartbeatTimer: null,
    heartbeatTimeoutTimer: null,
    waitingPong: false,
    reconnectAction: null,
    clientShouldReconnect: false,
    clientReconnectTimer: null,
    fpsFrameCount: 0,
    fpsLastTime: 0,
    fpsValue: null,
    currentMode: 'retail',
    onFrameCallbacks: [],
    onEventCallbacks: [],

    init(canvasId) {
        this.canvas = document.getElementById(canvasId);
        if (this.canvas) {
            this.canvas.style.display = '';
            this.ctx = this.canvas.getContext('2d');
            this.ctx.fillStyle = '#0f1923';
            this.ctx.fillRect(0, 0, this.canvas.width, this.canvas.height);
            this.ctx.fillStyle = '#556677';
            this.ctx.font = '48px sans-serif';
            this.ctx.textAlign = 'center';
            this.ctx.fillText('\u{1F4F7}', this.canvas.width/2, this.canvas.height/2 - 10);
            this.ctx.font = '14px sans-serif';
            this.ctx.fillStyle = '#8899aa';
            this.ctx.fillText('点击摄像头按钮开始', this.canvas.width/2, this.canvas.height/2 + 40);
        }
        this.img = new Image();
        this.img.onload = () => {
            if (this.ctx && this.canvas) {
                const scale = Math.max(
                    this.canvas.width / this.img.width,
                    this.canvas.height / this.img.height
                );
                const dw = this.img.width * scale;
                const dh = this.img.height * scale;
                const dx = (this.canvas.width - dw) / 2;
                const dy = (this.canvas.height - dh) / 2;
                this.ctx.clearRect(0, 0, this.canvas.width, this.canvas.height);
                this.ctx.drawImage(this.img, dx, dy, dw, dh);
            }
        };
    },

    setMode(mode) {
        this.currentMode = mode;
    },

    connect() {
        clearTimeout(this.reconnectTimer);
        this._stopHeartbeat();
        this.manualDisconnect = false;
        this.shouldReconnect = true;
        if (this.ws) {
            try { this.ws.close(); } catch (e) {}
            this.ws = null;
        }

        const protocol = location.protocol === 'https:' ? 'wss:' : 'ws:';
        const url = `${protocol}//${location.host}/api/ws/stream`;

        this.ws = new WebSocket(url);
        this.ws.binaryType = 'arraybuffer';

        this.ws.onopen = () => {
            console.log('[Stream] WebSocket 已连接');
            this.connected = true;
            this.reconnectAttempts = 0;
            this.fpsFrameCount = 0;
            this.fpsLastTime = performance.now();
            this.fpsValue = document.getElementById('fps-value');
            updateStatus('online', '已连接');
            this._startHeartbeat();
            if (this.reconnectAction) {
                this.sendAction(this.reconnectAction.action, this.reconnectAction.params);
            }
        };

        this.ws.onmessage = (event) => {
            if (event.data instanceof ArrayBuffer) {
                this._handleBinaryFrame(event.data);
                return;
            }
            this.waitingPong = false;
            clearTimeout(this.heartbeatTimeoutTimer);
            try {
                const msg = JSON.parse(event.data);
                if (msg.type === 'pong') {
                    return;
                }
                this._handleMessage(msg);
            } catch(e) {
                console.error('[Stream] 消息解析失败:', e);
            }
        };

        this.ws.onerror = (err) => {
            console.error('[Stream] WebSocket 错误:', err);
        };

        this.ws.onclose = (event) => {
            console.log('[Stream] WebSocket 已断开');
            this.connected = false;
            this._stopHeartbeat();
            // D3：1008 = 鉴权失效（会话过期）→ 不再重连，由 auth-guard 统一回登录页。
            // 注意 1012（service restart，后端重启）必须继续重连，见 auth-guard.js。
            const closeCode = (event && typeof event.code === 'number') ? event.code : 0;
            if (window.DSH_AUTH &&
                window.DSH_AUTH.shouldReconnectOnClose(closeCode) === false) {
                if (this.canvas) this.canvas.style.display = 'none';
                const ph = document.getElementById('video-placeholder');
                if (ph) ph.style.display = 'flex';
                updateStatus('offline', '登录已过期，请重新登录');
                return;
            }
            if (!this.manualDisconnect && this.reconnectAction) {
                updateStatus('warning', '连接断开，正在重连...');
            } else {
                if (this.canvas) this.canvas.style.display = 'none';
                const ph = document.getElementById('video-placeholder');
                if (ph) ph.style.display = 'flex';
                updateStatus('offline', '已断开');
            }
            if (!this.manualDisconnect && this.shouldReconnect) {
                const delay = Math.min(10000, 1000 * Math.pow(2, this.reconnectAttempts));
                this.reconnectAttempts += 1;
                this.reconnectTimer = setTimeout(() => this.connect(), delay);
            }
        };
    },

    _startHeartbeat() {
        this._stopHeartbeat();
        this.heartbeatTimer = setInterval(() => {
            if (!this.ws || this.ws.readyState !== WebSocket.OPEN) return;
            this.waitingPong = true;
            this.ws.send(JSON.stringify({ type: 'ping', ts: Date.now() }));
            this.heartbeatTimeoutTimer = setTimeout(() => {
                if (this.waitingPong && this.ws) {
                    this.ws.close();
                }
            }, 8000);
        }, 15000);
    },

    _stopHeartbeat() {
        clearInterval(this.heartbeatTimer);
        clearTimeout(this.heartbeatTimeoutTimer);
        this.heartbeatTimer = null;
        this.heartbeatTimeoutTimer = null;
        this.waitingPong = false;
    },

    _trackFps() {
        const now = performance.now();
        this.fpsFrameCount += 1;
        if (!this.fpsLastTime) this.fpsLastTime = now;
        if (now - this.fpsLastTime < 1000) return;
        const fps = Math.round(this.fpsFrameCount * 1000 / (now - this.fpsLastTime));
        if (!this.fpsValue) this.fpsValue = document.getElementById('fps-value');
        if (this.fpsValue) this.fpsValue.textContent = fps;
        this.fpsFrameCount = 0;
        this.fpsLastTime = now;
    },

    _handleBinaryFrame(buffer) {
        const view = new DataView(buffer);
        const frameId = view.getUint32(0);
        const jpegBytes = new Uint8Array(buffer, 4);
        const blob = new Blob([jpegBytes], { type: 'image/jpeg' });
        const url = URL.createObjectURL(blob);
        // 前一帧若仍在解码（onload 未触发）其 blob URL 无人 revoke，
        // 这里在赋值新 src 前主动释放旧 URL，防止高帧率下内存无界增长
        if (this._pendingFrameUrl) {
            URL.revokeObjectURL(this._pendingFrameUrl);
        }
        this._pendingFrameUrl = url;
        const drawImage = () => {
            if (this.ctx && this.canvas) {
                const scale = Math.max(
                    this.canvas.width / this.img.width,
                    this.canvas.height / this.img.height
                );
                const dw = this.img.width * scale;
                const dh = this.img.height * scale;
                const dx = (this.canvas.width - dw) / 2;
                const dy = (this.canvas.height - dh) / 2;
                this.ctx.clearRect(0, 0, this.canvas.width, this.canvas.height);
                this.ctx.drawImage(this.img, dx, dy, dw, dh);
            }
        };
        this.img.onload = () => {
            URL.revokeObjectURL(url);
            if (this._pendingFrameUrl === url) this._pendingFrameUrl = null;
            drawImage();
        };
        if (this.canvas) this.canvas.style.display = '';
        this.img.src = url;
        if (this.currentMode === 'emotion') {
            const emoFrameId = document.getElementById('emo-frame-id');
            if (emoFrameId) emoFrameId.textContent = frameId;
        } else {
            const frameIdEl = document.getElementById('frame-id');
            if (frameIdEl) frameIdEl.textContent = frameId;
        }
        this._trackFps();
        const placeholder = document.getElementById('video-placeholder');
        if (placeholder) placeholder.style.display = 'none';
    },

    _handleMessage(msg) {
        switch (msg.type) {
            case 'frame':
                // 服务端以二进制帧推送画面，JSON 元数据仅用于更新统计信息
                if (this.canvas) this.canvas.style.display = '';

                if (msg.mode === 'emotion') {
                    const emoFrameId = document.getElementById('emo-frame-id');
                    if (emoFrameId) emoFrameId.textContent = msg.frame_id;
                    updateEmotionFaces(msg.faces || []);
                } else {
                    const frameIdEl = document.getElementById('frame-id');
                    if (frameIdEl) frameIdEl.textContent = msg.frame_id;
                    const tracksEl = document.getElementById('active-tracks');
                    if (tracksEl) tracksEl.textContent = msg.tracks ? msg.tracks.length : 0;
                }

                const placeholder = document.getElementById('video-placeholder');
                if (placeholder) placeholder.style.display = 'none';

                this.onFrameCallbacks.forEach(cb => cb(msg));
                break;

            case 'event':
                this.onEventCallbacks.forEach(cb => cb(msg));
                break;

            case 'status':
                console.log('[Stream] 状态:', msg.message);
                if (msg.status === 'finished' || msg.status === 'stopped') {
                    if (msg.status === 'finished') {
                        // 视频播放完毕，避免断线重连后自动重播同一文件
                        this.reconnectAction = null;
                    }
                    if (msg.segment_analysis && typeof window.renderAnalysisReport === 'function') {
                        window.renderAnalysisReport(msg.segment_analysis);
                    }
                    updateStatus('offline', msg.message);
                    setTimeout(() => {
                        if (this.canvas && this.ctx) {
                            this.canvas.style.display = 'none';
                            this.ctx.clearRect(0, 0, this.canvas.width, this.canvas.height);
                        }
                        const ph = document.getElementById('video-placeholder');
                        if (ph) ph.style.display = 'flex';
                    }, 100);
                } else if (msg.status === 'error') {
                    updateStatus('warning', msg.message);
                }
                break;
        }
    },

    sendAction(action, params = {}) {
        if (this.ws && this.ws.readyState === WebSocket.OPEN) {
            this.ws.send(JSON.stringify({ action, mode: this.currentMode, ...params }));
        }
    },

    startWebcam(cameraId = 0) {
        this.reconnectAction = { action: 'start_webcam', params: { camera_id: cameraId } };
        this.sendAction('start_webcam', { camera_id: cameraId });
    },

    startFile(filePath) {
        this.reconnectAction = { action: 'start_file', params: { file_path: filePath } };
        this.sendAction('start_file', { file_path: filePath });
    },

    startClientCamera() {
        this.reconnectAction = { action: 'start_client_camera', params: {} };
        this.sendAction('start_client_camera', {});
        this.startClientStream();
    },

    startClientStream() {
        this.closeClientStream();
        clearTimeout(this.clientReconnectTimer);
        this.clientShouldReconnect = true;
        const protocol = location.protocol === 'https:' ? 'wss:' : 'ws:';
        this.clientWs = new WebSocket(`${protocol}//${location.host}/api/ws/client`);
        this.clientWs.onopen = () => {
            console.log('[Stream] 本机摄像头二进制流已连接');
        };
        this.clientWs.onclose = () => {
            if (this.clientWs) this.clientWs = null;
            if (this.clientShouldReconnect) {
                this.clientReconnectTimer = setTimeout(() => this.startClientStream(), 1500);
            }
        };
    },

    sendClientFrame(frameBlob) {
        if (this.clientWs && this.clientWs.readyState === WebSocket.OPEN) {
            this.clientWs.send(frameBlob);
        }
    },

    closeClientStream() {
        this.clientShouldReconnect = false;
        clearTimeout(this.clientReconnectTimer);
        if (this.clientWs) {
            try {
                this.clientWs.close();
            } catch (e) {}
            this.clientWs = null;
        }
    },

    pause() { this.sendAction('pause'); },
    resume() { this.sendAction('resume'); },
    stop() {
        this.reconnectAction = null;
        this.sendAction('stop');
        this.closeClientStream();
    },

    disconnect() {
        this.manualDisconnect = true;
        this.shouldReconnect = false;
        clearTimeout(this.reconnectTimer);
        this.closeClientStream();
        if (this.ws) {
            this.ws.close();
            this.ws = null;
            this.connected = false;
            updateStatus('offline', '已断开');
        }
    },

    onFrame(callback) { this.onFrameCallbacks.push(callback); },
    onEvent(callback) { this.onEventCallbacks.push(callback); },

    // 清空全部回调（组件卸载时调用，防止重挂载后旧回调累积重复处理）
    clearCallbacks() {
        this.onFrameCallbacks = [];
        this.onEventCallbacks = [];
    },
};
