<template>
  <aside class="aigate-sidebar" :class="{ collapsed }" :data-theme="currentTheme">
    <div class="sidebar-brand">
      <span class="brand-dot"></span>
      <strong v-if="!collapsed">AIGate</strong>
      <span v-if="!collapsed" class="brand-tag">网关</span>
    </div>

    <nav class="sidebar-nav">
      <router-link
        v-for="r in routes"
        :key="r.path"
        :to="r.path"
        class="sidebar-item"
        :class="{ active: currentPath === r.path }"
        :title="r.name"
      >
        <span class="sidebar-icon" v-html="r.icon"></span>
        <span v-if="!collapsed" class="sidebar-label">{{ r.name }}</span>
      </router-link>
    </nav>

    <div class="sidebar-footer">
      <span v-if="!collapsed" class="login-user">{{ username }}</span>
      <button class="theme-toggle-btn" @click="handleLogout" title="退出登录">
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
          <path d="M9 21H5a2 2 0 01-2-2V5a2 2 0 012-2h4"/>
          <polyline points="16 17 21 12 16 7"/>
          <line x1="21" y1="12" x2="9" y2="12"/>
        </svg>
      </button>
      <button class="theme-toggle-btn" @click="toggleTheme" :title="currentTheme === 'dark' ? '切换到白天模式' : '切换到夜晚模式'">
        <svg v-if="currentTheme === 'dark'" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
          <circle cx="12" cy="12" r="5"/>
          <line x1="12" y1="1" x2="12" y2="3"/>
          <line x1="12" y1="21" x2="12" y2="23"/>
          <line x1="4.22" y1="4.22" x2="5.64" y2="5.64"/>
          <line x1="18.36" y1="18.36" x2="19.78" y2="19.78"/>
          <line x1="1" y1="12" x2="3" y2="12"/>
          <line x1="21" y1="12" x2="23" y2="12"/>
          <line x1="4.22" y1="19.78" x2="5.64" y2="18.36"/>
          <line x1="18.36" y1="5.64" x2="19.78" y2="4.22"/>
        </svg>
        <svg v-else width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
          <path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"/>
        </svg>
      </button>
      <span v-if="!collapsed" class="version-tag">v1.0.0</span>
      <button class="collapse-btn" @click="collapsed = !collapsed" :title="collapsed ? '展开' : '收起'">
        <svg v-if="collapsed" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
          <polyline points="9 18 15 12 9 6" />
        </svg>
        <svg v-else width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
          <polyline points="15 18 9 12 9 6" />
        </svg>
      </button>
    </div>
  </aside>
</template>

<script>
import api from '../api.js'

export default {
  name: 'NavBar',
  emits: ['theme-changed'],
  data() {
    return {
      collapsed: false,
      currentTheme: localStorage.getItem('aigate-theme') || 'dark',
      username: localStorage.getItem('aigate_username') || 'admin',
      routes: [
        { path: '/dashboard', name: '仪表盘', icon: '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="3" width="7" height="7" rx="1"/><rect x="14" y="3" width="7" height="7" rx="1"/><rect x="3" y="14" width="7" height="7" rx="1"/><rect x="14" y="14" width="7" height="7" rx="1"/></svg>' },
        { path: '/providers', name: '服务商', icon: '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="2" y="3" width="20" height="6" rx="2"/><rect x="2" y="15" width="20" height="6" rx="2"/><line x1="6" y1="6" x2="6.01" y2="6"/><line x1="6" y1="18" x2="6.01" y2="18"/></svg>' },
        { path: '/models', name: '模型', icon: '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="3"/><circle cx="5" cy="5" r="2"/><circle cx="19" cy="5" r="2"/><circle cx="5" cy="19" r="2"/><circle cx="19" cy="19" r="2"/><line x1="6.5" y1="6.5" x2="10" y2="10"/><line x1="17.5" y1="6.5" x2="14" y2="10"/><line x1="6.5" y1="17.5" x2="10" y2="14"/><line x1="17.5" y1="17.5" x2="14" y2="14"/></svg>' },
        { path: '/health', name: '健康', icon: '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M22 12h-4l-3 9L9 3l-3 9H2"/></svg>' },
        { path: '/auto', name: 'Auto', icon: '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/></svg>' },
        { path: '/token-saver', name: '省 Token', icon: '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M13 2L3 14h8l-1 8 11-14h-8l0-6z"/></svg>' },
        { path: '/combos', name: '组合', icon: '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M5 8h14M5 12h14M5 16h14"/><rect x="3" y="4" width="18" height="16" rx="2"/></svg>' },
        { path: '/proxies', name: '代理池', icon: '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><line x1="2" y1="12" x2="22" y2="12"/><path d="M12 2a15.3 15.3 0 014 10 15.3 15.3 0 01-4 10 15.3 15.3 0 01-4-10 15.3 15.3 0 014-10z"/></svg>' },
        { path: '/media', name: '媒体', icon: '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="3" width="18" height="18" rx="2"/><circle cx="8.5" cy="8.5" r="1.5"/><polyline points="21 15 16 10 5 21"/></svg>' },
        { path: '/analytics', name: '分析', icon: '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="18" y1="20" x2="18" y2="10"/><line x1="12" y1="20" x2="12" y2="4"/><line x1="6" y1="20" x2="6" y2="14"/></svg>' },
        { path: '/playground', name: 'Playground', icon: '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15a2 2 0 01-2 2H7l-4 4V5a2 2 0 012-2h14a2 2 0 012 2z"/></svg>' },
      ]
    }
  },
  computed: {
    currentPath() {
      return this.$route.path
    }
  },
  methods: {
    toggleTheme() {
      this.currentTheme = this.currentTheme === 'dark' ? 'light' : 'dark'
      this.$emit('theme-changed', this.currentTheme)
    },
    async handleLogout() {
      try { await api.logout() } catch (e) { /* ignore */ }
      localStorage.removeItem('aigate_session')
      localStorage.removeItem('aigate_username')
      window.location.href = '/login'
    }
  }
}
</script>

