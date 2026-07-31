<template>
  <div class="login-page">
    <div class="login-aura" aria-hidden="true"></div>

    <div class="login-card">
      <div class="login-brand">
        <span class="brand-mark" aria-hidden="true"></span>
        <h1>AIGate</h1>
      </div>
      <p class="login-subtitle">智能 LLM 聚合网关 · 管理控制台</p>

      <form @submit.prevent="handleLogin">
        <div class="form-group">
          <label for="login-user">用户名</label>
          <input
            id="login-user"
            v-model.trim="form.username"
            type="text"
            autocomplete="username"
            placeholder="admin"
            autofocus
          />
        </div>

        <div class="form-group">
          <label for="login-pass">密码</label>
          <div class="pass-wrap">
            <input
              id="login-pass"
              v-model="form.password"
              :type="showPass ? 'text' : 'password'"
              autocomplete="current-password"
              placeholder="请输入密码"
            />
            <button
              class="pass-toggle"
              type="button"
              :aria-label="showPass ? '隐藏密码' : '显示密码'"
              @click="showPass = !showPass"
            >
              <AppIcon :name="showPass ? 'eyeOff' : 'eye'" :size="15" />
            </button>
          </div>
        </div>

        <div v-if="error" class="alert alert-error login-alert">
          <AppIcon name="alert" :size="15" />
          <span>{{ error }}</span>
        </div>

        <button type="submit" class="btn btn-primary login-btn" :disabled="loading">
          {{ loading ? '登录中…' : '登录' }}
        </button>
      </form>
    </div>
  </div>
</template>

<script>
import api from '../api.js'
import AppIcon from '../components/AppIcon.vue'

export default {
  name: 'LoginView',
  components: { AppIcon },
  data() {
    return {
      form: { username: '', password: '' },
      loading: false,
      showPass: false,
      error: '',
    }
  },
  methods: {
    async handleLogin() {
      if (!this.form.username || !this.form.password) {
        this.error = '请填写用户名和密码'
        return
      }
      this.loading = true
      this.error = ''
      try {
        const res = await api.login(this.form.username, this.form.password)
        if (res.ok && res.token) {
          localStorage.setItem('aigate_session', res.token)
          localStorage.setItem('aigate_username', res.username || this.form.username)
          const redirect = this.$route.query.redirect
          this.$router.push(typeof redirect === 'string' && redirect.startsWith('/') ? redirect : '/dashboard')
        } else {
          // 兜底：后端返回 ok 但没带 token 时也要给出提示，不能静默卡住
          this.error = res.detail || '登录失败，请重试'
        }
      } catch (e) {
        this.error = e.message || '登录失败，请检查网络'
      } finally {
        this.loading = false
      }
    },
  },
}
</script>

<style scoped>
.login-page {
  position: relative;
  display: flex;
  align-items: center;
  justify-content: center;
  min-height: 100vh;
  padding: var(--space-5);
  background: var(--bg-base);
  overflow: hidden;
}
/* 背景光晕，纯装饰 */
.login-aura {
  position: absolute;
  width: 620px;
  height: 620px;
  border-radius: 50%;
  background: radial-gradient(circle, var(--primary-soft) 0%, transparent 68%);
  filter: blur(20px);
  pointer-events: none;
}

.login-card {
  position: relative;
  width: 372px;
  max-width: 100%;
  padding: var(--space-8);
  background: var(--bg-surface);
  border: 1px solid var(--border-soft);
  border-radius: var(--radius-xl);
  box-shadow: var(--shadow-lg);
}

.login-brand {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: var(--space-3);
}
.login-brand h1 {
  font-size: var(--text-3xl);
  font-weight: 650;
  letter-spacing: -0.02em;
}
.brand-mark {
  width: 12px;
  height: 12px;
  border-radius: var(--radius-pill);
  background: linear-gradient(135deg, var(--primary), var(--success));
  box-shadow: 0 0 0 4px var(--primary-soft);
}
.login-subtitle {
  text-align: center;
  color: var(--text-muted);
  font-size: var(--text-base);
  margin: var(--space-2) 0 var(--space-6);
}

.pass-wrap { position: relative; }
.pass-wrap input { padding-right: var(--space-8); }
.pass-toggle {
  position: absolute;
  right: var(--space-1);
  top: 50%;
  transform: translateY(-50%);
  width: 28px;
  height: 28px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  background: transparent;
  border: 0;
  border-radius: var(--radius-sm);
  color: var(--text-muted);
  cursor: pointer;
}
.pass-toggle:hover { color: var(--text-primary); background: var(--bg-hover); }

.login-alert {
  margin-bottom: var(--space-4);
  align-items: center;
  font-size: var(--text-base);
}
.login-btn {
  width: 100%;
  --btn-h: 40px;
  font-size: var(--text-md);
  margin-top: var(--space-2);
}
</style>
