/**
 * 本地语音唤醒 + 指令识别
 * 麦克风 PCM -> /api/voice/ws -> sherpa-onnx KWS 检测“小零”
 * 唤醒后回复“我在”，并用本地 ASR /api/asr/ws 识别 5 秒内的指令。
 */
(function () {
    'use strict';

    const btnVoice = document.getElementById('btn-voice');
    const btnTranscribeTest = document.getElementById('btn-transcribe-test');
    const btnVoiceReply = document.getElementById('btn-voice-reply');

    // ⚠ 防御性守卫（D1）：本脚本在**顶层**就要拿 DOM 并绑定事件。
    // 若在仪表盘挂载之前执行，这些元素还不存在，后面 `btnVoice.addEventListener`
    // 会抛 TypeError 并**中断整个 IIFE** —— 语音功能整体失效，且只留一条易忽略的报错。
    // Root.vue 已改为"先挂载仪表盘、DOM 就绪后再加载本脚本"，
    // 这里再加一道守卫：即使被提前加载（例如缓存/顺序变更），也只提示而不静默崩溃。
    if (!btnVoice) {
        console.warn('[voice] 未找到 #btn-voice：本脚本需在仪表盘挂载后加载（见 Root.vue）。语音功能已跳过。');
        return;
    }

    const voiceBar = document.getElementById('voice-bar');
    const voiceStatus = document.getElementById('voice-status');
    const voiceHeard = document.getElementById('voice-heard');
    const voiceResult = document.getElementById('voice-result');
    const answerText = document.getElementById('answer-text');
    const resultIntent = document.getElementById('result-intent');
    const suggestions = document.getElementById('suggestions');

    const COMMAND_TIMEOUT_MS = 5000;
    const WAKE_COOLDOWN_MS = 3000;
    const WAKE_REPLY = '我在';
    const TARGET_SAMPLE_RATE = 16000;

    const sessionId = sessionStorage.getItem('chat_session_id') ||
        'voice_' + Date.now() + '_' + Math.random().toString(36).slice(2, 8);
    sessionStorage.setItem('chat_session_id', sessionId);

    const COMMAND_ALIASES = {
        '打开账单': '打开摄像头',
        '打开帐单': '打开摄像头',
        '打开照相': '打开摄像头',
        '打开张丹': '打开摄像头',
        '打开帐号': '打开摄像头',
        '打开': '打开摄像头',
    };

    let kwsWs = null;
    let kwsReconnectTimer = null;
    let audioContext = null;
    let mediaStream = null;
    let sourceNode = null;
    let processorNode = null;
    let commandAsrWs = null;
    let commandTimer = null;
    let currentAudio = null;
    let currentTtsSource = null;
    let ttsContext = null;
    let state = 'idle'; // idle | command | processing
    let isListening = false;
    let isStarting = false;  // 启动锁：防连点按钮/自动重连并发导致双开麦克风 + 双 WS
    let isTranscribing = false;
    let lastWakeAt = 0;
    let voiceReplyEnabled = true;
    let manualStop = false;
    let inputSampleCount = 0;
    let nextOutputIndex = 0;
    let asrWs = null;
    let asrContext = null;
    let asrStream = null;
    let asrSourceNode = null;
    let asrProcessorNode = null;

    function setVoiceStatus(text, isListeningClass) {
        voiceStatus.textContent = text;
        if (voiceBar) {
            voiceBar.classList.toggle('listening', Boolean(isListeningClass));
        }
    }

    function stopSpeaking() {
        if (currentTtsSource) {
            try { currentTtsSource.stop(); } catch (e) {}
            currentTtsSource = null;
        }
        if (currentAudio) {
            try { currentAudio.pause(); } catch (e) {}
            currentAudio = null;
        }
        if (window.speechSynthesis) {
            window.speechSynthesis.cancel();
        }
    }

    function ensureTtsContext() {
        if (!ttsContext) {
            const AudioContextClass = window.AudioContext || window.webkitAudioContext;
            if (!AudioContextClass) return null;
            ttsContext = new AudioContextClass();
        }
        if (ttsContext.state === 'suspended') {
            ttsContext.resume().catch(() => {});
        }
        return ttsContext;
    }

    async function speak(text) {
        if (!voiceReplyEnabled || !text) return;
        stopSpeaking();
        const cleanText = text.replace(/[*_#`>]+/g, '').trim();
        if (!cleanText) return;
        const ttsUrl = '/api/tts';
        const ttsQueryUrl = '/api/tts?text=' + encodeURIComponent(cleanText);
        const ctx = ensureTtsContext();

        try {
            const resp = await fetch(ttsUrl, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ text: cleanText }),
            });
            if (!resp.ok) throw new Error('TTS status ' + resp.status);
            const arrayBuffer = await resp.arrayBuffer();
            if (ctx) {
                const audioBuffer = await ctx.decodeAudioData(arrayBuffer);
                const source = ctx.createBufferSource();
                source.buffer = audioBuffer;
                source.connect(ctx.destination);
                currentTtsSource = source;
                source.onended = () => {
                    if (currentTtsSource === source) currentTtsSource = null;
                };
                if (ctx.state === 'suspended') {
                    await ctx.resume().catch(() => {});
                }
                source.start(0);
                return;
            }
        } catch (err) {
            console.warn('[TTS] AudioContext 播放失败，尝试浏览器语音合成:', err);
        }

        if (window.speechSynthesis) {
            const utterance = new SpeechSynthesisUtterance(cleanText);
            utterance.lang = 'zh-CN';
            utterance.rate = 1.0;
            window.speechSynthesis.speak(utterance);
            return;
        }

        const audio = new Audio(ttsQueryUrl);
        currentAudio = audio;
        audio.volume = 1.0;
        audio.play().catch(() => {
            if (currentAudio === audio) currentAudio = null;
        });
    }

    function escapeHtml(value) {
        return String(value || '')
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;')
            .replace(/'/g, '&#39;');
    }

    function renderAnswer(text, intent) {
        voiceResult.textContent = text || '';
        if (answerText) {
            answerText.innerHTML = `<p>${escapeHtml(text)}</p>`;
        }
        if (resultIntent) {
            resultIntent.textContent = intent || '';
        }
        if (suggestions) suggestions.innerHTML = '';
    }

    function executeVoiceCommand(data) {
        const btnWebcam = document.getElementById('btn-webcam');
        const btnLocalCam = document.getElementById('btn-local-cam');
        const btnStop = document.getElementById('btn-stop');

        if (data.command === 'start') {
            if (data.source === 'local' && btnLocalCam) {
                btnLocalCam.click();
            } else if (btnWebcam) {
                btnWebcam.click();
            }
            return;
        }
        if (data.command === 'stop') {
            if (btnStop) btnStop.click();
            return;
        }
        if (data.command === 'pause' && StreamManager) {
            StreamManager.pause();
            return;
        }
        if (data.command === 'resume' && StreamManager) {
            StreamManager.resume();
            return;
        }
        if (data.command === 'mode' && typeof window.switchMode === 'function') {
            window.switchMode(data.mode);
        }
    }

    function normalizeCommand(text) {
        const value = (text || '').trim();
        if (COMMAND_ALIASES[value]) return COMMAND_ALIASES[value];
        const matched = Object.keys(COMMAND_ALIASES).find(key => value.includes(key));
        return matched ? COMMAND_ALIASES[matched] : value;
    }

    async function sendVoiceCommand(text) {
        setVoiceStatus('已识别，正在处理...');
        try {
            const resp = await fetch('/api/voice/commands', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ text, session_id: sessionId }),
            });
            if (!resp.ok) {
                const err = await resp.json().catch(() => ({}));
                throw new Error(err.detail || '语音指令处理失败');
            }
            const data = await resp.json();

            if (data.action === 'command') {
                executeVoiceCommand(data);
                renderAnswer(data.text || '指令已执行。');
                speak(data.text || '指令已执行。');
            } else {
                renderAnswer(data.text || '已完成分析。', data.intent);
                speak(data.text || '已完成分析。');
            }
        } catch (err) {
            renderAnswer('语音处理失败：' + err.message);
        } finally {
            state = 'idle';
            clearTimeout(commandTimer);
            if (isListening) {
                setVoiceStatus('等待下一次唤醒词：请说“小零”');
            }
        }
    }

    function processCommand(text) {
        if (state !== 'command') return;
        const command = normalizeCommand(text);
        if (!command) return;
        stopCommandRecognition();
        state = 'processing';
        clearTimeout(commandTimer);
        sendVoiceCommand(command);
    }

    function startCommandRecognition() {
        if (commandAsrWs) return;
        const protocol = location.protocol === 'https:' ? 'wss:' : 'ws:';
        commandAsrWs = new WebSocket(`${protocol}//${location.host}/api/asr/ws`);
        commandAsrWs.binaryType = 'arraybuffer';

        commandAsrWs.onmessage = (event) => {
            const msg = JSON.parse(event.data);
            if (msg.event === 'ready') {
                setVoiceStatus('已唤醒，请在5秒内说出指令...', true);
            } else if (msg.event === 'debug') {
                console.debug('[Voice Command ASR]', msg);
            } else if (msg.event === 'interim') {
                voiceHeard.textContent = '识别中：';
                voiceResult.textContent = msg.text;
            } else if (msg.event === 'final' && msg.text && msg.text.trim()) {
                processCommand(msg.text.trim());
            }
        };

        commandAsrWs.onerror = (event) => {
            console.error('[Voice] 本地指令识别错误:', event);
        };

        commandAsrWs.onclose = () => {
            commandAsrWs = null;
            if (state === 'command' && !manualStop) {
                setVoiceStatus('本地指令识别连接断开，正在重连...', true);
                setTimeout(() => {
                    if (state === 'command' && !manualStop) {
                        startCommandRecognition();
                    }
                }, 300);
            }
        };

        voiceHeard.textContent = '';
        voiceResult.textContent = '';
        setVoiceStatus('正在启动本地指令识别...', true);
    }

    function stopCommandRecognition() {
        if (commandAsrWs) {
            try {
                commandAsrWs.close();
            } catch (e) {}
            commandAsrWs = null;
        }
    }

    function resetCommandTimer() {
        clearTimeout(commandTimer);
        commandTimer = setTimeout(() => {
            if (state !== 'command') return;
            stopCommandRecognition();
            state = 'idle';
            voiceHeard.textContent = '';
            setVoiceStatus('5秒内未收到指令，已回到等待唤醒词状态');
        }, COMMAND_TIMEOUT_MS);
    }

    function enterCommandMode() {
        if (state !== 'idle') return;
        state = 'command';
        speak(WAKE_REPLY);
        setVoiceStatus('已唤醒，请在5秒内说出指令...', true);
        resetCommandTimer();
        startCommandRecognition();
    }

    function resampleTo16k(input, context) {
        const sourceContext = context || audioContext;
        if (!sourceContext) return new Float32Array(0);
        const sampleRate = sourceContext.sampleRate || TARGET_SAMPLE_RATE;
        if (sampleRate === TARGET_SAMPLE_RATE) {
            inputSampleCount += input.length;
            nextOutputIndex += input.length;
            return new Float32Array(input);
        }
        const ratio = sampleRate / TARGET_SAMPLE_RATE;
        const startInput = inputSampleCount;
        const endInput = startInput + input.length;
        const firstOut = Math.max(nextOutputIndex, Math.ceil(startInput / ratio));
        const lastOut = Math.floor((endInput - 1) / ratio);
        const outLen = Math.max(0, lastOut - firstOut + 1);
        const output = new Float32Array(outLen);
        for (let i = 0; i < outLen; i++) {
            const sampleIndex = firstOut + i;
            const pos = sampleIndex * ratio - startInput;
            const i0 = Math.min(Math.max(0, Math.floor(pos)), input.length - 1);
            const i1 = Math.min(i0 + 1, input.length - 1);
            const frac = pos - i0;
            output[i] = input[i0] * (1 - frac) + input[i1] * frac;
        }
        inputSampleCount = endInput;
        nextOutputIndex = Math.max(nextOutputIndex, firstOut + outLen);
        return output;
    }

    function cleanupAudio() {
        if (processorNode) {
            try { processorNode.disconnect(); } catch (e) {}
            processorNode = null;
        }
        if (sourceNode) {
            try { sourceNode.disconnect(); } catch (e) {}
            sourceNode = null;
        }
        if (mediaStream) {
            mediaStream.getTracks().forEach(t => t.stop());
            mediaStream = null;
        }
        if (audioContext) {
            try { audioContext.close(); } catch (e) {}
            audioContext = null;
        }
        if (ttsContext) {
            try { ttsContext.close(); } catch (e) {}
            ttsContext = null;
        }
        if (kwsWs) {
            try { kwsWs.close(); } catch (e) {}
            kwsWs = null;
        }
    }

    function stopListening() {
        manualStop = true;
        state = 'idle';
        clearTimeout(kwsReconnectTimer);
        clearTimeout(commandTimer);
        stopCommandRecognition();
        stopSpeaking();
        cleanupAudio();
        isListening = false;
        btnVoice.textContent = '🎤 语音输入';
        if (voiceBar) voiceBar.classList.remove('listening');
        setVoiceStatus('本地唤醒已停止');
        manualStop = false;
    }

    function cleanupAsr() {
        if (asrProcessorNode) {
            try { asrProcessorNode.disconnect(); } catch (e) {}
            asrProcessorNode = null;
        }
        if (asrSourceNode) {
            try { asrSourceNode.disconnect(); } catch (e) {}
            asrSourceNode = null;
        }
        if (asrStream) {
            asrStream.getTracks().forEach(track => track.stop());
            asrStream = null;
        }
        if (asrContext) {
            try { asrContext.close(); } catch (e) {}
            asrContext = null;
        }
        if (asrWs) {
            try { asrWs.close(); } catch (e) {}
            asrWs = null;
        }
    }

    function stopTranscribeTest() {
        isTranscribing = false;
        cleanupAsr();
        if (btnTranscribeTest) {
            btnTranscribeTest.textContent = '转文字测试';
        }
        setVoiceStatus('转文字测试已停止');
    }

    async function startTranscribeTest() {
        if (isTranscribing) {
            stopTranscribeTest();
            return;
        }
        if (isListening) {
            stopListening();
        }
        if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
            setVoiceStatus('当前浏览器不支持麦克风采集，请使用 Chrome 或 Edge。');
            return;
        }
        if (!window.AudioContext && !window.webkitAudioContext) {
            setVoiceStatus('当前浏览器不支持 AudioContext。');
            return;
        }

        voiceHeard.textContent = '';
        voiceResult.textContent = '';
        if (answerText) {
            answerText.innerHTML = '<p>正在连接本地转文字...</p>';
        }

        try {
            asrStream = await navigator.mediaDevices.getUserMedia({
                audio: {
                    echoCancellation: true,
                    noiseSuppression: true,
                    channelCount: 1,
                },
            });

            const AudioContextClass = window.AudioContext || window.webkitAudioContext;
            asrContext = new AudioContextClass();
            if (asrContext.state === 'suspended') {
                await asrContext.resume();
            }
            asrSourceNode = asrContext.createMediaStreamSource(asrStream);
            asrProcessorNode = asrContext.createScriptProcessor(4096, 1, 1);
            asrProcessorNode.onaudioprocess = (event) => {
                if (!isTranscribing || !asrWs || asrWs.readyState !== WebSocket.OPEN) return;
                const input = event.inputBuffer.getChannelData(0);
                const pcm = resampleTo16k(input, asrContext);
                if (pcm.length > 0) {
                    asrWs.send(pcm.buffer);
                }
            };
            asrSourceNode.connect(asrProcessorNode);
            asrProcessorNode.connect(asrContext.destination);

            const protocol = location.protocol === 'https:' ? 'wss:' : 'ws:';
            asrWs = new WebSocket(`${protocol}//${location.host}/api/asr/ws`);
            asrWs.binaryType = 'arraybuffer';

            asrWs.onopen = () => {
                if (isTranscribing) {
                    setVoiceStatus('本地转文字已连接，请说话...', true);
                }
            };

            asrWs.onmessage = (event) => {
                const msg = JSON.parse(event.data);
                if (msg.event === 'ready') {
                    setVoiceStatus('本地转文字已启动，请说话...', true);
                } else if (msg.event === 'debug') {
                    console.debug('[ASR]', msg);
                    if (!voiceHeard.textContent) {
                        voiceResult.textContent = `麦克风电平 ${msg.rms.toFixed(3)}，增益 ${msg.gain.toFixed(1)}`;
                    }
                } else if (msg.event === 'interim') {
                    voiceHeard.textContent = '识别中：';
                    voiceResult.textContent = msg.text;
                    if (answerText) {
                        answerText.innerHTML = `<p>${escapeHtml(msg.text)}</p>`;
                    }
                    if (resultIntent) {
                        resultIntent.textContent = '本地转文字';
                    }
                    if (suggestions) suggestions.innerHTML = '';
                } else if (msg.event === 'final') {
                    renderAnswer(msg.text || '未识别到内容', '本地转文字');
                    voiceHeard.textContent = '识别结果：';
                }
            };

            asrWs.onclose = () => {
                if (isTranscribing) {
                    isTranscribing = false;
                    cleanupAsr();
                    if (btnTranscribeTest) {
                        btnTranscribeTest.textContent = '转文字测试';
                    }
                    setVoiceStatus('本地转文字连接已断开');
                }
            };

            isTranscribing = true;
            inputSampleCount = 0;
            nextOutputIndex = 0;
            btnTranscribeTest.textContent = '停止转文字';
            setVoiceStatus('本地转文字测试启动中...', true);
        } catch (err) {
            isTranscribing = false;
            cleanupAsr();
            if (btnTranscribeTest) {
                btnTranscribeTest.textContent = '转文字测试';
            }
            setVoiceStatus('麦克风启动失败：' + err.message);
            renderAnswer('麦克风启动失败：' + err.message, '转文字测试');
        }
    }

    async function startListening() {
        clearTimeout(kwsReconnectTimer);
        // 启动锁：isListening 或 isStarting 时直接返回，
        // 防止连点按钮/断线自动重连并发启动两个 getUserMedia 流和两个 WS 连接
        if (isListening || isStarting) return;
        isStarting = true;
        if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
            setVoiceStatus('当前浏览器不支持麦克风采集，请使用 Chrome 或 Edge。');
            isStarting = false;
            return;
        }
        if (isTranscribing) {
            stopTranscribeTest();
        }
        if (!window.AudioContext && !window.webkitAudioContext) {
            setVoiceStatus('当前浏览器不支持 AudioContext。');
            isStarting = false;
            return;
        }

        try {
            mediaStream = await navigator.mediaDevices.getUserMedia({
                audio: {
                    echoCancellation: true,
                    noiseSuppression: true,
                    channelCount: 1,
                },
            });

            const AudioContextClass = window.AudioContext || window.webkitAudioContext;
            audioContext = new AudioContextClass();
            if (audioContext.state === 'suspended') {
                await audioContext.resume();
            }
            ensureTtsContext();
            sourceNode = audioContext.createMediaStreamSource(mediaStream);
            processorNode = audioContext.createScriptProcessor(4096, 1, 1);
            processorNode.onaudioprocess = (event) => {
                if (!isListening) return;
                const input = event.inputBuffer.getChannelData(0);
                const pcm = resampleTo16k(input, audioContext);
                if (pcm.length > 0) {
                    if (state === 'command' && commandAsrWs && commandAsrWs.readyState === WebSocket.OPEN) {
                        commandAsrWs.send(pcm.buffer);
                    } else if (kwsWs && kwsWs.readyState === WebSocket.OPEN) {
                        kwsWs.send(pcm.buffer);
                    }
                }
            };
            sourceNode.connect(processorNode);
            processorNode.connect(audioContext.destination);

            const protocol = location.protocol === 'https:' ? 'wss:' : 'ws:';
            kwsWs = new WebSocket(`${protocol}//${location.host}/api/voice/ws`);
            kwsWs.binaryType = 'arraybuffer';

            kwsWs.onopen = () => {
                setVoiceStatus('本地唤醒已启动，等待“小零”...', true);
            };

            kwsWs.onmessage = (event) => {
                const msg = JSON.parse(event.data);
                if (msg.event === 'ready') {
                    setVoiceStatus('本地唤醒已启动，等待“小零”...', true);
                } else if (msg.event === 'debug') {
                    console.debug('[KWS]', msg);
                } else if (msg.event === 'wake') {
                    const now = Date.now();
                    if (now - lastWakeAt < WAKE_COOLDOWN_MS) {
                        return;
                    }
                    lastWakeAt = now;
                    enterCommandMode();
                }
            };

            kwsWs.onclose = () => {
                if (isListening && !manualStop) {
                    state = 'idle';
                    cleanupAudio();
                    isListening = false;
                    btnVoice.textContent = '🎤 语音输入';
                    setVoiceStatus('本地唤醒连接断开，正在自动重连...');
                    kwsReconnectTimer = setTimeout(() => {
                        // 重连前再确认：手动停止或已在启动/运行则不重复拉起
                        if (!manualStop && !isListening && !isStarting) startListening();
                    }, 1500);
                }
            };

            isListening = true;
            manualStop = false;
            lastWakeAt = 0;
            inputSampleCount = 0;
            nextOutputIndex = 0;
            btnVoice.textContent = '■ 停止语音';
            setVoiceStatus('正在启动本地唤醒...', true);
        } catch (err) {
            cleanupAudio();
            setVoiceStatus('麦克风启动失败：' + err.message);
        } finally {
            isStarting = false;
        }
    }

    btnVoice.addEventListener('click', () => {
        if (isTranscribing) {
            stopTranscribeTest();
        }
        if (isListening) {
            stopListening();
        } else {
            startListening();
        }
    });

    if (btnTranscribeTest) {
        btnTranscribeTest.addEventListener('click', startTranscribeTest);
    }

    try {
        const savedVoiceReply = localStorage.getItem('voice_reply_enabled');
        if (savedVoiceReply !== null) {
            voiceReplyEnabled = savedVoiceReply === '1';
        }
    } catch (e) {}

    function updateVoiceReplyButton() {
        if (btnVoiceReply) {
            btnVoiceReply.textContent = voiceReplyEnabled ? '语音回复: 开' : '语音回复: 关';
        }
    }

    if (btnVoiceReply) {
        updateVoiceReplyButton();
        btnVoiceReply.addEventListener('click', () => {
            voiceReplyEnabled = !voiceReplyEnabled;
            try {
                localStorage.setItem('voice_reply_enabled', voiceReplyEnabled ? '1' : '0');
            } catch (e) {}
            updateVoiceReplyButton();
            if (!voiceReplyEnabled) {
                stopSpeaking();
            }
        });
    }

    window.prepareVoiceReply = function () {
        if (voiceReplyEnabled) {
            ensureTtsContext();
        }
    };

    window.playVoiceReply = function (text) {
        speak(text);
    };

    setVoiceStatus('点击顶部“语音输入”后开始本地唤醒，检测到“小零”会回复“我在”');
})();
