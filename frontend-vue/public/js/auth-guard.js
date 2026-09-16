/**
 * 登录态兜底（D3）：会话过期后统一「停止重连 + 回登录页」
 *
 * 背景（实测确认的机制）：
 *   - 服务端会话默认 12 小时（`AUTH_SESSION_HOURS`），过期后**没有续期**；
 *   - HTTP 受保护接口一律返 401；
 *   - WebSocket 被鉴权中间件「先 accept 再以 **1008**（策略违规）关闭」（`api/security.py`）。
 * 而前端此前只在**启动时**查一次 `/api/auth/me`，此后：
 *   - 401 被各处静默吞掉 → 面板停在旧数字，看起来像"网络卡住了"；
 *   - `stream.js` / `App.vue` 的 `onclose` **完全不看关闭码**，一律指数退避重连
 *     → 1008（鉴权失效）也会**无限重连**，永不回登录页。
 *
 * 本脚本在 `<head>` 中以普通 `<script src>` 加载（早于 `main.js`，且符合
 * CSP `script-src 'self'`），做两件事：
 *   1. 包装 `window.fetch`：任何 `/api/` 请求返 401、且当前**已登录**时 → 触发 `expired()`；
 *   2. 暴露 `window.DSH_AUTH.shouldReconnectOnClose(code)`：1008 → 不再重连。
 *
 * ⚠ 只在**已登录**（`window.__currentUser` 有值）时才判定为"过期"，
 * 否则登录页上输错密码的 401 会把用户直接刷成死循环。
 */
(function () {
    'use strict';

    var RELOAD_DELAY_MS = 1500;   // 留一点时间让用户看清提示
    var handled = false;

    function isLoggedIn() {
        try { return !!window.__currentUser; } catch (e) { return false; }
    }

    function isAuthEndpoint(url) {
        // 登录/登出接口的 401 属于正常业务结果（口令错误），不能当成"过期"
        return /\/api\/auth\/(login|logout)\b/.test(String(url || ''));
    }

    function showOverlay(reason) {
        try {
            if (document.getElementById('dsh-auth-expired')) return;
            var box = document.createElement('div');
            box.id = 'dsh-auth-expired';
            box.setAttribute('style', [
                'position:fixed', 'inset:0', 'z-index:99999',
                'background:rgba(8,14,22,.88)', 'color:#e6edf3',
                'display:flex', 'flex-direction:column', 'align-items:center',
                'justify-content:center', 'gap:10px',
                'font:16px/1.6 system-ui,-apple-system,"Microsoft YaHei",sans-serif'
            ].join(';'));
            var title = document.createElement('div');
            title.textContent = '登录已过期，请重新登录';
            title.setAttribute('style', 'font-size:20px;font-weight:600');
            var sub = document.createElement('div');
            sub.textContent = '正在返回登录页…（' + (reason || '') + '）';
            sub.setAttribute('style', 'color:#8b9bb0;font-size:13px');
            box.appendChild(title);
            box.appendChild(sub);
            document.body.appendChild(box);
        } catch (e) { /* 界面失败也不能拦着回登录页 */ }
    }

    /** 会话过期统一处理（幂等：多个通道同时报错只处理一次） */
    function expired(reason) {
        if (handled) return false;
        handled = true;
        try { console.warn('[Auth] 登录已过期：' + (reason || '未知原因')); } catch (e) {}
        // 标记：让还在退避重连的 WS 循环停下（stream.js / 汇报 WS 会检查）
        try { window.__dshAuthExpired = true; } catch (e) {}
        showOverlay(reason);
        setTimeout(function () {
            try { location.reload(); } catch (e) {}
        }, RELOAD_DELAY_MS);
        return true;
    }

    /**
     * WS 断开后是否还应重连。
     * 1008 = 策略违规；本项目里它就是「未登录 / 会话过期」（见 api/security.py）。
     * 注意 **1012（service restart）必须继续重连** —— 那是后端重启的正常关闭码。
     */
    function shouldReconnectOnClose(code) {
        if (code === 1008) {
            expired('WebSocket 1008');
            return false;
        }
        return true;
    }

    function installFetchGuard() {
        if (typeof window.fetch !== 'function' || window.fetch.__dshGuarded) return;
        var orig = window.fetch.bind(window);
        var wrapped = function (input, init) {
            var url = (typeof input === 'string') ? input : ((input && input.url) || '');
            return orig(input, init).then(function (res) {
                try {
                    if (res && res.status === 401 && isLoggedIn() && !isAuthEndpoint(url)) {
                        expired('HTTP 401 ' + url);
                    }
                } catch (e) {}
                return res;
            });
        };
        wrapped.__dshGuarded = true;
        window.fetch = wrapped;
    }

    window.DSH_AUTH = {
        expired: expired,
        shouldReconnectOnClose: shouldReconnectOnClose,
        isLoggedIn: isLoggedIn
    };
    installFetchGuard();
})();
