<template>
  <aside class="sidebar" :class="{ collapsed }">
    <div class="sidebar-brand">
      <span class="brand-mark" aria-hidden="true"></span>
      <span v-if="!collapsed" class="brand-text">
        <strong>AIGate</strong>
        <span class="brand-tag">LLM 网关</span>
      </span>
    </div>

    <nav class="sidebar-nav" aria-label="主导航">
      <router-link
        v-for="r in routes"
        :key="r.path"
        :to="r.path"
        class="nav-item"
        :class="{ active: isActive(r.path) }"
        :title="collapsed ? r.name : null"
      >
        <AppIcon :name="r.icon" :size="17" />
        <span v-if="!collapsed" class="nav-label">{{ r.name }}</span>
      </router-link>
    </nav>

    <div class="sidebar-foot">
      <div v-if="!collapsed" class="foot-user">
        <span class="user-avatar" aria-hidden="true">{{ (username || 'A').charAt(0).toUpperCase() }}</span>
        <span class="user-name">{{ username }}</span>
        <span class="version-tag">v{{ version }}</span>
      </div>
      <div class="foot-actions">
        <button
          class="icon-btn"
          type="button"
          :title="currentTheme === 'dark' ? '切换到白天模式' : '切换到夜晚模式'"
          :aria-label="currentTheme === 'dark' ? '切换到白天模式' : '切换到夜晚模式'"
          @click="toggleTheme"
        >
          <AppIcon :name="currentTheme === 'dark' ? 'sun' : 'moon'" :size="15" />
        </button>
        <button class="icon-btn" type="button" title="退出登录" aria-label="退出登录" @click="handleLogout">
          <AppIcon name="logout" :size="15" />
        </button>
        <button
          class="icon-btn"
          type="button"
          :title="collapsed ? '展开侧边栏' : '收起侧边栏'"
          :aria-label="collapsed ? '展开侧边栏' : '收起侧边栏'"
          @click="toggleCollapse"
        >
          <AppIcon :name="collapsed ? 'chevronRight' : 'chevronLeft'" :size="15" />
        </button>
      </div>
    </div>
  </aside>
</template>

<script>
import api from '../api.js'
import AppIcon from './AppIcon.vue'

const COLLAPSE_KEY = 'aigate:navCollapsed'

export default {
  name: 'NavBar',
  components: { AppIcon },
  emits: ['theme-changed'],
  data() {
    return {
      collapsed: localStorage.getItem(COLLAPSE_KEY) === '1',
      currentTheme: localStorage.getItem('aigate-theme') || 'dark',
      username: localStorage.getItem('aigate_username') || 'admin',
      version: '1.0.0',
      routes: [
        { path: '/dashboard', name: '仪表盘', icon: 'dashboard' },
        { path: '/providers', name: '服务商', icon: 'server' },
        { path: '/models', name: '模型', icon: 'cpu' },
        { path: '/health', name: '健康', icon: 'activity' },
        { path: '/auto', name: 'Auto 选举', icon: 'scale' },
        { path: '/token-saver', name: '省 Token', icon: 'zap' },
        { path: '/combos', name: '组合路由', icon: 'layers' },
        { path: '/proxies', name: '代理池', icon: 'globe' },
        { path: '/media', name: '媒体', icon: 'image' },
        { path: '/analytics', name: '分析', icon: 'chart' },
        { path: '/playground', name: 'Playground', icon: 'message' },
      ],
    }
  },
  methods: {
    isActive(path) {
      return this.$route.path === path || this.$route.path.startsWith(path + '/')
    },
    toggleCollapse() {
      this.collapsed = !this.collapsed
      localStorage.setItem(COLLAPSE_KEY, this.collapsed ? '1' : '0')
    },
    toggleTheme() {
      this.currentTheme = this.currentTheme === 'dark' ? 'light' : 'dark'
      this.$emit('theme-changed', this.currentTheme)
    },
    async handleLogout() {
      try {
        await api.logout()
      } catch (e) {
        /* 服务端 session 可能已过期，本地照常清理 */
      }
      localStorage.removeItem('aigate_session')
      localStorage.removeItem('aigate_username')
      window.location.href = '/login'
    },
  },
}
</script>

<style scoped>
.sidebar {
  width: 216px;
  flex-shrink: 0;
  display: flex;
  flex-direction: column;
  position: sticky;
  top: 0;
  height: 100vh;
  background: var(--bg-surface);
  border-right: 1px solid var(--border-soft);
  transition: width var(--dur-slow) var(--ease-out);
}
.sidebar.collapsed {
  width: 60px;
}

