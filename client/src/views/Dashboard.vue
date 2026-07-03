<template>
  <div>
    <h1 style="margin-bottom: 20px;">仪表盘 📊</h1>
    <div class="stats-grid" v-if="summary">
      <div class="stat-card">
        <div class="stat-number">{{ summary.total_providers }}</div>
        <div class="stat-label">服务商总数</div>
      </div>
      <div class="stat-card">
        <div class="stat-number">{{ summary.total_keys }}</div>
        <div class="stat-label">已配置密钥</div>
      </div>
      <div class="stat-card">
        <div class="stat-number">{{ summary.total_models }}</div>
        <div class="stat-label">启用模型</div>
      </div>
      <div class="stat-card">
        <div class="stat-number">{{ summary.auto_candidates }}</div>
        <div class="stat-label">Auto 候选</div>
      </div>
    </div>
    <!-- AIGate 连接密钥 -->
    <div class="card" style="margin-bottom: 20px;">
      <div class="card-header" style="display: flex; justify-content: space-between; align-items: center;">
        <h2>🔑 AIGate 连接密钥</h2>
        <div style="display: flex; gap: 8px; align-items: center;">
          <code v-if="!showAIGateKey" style="font-size: 14px;">{{ maskedKey }}</code>
          <code v-else style="font-size: 14px; word-break: break-all;">{{ aigateKey }}</code>
          <button class="btn btn-outline btn-sm" @click="toggleAIGateKey">
            {{ showAIGateKey ? '隐藏' : '显示' }}
          </button>
          <button class="btn btn-outline btn-sm" @click="copyAIGateKey" v-if="aigateKey">复制</button>
        </div>
      </div>
      <p style="font-size: 13px; color: var(--gray-500); margin-top: 8px;">
        使用此密钥连接 AIGate 网关，所有请求需在 <code>Authorization: Bearer &lt;key&gt;</code> 中携带此密钥。
      </p>
    </div>
    <div class="card" style="margin-bottom: 20px;" v-if="currentModel && currentModel.provider">
      <div class="card-header">
        <h2>🎯 Auto 排名第一</h2>
      </div>
      <div style="display: flex; align-items: center; gap: 12px; font-size: 18px; margin-top: 8px; flex-wrap: wrap;">
        <strong style="color: var(--primary);">{{ currentModel.provider }}/{{ currentModel.model }}</strong>
        <span v-if="currentModel.display_name" style="font-size: 13px; color: var(--gray-500);">({{ currentModel.display_name }})</span>
        <span v-if="currentModel.final_score != null" class="badge badge-success">{{ Number(currentModel.final_score).toFixed(1) }} 分</span>
        <span v-if="currentModel.excluded_reason" class="badge badge-warning">{{ currentModel.excluded_reason }}</span>
      </div>
    </div>
    <div class="card">
      <div class="card-header">
        <h2>健康状态分布</h2>
      </div>
      <div class="status-stats">
        <div class="status-row">
          <span>✅ 健康</span>
          <div class="bar-container">
            <div class="bar healthy" :style="{width: percent(summary.healthy_models, total) + '%'}"></div>
          </div>
          <span class="count">{{ summary.healthy_models }}</span>
        </div>
        <div class="status-row">
          <span>⚠️ 延迟</span>
          <div class="bar-container">
            <div class="bar degraded" :style="{width: percent(summary.degraded_models, total) + '%'}"></div>
          </div>
          <span class="count">{{ summary.degraded_models }}</span>
        </div>
        <div class="status-row">
          <span>⏸️ 限流</span>
          <div class="bar-container">
            <div class="bar rate_limited" :style="{width: percent(summary.rate_limited_models, total) + '%'}"></div>
          </div>
          <span class="count">{{ summary.rate_limited_models }}</span>
        </div>
        <div class="status-row">
          <span>❌ 故障</span>
          <div class="bar-container">
            <div class="bar unhealthy" :style="{width: percent(summary.unhealthy_models, total) + '%'}"></div>
          </div>
          <span class="count">{{ summary.unhealthy_models }}</span>
        </div>
      </div>
    </div>
    <div class="card" style="margin-bottom: 20px;">
      <h2>健康探测配置 ⏱️</h2>
      <div style="display: flex; align-items: center; gap: 12px; margin-top: 12px; flex-wrap: wrap;">
        <label style="font-weight: 600;">探测间隔：</label>
        <input type="number" v-model.number="healthInterval" min="1" max="1440" style="width: 80px;" /> 分钟
        <button class="btn btn-primary btn-sm" @click="saveHealthInterval" :disabled="savingHealth">保存</button>
        <span v-if="healthSaved" style="color: var(--success); font-size: 13px;">已保存</span>
      </div>
    </div>
    <div class="card">
      <h2>快速开始 🚀</h2>
      <div class="alert alert-info">
        <ol>
          <li>先在 <router-link to="/providers">服务商</router-link> 添加服务商并配置 API 密钥</li>
          <li>在 <router-link to="/models">模型</router-link> 刷新模型列表，确认 auto 开关正确</li>
          <li>在 <router-link to="/playground">Playground</router-link> 测试对话</li>
        </ol>
      </div>
      <p><strong>使用方式：</strong></p>
      <pre style="background: var(--gray-100); padding: 12px; border-radius: 6px; overflow-x: auto;">
