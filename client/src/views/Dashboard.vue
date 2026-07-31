<template>
  <div>
    <PageHeader title="仪表盘" icon="dashboard" subtitle="网关总览：服务商、模型、健康分布与接入方式">
      <template #actions>
        <button class="btn btn-outline" :disabled="loading" @click="load">
          <AppIcon name="refresh" :size="14" />
          {{ loading ? '刷新中…' : '刷新' }}
        </button>
      </template>
    </PageHeader>

    <!-- 概览指标 -->
    <div class="stats-grid">
      <StatCard label="服务商" icon="server" :value="stat('total_providers')" />
      <StatCard label="已配置密钥" icon="key" :value="stat('total_keys')" />
      <StatCard label="启用模型" icon="cpu" :value="stat('total_models')" />
      <StatCard label="Auto 候选" icon="scale" :value="stat('auto_candidates')" />
    </div>

    <div class="dash-grid">
      <!-- 健康分布 -->
      <section class="card">
        <div class="card-header">
          <span class="card-title"><AppIcon name="activity" :size="16" />健康状态分布</span>
          <span class="text-sm text-muted">Auto 候选 {{ healthTotal }} 个</span>
        </div>

        <div v-if="healthTotal > 0" class="health-rows">
          <div v-for="h in healthRows" :key="h.key" class="health-row">
            <span class="health-name">
              <span class="dot" :style="{ color: h.color }"></span>{{ h.label }}
            </span>
            <div class="progress">
              <div class="progress-bar" :style="{ width: h.pct + '%', background: h.color }"></div>
            </div>
            <span class="health-count tabular">{{ h.value }}</span>
            <span class="health-pct text-sm text-muted tabular">{{ h.pct }}%</span>
          </div>
        </div>
        <EmptyState v-else icon="activity" title="暂无健康数据" hint="健康数据来自真实调用日志，先在 Playground 发几个请求" small />
      </section>

      <!-- Auto 当前最优 -->
      <section class="card">
        <div class="card-header">
          <span class="card-title"><AppIcon name="star" :size="16" />Auto 当前最优</span>
          <router-link to="/auto" class="text-sm">查看排名</router-link>
        </div>

        <div v-if="currentModel && currentModel.provider" class="best-model">
          <div class="best-id mono">{{ currentModel.provider }}/{{ currentModel.model }}</div>
          <div v-if="currentModel.display_name" class="text-sm text-muted">{{ currentModel.display_name }}</div>
          <div class="best-tags">
            <span v-if="currentModel.final_score != null" class="badge badge-success">
              {{ Number(currentModel.final_score).toFixed(1) }} 分
            </span>
            <span v-if="currentModel.excluded_reason" class="badge badge-warning">
              {{ currentModel.excluded_reason }}
            </span>
          </div>
        </div>
        <EmptyState v-else icon="scale" title="还没有 Auto 候选" hint="到「模型」页把要参与选举的模型打开 Auto 开关" small />
      </section>
    </div>

    <!-- 连接密钥 -->
    <section class="card">
      <div class="card-header">
        <span class="card-title"><AppIcon name="key" :size="16" />AIGate 连接密钥</span>
        <div class="row">
          <code class="key-display">{{ showKey ? aigateKey || '未配置' : maskedKey }}</code>
          <button class="btn btn-outline btn-sm" :disabled="!aigateKey" @click="showKey = !showKey">
            <AppIcon :name="showKey ? 'eyeOff' : 'eye'" :size="13" />
            {{ showKey ? '隐藏' : '显示' }}
          </button>
          <button class="btn btn-outline btn-sm" :disabled="!aigateKey" @click="copyKey">
            <AppIcon name="copy" :size="13" />复制
          </button>
        </div>
      </div>
      <p class="text-sm text-muted">
        所有请求需在 <code>Authorization: Bearer &lt;key&gt;</code> 头中携带此密钥。
        密钥在 <code>config.yaml</code> 的 <code>security.aigate_api_key</code> 配置。
      </p>
    </section>

    <!-- 快速开始 -->
    <section class="card">
      <div class="card-header">
        <span class="card-title"><AppIcon name="play" :size="16" />快速开始</span>
      </div>
      <ol class="quick-steps">
        <li><router-link to="/providers">服务商</router-link>页添加服务商并配置 API 密钥</li>
        <li><router-link to="/models">模型</router-link>页刷新模型列表，确认 Auto 开关</li>
        <li><router-link to="/playground">Playground</router-link>发一条消息验证链路</li>
      </ol>
      <div class="code-head">
        <span class="text-sm text-muted">OpenAI SDK 接入</span>
        <button class="btn btn-ghost btn-xs" @click="copy(usageCode)">
          <AppIcon name="copy" :size="12" />复制
        </button>
      </div>
      <pre class="code-block">{{ usageCode }}</pre>
    </section>
  </div>
</template>

<script>
import api from '../api.js'
import toast from '../toast.js'
import AppIcon from '../components/AppIcon.vue'
import PageHeader from '../components/PageHeader.vue'
import StatCard from '../components/StatCard.vue'
import EmptyState from '../components/EmptyState.vue'

