import { createApp } from 'vue'
import App from './App.vue'
import router from './router'
import naive from 'naive-ui'
import './style.css'

const app = createApp(App)
app.use(router)
app.use(naive)

// 主题持久化
const savedTheme = localStorage.getItem('aigate-theme') || 'dark'
document.documentElement.setAttribute('data-theme', savedTheme)

app.mount('#app')
