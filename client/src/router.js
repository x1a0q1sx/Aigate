import { createRouter, createWebHistory } from 'vue-router'
import Dashboard from './views/Dashboard.vue'
import Providers from './views/Providers.vue'
import Models from './views/Models.vue'
import Health from './views/Health.vue'
import Auto from './views/Auto.vue'
import Analytics from './views/Analytics.vue'
import Playground from './views/Playground.vue'

const routes = [
  { path: '/', redirect: '/dashboard' },
  { path: '/dashboard', component: Dashboard, name: '仪表盘' },
  { path: '/providers', component: Providers, name: '服务商' },
  { path: '/models', component: Models, name: '模型' },
  { path: '/health', component: Health, name: '健康' },
  { path: '/auto', component: Auto, name: 'Auto' },
  { path: '/analytics', component: Analytics, name: '分析' },
  { path: '/playground', component: Playground, name: 'Playground' }
]

const router = createRouter({
  history: createWebHistory(),
  routes
})

export default router
