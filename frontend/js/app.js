/**
 * 主应用逻辑 — 双模式整合（零售分析 + 表情分析）
 * 初始化组件、绑定事件、模式切换、协调各模块
 */
(function () {
    'use strict';

    const $ = (id) => document.getElementById(id);

    // ==================== DOM 元素引用 ====================
    const btnWebcam = $('btn-webcam');
    const btnLocalCam = $('btn-local-cam');
    const btnCameraMenu = $('btn-camera-menu');
    const cameraMenu = $('camera-menu');
    const btnUpload = $('btn-upload');
    const btnStop = $('btn-stop');
    const fileInput = $('file-input');
    const cameraSelect = $('camera-select');
    const queryInput = $('query-input');
    const btnQuery = $('btn-query');
    const statusIndicator = $('status-indicator');
    const statusText = $('status-text');
    const answerText = $('answer-text');
    const resultIntent = $('result-intent');
    const resultConfidence = $('result-confidence');
    const suggestions = $('suggestions');
    const alertStatus = $('alert-status');
    const alertList = $('alert-list');
    const rankingList = $('ranking-list');

    // ==================== 初始化 ====================
    StreamManager.init('video-canvas');
    StreamManager.connect();
    ChartManager.init('chart-heat');

    // ==================== 状态管理 ====================
    let appState = {
        videoActive: false,
        sourceType: null,
        currentMode: 'retail',
        localStream: null,
        localPreviewRaf: null,
    };

    let currentMode = 'retail';

    window.updateStatus = function(status, text) {
        statusIndicator.className = 'status-dot ' + status;
        statusText.textContent = text;
    };

    window.updateEmotionFaces = function(faces) {
        const faceCount = faces.length;
        $('emo-face-count').textContent = faceCount;
        $('emo-active-faces').textContent = faceCount;
    };

    // ==================== 模式切换 ====================
    window.switchMode = async function(mode) {
        if (mode === currentMode) return;

        // 如果视频正在运行，先 await 停止完成（否则旧 stop 的异步 DELETE/WS 关闭
        // 会与新模式的启动竞态，误停刚启动的新视频）
        if (appState.videoActive) {
            await stopVideo();
        }

        currentMode = mode;
        appState.currentMode = mode;
        StreamManager.setMode(mode);

        // 更新Tab样式
        $('tab-retail').classList.toggle('active', mode === 'retail');
        $('tab-emotion').classList.toggle('active', mode === 'emotion');

        // 显示/隐藏对应容器
        $('retail-container').style.display = mode === 'retail' ? '' : 'none';
        $('emotion-container').style.display = mode === 'emotion' ? '' : 'none';
        $('query-section').style.display = mode === 'retail' ? '' : 'none';

        // 更新主容器class
        const mainContainers = document.querySelectorAll('.main-container');
        mainContainers.forEach(c => {
            c.style.display = '';
        });

        if (mode === 'retail') {
            $('emotion-container').style.display = 'none';
            // 切换Canvas
            StreamManager.canvas = $('video-canvas');
            StreamManager.ctx = StreamManager.canvas.getContext('2d');
            // 刷新零售数据
            fetchPopularityReport();
            fetchAnomalyReport();
            fetchEmotionStats();
        } else {
            $('retail-container').style.display = 'none';
            // 切换Canvas
            StreamManager.canvas = $('video-canvas-emo');
            StreamManager.ctx = StreamManager.canvas.getContext('2d');
            StreamManager.init('video-canvas-emo');
            // 调用启动接口
            fetch('/api/emotion-cameras', { method: 'POST' }).then(r => r.json()).then(d => {
                console.log('[Emotion] 摄像头就绪:', d.msg);
            }).catch(() => {});
            // 刷新表情数据
            fetchEmotionSummary();
            fetchEmotionRecords();
            updateEmotionDbCount();
        }

        console.log('[Mode] 切换到', mode === 'retail' ? '零售分析' : '表情分析');
    };

    function setButtonsState(active) {
        btnWebcam.disabled = active;
        btnLocalCam.disabled = active;
        if (btnCameraMenu) {
            btnCameraMenu.textContent = active ? '切换摄像头' : '摄像头';
        }
        btnUpload.disabled = active;
        btnStop.disabled = !active;
        if (active) {
            const ph = currentMode === 'retail'
                ? $('video-placeholder')
                : $('video-placeholder-emo');
            if (ph) ph.style.display = 'none';
        }
    }

    // ==================== 摄像头扫描 ====================
    async function scanCameras() {
        try {
            const resp = await fetch('/api/cameras');
            const data = await resp.json();
            cameraSelect.innerHTML = '';
            if (data.cameras && data.cameras.length > 0) {
                data.cameras.forEach(c => {
                    const opt = document.createElement('option');
                    opt.value = c.id;
                    opt.textContent = `摄像头 ${c.id} (${c.resolution})`;
                    if (c.id === data.default) opt.selected = true;
                    cameraSelect.appendChild(opt);
                });
            } else {
                cameraSelect.innerHTML = '<option value="">无可用摄像头</option>';
            }
        } catch(e) {
            cameraSelect.innerHTML = '<option value="">扫描失败</option>';
        }
    }
    scanCameras();

    // ==================== 视频控制（双模式通用） ====================
    function closeCameraMenu() {
        if (cameraMenu) cameraMenu.style.display = 'none';
    }

    async function selectCamera(kind) {
        closeCameraMenu();
        if (appState.videoActive && appState.sourceType === kind) return;
        if (appState.videoActive) {
            // await 停止完成，避免旧 stop 的异步关闭误停新摄像头
            await stopVideo();
        }
        if (kind === 'webcam') {
            btnWebcam.click();
        } else if (kind === 'local') {
            btnLocalCam.click();
        }
    }

    if (btnCameraMenu) {
        btnCameraMenu.addEventListener('click', (e) => {
            e.stopPropagation();
            if (cameraMenu) {
                cameraMenu.style.display = cameraMenu.style.display === 'none' ? '' : 'none';
            }
        });
    }
    if (cameraMenu) {
        cameraMenu.addEventListener('click', (e) => {
            const kind = e.target.closest('button')?.dataset.camera;
            if (kind) selectCamera(kind);
        });
    }
    document.addEventListener('click', closeCameraMenu);

    btnWebcam.addEventListener('click', () => {
        const camId = parseInt(cameraSelect.value) || 0;
        StreamManager.startWebcam(camId);
        appState.videoActive = true;
        appState.sourceType = 'webcam';
        setButtonsState(true);
        const modeLabel = currentMode === 'retail' ? '货架' : '出入口';
        updateStatus('online', `${modeLabel}摄像头 #${camId} 运行中`);

        if (currentMode === 'emotion') {
            updateEmoStatus(true);
        }
    });

    btnLocalCam.addEventListener('click', async () => {
        try {
            const devices = await navigator.mediaDevices.enumerateDevices();
            const cameras = devices.filter(d => d.kind === 'videoinput');
            let deviceId = undefined;
            if (cameras.length > 1) {
                const labels = cameras.map((c, i) => {
                    const name = c.label || ('摄像头 ' + (i + 1));
                    return `${i}: ${name}`;
                });
                const choice = prompt('选择本机摄像头：\n' + labels.join('\n'), '0');
                if (choice === null) return;
                const idx = parseInt(choice) || 0;
                deviceId = cameras[Math.min(idx, cameras.length - 1)]?.deviceId;
            }
            const videoConstraints = {
                width: { ideal: 640 },
                height: { ideal: 480 },
                aspectRatio: { ideal: 4 / 3 },
                frameRate: { ideal: 30 },
            };
            if (deviceId) videoConstraints.deviceId = { exact: deviceId };
            else videoConstraints.facingMode = 'environment';
            const stream = await navigator.mediaDevices.getUserMedia({ video: videoConstraints });
            appState.localStream = stream;
            const videoEl = document.createElement('video');
            videoEl.srcObject = stream;
            videoEl.setAttribute('playsinline', '');
            videoEl.setAttribute('autoplay', '');
            videoEl.setAttribute('muted', '');
            videoEl.style.cssText = 'position:fixed;top:-9999px;left:-9999px';
            document.body.appendChild(videoEl);
            appState.localVideoEl = videoEl;  // 保存引用，停止时移除（防 DOM 累积泄漏）
            await videoEl.play();
            for (let i = 0; i < 20 && !(videoEl.videoWidth && videoEl.videoHeight); i++) {
                await new Promise(resolve => setTimeout(resolve, 50));
            }
            const drawLocalPreview = () => {
                if (!appState.videoActive || appState.sourceType !== 'local') return;
                const previewCanvas = currentMode === 'retail'
                    ? $('video-canvas')
                    : $('video-canvas-emo');
                const previewCtx = previewCanvas.getContext('2d');
                const pW = previewCanvas.width || 960;
                const pH = previewCanvas.height || 540;
                const srcWPreview = videoEl.videoWidth || 480;
                const srcHPreview = videoEl.videoHeight || 360;
                const previewScale = Math.max(pW / srcWPreview, pH / srcHPreview);
                const dw = srcWPreview * previewScale;
                const dh = srcHPreview * previewScale;
                const dx = (pW - dw) / 2;
                const dy = (pH - dh) / 2;
                try {
                    previewCtx.fillStyle = '#000';
                    previewCtx.fillRect(0, 0, pW, pH);
                    // 镜像模式：水平翻转（自拍视角，画面与手势同向）
                    previewCtx.save();
                    previewCtx.translate(pW, 0);
                    previewCtx.scale(-1, 1);
                    previewCtx.drawImage(videoEl, dx, dy, dw, dh);
                    previewCtx.restore();
                } catch (e) {}
                appState.localPreviewRaf = requestAnimationFrame(drawLocalPreview);
            };
            StreamManager.startClientCamera();
            appState.videoActive = true;
            appState.sourceType = 'local';
            drawLocalPreview();
            setButtonsState(true);
            updateStatus('online', '本机摄像头运行中');

            if (currentMode === 'emotion') {
                updateEmoStatus(true);
            }

            const vw = videoEl.videoWidth || 480;
            const vh = videoEl.videoHeight || 360;
            const outW = 640;
            const outH = 480;
            const canvas = document.createElement('canvas');
            canvas.width = outW;
            canvas.height = outH;
            const ctx = canvas.getContext('2d');
            console.log('[Camera] 本机视频尺寸', vw, vh, '发送尺寸', outW, outH);

            let srcX = 0;
            let srcY = 0;
            let srcW = vw;
            let srcH = vh;
            if (vw / vh > outW / outH) {
                srcW = vh * outW / outH;
                srcX = (vw - srcW) / 2;
            } else if (vw / vh < outW / outH) {
                srcH = vw * outH / outW;
                srcY = (vh - srcH) / 2;
            }

            let sending = false;
            async function captureAndSend() {
                if (!appState.videoActive) {
                    stream.getTracks().forEach(t => t.stop());
                    return;
                }
                if (sending) { setTimeout(captureAndSend, 33); return; }
                sending = true;
                try {
                    // 与预览一致的镜像翻转：发送给服务端的帧也水平翻转，
                    // 保证服务端叠加的检测框/标注与用户看到的画面方向一致
                    ctx.save();
                    ctx.translate(outW, 0);
                    ctx.scale(-1, 1);
                    ctx.drawImage(videoEl, srcX, srcY, srcW, srcH, 0, 0, outW, outH);
                    ctx.restore();
                    canvas.toBlob((blob) => {
                        try {
                            if (blob) {
                                StreamManager.sendClientFrame(blob);
                            }
                        } catch (e) {}
                        sending = false;
                        setTimeout(captureAndSend, 33);
                    }, 'image/jpeg', 0.5);
                } catch(e) {
                    sending = false;
                    setTimeout(captureAndSend, 33);
                }
            }
            captureAndSend();
        } catch(e) {
            if (currentMode === 'retail') {
                answerText.innerHTML = `<p style="color:var(--accent-red)">无法访问摄像头: ${e.message}</p>`;
            }
            console.error('[Camera] 错误:', e);
        }
    });

    btnUpload.addEventListener('click', () => fileInput.click());

    fileInput.addEventListener('change', async (e) => {
        const file = e.target.files[0];
        if (!file) return;
        const formData = new FormData();
        formData.append('file', file);
        const statusEl = currentMode === 'retail' ? answerText : $('emo-status-text');
        if (currentMode === 'retail') {
            answerText.innerHTML = '<p class="placeholder-text">正在上传视频文件...</p>';
        } else if (statusEl) {
            statusEl.textContent = '正在上传视频...';
        }
        try {
            const resp = await fetch('/api/videos', { method: 'POST', body: formData });
            if (!resp.ok) {
                const err = await resp.json();
                if (currentMode === 'retail') {
                    answerText.innerHTML = `<p style="color:var(--accent-red)">上传失败: ${escapeHtml(err.detail)}</p>`;
                } else if (statusEl) {
                    statusEl.textContent = '上传失败: ' + (err.detail || '未知错误');
                }
                fileInput.value = '';
                return;
            }
            const result = await resp.json();
            StreamManager.startFile(result.path);
            appState.videoActive = true;
            appState.sourceType = 'file';
            setButtonsState(true);
            updateStatus('online', '视频播放中');
            if (currentMode === 'emotion') updateEmoStatus(true);
        } catch(err) {
            console.error('[Upload] 错误:', err);
            if (currentMode !== 'retail' && statusEl) {
                statusEl.textContent = '上传失败: ' + (err.message || '网络错误');
            }
        }
        fileInput.value = '';
    });

    async function stopVideo() {
        // 记录发起停止时的模式：await 期间 currentMode 可能已被 switchMode 修改
        const stopMode = currentMode;

        if (appState.localPreviewRaf) {
            cancelAnimationFrame(appState.localPreviewRaf);
            appState.localPreviewRaf = null;
        }

        // 如果是表情模式，先调用stop接口获取分段分析并等待完成，再发WS停止，
        // 避免 REST 与 WS 竞争导致分段报告丢失
        if (stopMode === 'emotion' && appState.videoActive) {
            await stopEmotionCamera();
        }

        StreamManager.stop();
        appState.videoActive = false;
        setButtonsState(false);

        // 停止本地摄像头流 + 移除隐藏 video 元素（防 DOM 累积泄漏）
        if (appState.localStream) {
            appState.localStream.getTracks().forEach(t => t.stop());
            appState.localStream = null;
        }
        if (appState.localVideoEl) {
            appState.localVideoEl.srcObject = null;
            appState.localVideoEl.remove();
            appState.localVideoEl = null;
        }

        updateStatus('online', '等待指令');

        // 清空Canvas
        const canvasId = stopMode === 'retail' ? 'video-canvas' : 'video-canvas-emo';
        const canvas = $(canvasId);
        if (canvas) {
            const ctx = canvas.getContext('2d');
            ctx.clearRect(0, 0, canvas.width, canvas.height);
        }
        const phId = stopMode === 'retail' ? 'video-placeholder' : 'video-placeholder-emo';
        const ph = $(phId);
        if (ph) ph.style.display = 'flex';

        if (stopMode === 'emotion') {
            updateEmoStatus(false);
        }
    }

    btnStop.addEventListener('click', stopVideo);

    // ==================== 表情模式：状态与数据 ====================

    function updateEmoStatus(running) {
        const dot = $('emo-status-dot');
        const text = $('emo-status-text');
        if (running) {
            dot.className = 'status-dot online';
            text.textContent = '运行中';
            text.style.color = 'var(--accent-green)';
        } else {
            dot.className = 'status-dot offline';
            text.textContent = '已关闭';
            text.style.color = 'var(--text-secondary)';
        }
    }

    async function stopEmotionCamera() {
        try {
            const resp = await fetch('/api/emotion-cameras', { method: 'DELETE' });
            const data = await resp.json();
            if (data.code === 0 && data.segment_analysis) {
                renderAnalysisReport(data.segment_analysis);
            }
        } catch(e) {
            console.error('[Emotion] 停止失败:', e);
        }
    }

    function renderAnalysisReport(seg) {
        const report = $('analysis-report');
        const content = $('analysis-content');
        const labels = { happy: '开心', neutral: '平静', surprise: '惊讶',
                         sad: '悲伤', angry: '愤怒', fear: '害怕', disgust: '厌恶' };
        const fmt = arr => arr.map(([k, v]) => `${labels[k] || k}: ${v}`).join(', ') || '无数据';
        content.innerHTML = `
            <table class="analysis-table">
                <tr><th>阶段</th><th>表情分布</th></tr>
                <tr><td>前半段（采集前期）</td><td>${fmt(seg['前半段(采集前期)'])}</td></tr>
                <tr><td>后半段（采集后期）</td><td>${fmt(seg['后半段(采集后期)'])}</td></tr>
            </table>
            <div class="analysis-conclusion">${seg['分析结论']}</div>
        `;
        report.classList.add('active');
    }

    // 暴露给 stream.js：WS stop 消息携带分段分析时直接渲染
    window.renderAnalysisReport = renderAnalysisReport;

    // 表情统计刷新
    const _emoCN = { happy: '开心', neutral: '平静', surprise: '惊讶',
                     sad: '悲伤', angry: '愤怒', fear: '害怕', disgust: '厌恶' };
    const _emoColors = { happy: '#f0c040', neutral: '#8899aa', surprise: '#c084fc',
                         sad: '#4da6ff', angry: '#f05050', fear: '#f08030', disgust: '#4caf84' };
    const _emoBarColors = { happy: '#f0c040', neutral: '#8899aa', surprise: '#c084fc',
                            sad: '#4da6ff', angry: '#f05050', fear: '#f08030', disgust: '#4caf84' };
    const POSITIVE = ['happy', 'neutral'];
    const NEGATIVE = ['angry', 'disgust', 'fear', 'sad'];
    let _emoPieChart = null;

    async function fetchEmotionSummary() {
        try {
            const resp = await fetch('/api/emotion-records/summary?camera_id=camera_entrance&hours=1');
            if (!resp.ok) return;
            const data = await resp.json();
            const total = data.total || 0;
            $('stat-total').textContent = total;

            let pos = 0, neg = 0;
            const dist = {};
            (data.distribution || []).forEach(([emotion, count]) => {
                dist[emotion] = count;
                if (POSITIVE.includes(emotion)) pos += count;
                if (NEGATIVE.includes(emotion)) neg += count;
            });
            $('stat-positive').textContent = pos;
            $('stat-negative').textContent = neg;

            renderEmotionBarChart(dist, total);
            renderEmotionPieChart(dist);
        } catch(e) {}
    }

    function renderEmotionBarChart(dist, total) {
        const chart = $('emotion-bar-chart');
        if (!total) {
            chart.innerHTML = '<p class="placeholder-text">暂无识别数据</p>';
            return;
        }
        const allEmotions = ['happy', 'neutral', 'surprise', 'sad', 'angry', 'fear', 'disgust'];
        let html = '';
        allEmotions.forEach(emo => {
            const count = dist[emo] || 0;
            const pct = total ? Math.round(count / total * 100) : 0;
            html += `
                <div class="bar-row">
                    <div class="bar-label">${_emoCN[emo]}</div>
                    <div class="bar-track">
                        <div class="bar-fill" style="width:${pct}%;background:${_emoBarColors[emo]}"></div>
                    </div>
                    <div class="bar-value">${count}</div>
                </div>
            `;
        });
        chart.innerHTML = html;
    }

    function renderEmotionPieChart(dist) {
        if (typeof echarts === 'undefined') return;
        if (!_emoPieChart) {
            const el = $('emotion-pie-chart');
            if (!el) return;
            _emoPieChart = echarts.init(el);
        }
        const data = Object.entries(_emoCN)
            .filter(([k]) => dist[k] > 0)
            .map(([k, v]) => ({ name: v, value: dist[k], itemStyle: { color: _emoColors[k] } }));
        if (!data.length) data.push({ name: '暂无', value: 1, itemStyle: { color: '#333' } });

        _emoPieChart.setOption({
            tooltip: { trigger: 'item', formatter: '{b}: {c}次 ({d}%)' },
            series: [{ type: 'pie', radius: ['40%','65%'], center: ['50%','50%'],
                label: { color: '#8899aa', fontSize: 10, formatter: '{b}\n{d}%' },
                labelLine: { lineStyle: { color: '#2a3f55' } },
                data: data }]
        }, true);
        setTimeout(() => {
            if (_emoPieChart) _emoPieChart.resize();
        }, 0);
    }

    async function fetchEmotionRecords() {
        try {
            const resp = await fetch('/api/emotion-records?camera_id=camera_entrance&limit=10');
            if (!resp.ok) return;
            const data = await resp.json();
            const tbody = $('emo-records-body');
            if (!data.records || data.records.length === 0) {
                tbody.innerHTML = '<tr><td colspan="3" style="color:var(--text-secondary)">暂无数据</td></tr>';
                return;
            }
            tbody.innerHTML = data.records.map(r => {
                let cls = 'badge-neutral';
                if (POSITIVE.includes(r.emotion)) cls = 'badge-happy';
                else if (NEGATIVE.includes(r.emotion)) cls = 'badge-negative';
                else cls = 'badge-surprise';
                return `
                    <tr>
                        <td>${r.time}</td>
                        <td><span class="emotion-badge ${cls}">${_emoCN[r.emotion] || r.emotion}</span></td>
                        <td>${((r.conf || 0) * 100).toFixed(1)}%</td>
                    </tr>
                `;
            }).join('');
        } catch(e) {}
    }

    async function updateEmotionDbCount() {
        try {
            const resp = await fetch('/api/emotion-records/summary?camera_id=camera_entrance&hours=24');
            if (!resp.ok) return;
            const data = await resp.json();
            $('emo-db-count').textContent = data.total || 0;
        } catch(e) {}
    }

    // ==================== 视频帧回调 ====================
    StreamManager.onFrame((msg) => {
        if (appState.localPreviewRaf) {
            cancelAnimationFrame(appState.localPreviewRaf);
            appState.localPreviewRaf = null;
        }
        if (currentMode === 'retail') {
            if (msg.anomaly_alerts && msg.anomaly_alerts.length > 0) {
                updateAlertsPanel(msg.anomaly_alerts, msg.active_suspicious || []);
            }
            if (msg.popularity_events && msg.popularity_events.length > 0) {
                fetchPopularityReport();
            }
        }
    });

    StreamManager.onEvent((msg) => {
        if (msg.event_type === 'crowd_gathering') {
            console.log('人群聚集:', msg.detail);
        } else if (msg.event_type === 'trajectory_anomaly') {
            console.log('轨迹异常:', msg.detail);
        }
    });

    // ==================== 零售模式：自然语言查询 ====================
    function getSessionId() {
        const sid = sessionStorage.getItem('chat_session_id') ||
            'sess_' + Date.now() + '_' + Math.random().toString(36).slice(2, 8);
        sessionStorage.setItem('chat_session_id', sid);
        return sid;
    }

    function escapeHtml(value) {
        return String(value == null ? '' : value)
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;')
            .replace(/'/g, '&#39;');
    }

    // 渲染 AI 回答：先转义（防 XSS）再还原 markdown 强调（只还原我们生成的标签，安全）
    function renderAnswerHtml(text) {
        return escapeHtml(text)
            .replace(/\*\*(.*?)\*\*/g, '<strong>$1</strong>')
            .replace(/\n/g, '<br>');
    }

    async function sendQuery(question) {
        if (window.prepareVoiceReply) window.prepareVoiceReply();
        answerText.innerHTML = '<p class="placeholder-text">分析中...</p>';
        resultIntent.textContent = '';
        resultConfidence.textContent = '';
        suggestions.innerHTML = '';
        try {
            const resp = await fetch('/api/queries/stream', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ question, session_id: getSessionId() }),
            });
            if (!resp.ok) {
                const err = await resp.json();
                answerText.innerHTML = `<p style="color:var(--accent-red)">查询失败: ${escapeHtml(err.detail)}</p>`;
                return;
            }
            const reader = resp.body.getReader();
            const decoder = new TextDecoder();
            let fullText = '';
            let buffer = '';  // SSE 行缓冲：跨 chunk 被拆开的半行保留到下一个 chunk，避免丢字
            answerText.innerHTML = '<p></p>';
            while (true) {
                const { done, value } = await reader.read();
                if (done) break;
                buffer += decoder.decode(value, { stream: true });
                const lines = buffer.split('\n');
                buffer = lines.pop();  // 末段可能是不完整行，留到下一 chunk
                for (const line of lines) {
                    if (line.startsWith('data: ')) {
                        const token = line.slice(6);
                        if (token === '[DONE]') continue;
                        fullText += token;
                        answerText.innerHTML = `<p>${renderAnswerHtml(fullText)}</p>`;
                    }
                }
            }
            // 处理结尾残留（可能是不带换行的最后一行）
            if (buffer.startsWith('data: ')) {
                const token = buffer.slice(6);
                if (token !== '[DONE]') {
                    fullText += token;
                    answerText.innerHTML = `<p>${renderAnswerHtml(fullText)}</p>`;
                }
            }
            fetchDashboardData();
            if (window.playVoiceReply && fullText.trim()) {
                window.playVoiceReply(fullText);
            }
            if (window.refreshSessions) {
                window.refreshSessions();
            }
            requestAnimationFrame(() => {
                const target = document.getElementById('query-input-row') ||
                    document.getElementById('query-section');
                if (target) {
                    target.scrollIntoView({ behavior: 'smooth', block: 'end' });
                }
            });
        } catch (err) {
            answerText.innerHTML = `<p style="color:var(--accent-red)">网络错误: ${err.message}</p>`;
        }
    }

    async function fetchDashboardData() {
        try {
            const resp = await fetch('/api/reports/dashboard');
            if (resp.ok) {
                const data = await resp.json();
                if (data.popularity) {
                    // /report/dashboard 的 popularity 只含 ranking 摘要，
                    // 转成与 /report/popularity 相同的 zones 结构再喂给图表
                    const ranking = data.popularity.ranking || [];
                    const zones = {};
                    ranking.forEach(r => {
                        zones[r.zone_id] = {
                            zone_id: r.zone_id,
                            zone_label: r.label || r.zone_id,
                            visit_count: r.visit_count || 0,
                            heat_score: r.heat_score || 0,
                            total_dwell_seconds: r.total_dwell_seconds || 0,
                            staff_count: r.staff_count || 0,
                        };
                    });
                    const stats = {
                        zones,
                        top_zone: data.popularity.top_zone,
                        total_visitors: data.popularity.total_visitors || 0,
                    };
                    ChartManager.updateFromStats(stats);
                    updateRankingList(stats);
                }
            }
        } catch (e) {}
    }

    btnQuery.addEventListener('click', () => {
        const q = queryInput.value.trim();
        if (!q) { answerText.innerHTML = '<p style="color:var(--accent-yellow)">请输入查询内容</p>'; return; }
        queryInput.value = '';
        sendQuery(q);
    });

    queryInput.addEventListener('keydown', (e) => {
        if (e.key === 'Enter') {
            e.preventDefault();
            const q = queryInput.value.trim();
            if (!q) return;
            queryInput.value = '';
            sendQuery(q);
        }
    });

    $('btn-quick-overview').addEventListener('click', () => sendQuery('今天整体情况怎么样？'));
    $('btn-quick-heat').addEventListener('click', () => sendQuery('哪个货架最受欢迎？'));
    $('btn-quick-alert').addEventListener('click', () => sendQuery('有没有异常行为需要关注？'));

    // ==================== 零售模式：仪表盘数据刷新 ====================
    async function fetchPopularityReport() {
        try {
            const resp = await fetch('/api/reports/popularity');
            if (resp.ok) {
                const data = await resp.json();
                ChartManager.updateFromStats(data);
                updateRankingList(data);
                $('total-visitors').textContent = data.total_visitors || 0;
            }
        } catch (e) {}
    }

    async function fetchAnomalyReport() {
        try {
            const resp = await fetch('/api/reports/anomaly');
            if (resp.ok) {
                const data = await resp.json();
                // 无条件刷新：告警归零时也要清空面板（否则旧告警残留）
                updateAlertsFromQuery((data.high_risk || []).concat(data.watch_list || []));
            }
        } catch (e) {}
    }

    // 零售模式表情统计（内存版）
    let _retailEmoChart = null;
    async function fetchEmotionStats() {
        try {
            const resp = await fetch('/api/emotions/stats');
            if (!resp.ok) return;
            const d = await resp.json();
            $('emo-pos').textContent = d.positive_count || '0';
            $('emo-neg').textContent = d.negative_count || '0';
            $('emo-tot').textContent = d.total_faces || '0';

            if (typeof echarts === 'undefined') return;
            if (!_retailEmoChart) {
                const el = $('chart-emotion');
                if (!el) return;
                _retailEmoChart = echarts.init(el);
            }
            const dist = d.distribution || {};
            const data = Object.entries(_emoCN)
                .filter(([k]) => dist[k] > 0)
                .map(([k, v]) => ({ name: v, value: dist[k], itemStyle: { color: _emoColors[k] } }));
            if (!data.length) data.push({ name: '暂无', value: 1, itemStyle: { color: '#333' } });
            _retailEmoChart.setOption({
                tooltip: { trigger: 'item', formatter: '{b}: {c}次 ({d}%)' },
                series: [{ type: 'pie', radius: ['45%','70%'], center: ['50%','50%'],
                    label: { color: '#8899aa', fontSize: 10, formatter: '{b}\n{d}%' },
                    labelLine: { lineStyle: { color: '#2a3f55' } },
                    data: data }]
            }, true);
            setTimeout(() => {
                if (_retailEmoChart) _retailEmoChart.resize();
            }, 0);
        } catch(e) {}
    }

    window.addEventListener('resize', () => {
        if (_emoPieChart) _emoPieChart.resize();
        if (_retailEmoChart) _retailEmoChart.resize();
    });

    // 定时刷新（根据模式选择不同数据源）
    setInterval(() => {
        if (currentMode === 'retail') {
            fetchPopularityReport();
            fetchAnomalyReport();
            fetchEmotionStats();
        } else {
            fetchEmotionSummary();
            fetchEmotionRecords();
            updateEmotionDbCount();
        }
    }, 5000);

    // ===== 最新运营汇报（主动汇报 Agent 生成，轮询展示） =====
    async function fetchLatestReport() {
        try {
            const resp = await fetch('/api/reports/heat-reports?limit=1');
            if (!resp.ok) return;
            const data = await resp.json();
            const reports = data.reports || [];
            const bar = document.getElementById('heat-report-bar');
            const text = document.getElementById('heat-report-text');
            if (!bar || !text) return;
            if (!reports.length) return;
            const r = reports[0];
            const type = (r.data && typeof r.data === 'object') ? r.data.type : null;
            text.textContent = (r.report_time ? '[' + r.report_time + '] ' : '') + r.summary;
            bar.style.borderColor = type === 'surge' ? 'var(--accent-red)' : 'var(--border)';
            bar.style.display = 'block';
        } catch (e) {}
    }
    fetchLatestReport();
    setInterval(fetchLatestReport, 15000);

    // ===== 点击视频画面全屏放大 =====
    ['video-container', 'video-container-emo'].forEach(id => {
        const el = $(id);
        if (el) {
            el.style.cursor = 'pointer';
            el.addEventListener('click', () => {
                if (document.fullscreenElement) {
                    document.exitFullscreen();
                } else {
                    el.requestFullscreen();
                }
            });
        }
    });

    // ==================== 初始加载 ====================
    fetchPopularityReport();
    fetchAnomalyReport();
    fetchEmotionStats();

    // ==================== UI 更新函数 ====================

    // ===== 热度 vs 销量比对（转化诊断） =====
    const QUADRANT_CN = {
        'healthy': '🟢 健康', 'high_heat_low_sales': '🔴 高热度低销量',
        'low_heat_high_sales': '🟡 低热度高销量', 'high_heat_no_sales': '⚪ 有热度无销量',
        'low_heat_low_sales': '⚫ 双低', 'cold': '⚪ 冷区',
    };
    async function fetchHotVsSales() {
        const box = $('sales-compare-box');
        if (!box) return;
        try {
            const resp = await fetch('/api/analytics/hot-vs-sales');
            if (!resp.ok) return;
            const data = await resp.json();
            const zones = data.zones || [];
            if (!zones.length) {
                box.innerHTML = '<p class="placeholder-text">暂无比对数据 —— <button id="btn-sales-simulate" style="background:var(--accent-blue);color:#fff;border:none;border-radius:4px;padding:2px 10px;cursor:pointer">生成演示数据</button></p>';
                bindSalesSimulate();
                return;
            }
            const rows = zones.map(z => `
                <div style="display:flex;justify-content:space-between;padding:4px 6px;border-bottom:1px solid var(--border)">
                    <span style="min-width:110px">${escapeHtml(z.zone_label)}</span>
                    <span>客流 <b>${z.visit_count}</b></span>
                    <span>销量 <b>${z.sold_count}</b></span>
                    <span>转化 <b>${z.conversion_rate}%</b></span>
                    <span title="${escapeHtml(z.diagnosis)}">${QUADRANT_CN[z.quadrant] || z.quadrant}</span>
                </div>`).join('');
            box.innerHTML = `
                <div style="margin-bottom:6px;color:var(--text-secondary)">${escapeHtml(data.summary || '')}</div>
                ${rows}
                <div style="margin-top:6px"><button id="btn-sales-simulate" style="background:var(--accent-blue);color:#fff;border:none;border-radius:4px;padding:2px 10px;cursor:pointer;font-size:0.9em">重新生成演示数据</button></div>`;
            bindSalesSimulate();
        } catch (e) {}
    }
    function bindSalesSimulate() {
        const btn = document.getElementById('btn-sales-simulate');
        if (btn && !btn._bound) {
            btn._bound = true;
            btn.addEventListener('click', async () => {
                try {
                    await fetch('/api/analytics/sales-simulate', { method: 'POST' });
                    fetchHotVsSales();
                } catch (e) {}
            });
        }
    }
    fetchHotVsSales();

    function updateRankingList(data) {
        const zones = data.zones || data;
        const items = Object.values(zones).filter(z => z.heat_score !== undefined || z.visit_count !== undefined);
        items.sort((a, b) => (b.heat_score || 0) - (a.heat_score || 0));
        if (!items.length) {
            rankingList.innerHTML = '<p class="placeholder-text">等待数据...</p>';
            return;
        }
        rankingList.innerHTML = items.slice(0, 8).map((z, i) => `
            <div class="ranking-item">
                <span class="rank">#${i + 1}</span>
                <span class="label">${z.zone_label || z.zone_id}</span>
                <span class="count">热度 ${(z.heat_score || 0).toFixed(0)}</span>
                <span class="dwell">${z.visit_count || 0}次/${(z.total_dwell_seconds || 0).toFixed(0)}s</span>
                ${z.staff_count > 0 ? `<span style="color:var(--accent-yellow);font-size:0.75em">店员${z.staff_count}</span>` : ''}
            </div>
        `).join('');
    }

    function updateAlertsPanel(newAlerts, suspiciousTracks) {
        // 实时告警明细：状态 + 列表 + 可疑轨迹（之前 suspiciousTracks 参数被丢弃、列表从未渲染）
        if (newAlerts.length === 0 && (!suspiciousTracks || !suspiciousTracks.length)) {
            alertStatus.className = 'alert-status safe';
            alertStatus.innerHTML = '🟢 无异常告警';
            alertList.innerHTML = '<p class="placeholder-text">监控正常运行中</p>';
            return;
        }
        const hasHigh = newAlerts.some(a => a.level === 'high');
        alertStatus.className = 'alert-status ' + (hasHigh ? 'danger' : 'warning');
        alertStatus.innerHTML = hasHigh
            ? `${newAlerts.length} 起高风险告警`
            : `${newAlerts.length} 起需关注告警`;
        const items = newAlerts.slice(0, 10).map(a => `
            <div class="alert-item ${a.level}">
                <div class="alert-title">
                    ${a.level === 'high' ? '🔴' : '🟡'}
                    人员 #${a.person_id}
                    <span style="float:right">评分: ${a.score}/100</span>
                </div>
                <div class="alert-detail">${(a.reasons || []).join('；')}</div>
            </div>
        `);
        const sus = (suspiciousTracks || []).slice(0, 5).map(t => `
            <div class="alert-item watch">
                <div class="alert-title">⚠️ 轨迹 #${t.track_id || t.person_id || '?'}
                    <span style="float:right">评分: ${t.score || 0}/100</span>
                </div>
            </div>
        `);
        alertList.innerHTML = items.concat(sus).join('') || '<p class="placeholder-text">暂无明细</p>';
    }

    function updateAlertsFromQuery(alerts) {
        if (!alerts.length) {
            alertStatus.className = 'alert-status safe';
            alertStatus.innerHTML = '🟢 无异常告警';
            alertList.innerHTML = '<p class="placeholder-text">监控正常运行中</p>';
            return;
        }
        const hasHigh = alerts.some(a => a.level === 'high');
        alertStatus.className = 'alert-status ' + (hasHigh ? 'danger' : 'warning');
        alertStatus.innerHTML = hasHigh
            ? `${alerts.length} 起告警（含高风险）`
            : `${alerts.length} 起关注告警`;
        alertList.innerHTML = alerts.slice(0, 10).map(a => `
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
        `).join('');
    }

    console.log('智能零售分析系统 — 前端就绪');
    console.log('  模式1: 零售视频分析（货架摄像头）— 热度/异常/表情/NL查询');
    console.log('  模式2: 门店人脸表情分析（出入口摄像头）— 人脸检测/表情识别/SQLite入库');
})();
