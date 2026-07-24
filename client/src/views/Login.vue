<template>
  <div class="login-page">
    <div class="login-card">
      <h1 class="login-title">AIGate</h1>
      <p class="login-subtitle">智能 LLM 聚合网关</p>
      <form @submit.prevent="handleLogin">
        <div class="form-group">
          <label>用户名</label>
          <input type="text" v-model="form.username" autocomplete="username" placeholder="admin" autofocus />
        </div>
        <div class="form-group">
          <label>密码</label>
          <input type="password" v-model="form.password" autocomplete="current-password" placeholder="请输入密码" />
        </div>
        <div v-if="error" class="login-error">{{ error }}</div>
        <button type="submit" class="btn btn-primary login-btn" :disabled="loading">
          {{ loading ? '登录中...' : '登录' }}
        </button>
      </form>
    </div>
  </div>
</template>

<script>
import api from '../api.js'

export default {
  name: 'LoginView',
  data() {
    return {
      form: { username: '', password: '' },
      loading: false,
      error: ''
    }
  },
  methods: {
    async handleLogin() {
      this.loading = true
      this.error = ''
      try {
        const res = await api.login(this.form.username, this.form.password)
        if (res.ok && res.token) {
          localStorage.setItem('aigate_session', res.token)
          localStorage.setItem('aigate_username', res.username || this.form.username)
          this.$router.push('/dashboard')
        } else if (!res.ok) {
          this.error = res.detail || '登录失败'
        }
      } catch (e) {
        this.error = e.message || '登录失败，请检查网络'
      } finally {
        this.loading = false
      }
    }
  }
}
</script>

<style scoped>
.login-page {
  display: flex;
  align-items: center;
  justify-content: center;
  min-height: 100vh;
  background: var(--bg-base);
}
.login-card {
  width: 360px;
  max-width: 90vw;
  padding: 40px 32px;
  background: var(--bg-card);
  border-radius: 12px;
  border: 1px solid var(--border-color);
  box-shadow: 0 4px 24px rgba(0,0,0,0.1);
}
.login-title {
  text-align: center;
  font-size: 28px;
  margin: 0 0 4px;
  color: var(--primary);
}
.login-subtitle {
  text-align: center;
  color: var(--text-muted);
  font-size: 14px;
  margin: 0 0 28px;
}
.form-group {
  margin-bottom: 16px;
}
.form-group label {
  display: block;
  font-size: 13px;
  font-weight: 600;
  margin-bottom: 6px;
  color: var(--text-secondary);
}
.form-group input {
  width: 100%;
  padding: 10px 12px;
  border: 1px solid var(--border-color);
  border-radius: 8px;
  font-size: 15px;
  background: var(--bg-input);
  color: var(--text-primary);
  transition: border-color 0.2s;
}
.form-group input:focus {
  outline: none;
  border-color: var(--primary);
}
.login-error {
  color: var(--danger);
  font-size: 13px;
  margin-bottom: 12px;
  padding: 8px 12px;
  background: rgba(220, 38, 38, 0.1);
  border-radius: 6px;
}
.login-btn {
  width: 100%;
  padding: 12px;
  font-size: 16px;
}
</style>