from openai import OpenAI
client = OpenAI(
    base_url="http://localhost:8000/v1",
    api_key="aigate-local"
)
response = client.chat.completions.create(
    model="auto",  # 自动选最优模型
    messages=[{"role": "user", "content": "你好！"}]
)
print(response.choices[0].message.content)
      </pre>
    </div>
  </div>
</template>
<script>
import api from '../api.js'
export default {
  name: 'Dashboard',
  data() {
    return {
      summary: null,
      currentModel: null,
      loading: false,
      healthInterval: 5,
      savingHealth: false,
      healthSaved: false,
      aigateKey: '',
      showAIGateKey: false
    }
  },
  computed: {
    total() {
      if (!this.summary) return 1
      return this.summary.total_models || 1
    },
    maskedKey() {
      if (!this.aigateKey) return '***'
      if (this.aigateKey.length <= 8) return '*'.repeat(this.aigateKey.length)
      return this.aigateKey.slice(0, 4) + '*'.repeat(Math.min(this.aigateKey.length - 8, 12)) + this.aigateKey.slice(-4)
    }
  },
  methods: {
    percent(value, total) {
      return Math.round((value / total) * 100)
    },
    formatTime(ts) {
      if (!ts) return '-'
      const normalized = typeof ts === 'string' && !/[zZ]|[+-]\d{2}:?\d{2}$/.test(ts) ? ts + 'Z' : ts
      const date = new Date(normalized)
      if (Number.isNaN(date.getTime())) return ts
      return date.toLocaleString('zh-CN', { timeZone: 'Asia/Shanghai', hour12: false })
    },
    async load() {
      this.loading = true
      try {
        const [summary] = await Promise.all([
          api.getDashboard()
        ])
        this.summary = summary
      } catch (e) { console.error('dashboard load', e) }
      try {
        const hc = await api.getHealthConfig()
        this.healthInterval = hc.interval_minutes || 5
      } catch (e) { console.error('health config load', e) }
      try {
        this.currentModel = await api.getCurrentModel()
      } catch (e) { console.error('current model load', e) }
      try {
        const keyData = await api.getAIGateKey(true)
        this.aigateKey = keyData.key || keyData || ''
      } catch (e) { console.error('aigate key load', e) }
      this.loading = false
    },
    async saveHealthInterval() {
      this.savingHealth = true; this.healthSaved = false
      try {
        await api.updateHealthConfig({ interval_minutes: this.healthInterval })
        this.healthSaved = true
      } catch (e) { alert('保存失败: ' + e.message) }
      finally { this.savingHealth = false }
    },
    toggleAIGateKey() {
      this.showAIGateKey = !this.showAIGateKey
    },
    async copyAIGateKey() {
      try {
        await navigator.clipboard.writeText(this.aigateKey)
        alert('已复制到剪贴板')
      } catch {
        const ta = document.createElement('textarea')
        ta.value = this.aigateKey
        document.body.appendChild(ta)
        ta.select()
        document.execCommand('copy')
        document.body.removeChild(ta)
        alert('已复制到剪贴板')
      }
    }
  },
  mounted() {
    this.load()
  }
}
</script>
<style scoped>
.status-stats {
  margin-top: 12px;
}
.status-row {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-bottom: 12px;
}
.status-row span {
  width: 80px;
  font-size: 14px;
}
.bar-container {
  flex: 1;
  height: 24px;
  background: var(--gray-100);
  border-radius: 12px;
  overflow: hidden;
}
.bar {
  height: 100%;
  border-radius: 12px;
  transition: width 0.3s;
}
.bar.healthy { background: var(--success); }
.bar.degraded { background: var(--warning); }
.bar.rate_limited { background: var(--info); }
.bar.unhealthy { background: var(--danger); }
.count {
  font-weight: bold;
  min-width: 30px;
  text-align: right;
}
</style>
