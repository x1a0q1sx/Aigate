<template>
  <nav class="navbar">
    <div class="container navbar-inner">
      <div class="navbar-brand">
        <strong>🔹 AIGate</strong>
        <span class="text-muted">智能 LLM 聚合网关</span>
      </div>
      <div class="navbar-menu">
        <router-link
          v-for="r in routes"
          :key="r.path"
          :to="r.path"
          class="nav-link"
          :class="{ active: currentPath === r.path }"
        >
          {{ r.name }}
        </router-link>
      </div>
    </div>
  </nav>
</template>
<script>
export default {
  name: 'NavBar',
  data() {
    return {
      routes: [
        { path: '/dashboard', name: '仪表盘' },
        { path: '/providers', name: '服务商' },
        { path: '/models', name: '模型' },
        { path: '/health', name: '健康' },
        { path: '/auto', name: 'Auto' },
        { path: '/analytics', name: '分析' },
        { path: '/playground', name: 'Playground' }
      ]
    }
  },
  computed: {
    currentPath() {
      return this.$route.path
    }
  }
}
</script>
<style scoped>
.navbar {
  background: white;
  box-shadow: 0 1px 3px rgba(0,0,0,0.1);
  margin-bottom: 20px;
}
.navbar-inner {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 16px 20px;
}
.navbar-brand {
  display: flex;
  align-items: center;
  gap: 12px;
  font-size: 18px;
}
.navbar-brand strong {
  color: var(--primary);
}
.navbar-brand .text-muted {
  font-size: 14px;
  color: var(--gray-500);
  font-weight: normal;
}
.navbar-menu {
  display: flex;
  gap: 8px;
}
.nav-link {
  padding: 8px 16px;
  text-decoration: none;
  color: var(--gray-700);
  border-radius: 6px;
  font-size: 14px;
  transition: all 0.2s;
}
.nav-link:hover {
  background: var(--gray-100);
}
.nav-link.active {
  background: var(--primary);
  color: white;
}
@media (max-width: 768px) {
  .navbar-inner {
    flex-direction: column;
    gap: 12px;
  }
  .navbar-menu {
    flex-wrap: wrap;
    justify-content: center;
  }
}
</style>