<style scoped>
.aigate-sidebar {
  width: 220px;
  background: var(--bg-elevated);
  border-right: 1px solid var(--border-soft);
  display: flex;
  flex-direction: column;
  position: sticky;
  top: 0;
  height: 100vh;
  transition: width 0.25s ease, background 0.25s;
  flex-shrink: 0;
}
.aigate-sidebar.collapsed {
  width: 64px;
}
.sidebar-brand {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 20px 18px;
  font-size: 17px;
  border-bottom: 1px solid var(--border-soft);
  flex-shrink: 0;
  overflow: hidden;
}
.sidebar-brand .brand-dot {
  width: 10px;
  height: 10px;
  border-radius: 9999px;
  background: linear-gradient(135deg, #3b82f6, #10b981);
  flex-shrink: 0;
}
.sidebar-brand strong {
  color: var(--text-primary);
  font-weight: 600;
}
.sidebar-brand .brand-tag {
  font-size: 12px;
  color: var(--text-dim);
  font-weight: 400;
  margin-left: 2px;
}
.sidebar-nav {
  flex: 1;
  padding: 8px;
  overflow-y: auto;
}
.sidebar-item {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 9px 12px;
  margin-bottom: 2px;
  text-decoration: none;
  color: var(--text-secondary);
  font-size: 14px;
  font-weight: 500;
  border-radius: 8px;
  transition: all 0.15s ease;
  cursor: pointer;
  white-space: nowrap;
  overflow: hidden;
}
.sidebar-item:hover {
  background: var(--bg-hover);
  color: var(--text-primary);
}
.sidebar-item.active {
  background: rgba(59, 130, 246, 0.14);
  color: #3b82f6;
}
.sidebar-item.active .sidebar-icon :deep(svg) {
  stroke: #3b82f6;
}
.sidebar-icon {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  flex-shrink: 0;
}
.sidebar-icon :deep(svg) {
  stroke: currentColor;
}
.sidebar-label {
  overflow: hidden;
  text-overflow: ellipsis;
}
.sidebar-footer {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 12px 14px;
  border-top: 1px solid var(--border-soft);
  flex-shrink: 0;
  gap: 6px;
}
.version-tag {
  font-size: 11px;
  color: var(--text-dim);
  font-family: ui-monospace, monospace;
}
.login-user {
  font-size: 12px;
  color: var(--text-dim);
  max-width: 80px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.theme-toggle-btn {
  background: transparent;
  border: 1px solid var(--border-medium);
  color: var(--text-secondary);
  border-radius: 6px;
  padding: 4px;
  cursor: pointer;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  transition: all 0.2s;
}
.theme-toggle-btn:hover {
  background: var(--bg-hover);
  color: var(--text-primary);
}
.collapse-btn {
  background: transparent;
  border: 1px solid var(--border-medium);
  color: var(--text-secondary);
  border-radius: 6px;
  padding: 4px;
  cursor: pointer;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  transition: all 0.2s;
}
.collapse-btn:hover {
  background: var(--bg-hover);
  color: var(--text-primary);
}
@media (max-width: 768px) {
  .aigate-sidebar {
    position: fixed;
    z-index: 1000;
  }
}
</style>
