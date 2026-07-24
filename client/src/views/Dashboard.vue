<template>
  <div>
    <div class="dash-header">
      <h1>仪表盘</h1>
      <span class="dash-sub">请求量、延迟、Token 用量和失败统计</span>
    </div>

    <n-grid cols="2 s:2 m:4" responsive="screen" gap="16" v-if="summary" style="margin-bottom: 20px;">
      <n-gi>
        <n-card hoverable>
          <n-statistic label="服务商总数" :value="summary.total_providers" />
        </n-card>
      </n-gi>
      <n-gi>
        <n-card hoverable>
          <n-statistic label="已配置密钥" :value="summary.total_keys" />
        </n-card>
      </n-gi>
      <n-gi>
        <n-card hoverable>
          <n-statistic label="启用模型" :value="summary.total_models" />
        </n-card>
      </n-gi>
      <n-gi>
        <n-card hoverable>
          <n-statistic label="Auto 候选" :value="summary.auto_candidates" />
        </n-card>
      </n-gi>
    </n-grid>

    <n-card title="AIGate 连接密钥" hoverable style="margin-bottom: 20px;" size="small">
      <template #header-extra>
        <n-space align="center" size="small">
          <n-text code>{{ showAIGateKey ? aigateKey : maskedKey }}</n-text>
          <n-button size="small" tertiary @click="toggleAIGateKey">
            {{ showAIGateKey ? '隐藏' : '显示' }}
          </n-button>
          <n-button size="small" tertiary type="primary" @click="copyAIGateKey" :disabled="!aigateKey">
            复制
          </n-button>
        </n-space>
      </template>
      <n-text depth="3" style="font-size: 13px;">
        使用此密钥连接 AIGate 网关，所有请求需在 <n-text code>Authorization: Bearer &lt;key&gt;</n-text> 中携带此密钥。
      </n-text>
    </n-card>

    <n-card v-if="currentModel && currentModel.provider" title="Auto 排名第一" hoverable style="margin-bottom: 20px;" size="small">
      <n-space align="center" size="small">
        <n-text strong type="primary" style="font-size: 18px;">
          {{ currentModel.provider }}/{{ currentModel.model }}
        </n-text>
        <n-text depth="3" v-if="currentModel.display_name">({{ currentModel.display_name }})</n-text>
        <n-tag v-if="currentModel.final_score != null" type="success" size="small" round>
          {{ Number(currentModel.final_score).toFixed(1) }} 分
        </n-tag>
        <n-tag v-if="currentModel.excluded_reason" type="warning" size="small" round>
          {{ currentModel.excluded_reason }}
        </n-tag>
      </n-space>
    </n-card>

    <n-card title="健康状态分布" hoverable style="margin-bottom: 20px;" size="small">
      <div class="status-row">
        <span>健康</span>
        <div class="bar-container">
          <div class="bar healthy" :style="{width: percent(summary.healthy_models, total) + '%'}"></div>
        </div>
        <span class="count">{{ summary.healthy_models }}</span>
      </div>
      <div class="status-row">
        <span>延迟</span>
        <div class="bar-container">
          <div class="bar degraded" :style="{width: percent(summary.degraded_models, total) + '%'}"></div>
        </div>
        <span class="count">{{ summary.degraded_models }}</span>
      </div>
      <div class="status-row">
        <span>限流</span>
        <div class="bar-container">
          <div class="bar rate_limited" :style="{width: percent(summary.rate_limited_models, total) + '%'}"></div>
        </div>
        <span class="count">{{ summary.rate_limited_models }}</span>
      </div>
      <div class="status-row">
        <span>故障</span>
        <div class="bar-container">
          <div class="bar unhealthy" :style="{width: percent(summary.unhealthy_models, total) + '%'}"></div>
        </div>
        <span class="count">{{ summary.unhealthy_models }}</span>
      </div>
    </n-card>

    <n-card title="快速开始" hoverable size="small">
      <n-alert type="info" :show-icon="false" style="margin-bottom: 12px;">
        <ol style="margin: 0; padding-left: 16px;">
          <li>先在 <router-link to="/providers">服务商</router-link> 添加服务商并配置 API 密钥</li>
          <li>在 <router-link to="/models">模型</router-link> 刷新模型列表，确认 auto 开关正确</li>
          <li>在 <router-link to="/playground">Playground</router-link> 测试对话</li>
        </ol>
      </n-alert>
      <n-text strong>使用方式：</n-text>
      <n-code :code="usageCode" language="python" style="margin-top: 8px;" />
    </n-card>
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
      aigateKey: '',
      showAIGateKey: false,
      usageCode: `from openai import OpenAI
client = OpenAI(
    base_url="http://localhost:8000/v1",
    api_key="aigate-local"
)
response = client.chat.completions.create(
    model="auto",  # 自动选最优模型
    messages=[{"role": "user", "content": "你好！"}]
)
print(response.choices[0].message.content)`
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
    async load() {
      this.loading = true
      try {
        this.summary = await api.getDashboard()
      } catch (e) { console.error('dashboard load', e) }
      try {
        this.currentModel = await api.getCurrentModel()
      } catch (e) { console.error('current model load', e) }
      try {
        const keyData = await api.getAIGateKey(true)
        this.aigateKey = keyData.key || keyData || ''
      } catch (e) { console.error('aigate key load', e) }
      this.loading = false
    },
    toggleAIGateKey() {
      this.showAIGateKey = !this.showAIGateKey
    },
    async copyAIGateKey() {
      try {
        await navigator.clipboard.writeText(this.aigateKey)
      } catch {
        const ta = document.createElement('textarea')
        ta.value = this.aigateKey
        document.body.appendChild(ta)
        ta.select()
        document.execCommand('copy')
        document.body.removeChild(ta)
      }
    }
  },
  mounted() {
    this.load()
  }
}
</script>
<style scoped>
.dash-header {
  margin-bottom: 20px;
}
.dash-header h1 {
  margin: 0 0 4px 0;
  font-size: 22px;
  font-weight: 600;
}
.dash-sub {
  font-size: 13px;
  color: var(--text-muted);
}
.status-row {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-bottom: 12px;
}
.status-row span {
  width: 60px;
  font-size: 14px;
}
.bar-container {
  flex: 1;
  height: 24px;
  background: var(--surface-1);
  border-radius: 12px;
  overflow: hidden;
}
.bar {
  height: 100%;
  border-radius: 12px;
  transition: width 0.3s;
}
.bar.healthy { background: #10b981; }
.bar.degraded { background: #f59e0b; }
.bar.rate_limited { background: #3b82f6; }
.bar.unhealthy { background: #ef4444; }
.count {
  font-weight: 600;
  min-width: 30px;
  text-align: right;
}
</style>
