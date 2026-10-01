<template>
  <section id="realtime" class="card rt-panel">
    <div class="rt-head">
      <h2>实时</h2>
      <div class="rt-pills">
        <span class="pill info">进行中 {{ fmtNum(d.pending) }}</span>
        <span class="pill" :class="cooling.length ? 'warn' : 'ok'">冷却 {{ cooling.length }}</span>
        <span class="pill" :class="paused ? 'muted' : 'ok'">{{ paused ? '已暂停' : '实时 3s' }}</span>
      </div>
    </div>

    <div class="rt-grid">
      <div><span class="dim">今日请求</span><strong>{{ fmtNum(d.requests) }}</strong></div>
      <div class="okc"><span class="dim">成功</span><strong>{{ fmtNum(d.success) }}</strong></div>
      <div :class="{ errc: d.errors > 0 }"><span class="dim">失败</span><strong>{{ fmtNum(d.errors) }}</strong></div>
      <div><span class="dim">输入 Token</span><strong>{{ fmtNum(d.prompt_tokens) }}</strong></div>
      <div><span class="dim">输出 Token</span><strong>{{ fmtNum(d.completion_tokens) }}</strong></div>
      <div><span class="dim">今日成本</span><strong>${{ (d.cost_usd || 0).toFixed(4) }}</strong></div>
      <div><span class="dim">缓存命中</span><strong>{{ fmtNum(cache.hits) }}</strong></div>
    </div>

    <h3 class="rt-sub">冷却中模型
      <router-link to="/health" class="rt-more">健康页查看全部 →</router-link>
    </h3>
    <table v-if="cooling.length">
      <thead><tr><th>模型</th><th>服务商</th><th>恢复时间</th></tr></thead>
      <tbody>
        <tr v-for="c in cooling.slice(0, 3)" :key="c.model_id">
          <td><code>{{ c.model_id_str }}</code></td>
          <td class="dim">{{ c.provider || '—' }}</td>
          <td class="dim">{{ c.until }}</td>
        </tr>
      </tbody>
    </table>
    <div v-else class="empty">没有模型处于冷却状态 ✅</div>

    <h3 class="rt-sub">最近请求</h3>
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

    <div class="comp-row">
      <span class="dim">日志队列：待写 {{ lq.queued || 0 }} / 已写 {{ lq.flushed || 0 }} / 丢弃 {{ lq.dropped || 0 }}</span>
      <span class="dim">响应缓存：{{ cache.size || 0 }} 条（命中 {{ fmtNum(cache.hits) }}）</span>
      <span class="dim">服务器时间：{{ d.server_time || '—' }}</span>
    </div>
  </section>
</template>

<script>
import api from '../api.js'

// 原实时监控页（Monitor.vue）的面板主体：轻量尾随视图，自管 3s 轮询。
// 重查询（筛选/详情/归档）在日志区（LogsPanel），两者职责不同。
export default {
  name: 'RealtimePanel',
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
.rt-panel { margin-top: 20px; }
.rt-head { display: flex; justify-content: space-between; align-items: center; margin-bottom: 12px; }
.rt-head h2 { margin: 0; }
.rt-pills { display: flex; gap: 8px; }
.rt-grid {
  display: grid; grid-template-columns: repeat(auto-fill, minmax(130px, 1fr));
  gap: var(--space-3, 12px); margin-bottom: 14px;
}
.rt-grid > div {
  display: flex; flex-direction: column; gap: 4px;
  background: var(--bg-card, var(--surface-2)); border: 1px solid var(--border-soft, var(--border-base));
  border-radius: 10px; padding: 10px 12px;
}
.rt-grid strong { font-size: 1.15rem; font-variant-numeric: tabular-nums; }
.okc strong { color: #22c55e; }
.errc strong { color: #ef4444; }
.rt-sub { font-size: 13px; color: var(--text-muted); margin: 14px 0 8px; display: flex; align-items: center; gap: 12px; }
.rt-more { font-size: 12px; font-weight: 400; }
.comp-row { display: flex; gap: 18px; flex-wrap: wrap; margin-top: 14px; padding-top: 10px; border-top: 1px solid var(--border-soft); }
table { width: 100%; border-collapse: collapse; font-size: var(--text-sm, 13px); }
th, td { padding: 7px 10px; text-align: left; border-bottom: 1px solid var(--border-soft); white-space: nowrap; max-width: 320px; overflow: hidden; text-overflow: ellipsis; }
th { color: var(--text-muted); font-weight: 500; font-size: var(--text-xs, 12px); }
.err-text { color: #ef4444; font-size: var(--text-xs, 12px); white-space: normal; }
.empty { padding: 14px; text-align: center; color: var(--text-dim, var(--gray-500)); font-size: var(--text-sm, 13px); }
.dim { color: var(--text-dim, var(--gray-500)); font-size: var(--text-xs, 12px); }
code { font-family: var(--font-mono, monospace); font-size: var(--text-xs, 12px); }
.pill { padding: 2px 8px; border-radius: 999px; font-size: var(--text-xs, 12px); }
.pill.ok { background: rgba(34, 197, 94, .15); color: #22c55e; }
.pill.muted { background: rgba(148, 163, 184, .15); color: #94a3b8; }
.pill.error { background: rgba(239, 68, 68, .15); color: #ef4444; }
.pill.info { background: rgba(59, 130, 246, .15); color: #3b82f6; }
.pill.warn { background: rgba(245, 158, 11, .15); color: #f59e0b; }
</style>