export default {
  name: 'Dashboard',
  components: { AppIcon, PageHeader, StatCard, EmptyState },
  data() {
    return {
      summary: null,
      currentModel: null,
      loading: false,
      aigateKey: '',
      showKey: false,
      usageCode: `from openai import OpenAI

client = OpenAI(
    base_url="http://localhost:8000/v1",
    api_key="<你的 AIGate 密钥>",
)

resp = client.chat.completions.create(
    model="auto",          # 交给 AIGate 选最优模型
    messages=[{"role": "user", "content": "你好！"}],
)
print(resp.choices[0].message.content)`,
    }
  },
  computed: {
    /** Auto 候选总数：健康分布的分母应是四种状态之和，而不是"启用模型总数" */
    healthTotal() {
      const s = this.summary
      if (!s) return 0
      return (
        (s.healthy_models || 0) +
        (s.degraded_models || 0) +
        (s.rate_limited_models || 0) +
        (s.unhealthy_models || 0)
      )
    },
    healthRows() {
      const s = this.summary || {}
      const rows = [
        { key: 'healthy', label: '健康', value: s.healthy_models || 0, color: 'var(--success)' },
        { key: 'degraded', label: '延迟', value: s.degraded_models || 0, color: 'var(--warning)' },
        { key: 'rate_limited', label: '限流', value: s.rate_limited_models || 0, color: 'var(--info)' },
        { key: 'unhealthy', label: '故障', value: s.unhealthy_models || 0, color: 'var(--danger)' },
      ]
      const total = this.healthTotal || 1
      return rows.map((r) => ({ ...r, pct: Math.round((r.value / total) * 100) }))
    },
    maskedKey() {
      const k = this.aigateKey
      if (!k) return '未配置'
      if (k.length <= 8) return '•'.repeat(k.length)
      return k.slice(0, 4) + '•'.repeat(Math.min(k.length - 8, 12)) + k.slice(-4)
    },
  },
  methods: {
    /** summary 在首次渲染时还是 null，模板里一律走这里取值，避免读 null 属性直接把整页渲染打崩 */
    stat(key) {
      return this.summary ? this.summary[key] ?? 0 : '—'
    },
    async load() {
      this.loading = true
      const [summary, current, key] = await Promise.all([
        api.getDashboard().catch((e) => {
          toast.error('仪表盘数据加载失败：' + e.message)
          return null
        }),
        api.getCurrentModel().catch(() => null),
        api.getAIGateKey(true).catch(() => null),
      ])
      if (summary) this.summary = summary
      this.currentModel = current
      this.aigateKey = (key && key.key) || ''
      this.loading = false
    },
    async copy(text) {
      if (!text) return
      try {
        await navigator.clipboard.writeText(text)
        toast.success('已复制到剪贴板')
      } catch {
        const ta = document.createElement('textarea')
        ta.value = text
        ta.style.position = 'fixed'
        ta.style.opacity = '0'
        document.body.appendChild(ta)
        ta.select()
        try {
          document.execCommand('copy')
          toast.success('已复制到剪贴板')
        } catch {
          toast.error('复制失败，请手动选中复制')
        }
        document.body.removeChild(ta)
      }
    },
    copyKey() {
      this.copy(this.aigateKey)
    },
  },
  mounted() {
    this.load()
  },
}
</script>

<style scoped>
.dash-grid {
  display: grid;
  grid-template-columns: minmax(0, 1.35fr) minmax(0, 1fr);
  gap: var(--space-4);
  margin-bottom: var(--space-4);
}
.dash-grid .card { margin-bottom: 0; }

/* 健康分布 */
.health-rows { display: flex; flex-direction: column; gap: var(--space-3); }
.health-row {
  display: grid;
  grid-template-columns: 62px 1fr 34px 38px;
  align-items: center;
  gap: var(--space-3);
}
.health-name {
  display: inline-flex;
  align-items: center;
  gap: var(--space-2);
  font-size: var(--text-base);
  color: var(--text-secondary);
}
.health-count { font-weight: 600; text-align: right; }
.health-pct { text-align: right; }

/* Auto 最优 */
.best-model { display: flex; flex-direction: column; gap: var(--space-2); }
.best-id {
  font-size: var(--text-lg);
  font-weight: 600;
  color: var(--primary);
  word-break: break-all;
}
.best-tags { display: flex; gap: var(--space-2); flex-wrap: wrap; }

/* 密钥 */
.key-display {
  font-family: var(--font-mono);
  font-size: var(--text-sm);
  padding: 4px var(--space-2);
  max-width: 260px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

/* 快速开始 */
.quick-steps {
  margin: 0 0 var(--space-4);
  padding-left: var(--space-5);
  color: var(--text-secondary);
  font-size: var(--text-base);
  line-height: 2;
}
.code-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: var(--space-2);
}

@media (max-width: 1000px) {
  .dash-grid { grid-template-columns: 1fr; }
}
</style>
