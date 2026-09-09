<template>
  <div class="monitor-page">
    <div class="page-header">
      <div>
        <h1>实时监控</h1>
        <p>今日概况 / 冷却中模型 / 最近请求（每 3 秒自动刷新）</p>
      </div>
      <span class="pill" :class="paused ? 'muted' : 'ok'">{{ paused ? '已暂停' : '实时' }}</span>
    </div>

    <div class="stat-grid">
      <div class="stat-card"><span class="dim">今日请求</span><strong>{{ fmtNum(d.requests) }}</strong></div>
      <div class="stat-card ok"><span class="dim">成功</span><strong>{{ fmtNum(d.success) }}</strong></div>
      <div class="stat-card" :class="{ err: d.errors > 0 }"><span class="dim">失败</span><strong>{{ fmtNum(d.errors) }}</strong></div>
      <div class="stat-card"><span class="dim">输入 Token</span><strong>{{ fmtNum(d.prompt_tokens) }}</strong></div>
      <div class="stat-card"><span class="dim">输出 Token</span><strong>{{ fmtNum(d.completion_tokens) }}</strong></div>
      <div class="stat-card"><span class="dim">今日成本</span><strong>${{ (d.cost_usd || 0).toFixed(4) }}</strong></div>
      <div class="stat-card"><span class="dim">进行中</span><strong>{{ fmtNum(d.pending) }}</strong></div>
      <div class="stat-card"><span class="dim">冷却中模型</span><strong>{{ cooling.length }}</strong></div>
      <div class="stat-card"><span class="dim">缓存命中</span><strong>{{ fmtNum(cache.hits) }}</strong></div>
    </div>

    <section class="panel">
      <h2>冷却中模型</h2>
      <table v-if="cooling.length">
        <thead><tr><th>模型</th><th>服务商</th><th>恢复时间</th></tr></thead>
        <tbody>
          <tr v-for="c in cooling" :key="c.model_id">
            <td><code>{{ c.model_id_str }}</code></td>
            <td class="dim">{{ c.provider || '—' }}</td>
            <td class="dim">{{ c.until }}</td>
          </tr>
        </tbody>
      </table>
      <div v-else class="empty">没有模型处于冷却状态 ✅</div>
    </section>

    <section class="panel">
      <h2>最近请求</h2>
      <table v-if="recent.length">
        <thead><tr><th>时间</th><th>请求模型</th><th>路由到</th><th>状态</th><th>延迟</th><th>错误</th></tr></thead>
        <tbody>
          <tr v-for="r in recent" :key="r.id">
            <td class="dim">{{ shortTime(r.created_at) }}</td>
            <td>{{ r.requested_model || '—' }}</td>
            <td><code>{{ r.routed_provider || '' }}/{{ r.routed_model || '' }}</code></td>
            <td><span class="pill" :class="r.status === 'success' ? 'ok' : 'error'">{{ r.status === 'success' ? '成功' : '失败' }}</span></td>
            <td>{{ r.latency_ms != null ? r.latency_ms + 'ms' : '—' }}<span v-if="r.ttft_ms" class="dim"> / 首字 {{ r.ttft_ms }}ms</span></td>
            <td class="err-text" :title="r.error">{{ r.error || '—' }}</td>
          </tr>
        </tbody>
      </table>
      <div v-else class="empty">暂无请求</div>
    </section>

    <section class="panel">
      <h2>组件状态</h2>
      <div class="comp-grid">
        <div><span class="dim">日志队列</span><strong>待写 {{ lq.queued || 0 }} / 已写 {{ lq.flushed || 0 }} / 丢弃 {{ lq.dropped || 0 }}</strong></div>
        <div><span class="dim">响应缓存</span><strong>{{ cache.size || 0 }} 条（命中 {{ fmtNum(cache.hits) }}）</strong></div>
        <div><span class="dim">服务器时间</span><strong>{{ d.server_time || '—' }}</strong></div>
      </div>
    </section>
  </div>
</template>

<script>
import api from '../api'

export default {
  name: 'Monitor',
  data() {
    return { d: {}, cooling: [], recent: [], lq: {}, cache: {}, paused: false, timer: null }
  },
  mounted() {
    this.load()
    this.timer = setInterval(() => { if (!document.hidden) this.load() }, 3000)
  },
  beforeUnmount() { if (this.timer) clearInterval(this.timer) },
  methods: {
    async load() {
      try {
        const r = await api.getLiveSummary()
        this.d = r.today || {}
        this.cooling = r.cooling || []
        this.recent = r.recent || []
        this.lq = r.log_queue || {}
        this.cache = r.cache || {}
        this.paused = false
      } catch (e) { this.paused = true }
    },
    shortTime(v) { return v ? String(v).slice(11, 19) : '—' },
    fmtNum(n) { return Number(n || 0).toLocaleString('zh-CN') },
  },
}
</script>

<style scoped>
.monitor-page { display: flex; flex-direction: column; gap: var(--space-4); }
.page-header { display: flex; justify-content: space-between; align-items: flex-start; }
.page-header h1 { margin: 0 0 4px; font-size: var(--text-2xl, 1.5rem); }
.page-header p { margin: 0; color: var(--text-muted); font-size: var(--text-sm); }
.stat-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(140px, 1fr)); gap: var(--space-3); }
.stat-card { background: var(--surface-2); border: 1px solid var(--border-base); border-radius: var(--radius-lg); padding: var(--space-4); display: flex; flex-direction: column; gap: 6px; }
.stat-card strong { font-size: 1.3rem; font-variant-numeric: tabular-nums; }
.stat-card.ok strong { color: #22c55e; }
.stat-card.err strong { color: #ef4444; }
.panel { background: var(--surface-2); border: 1px solid var(--border-base); border-radius: var(--radius-lg); padding: var(--space-4); overflow-x: auto; }
.panel h2 { margin: 0 0 var(--space-3); font-size: var(--text-lg, 1.1rem); }
table { width: 100%; border-collapse: collapse; font-size: var(--text-sm); }
th, td { padding: 8px 10px; text-align: left; border-bottom: 1px solid var(--border-base); white-space: nowrap; max-width: 320px; overflow: hidden; text-overflow: ellipsis; }
th { color: var(--text-muted); font-weight: 500; font-size: var(--text-xs); }
.comp-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(220px, 1fr)); gap: var(--space-3); }
.comp-grid div { display: flex; flex-direction: column; gap: 4px; background: var(--surface-1); border-radius: 8px; padding: var(--space-3); }
.pill { padding: 2px 8px; border-radius: 999px; font-size: var(--text-xs); }
.pill.ok { background: rgba(34, 197, 94, .15); color: #22c55e; }
.pill.muted { background: rgba(148, 163, 184, .15); color: #94a3b8; }
.pill.error { background: rgba(239, 68, 68, .15); color: #ef4444; }
.err-text { color: #ef4444; font-size: var(--text-xs); white-space: normal; }
.empty { padding: var(--space-5); text-align: center; color: var(--text-dim); font-size: var(--text-sm); }
.dim { color: var(--text-dim); font-size: var(--text-xs); }
code { font-family: var(--font-mono, monospace); font-size: var(--text-xs); }
</style>
