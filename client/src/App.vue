<template>
  <n-config-provider :theme="naiveTheme" :theme-overrides="themeOverrides">
    <n-message-provider>
      <n-dialog-provider>
        <n-notification-provider>
          <!-- 登录页是独立全屏布局，不套后台外壳（原来登录时侧边栏还挂在旁边） -->
          <router-view v-if="isBareRoute" />
          <div v-else class="aigate-shell">
            <NavBar @theme-changed="onThemeChanged" />
            <main class="aigate-main">
              <div class="aigate-content">
                <router-view />
              </div>
            </main>
          </div>
          <ToastStack />
        </n-notification-provider>
      </n-dialog-provider>
    </n-message-provider>
  </n-config-provider>
</template>

<script>
import { darkTheme } from 'naive-ui'
import NavBar from './components/NavBar.vue'
import ToastStack from './components/ToastStack.vue'

/** 深浅两套色值需与 style.css 的令牌保持一致，否则 naive-ui 组件会和自绘组件"两种画风" */
const PALETTE = {
  dark: {
    primary: '#5b8dff',
    primaryHover: '#85a9ff',
    primaryPressed: '#3f74ec',
    success: '#34d399',
    warning: '#fbbf24',
    error: '#f87171',
    info: '#60a5fa',
    bodyColor: '#0a0f1a',
    cardColor: '#0f1623',
    modalColor: '#131c2c',
    popoverColor: '#131c2c',
    borderColor: '#1c2839',
    textBase: '#e9eef7',
    text1: '#e9eef7',
    text2: '#a9b7cd',
    text3: '#7e8ea9',
  },
  light: {
    primary: '#2f5fe0',
    primaryHover: '#4f7dff',
    primaryPressed: '#1e46b8',
    success: '#0f8a5f',
    warning: '#a45c07',
    error: '#c62d2d',
    info: '#1d4ed8',
    bodyColor: '#f5f7fb',
    cardColor: '#ffffff',
    modalColor: '#ffffff',
    popoverColor: '#ffffff',
    borderColor: '#e6eaf1',
    textBase: '#101827',
    text1: '#101827',
    text2: '#45536b',
    text3: '#697894',
  },
}

/** 登录页等不需要后台外壳的路由 */
const BARE_ROUTES = ['/login']

export default {
  name: 'App',
  components: { NavBar, ToastStack },
  data() {
    return {
      currentTheme: localStorage.getItem('aigate-theme') || 'dark',
    }
  },
  computed: {
    isBareRoute() {
      return BARE_ROUTES.includes(this.$route.path)
    },
    naiveTheme() {
      // naive-ui 用 null 表示浅色主题，darkTheme 对象表示暗色
      return this.currentTheme === 'dark' ? darkTheme : null
    },
    themeOverrides() {
      const c = PALETTE[this.currentTheme] || PALETTE.dark
      return {
        common: {
          borderRadius: '8px',
          borderRadiusSmall: '6px',
          fontFamily:
            "-apple-system, BlinkMacSystemFont, 'Segoe UI', 'PingFang SC', 'Hiragino Sans GB', 'Microsoft YaHei', Roboto, sans-serif",
          fontFamilyMono: "ui-monospace, 'SF Mono', SFMono-Regular, Menlo, Consolas, monospace",
          fontSize: '14px',
          primaryColor: c.primary,
          primaryColorHover: c.primaryHover,
          primaryColorPressed: c.primaryPressed,
          primaryColorSuppl: c.primaryHover,
          successColor: c.success,
          warningColor: c.warning,
          errorColor: c.error,
          infoColor: c.info,
          bodyColor: c.bodyColor,
          cardColor: c.cardColor,
          modalColor: c.modalColor,
          popoverColor: c.popoverColor,
          borderColor: c.borderColor,
          dividerColor: c.borderColor,
          textColorBase: c.textBase,
          textColor1: c.text1,
          textColor2: c.text2,
          textColor3: c.text3,
        },
        Card: {
          borderRadius: '12px',
          paddingSmall: '16px',
          paddingMedium: '20px',
        },
        Statistic: {
          valueFontSize: '28px',
        },
      }
    },
  },
  methods: {
    onThemeChanged(theme) {
      this.currentTheme = theme
      document.documentElement.setAttribute('data-theme', theme)
      localStorage.setItem('aigate-theme', theme)
    },
  },
  mounted() {
    document.documentElement.setAttribute('data-theme', this.currentTheme)
  },
}
</script>

<style>
.aigate-shell {
  display: flex;
  min-height: 100vh;
}
.aigate-main {
  flex: 1;
  min-width: 0;
  padding: var(--space-6) var(--space-8);
  overflow-x: hidden;
}
.aigate-content {
  max-width: 1240px;
  margin: 0 auto;
}
@media (max-width: 900px) {
  .aigate-main {
    padding: var(--space-4);
  }
}
</style>
