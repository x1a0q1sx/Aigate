import { createRouter, createWebHistory } from 'vue-router'
import Dashboard from './views/Dashboard.vue'
import Providers from './views/Providers.vue'
import Models from './views/Models.vue'
import Health from './views/Health.vue'
import Auto from './views/Auto.vue'
import RouteDecisions from './views/RouteDecisions.vue'
import Analytics from './views/Analytics.vue'
import Playground from './views/Playground.vue'
import Combos from './views/Combos.vue'
import ProxyPool from './views/ProxyPool.vue'
import Media from './views/Media.vue'
import TokenSaver from './views/TokenSaver.vue'
import Settings from './views/Settings.vue'
import Login from './views/Login.vue'

const routes = [
  { path: '/login', component: Login, meta: { title: '登录', public: true } },
  { path: '/', redirect: '/dashboard' },
  { path: '/dashboard', component: Dashboard, meta: { title: '仪表盘' } },
  { path: '/providers', component: Providers, meta: { title: '服务商' } },
  { path: '/models', component: Models, meta: { title: '模型' } },
  { path: '/health', component: Health, meta: { title: '健康监控' } },
  { path: '/auto', component: Auto, meta: { title: 'Auto 选举' } },
  { path: '/route-decisions', component: RouteDecisions, meta: { title: '路由决策' } },
  { path: '/analytics', component: Analytics, meta: { title: '分析' } },
  { path: '/playground', component: Playground, meta: { title: 'Playground' } },
  { path: '/combos', component: Combos, meta: { title: '组合路由' } },
  { path: '/proxies', component: ProxyPool, meta: { title: '代理池' } },
  { path: '/media', component: Media, meta: { title: '媒体中心' } },
  { path: '/token-saver', component: TokenSaver, meta: { title: '省 Token' } },
  { path: '/settings', component: Settings, meta: { title: '设置' } },
  { path: '/oauth', redirect: '/providers' },
  // 兜底：未知路径回仪表盘，避免白屏
  { path: '/:pathMatch(.*)*', redirect: '/dashboard' },
]

const router = createRouter({
  history: createWebHistory(),
  routes,
  scrollBehavior() {
    return { top: 0 }
  },
})

// 路由守卫：未登录跳登录页，并记下原目标，登录后跳回
router.beforeEach((to, from, next) => {
  const token = localStorage.getItem('aigate_session')
  if (to.meta.public) {
    next()
  } else if (!token) {
    next({ path: '/login', query: to.fullPath === '/dashboard' ? {} : { redirect: to.fullPath } })
  } else {
    next()
  }
})

router.afterEach((to) => {
  document.title = to.meta.title ? `${to.meta.title} · AIGate` : 'AIGate'
})

export default router
