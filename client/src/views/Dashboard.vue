<template>
  <div>
    <PageHeader title="仪表盘" icon="dashboard" subtitle="系统运行总览：实时指标、健康分布、失败告警与账号权益">
      <template #actions>
        <span class="text-sm text-muted" title="每 30 秒自动刷新（切后台暂停）">自动 30s</span>
        <button class="btn btn-outline" :disabled="loading" @click="load">
          <AppIcon name="refresh" :size="14" />
          {{ loading ? '刷新中…' : '刷新' }}
        </button>
      </template>
    </PageHeader>

    <!-- ① 核心运行指标（数据源 /live/summary.today） -->
    <div class="live-grid">
      <div class="live-card">
        <span class="dim">今日请求</span><strong>{{ fmtNum(t.requests) }}</strong>
      </div>
      <div class="live-card ok">
        <span class="dim">成功</span><strong>{{ fmtNum(t.success) }}</strong>
      </div>
      <div class="live-card" :class="{ err: t.errors > 0 }">
        <span class="dim">失败</span><strong>{{ fmtNum(t.errors) }}</strong>
      </div>
      <div class="live-card">
        <span class="dim">输入 Token</span><strong>{{ fmtNum(t.prompt_tokens) }}</strong>
      </div>
      <div class="live-card">
        <span class="dim">输出 Token</span><strong>{{ fmtNum(t.completion_tokens) }}</strong>
      </div>
      <div class="live-card">
        <span class="dim">今日成本</span><strong>${{ (t.cost_usd || 0).toFixed(4) }}</strong>
      </div>
      <div class="live-card">
        <span class="dim">进行中</span><strong>{{ fmtNum(t.pending) }}</strong>
      </div>
    </div>

    <!-- ② 系统规模 -->
    <div class="stats-grid">
      <StatCard label="服务商" icon="server" :value="stat('total_providers')" />
      <StatCard label="已配置密钥" icon="key" :value="stat('total_keys')" :sub="keySub" />
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

      <div class="dash-col">
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

        <!-- 组件状态摘要 -->
        <section class="card">
          <div class="card-header">
            <span class="card-title"><AppIcon name="gauge" :size="16" />组件状态</span>
            <router-link to="/analytics" class="text-sm">实时详情</router-link>
          </div>
          <div class="comp-facts">
            <div>
              <span class="text-sm text-muted">日志队列</span>
              <strong>待写 {{ fmtNum(lq.queued) }} / 丢弃 {{ fmtNum(lq.dropped) }}</strong>
            </div>
            <div>
              <span class="text-sm text-muted">响应缓存</span>
              <strong>{{ fmtNum(cache.size) }} 条 · 命中 {{ fmtNum(cache.hits) }}</strong>
            </div>
          </div>
        </section>
      </div>
    </div>

    <div class="dash-grid">
      <!-- 最近失败告警 -->
      <section class="card">
        <div class="card-header">
          <span class="card-title"><AppIcon name="alert" :size="16" />最近失败告警（24h）</span>
          <router-link to="/analytics" class="text-sm">查看全部 →</router-link>
        </div>
        <template v-if="failures">
          <div class="fail-summary">
            <span class="badge" :class="failures.total_errors > 0 ? 'badge-danger' : 'badge-success'">
              失败 {{ failures.total_errors }} 次
            </span>
            <span v-if="failures.latest_error_sample" class="fail-sample text-xs" :title="failures.latest_error_sample">
              {{ failures.latest_error_sample }}
            </span>
          </div>
          <table v-if="errRows.length" class="fail-table">
            <thead><tr><th>时间</th><th>请求模型</th><th>路由到</th><th>错误</th></tr></thead>
            <tbody>
              <tr v-for="r in errRows" :key="r.id">
                <td class="dim">{{ shortTime(r.created_at) }}</td>
                <td class="mono">{{ r.requested_model || '—' }}</td>
                <td class="dim mono">{{ r.routed_provider || '' }}/{{ r.routed_model || '' }}</td>
                <td class="err-text" :title="r.error_msg">{{ r.error_type || (r.error_msg || '—').slice(0, 60) }}</td>
              </tr>
            </tbody>
          </table>
          <p v-else style="color: var(--gray-500); font-size: 13px; padding: 8px 0;">近 24 小时无失败请求 🎉</p>
        </template>
        <EmptyState v-else icon="alert" title="失败数据加载中" small />
      </section>

      <!-- 账号权益摘要（只读展示，不带写操作） -->
      <section class="card">
        <div class="card-header">
          <span class="card-title"><AppIcon name="gift" :size="16" />账号权益摘要</span>
          <router-link to="/providers/checkin" class="text-sm">签到监控 →</router-link>
        </div>
        <template v-if="checkin">
          <div class="eq-summary">
            今日已领 <strong>{{ checkin.summary.done_today }}</strong> / {{ checkin.summary.total_accounts }} 账号
            · 合计 <strong class="eq-credit">+{{ checkin.summary.credit_today }}</strong> 积分
          </div>
          <div class="eq-rows">
            <div v-for="p in checkin.providers" :key="p.provider_code" class="eq-row">
              <span class="eq-code mono">{{ p.provider_code }}</span>
              <span class="badge" :class="p.done_today >= p.total_accounts ? 'badge-success' : (p.done_today > 0 ? 'badge-warning' : 'badge-danger')">
                {{ p.done_today }}/{{ p.total_accounts }}
              </span>
              <span class="text-sm text-muted">+{{ p.credit_today }}</span>
              <span v-if="p.supported === false" class="badge badge-neutral" title="上游未提供签到接口">不支持</span>
            </div>
            <p v-if="!checkin.providers.length" style="color: var(--gray-500); font-size: 13px;">
              暂无已连接账号，到「签到与成长」页连接后显示
            </p>
          </div>
        </template>
        <EmptyState v-else icon="gift" title="暂无权益数据" hint="到「签到与成长」页连接账号后显示" small />
      </section>
    </div>
  </div>