/* ── 品牌 ── */
.sidebar-brand {
  display: flex;
  align-items: center;
  gap: var(--space-3);
  height: 56px;
  padding: 0 var(--space-4);
  flex-shrink: 0;
  border-bottom: 1px solid var(--border-soft);
  overflow: hidden;
}
.brand-mark {
  width: 10px;
  height: 10px;
  flex-shrink: 0;
  border-radius: var(--radius-pill);
  background: linear-gradient(135deg, var(--primary), var(--success));
  box-shadow: 0 0 0 3px var(--primary-soft);
}
.brand-text {
  display: flex;
  align-items: baseline;
  gap: var(--space-2);
  white-space: nowrap;
}
.brand-text strong {
  font-size: var(--text-lg);
  font-weight: 650;
  letter-spacing: -0.01em;
}
.brand-tag {
  font-size: var(--text-xs);
  color: var(--text-dim);
}

/* ── 导航 ── */
.sidebar-nav {
  flex: 1;
  padding: var(--space-2);
  overflow-y: auto;
  overflow-x: hidden;
  display: flex;
  flex-direction: column;
  gap: 2px;
}
.nav-item {
  display: flex;
  align-items: center;
  gap: var(--space-3);
  height: 34px;
  padding: 0 var(--space-3);
  border-radius: var(--radius-sm);
  color: var(--text-secondary);
  font-size: var(--text-base);
  font-weight: 500;
  text-decoration: none;
  white-space: nowrap;
  overflow: hidden;
  position: relative;
  transition: background var(--dur-fast) var(--ease-out), color var(--dur-fast) var(--ease-out);
}
.sidebar.collapsed .nav-item {
  justify-content: center;
  padding: 0;
}
.nav-item:hover {
  background: var(--bg-hover);
  color: var(--text-primary);
  text-decoration: none;
}
.nav-item.active {
  background: var(--primary-soft);
  color: var(--primary);
}
.nav-item.active::before {
  content: '';
  position: absolute;
  left: 0;
  top: 50%;
  transform: translateY(-50%);
  width: 2px;
  height: 16px;
  border-radius: 0 2px 2px 0;
  background: var(--primary);
}
.nav-label {
  overflow: hidden;
  text-overflow: ellipsis;
}

/* ── 底部 ── */
.sidebar-foot {
  flex-shrink: 0;
  padding: var(--space-3);
  border-top: 1px solid var(--border-soft);
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
}
.foot-user {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  min-width: 0;
}
.user-avatar {
  width: 22px;
  height: 22px;
  flex-shrink: 0;
  border-radius: var(--radius-pill);
  background: var(--primary-soft);
  color: var(--primary);
  font-size: var(--text-xs);
  font-weight: 700;
  display: inline-flex;
  align-items: center;
  justify-content: center;
}
.user-name {
  flex: 1;
  min-width: 0;
  font-size: var(--text-sm);
  color: var(--text-secondary);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.version-tag {
  font-family: var(--font-mono);
  font-size: var(--text-xs);
  color: var(--text-dim);
}
.foot-actions {
  display: flex;
  gap: var(--space-1);
}
.sidebar.collapsed .foot-actions {
  flex-direction: column;
  align-items: center;
}
.icon-btn {
  flex: 1;
  height: 28px;
  min-width: 28px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  background: transparent;
  border: 1px solid var(--border-soft);
  border-radius: var(--radius-sm);
  color: var(--text-muted);
  cursor: pointer;
  transition: background var(--dur-fast) var(--ease-out), color var(--dur-fast) var(--ease-out),
    border-color var(--dur-fast) var(--ease-out);
}
.icon-btn:hover {
  background: var(--bg-hover);
  border-color: var(--border-medium);
  color: var(--text-primary);
}
.sidebar.collapsed .icon-btn {
  flex: none;
  width: 30px;
}

@media (max-width: 900px) {
  .sidebar {
    width: 60px;
  }
  .sidebar .brand-text,
  .sidebar .nav-label,
  .sidebar .foot-user {
    display: none;
  }
  .sidebar .nav-item {
    justify-content: center;
    padding: 0;
  }
  .sidebar .foot-actions {
    flex-direction: column;
    align-items: center;
  }
}
</style>
