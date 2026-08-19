/**
 * 会话记录管理：新建会话、保存会话、切换会话
 */
(function () {
    'use strict';

    const btnNew = document.getElementById('btn-new-session');
    const btnSave = document.getElementById('btn-save-session');
    const btnOpen = document.getElementById('btn-open-sessions');
    const btnToggle = document.getElementById('btn-toggle-sessions');
    const drawer = document.getElementById('session-drawer');
    const searchInput = document.getElementById('session-search');
    const btnSearch = document.getElementById('btn-search-session');
    const sessionList = document.getElementById('session-list');
    const sessionCurrent = document.getElementById('session-current');
    const answerText = document.getElementById('answer-text');
    const resultIntent = document.getElementById('result-intent');
    const suggestions = document.getElementById('suggestions');

    function openDrawer() {
        if (drawer) drawer.classList.add('open');
        if (btnOpen) btnOpen.style.display = 'none';
    }

    function closeDrawer() {
        if (drawer) drawer.classList.remove('open');
        if (btnOpen) btnOpen.style.display = '';
    }

    function toggleDrawer() {
        if (drawer && drawer.classList.contains('open')) {
            closeDrawer();
        } else {
            openDrawer();
        }
    }

    // 点击抽屉外部区域时自动收回抽屉
    document.addEventListener('click', (e) => {
        if (!drawer || !drawer.classList.contains('open')) return;
        // 点击抽屉内部或"会话记录"按钮（用于打开）时不关闭
        if (drawer.contains(e.target)) return;
        if (btnOpen && btnOpen.contains(e.target)) return;
        closeDrawer();
    });

    function currentSessionId() {
        return sessionStorage.getItem('chat_session_id') ||
            'sess_' + Date.now() + '_' + Math.random().toString(36).slice(2, 8);
    }

    function escapeHtml(value) {
        return String(value || '')
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;')
            .replace(/'/g, '&#39;');
    }

    function renderMessages(messages) {
        if (!answerText) return;
        if (!messages || !messages.length) {
            answerText.innerHTML = '<p class="placeholder-text">当前会话暂无记录</p>';
            if (resultIntent) resultIntent.textContent = '';
            if (suggestions) suggestions.innerHTML = '';
            scrollToLatestMessage();
            return;
        }
        answerText.innerHTML = messages.map(msg => `
            <div class="chat-bubble-row question-row">
                <div class="chat-bubble question">${escapeHtml(msg.question)}
                    <div class="chat-time">${escapeHtml(msg.created_at || '')}</div>
                </div>
            </div>
            <div class="chat-bubble-row answer-row">
                <div class="chat-bubble answer">${escapeHtml(msg.answer || '无回答')}
                    <div class="chat-time">${escapeHtml(msg.created_at || '')}</div>
                </div>
            </div>
        `).join('');
        if (resultIntent) resultIntent.textContent = '会话记录';
        if (suggestions) suggestions.innerHTML = '';
        scrollToLatestMessage();
    }

    function scrollToLatestMessage() {
        requestAnimationFrame(() => {
            const target = document.getElementById('query-input-row') ||
                document.getElementById('query-section');
            if (target) {
                target.scrollIntoView({ behavior: 'smooth', block: 'end' });
            }
        });
    }

    async function loadSessions() {
        if (!sessionList) return;
        const current = currentSessionId();
        try {
            const resp = await fetch('/api/chat/sessions');
            if (!resp.ok) throw new Error('load sessions failed');
            const data = await resp.json();
            const sessions = data.sessions || [];
            sessionList.innerHTML = sessions.length ? '' : '<div class="session-item">暂无会话</div>';
            sessions.forEach(item => {
                const div = document.createElement('div');
                div.className = 'session-item' + (item.session_id === current ? ' active' : '');
                div.dataset.sessionId = item.session_id;
                div.innerHTML = `
                    <span class="session-item-title">${escapeHtml(item.title || item.session_id)}</span>
                    <span class="session-item-meta">${item.message_count || 0}条</span>
                    <button type="button" class="session-rename-btn" title="重命名会话">重命名</button>
                    <button type="button" class="session-delete-btn" title="删除会话">删除</button>
                `;
                sessionList.appendChild(div);
            });
            if (sessionCurrent) {
                const active = sessions.find(item => item.session_id === current);
                sessionCurrent.textContent = '当前：' + (active ? (active.title || current) : current);
            }
        } catch (e) {
            sessionList.innerHTML = '<div class="session-item">会话列表加载失败</div>';
        }
    }

    async function searchSessions() {
        const keyword = (searchInput ? searchInput.value : '').trim();
        if (!keyword) {
            await loadSessions();
            return;
        }
        if (!sessionList) return;
        try {
            const resp = await fetch('/api/chat/search?q=' + encodeURIComponent(keyword));
            if (!resp.ok) throw new Error('search failed');
            const data = await resp.json();
            const results = data.results || [];
            sessionList.innerHTML = results.length ? '' : '<div class="session-item">未找到相关会话内容</div>';
            results.forEach(item => {
                const div = document.createElement('div');
                div.className = 'session-item';
                div.dataset.sessionId = item.session_id;
                div.innerHTML = `
                    <span class="session-item-title">${escapeHtml(item.title || item.session_id)}</span>
                    <span class="session-item-meta">#${item.seq_no || 1}</span>
                `;
                div.title = item.question || '';
                sessionList.appendChild(div);
            });
        } catch (e) {
            sessionList.innerHTML = '<div class="session-item">搜索失败</div>';
        }
    }

    async function switchSession(sessionId) {
        sessionStorage.setItem('chat_session_id', sessionId);
        closeDrawer();
        try {
            const resp = await fetch(`/api/chat/sessions/${encodeURIComponent(sessionId)}/messages`);
            if (!resp.ok) throw new Error('load messages failed');
            const data = await resp.json();
            renderMessages(data.messages || []);
        } catch (e) {
            renderMessages([]);
        }
        await loadSessions();
    }

    async function createSession() {
        try {
            const resp = await fetch('/api/chat/sessions', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ title: '新会话' }),
            });
            if (!resp.ok) throw new Error('create failed');
            const data = await resp.json();
            sessionStorage.setItem('chat_session_id', data.session_id);
            renderMessages([]);
            await loadSessions();
        } catch (e) {
            if (sessionCurrent) sessionCurrent.textContent = '新建会话失败';
        }
    }

    async function saveSession() {
        const sessionId = currentSessionId();
        try {
            const resp = await fetch(`/api/chat/sessions/${encodeURIComponent(sessionId)}/save`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({}),
            });
            if (!resp.ok) throw new Error('save failed');
            if (sessionCurrent) sessionCurrent.textContent = '已保存';
            await loadSessions();
        } catch (e) {
            if (sessionCurrent) sessionCurrent.textContent = '保存失败';
        }
    }

    async function renameSession(sessionId) {
        const active = document.querySelector(`.session-item[data-session-id="${CSS.escape(sessionId)}"] .session-item-title`);
        const oldTitle = active ? active.textContent : '';
        const title = prompt('重命名会话', oldTitle || '新会话');
        if (!title || !title.trim()) return;
        try {
            const resp = await fetch(`/api/chat/sessions/${encodeURIComponent(sessionId)}/rename`, {
                method: 'PUT',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ title: title.trim() }),
            });
            if (!resp.ok) throw new Error('rename failed');
            await loadSessions();
        } catch (e) {
            if (sessionCurrent) sessionCurrent.textContent = '重命名失败';
        }
    }

    async function deleteSession(sessionId) {
        if (!confirm('确定删除该会话及其全部记录？')) return;
        try {
            const resp = await fetch(`/api/chat/sessions/${encodeURIComponent(sessionId)}`, {
                method: 'DELETE',
            });
            if (!resp.ok) throw new Error('delete failed');
            if (currentSessionId() === sessionId) {
                await createSession();
            } else {
                await loadSessions();
            }
        } catch (e) {
            if (sessionCurrent) sessionCurrent.textContent = '删除失败';
        }
    }

    if (btnNew) btnNew.addEventListener('click', createSession);
    if (btnSave) btnSave.addEventListener('click', saveSession);
    if (btnOpen) btnOpen.addEventListener('click', openDrawer);
    if (btnToggle) btnToggle.addEventListener('click', closeDrawer);
    if (btnSearch) btnSearch.addEventListener('click', searchSessions);
    if (searchInput) {
        searchInput.addEventListener('keydown', (e) => {
            if (e.key === 'Enter') {
                e.preventDefault();
                searchSessions();
            }
        });
    }
    if (sessionList) {
        sessionList.addEventListener('click', (event) => {
            const renameBtn = event.target.closest('.session-rename-btn');
            const deleteBtn = event.target.closest('.session-delete-btn');
            const item = event.target.closest('.session-item');
            if (!item) return;
            if (renameBtn) {
                renameSession(item.dataset.sessionId);
                return;
            }
            if (deleteBtn) {
                deleteSession(item.dataset.sessionId);
                return;
            }
            switchSession(item.dataset.sessionId);
        });
    }

    window.refreshSessions = loadSessions;
    window.switchSession = switchSession;

    loadSessions();
})();
