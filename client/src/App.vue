<template>
  <n-config-provider :theme="naiveTheme" :theme-overrides="themeOverrides">
    <n-message-provider>
      <n-dialog-provider>
        <n-notification-provider>
          <div class="aigate-shell">
            <NavBar @theme-changed="onThemeChanged" />
            <main class="aigate-main" :data-theme="currentTheme">
              <div class="aigate-content">
                <router-view />
              </div>
            </main>
          </div>
        </n-notification-provider>
      </n-dialog-provider>
    </n-message-provider>
  </n-config-provider>
</template>
<script>
import { darkTheme } from 'naive-ui'
import NavBar from './components/NavBar.vue'
export default {
  name: 'App',
  components: { NavBar },
  data() {
    return {
      currentTheme: localStorage.getItem('aigate-theme') || 'dark',
      themeOverrides: {
        common: {
          borderRadius: '8px',
          borderRadiusSmall: '6px',
        }
      }
    }
  },
  computed: {
    naiveTheme() {
      // naive-ui 用 null 表示浅色主题，darkTheme 对象表示暗色
      return this.currentTheme === 'dark' ? darkTheme : null
    }
  },
  methods: {
    onThemeChanged(theme) {
      this.currentTheme = theme
      document.documentElement.setAttribute('data-theme', theme)
      localStorage.setItem('aigate-theme', theme)
    }
  },
  mounted() {
    document.documentElement.setAttribute('data-theme', this.currentTheme)
  }
}
</script>
<style>
* { box-sizing: border-box; }
html, body, #app {
  height: 100%;
  margin: 0;
  padding: 0;
}
body {
  background: var(--bg-base);
  color: var(--text-primary);
  font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, 'Helvetica Neue', Arial, sans-serif;
}
.aigate-shell {
  display: flex;
  min-height: 100vh;
}
.aigate-main {
  flex: 1;
  min-width: 0;
  padding: 28px 32px;
  overflow-y: auto;
}
.aigate-content {
  max-width: 1100px;
  margin: 0 auto;
}
</style>