</template>

<script>
import api from '../api.js'
import toast from '../toast.js'
import AppIcon from '../components/AppIcon.vue'
import PageHeader from '../components/PageHeader.vue'
import StatCard from '../components/StatCard.vue'
import EmptyState from '../components/EmptyState.vue'

// 运行总览页：实时指标 + 规模 + 健康/Auto + 失败告警 + 权益摘要（全部只读）。
// 接入引导（AIGate 密钥 / SDK 示例）已迁至设置页「接入信息」卡。
export default {
  name: 'Dashboard',
  components: { AppIcon, PageHeader, StatCard, EmptyState },
  data() {
    return {
      live: {},
      summary: null,
      currentModel: null,
      failures: null,
      errRows: [],
      checkin: null,
      loading: false,
      timer: null,
    }
  },
  computed: {
    /** /live/summary.today 兜底（首次渲染时 live 还是 {}） */
    t() { return this.live.today || {} },
    lq() { return this.live.log_queue || {} },
    cache() { return this.live.cache || {} },
    /** 密钥口径拆分说明：总数 vs 启用数 vs 已关联模型数 */
    keySub() {
      const s = this.summary
      if (!s || s.active_keys == null) return ''
      return `启用 ${s.active_keys} · 关联模型 ${s.associated_keys ?? 0}`
    },
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
  },
  methods: {
    /** summary 在首次渲染时还是 null，模板里一律走这里取值，避免读 null 属性直接把整页渲染打崩 */
    stat(key) {
      return this.summary ? this.summary[key] ?? 0 : '—'
    },
    fmtNum(n) { return Number(n || 0).toLocaleString('zh-CN') },
    shortTime(v) { return v ? String(v).slice(11, 19) : '—' },
    async load() {
      this.loading = true
      const [live, summary, current, failures, errs, checkin] = await Promise.all([
        api.getLiveSummary().catch(() => null),
        api.getDashboard().catch((e) => {
          toast.error('仪表盘数据加载失败：' + e.message)
          return null
        }),
        api.getCurrentModel().catch(() => null),
        api.getFailures(24).catch(() => null),
        api.getLogs({ page: 1, page_size: 8, status: 'error' }).catch(() => null),
        api.getCheckinOverview(false).catch(() => null),   // 权益摘要不带额度查询（重），额度在签到页看
      ])
      if (live) this.live = live
      if (summary) this.summary = summary
      this.currentModel = current
      this.failures = failures
      this.errRows = (errs && errs.items) || []
      this.checkin = checkin
      this.loading = false
    },
  },
  mounted() {
    this.load()
    this.timer = setInterval(() => { if (!document.hidden) this.load() }, 30000)
  },
  beforeUnmount() {
    if (this.timer) clearInterval(this.timer)
  },
}
</script>

<style scoped>
/* ① 运行指标行 */
.live-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(130px, 1fr));
  gap: var(--space-3);
  margin-bottom: var(--space-4);
}
.live-card {
  display: flex;
  flex-direction: column;
  gap: 6px;
  background: var(--surface-2);
  border: 1px solid var(--border-base);
  border-radius: var(--radius-lg);
  padding: var(--space-4);
}
.live-card strong { font-size: 1.3rem; font-variant-numeric: tabular-nums; }
.live-card.ok strong { color: #22c55e; }
.live-card.err strong { color: #ef4444; }
.live-card .dim { color: var(--text-dim); font-size: var(--text-xs); }

.stats-grid { margin-bottom: var(--space-4); }

.dash-grid {
  display: grid;
  grid-template-columns: minmax(0, 1.35fr) minmax(0, 1fr);
  gap: var(--space-4);
  margin-bottom: var(--space-4);
}
.dash-grid .card { margin-bottom: 0; }
.dash-col { display: flex; flex-direction: column; gap: var(--space-4); }
.dash-col .card { margin-bottom: 0; flex: 1; }

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

/* 组件状态 */
.comp-facts { display: flex; flex-direction: column; gap: var(--space-3); }
.comp-facts div { display: flex; flex-direction: column; gap: 2px; }

/* 失败告警 */
.fail-summary { display: flex; gap: 10px; align-items: center; margin-bottom: 10px; }
.fail-sample {
  color: var(--gray-500);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  max-width: 360px;
}
.fail-table { width: 100%; border-collapse: collapse; font-size: 13px; }
.fail-table th, .fail-table td { padding: 6px 8px; text-align: left; border-bottom: 1px solid var(--border-soft); max-width: 200px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.fail-table th { color: var(--text-muted); font-weight: 500; font-size: 12px; }
.fail-table .err-text { color: #ef4444; font-size: 12px; }
.fail-table .dim { color: var(--text-dim); font-size: 12px; }
.mono { font-family: var(--font-mono); font-size: 12px; }

/* 权益摘要 */
.eq-summary { margin-bottom: var(--space-3); font-size: var(--text-base); color: var(--text-secondary); }
.eq-credit { color: var(--success); }
.eq-rows { display: flex; flex-direction: column; gap: var(--space-2); }
.eq-row { display: flex; align-items: center; gap: var(--space-3); }
.eq-code { font-size: var(--text-sm); min-width: 130px; }

@media (max-width: 1000px) {
  .dash-grid { grid-template-columns: 1fr; }
}
</style>
