<template>
  <div v-if="phase === 'checking'" class="auth-loading">正在检查登录状态…</div>
  <LoginView v-else-if="phase === 'login'" @success="onLoginSuccess" />
  <App v-else />
</template>

<script>
/**
 * 登录门禁（根组件）
 *
 * 为什么不改 App.vue：App.vue 是一整个仪表盘（1300+ 行），
 * 在它外层做门禁可以做到「未登录完全不挂载仪表盘」——视频 WS 等
 * 受保护通道也就不会在未登录时反复重连。
 *
 * 流程：启动先问 /api/auth/me
 *   200 → 已登录：加载 voice.js 后挂载仪表盘
 *   401 → 未登录：显示登录页
 *
 * 登录态走 HttpOnly Cookie（同源自动携带），前端无需管理 token。
 */
import App from './App.vue'
import LoginView from './Login.vue'

function loadScriptOnce(src) {
  return new Promise((resolve) => {
    if (document.querySelector(`script[data-src="${src}"]`)) return resolve()
    const s = document.createElement('script')
    s.src = src
    s.dataset.src = src
    s.onload = () => resolve()
    s.onerror = () => resolve()   // 加载失败不阻塞登录，界面仍可用
    document.body.appendChild(s)
  })
}

export default {
  name: 'RootView',
  components: { App, LoginView },
  data() {
    return { phase: 'checking' }   // checking | login | ready
  },
  created() {
    this.checkAuth()
  },
  methods: {
    async checkAuth() {
      try {
        const resp = await fetch('/api/auth/me')
        if (resp.ok) {
          const data = await resp.json()
          window.__currentUser = data.user || null
          window.__currentPerms = data.permissions || []
          await this.enterApp()
          return
        }
      } catch (e) {
        // 网络异常也走登录页，由登录页给出提示
      }
      this.phase = 'login'
    },
    async onLoginSuccess(data) {
      window.__currentUser = (data && data.user) || null
      window.__currentPerms = (data && data.permissions) || []
      await this.enterApp()
    },
    async enterApp() {
      // ⚠ 顺序很重要（D1 修复）：voice.js 在**顶层**就 `document.getElementById`
      // 去绑定语音按钮。若在仪表盘挂载**之前**加载，DOM 里只有 loading 占位，
      // 取到 null 后 `addEventListener` 抛 TypeError，**整个 IIFE 中断**——
      // 结果是：语音输入 / 转文字测试 / 语音回复开关三个按钮都没有监听，
      // 且 window.playVoiceReply / prepareVoiceReply 未定义（App.vue 的 TTS 播报因此永不生效）。
      // 表现是"语音功能整体失效"，而控制台只有一条容易被忽略的报错。
      //
      // 正确顺序：先切到 ready 让 App 挂载（DOM 出现按钮）→ 等 DOM 更新 → 再加载脚本。
      this.phase = 'ready'
      await this.$nextTick()
      await loadScriptOnce('/js/voice.js')
    },
  },
}
</script>

<style scoped>
.auth-loading {
  min-height: 100vh;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: .9em;
  color: var(--text-secondary, #8b98a5);
  background: var(--bg-primary, #0f1419);
}
</style>
