/*
 * Vue 页面加载失败时的兜底提示（资源失败 / 挂载超时 → 提示刷新）。
 *
 * ⚠ 为什么是**独立文件**而不是内联 <script>（2026-09-19 实测踩坑）：
 *   这段逻辑原本由 `api/main.py` 的 `VUE_FALLBACK_SCRIPT` 以**内联脚本**注入到
 *   `</body>` 前，而安全加固加的 CSP 是 `script-src 'self'`（当时注释还写着
 *   "index.html 无内联脚本，所以不需要 unsafe-inline"）→ 两者叠加后浏览器**直接拦掉**：
 *
 *     Refused to execute inline script because it violates the following
 *     Content Security Policy directive: "script-src 'self'"
 *
 *   后果不只是控制台报错：**兜底提示本身失效了** —— 恰恰是它要处理的场景
 *   （前端资源挂了、Vue 没挂载）下，用户只会看到白屏，一个字都没有。
 *
 * 现在改成同源外部脚本（`/js/vue-fallback.js`），CSP 保持严格、兜底也真的能跑。
 */
(function () {
  function notify(reason) {
    if (window.__dshVueFallback) return;   // 只提示一次
    window.__dshVueFallback = true;
    try {
      fetch('/api/frontend/fallback?reason=' + encodeURIComponent(reason), { method: 'POST' });
    } catch (e) { /* 上报失败不影响提示 */ }
    var box = document.createElement('div');
    box.style.cssText = 'position:fixed;left:50%;top:50%;transform:translate(-50%,-50%);' +
      'background:#fff;color:#333;padding:24px;border-radius:8px;' +
      'box-shadow:0 2px 12px rgba(0,0,0,.2);z-index:9999;text-align:center;font-family:sans-serif';
    box.innerHTML = '<div style="font-size:20px;margin-bottom:8px">前端资源加载异常</div>' +
      '<div style="font-size:14px;color:#666">请刷新页面重试，或检查后端控制台日志</div>';
    document.body.appendChild(box);
  }

  // 1) 静态资源（/assets/*）加载失败
  window.addEventListener('error', function (e) {
    var src = (e.target && (e.target.src || e.target.href)) || '';
    if (src && src.indexOf('/assets/') !== -1) notify('资源加载失败: ' + src);
  }, true);

  // 2) 未处理的 Promise 异常且应用未挂载
  window.addEventListener('unhandledrejection', function () {
    var app = document.getElementById('app');
    if (!app || app.childElementCount === 0) notify('未处理的Promise异常');
  });

  // 3) 8 秒内 Vue 仍未挂载
  setTimeout(function () {
    var app = document.getElementById('app');
    if (!app || app.childElementCount === 0) notify('Vue 挂载超时');
  }, 8000);
})();
