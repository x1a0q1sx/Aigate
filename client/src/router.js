import { createRouter, createWebHistory } from 'vue-router'
import Dashboard from './views/Dashboard.vue'
import Providers from './views/Providers.vue'
import Models from './views/Models.vue'
import Health from './views/Health.vue'
import Auto from './views/Auto.vue'
import Analytics from './views/Analytics.vue'
import Playground from './views/Playground.vue'
import Combos from './views/Combos.vue'
import ProxyPool from './views/ProxyPool.vue'
import Media from './views/Media.vue'
import TokenSaver from './views/TokenSaver.vue'
import Login from './views/Login.vue'

const routes = [
  { path: '/login', component: Login, name: '登录' },
  { path: '/', redirect: '/dashboard' },
  { path: '/dashboard', component: Dashboard, name: '仪表盘' },
  { path: '/providers', component: Providers, name: '服务商' },
  { path: '/models', component: Models, name: '模型' },
  { path: '/health', component: Health, name: '健康' },
  { path: '/auto', component: Auto, name: 'Auto' },
  { path: '/analytics', component: Analytics, name: '分析' },
  { path: '/playground', component: Playground, name: 'Playground' },
  { path: '/combos', component: Combos, name: '组合' },
  { path: '/proxies', component: ProxyPool, name: '代理池' },
  { path: '/media', component: Media, name: '媒体' },
  { path: '/token-saver', component: TokenSaver, name: '省 Token' },
  { path: '/oauth', redirect: '/providers' }
]

const router = createRouter({
  history: createWebHistory(),
  routes
})

// 路由守卫：未登录跳转到 /login
router.beforeEach((to, from, next) => {
  const token = localStorage.getItem('aigate_session')
  if (to.path === '/login') {
    next()
  } else if (!token) {
    next('/login')
  } else {
    next()
  }
})

export default router
