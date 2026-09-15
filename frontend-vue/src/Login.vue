<template>
  <div class="login-wrap">
    <form class="login-card" @submit.prevent="submit">
      <h1 class="login-title">智能零售分析系统</h1>
      <p class="login-sub">请登录后使用（账号由平台管理员分配）</p>

      <label class="login-label" for="login-username">用户名</label>
      <input id="login-username" v-model.trim="username" class="login-input"
             type="text" autocomplete="username" placeholder="请输入用户名"
             :disabled="loading" />

      <label class="login-label" for="login-password">密码</label>
      <input id="login-password" v-model="password" class="login-input"
             type="password" autocomplete="current-password" placeholder="请输入密码"
             :disabled="loading" />

      <p v-if="error" class="login-error">{{ error }}</p>

      <button class="login-btn" type="submit" :disabled="loading || !canSubmit">
        {{ loading ? '登录中…' : '登 录' }}
      </button>

      <p class="login-hint">
        首次部署：账号密码见服务端启动终端打印的「首次启动」提示；
        若已配置环境变量 AUTH_ROOT_PASSWORD，则使用该密码。
      </p>
    </form>
  </div>
</template>

<script>
export default {
  name: 'LoginView',
  emits: ['success'],
  data() {
    return { username: '', password: '', loading: false, error: '' }
  },
  computed: {
    canSubmit() {
      return this.username.length > 0 && this.password.length > 0
    },
  },
  mounted() {
    // 自动聚焦用户名输入框
    this.$nextTick(() => {
      const el = document.getElementById('login-username')
      if (el) el.focus()
    })
  },
  methods: {
    async submit() {
      if (!this.canSubmit || this.loading) return
      this.loading = true
      this.error = ''
      try {
        const resp = await fetch('/api/auth/login', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ username: this.username, password: this.password }),
        })
        if (resp.ok) {
          const data = await resp.json()
          this.password = ''
          this.$emit('success', data)
          return
        }
        if (resp.status === 401) {
          this.error = '用户名或密码错误，或账号已被停用'
        } else {
          let detail = ''
          try { detail = (await resp.json()).detail || '' } catch (e) {}
          this.error = detail || ('登录失败（HTTP ' + resp.status + '）')
        }
      } catch (e) {
        this.error = '无法连接服务端，请确认后端已启动'
      } finally {
        this.loading = false
      }
    },
  },
}
</script>

<style scoped>
.login-wrap {
  min-height: 100vh;
  display: flex;
  align-items: center;
  justify-content: center;
  background: var(--bg-primary, #0f1419);
  padding: 24px;
}
.login-card {
  width: 100%;
  max-width: 360px;
  background: var(--bg-secondary, #1a2029);
  border: 1px solid var(--border, #2a3441);
  border-radius: 10px;
  padding: 28px 26px 22px;
  box-shadow: 0 8px 28px rgba(0, 0, 0, .35);
  display: flex;
  flex-direction: column;
}
.login-title {
  margin: 0 0 6px;
  font-size: 1.15em;
  color: var(--text-primary, #e6edf3);
  text-align: center;
}
.login-sub {
  margin: 0 0 20px;
  font-size: .8em;
  color: var(--text-secondary, #8b98a5);
  text-align: center;
}
.login-label {
  font-size: .8em;
  color: var(--text-secondary, #8b98a5);
  margin-bottom: 6px;
}
.login-input {
  width: 100%;
  box-sizing: border-box;
  padding: 9px 11px;
  margin-bottom: 14px;
  font-size: .9em;
  color: var(--text-primary, #e6edf3);
  background: var(--bg-primary, #0f1419);
  border: 1px solid var(--border, #2a3441);
  border-radius: 6px;
  outline: none;
}
.login-input:focus { border-color: var(--accent, #2f81f7); }
.login-error {
  margin: 0 0 12px;
  font-size: .8em;
  color: var(--accent-red, #f85149);
}
.login-btn {
  padding: 10px;
  font-size: .95em;
  font-weight: 600;
  color: #fff;
  background: var(--accent, #2f81f7);
  border: none;
  border-radius: 6px;
  cursor: pointer;
}
.login-btn:disabled { opacity: .55; cursor: not-allowed; }
.login-hint {
  margin: 16px 0 0;
  font-size: .72em;
  line-height: 1.6;
  color: var(--text-secondary, #8b98a5);
  border-top: 1px solid var(--border, #2a3441);
  padding-top: 12px;
}
</style>
