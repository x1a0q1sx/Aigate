<template>
  <div class="decision-page">
    <PageHeader title="路由决策" icon="route" :subtitle="`最近 ${filters.window_hours} 小时`">
      <template #actions>
        <button class="btn btn-outline" :disabled="loading" @click="load">
          <AppIcon name="refresh" :size="14" />{{ loading ? '刷新中…' : '刷新' }}
        </button>
      </template>
    </PageHeader>

    <div class="summary-grid">
      <StatCard label="决策数" icon="route" :value="summary.total ?? 0" />
      <StatCard label="成功率" icon="checkCircle" :value="pct(summary.success_rate)" />
      <StatCard label="回退率" icon="layers" :value="pct(summary.fallback_rate)" />
      <StatCard label="平均决策耗时" icon="clock" :value="duration(summary.avg_decision_ms)" />
    </div>

    <div class="filters" aria-label="决策筛选">
      <div class="search-field">
        <AppIcon name="search" :size="14" />
        <input v-model.trim="filters.q" placeholder="请求模型、服务商、模型或 Trace ID" @keyup.enter="applyFilters" />
      </div>
      <select v-model="filters.route_type" @change="applyFilters">
        <option value="">全部路由</option>
        <option value="auto">Auto</option>
        <option value="combo">组合</option>
        <option value="direct">直连</option>
      </select>
      <select v-model="filters.status" @change="applyFilters">
        <option value="">全部结果</option>
        <option value="success">成功</option>
        <option value="error">失败</option>
      </select>
      <select v-model.number="filters.window_hours" @change="applyFilters">
        <option :value="1">1 小时</option>
        <option :value="24">24 小时</option>
        <option :value="168">7 天</option>
        <option :value="720">30 天</option>
      </select>
      <label class="check-filter">
        <input v-model="filters.fallback_only" type="checkbox" @change="applyFilters" />
        仅回退
      </label>
      <button class="btn btn-primary" @click="applyFilters">查询</button>
    </div>

    <div class="workspace" :class="{ 'has-detail': detail }">
      <section class="decision-list" aria-label="决策列表">
        <div class="table-wrap">
          <table>
            <thead>
              <tr>
                <th>时间</th>
                <th>类型</th>
                <th>请求</th>
                <th>选中</th>
                <th>链路</th>
                <th>耗时</th>
                <th>结果</th>
              </tr>
            </thead>
            <tbody>
              <tr v-if="!loading && items.length === 0">
                <td colspan="7" class="empty-cell">暂无决策记录</td>
              </tr>
              <tr
                v-for="row in items"
                :key="row.id"
                class="decision-row"
                :class="{ selected: detail?.id === row.id }"
                @click="openDetail(row.id)"
              >
                <td class="time-cell">{{ dateTime(row.created_at) }}</td>
                <td><span class="route-kind" :data-kind="row.route_type">{{ routeLabel(row.route_type) }}</span></td>
                <td>
                  <div class="primary-text mono ellipsis">{{ row.requested_model }}</div>
                  <div class="secondary-text mono">{{ shortTrace(row.conversation_id) }}</div>
                </td>
                <td>
                  <div class="primary-text ellipsis">{{ selectedName(row) }}</div>
                  <div class="secondary-text ellipsis">{{ row.selection_reason || '—' }}</div>
                </td>
                <td>
                  <span class="tabular">{{ row.attempt_count }}</span>
                  <span class="secondary-text inline"> 次尝试</span>
                  <span v-if="row.fallback_count" class="fallback-badge">+{{ row.fallback_count }}</span>
                </td>
                <td>
                  <div class="tabular">{{ duration(row.total_latency_ms) }}</div>
                  <div class="secondary-text">决策 {{ duration(row.decision_ms) }}</div>
                </td>
                <td><span class="result-pill" :data-status="row.status">{{ statusLabel(row.status) }}</span></td>
              </tr>
            </tbody>
          </table>
        </div>
        <div class="pager">
          <span>共 {{ total }} 条</span>
          <div class="pager-actions">
            <button class="icon-btn" title="上一页" :disabled="page <= 1" @click="changePage(page - 1)">
              <AppIcon name="chevronLeft" :size="15" />
            </button>
            <span class="tabular">{{ page }} / {{ totalPages }}</span>
            <button class="icon-btn" title="下一页" :disabled="page >= totalPages" @click="changePage(page + 1)">
              <AppIcon name="chevronRight" :size="15" />
            </button>
          </div>
        </div>
      </section>

      <aside v-if="detail" class="detail-panel" aria-label="决策详情">
        <header class="detail-head">
          <div>
            <div class="detail-title-row">
              <span class="route-kind" :data-kind="detail.route_type">{{ routeLabel(detail.route_type) }}</span>
              <strong>{{ selectedName(detail) }}</strong>
            </div>
            <code>{{ detail.conversation_id }}</code>
          </div>
          <button class="icon-btn" title="关闭详情" @click="detail = null">
            <AppIcon name="close" :size="15" />
          </button>
        </header>

        <dl class="facts">
          <div><dt>请求模型</dt><dd class="mono">{{ detail.requested_model }}</dd></div>
          <div><dt>策略</dt><dd>{{ detail.strategy || routeLabel(detail.route_type) }}</dd></div>
          <div><dt>估算输入</dt><dd class="tabular">{{ number(detail.estimated_tokens) }} tokens</dd></div>
          <div><dt>首字 / 总耗时</dt><dd class="tabular">{{ duration(detail.ttft_ms) }} / {{ duration(detail.total_latency_ms) }}</dd></div>
        </dl>

        <div v-if="detail.failure_reason" class="failure-strip">
          <AppIcon name="alert" :size="14" />
          <span>{{ detail.failure_reason }}</span>
        </div>

        <section class="detail-section">
          <div class="section-title">
            <h3>候选评分</h3>
            <span>{{ detail.candidate_count }} 个</span>
          </div>
          <div class="candidate-table-wrap">
            <table class="candidate-table">
              <thead><tr><th>#</th><th>候选</th><th>总分</th><th>速度</th><th>智力</th><th>稳定</th><th>状态</th></tr></thead>
              <tbody>
                <tr v-for="(candidate, index) in detail.candidates" :key="candidate.model_pk || `${candidate.provider}/${candidate.model}`" :class="{ picked: candidate.selected }">
                  <td class="tabular">{{ candidate.rank || index + 1 }}</td>
                  <td>
                    <div class="primary-text ellipsis">{{ candidate.provider ? `${candidate.provider}/${candidate.model}` : candidate.model }}</div>
                    <div v-if="candidate.skip_reason" class="skip-reason">{{ candidate.skip_reason }}</div>
                  </td>
                  <td class="tabular strong">{{ score(candidate.final_score) }}</td>
                  <td class="tabular">{{ score(candidate.speed_score) }}</td>
                  <td class="tabular">{{ score(candidate.intel_score) }}</td>
                  <td class="tabular">{{ score(candidate.stability_score) }}</td>
                  <td><span class="candidate-state" :data-state="candidate.selected ? 'selected' : candidate.eligible === false ? 'skipped' : 'ready'">{{ candidate.selected ? '选中' : candidate.eligible === false ? '跳过' : '候选' }}</span></td>
                </tr>
                <tr v-if="!detail.candidates?.length"><td colspan="7" class="empty-cell">无候选快照</td></tr>
              </tbody>
            </table>
          </div>
        </section>

        <section class="detail-section">
          <div class="section-title">
            <h3>尝试链</h3>
            <span>{{ detail.attempt_count }} 次</span>
          </div>
          <ol class="attempt-list">
            <li v-for="(attempt, index) in detail.attempts" :key="`${attempt.attempt}-${index}`" :data-status="attempt.status">
              <span class="attempt-index">{{ Number(attempt.attempt ?? index) + 1 }}</span>
              <div class="attempt-body">
                <div class="attempt-line">
                  <strong>{{ attempt.provider ? `${attempt.provider}/${attempt.model}` : attempt.model || '未选中候选' }}</strong>
                  <span class="tabular">{{ duration(attempt.ttft_ms) }} / {{ duration(attempt.latency_ms) }}</span>
                </div>
                <div v-if="attempt.error || attempt.reason" class="attempt-error">{{ attempt.error || attempt.reason }}</div>
              </div>
              <span class="result-pill" :data-status="attempt.status === 'success' ? 'success' : attempt.status === 'skipped' ? 'skipped' : 'error'">{{ attemptStatus(attempt.status) }}</span>
            </li>
            <li v-if="!detail.attempts?.length" class="empty-attempt">无上游尝试</li>
          </ol>
        </section>
      </aside>
    </div>
  </div>
</template>

<script>
import api from '../api'
import AppIcon from '../components/AppIcon.vue'
import PageHeader from '../components/PageHeader.vue'
import StatCard from '../components/StatCard.vue'
import toast from '../toast'

export default {
  name: 'RouteDecisions',
  components: { AppIcon, PageHeader, StatCard },
  data() {
    return {
      loading: false,
      detailLoading: false,
      items: [],
      detail: null,
      summary: {},
      total: 0,
      page: 1,
      pageSize: 30,
      filters: { q: '', route_type: '', status: '', fallback_only: false, window_hours: 24 },
    }
  },
  computed: {
    totalPages() { return Math.max(1, Math.ceil(this.total / this.pageSize)) },
  },
  mounted() { this.load() },
  methods: {
    async load() {
      this.loading = true
      try {
        const data = await api.getRouteDecisions({ ...this.filters, page: this.page, page_size: this.pageSize })
        this.items = data.items || []
        this.total = data.total || 0
        this.summary = data.summary || {}
        if (this.detail && !this.items.some((row) => row.id === this.detail.id)) this.detail = null
      } catch (error) {
        toast.error('路由决策加载失败：' + error.message)
      } finally {
        this.loading = false
      }
    },
    applyFilters() { this.page = 1; this.load() },
    changePage(page) { this.page = page; this.detail = null; this.load() },
    async openDetail(id) {
      if (this.detailLoading) return
      this.detailLoading = true
      try { this.detail = await api.getRouteDecision(id) }
      catch (error) { toast.error('决策详情加载失败：' + error.message) }
      finally { this.detailLoading = false }
    },
    routeLabel(value) { return ({ auto: 'Auto', combo: '组合', direct: '直连' })[value] || value || '未知' },
    statusLabel(value) { return value === 'success' ? '成功' : value === 'error' ? '失败' : value || '未知' },
    attemptStatus(value) { return ({ success: '成功', failed: '失败', skipped: '跳过' })[value] || value || '未知' },
    selectedName(row) { return row.selected_provider && row.selected_model ? `${row.selected_provider}/${row.selected_model}` : '未选中' },
    shortTrace(value) { return value ? value.slice(0, 8) : '—' },
    score(value) { return value == null ? '—' : Number(value).toFixed(1) },
    pct(value) { return value == null ? '—' : `${Number(value).toFixed(1)}%` },
    number(value) { return value == null ? '—' : Number(value).toLocaleString() },
    duration(value) {
      if (value == null) return '—'
      const ms = Number(value)
      return ms >= 1000 ? `${(ms / 1000).toFixed(1)}s` : `${Math.round(ms)}ms`
    },
    dateTime(value) {
      if (!value) return '—'
      const date = new Date(value.endsWith('Z') ? value : `${value}Z`)
      return date.toLocaleString('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false })
    },
  },
}
</script>

<style scoped>
.decision-page { display: flex; flex-direction: column; gap: var(--space-4); }
.summary-grid { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: var(--space-3); }
.filters { display: grid; grid-template-columns: minmax(240px, 1fr) repeat(3, minmax(112px, auto)) auto auto; gap: var(--space-2); align-items: center; padding-block: var(--space-3); border-block: 1px solid var(--border-soft); }
.filters select, .filters input { width: 100%; height: 36px; border: 1px solid var(--border-base); border-radius: var(--radius-sm); background: var(--bg-surface); color: var(--text-primary); padding: 0 var(--space-3); }
.search-field { position: relative; }
.search-field :deep(.app-icon) { position: absolute; left: 11px; top: 11px; color: var(--text-dim); }
.search-field input { padding-left: 34px; }
.check-filter { height: 36px; display: inline-flex; align-items: center; gap: var(--space-2); white-space: nowrap; font-size: var(--text-sm); color: var(--text-secondary); }
.check-filter input { width: 15px; height: 15px; accent-color: var(--primary); }
.workspace { min-width: 0; }
.workspace.has-detail { display: grid; grid-template-columns: minmax(500px, 1fr) minmax(420px, 0.82fr); gap: var(--space-4); align-items: start; }
.decision-list, .detail-panel { min-width: 0; border: 1px solid var(--border-soft); background: var(--bg-surface); }
.table-wrap, .candidate-table-wrap { overflow-x: auto; }
table { width: 100%; border-collapse: collapse; table-layout: fixed; }
th { height: 38px; padding: 0 var(--space-3); text-align: left; font-size: var(--text-xs); font-weight: 600; color: var(--text-dim); background: var(--bg-raised); border-bottom: 1px solid var(--border-soft); }
td { height: 54px; padding: var(--space-2) var(--space-3); border-bottom: 1px solid var(--border-soft); font-size: var(--text-sm); vertical-align: middle; }
th:nth-child(1) { width: 112px; } th:nth-child(2) { width: 70px; } th:nth-child(3) { width: 168px; } th:nth-child(4) { width: 210px; } th:nth-child(5) { width: 96px; } th:nth-child(6) { width: 96px; } th:nth-child(7) { width: 66px; }
.decision-row { cursor: pointer; transition: background var(--dur-fast); }
.decision-row:hover, .decision-row.selected { background: var(--bg-hover); }
.decision-row.selected td:first-child { box-shadow: inset 2px 0 var(--primary); }
.primary-text { color: var(--text-primary); }
.secondary-text { color: var(--text-dim); font-size: var(--text-xs); margin-top: 3px; }
.secondary-text.inline { margin-left: 2px; }
.mono { font-family: var(--font-mono); }
.tabular { font-variant-numeric: tabular-nums; }
.ellipsis { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.time-cell { font-variant-numeric: tabular-nums; color: var(--text-secondary); }
.route-kind, .result-pill, .candidate-state, .fallback-badge { display: inline-flex; align-items: center; min-height: 22px; padding: 2px 7px; border-radius: var(--radius-sm); font-size: var(--text-xs); white-space: nowrap; }
.route-kind { background: var(--primary-soft); color: var(--primary); }
.route-kind[data-kind="combo"] { background: color-mix(in srgb, var(--warning) 14%, transparent); color: var(--warning); }
.route-kind[data-kind="direct"] { background: var(--bg-hover); color: var(--text-secondary); }
.result-pill[data-status="success"], .candidate-state[data-state="selected"] { background: color-mix(in srgb, var(--success) 14%, transparent); color: var(--success); }
.result-pill[data-status="error"] { background: color-mix(in srgb, var(--danger) 14%, transparent); color: var(--danger); }
.result-pill[data-status="skipped"], .candidate-state[data-state="skipped"] { background: color-mix(in srgb, var(--warning) 14%, transparent); color: var(--warning); }
.candidate-state[data-state="ready"] { background: var(--bg-hover); color: var(--text-secondary); }
.fallback-badge { min-height: 18px; margin-left: 4px; padding: 0 5px; background: color-mix(in srgb, var(--warning) 14%, transparent); color: var(--warning); }
.empty-cell { height: 96px; text-align: center; color: var(--text-dim); }
.pager { height: 48px; display: flex; align-items: center; justify-content: space-between; padding: 0 var(--space-3); color: var(--text-dim); font-size: var(--text-sm); }
.pager-actions { display: flex; align-items: center; gap: var(--space-2); }
.icon-btn { width: 30px; height: 30px; display: inline-flex; align-items: center; justify-content: center; border: 1px solid var(--border-base); border-radius: var(--radius-sm); background: transparent; color: var(--text-secondary); cursor: pointer; }
.icon-btn:hover:not(:disabled) { background: var(--bg-hover); color: var(--text-primary); }
.icon-btn:disabled { opacity: .4; cursor: not-allowed; }
.detail-panel { position: sticky; top: var(--space-4); max-height: calc(100vh - var(--space-8)); overflow: auto; }
.detail-head { min-height: 66px; display: flex; justify-content: space-between; align-items: flex-start; gap: var(--space-3); padding: var(--space-3) var(--space-4); border-bottom: 1px solid var(--border-soft); }
.detail-title-row { display: flex; align-items: center; gap: var(--space-2); min-width: 0; }
.detail-title-row strong { overflow-wrap: anywhere; }
.detail-head code { display: block; margin-top: 5px; color: var(--text-dim); font-size: var(--text-xs); overflow-wrap: anywhere; }
.facts { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); margin: 0; padding: var(--space-3) var(--space-4); border-bottom: 1px solid var(--border-soft); }
.facts div { min-width: 0; padding: var(--space-2); }
.facts dt { color: var(--text-dim); font-size: var(--text-xs); }
.facts dd { margin: 4px 0 0; color: var(--text-primary); font-size: var(--text-sm); overflow-wrap: anywhere; }
.failure-strip { display: flex; align-items: flex-start; gap: var(--space-2); padding: var(--space-3) var(--space-4); color: var(--danger); background: color-mix(in srgb, var(--danger) 8%, transparent); font-size: var(--text-sm); overflow-wrap: anywhere; }
.detail-section { border-bottom: 1px solid var(--border-soft); }
.section-title { height: 42px; display: flex; align-items: center; justify-content: space-between; padding: 0 var(--space-4); }
.section-title h3 { margin: 0; font-size: var(--text-sm); }
.section-title span { color: var(--text-dim); font-size: var(--text-xs); }
.candidate-table { min-width: 680px; }
.candidate-table th, .candidate-table td { height: auto; padding: 8px 9px; }
.candidate-table th:nth-child(1) { width: 34px; } .candidate-table th:nth-child(2) { width: 230px; } .candidate-table th:nth-child(n+3):nth-child(-n+6) { width: 66px; } .candidate-table th:nth-child(7) { width: 64px; }
.candidate-table tr.picked { background: color-mix(in srgb, var(--success) 7%, transparent); }
.strong { font-weight: 650; }
.skip-reason, .attempt-error { margin-top: 3px; color: var(--danger); font-size: var(--text-xs); overflow-wrap: anywhere; }
.attempt-list { list-style: none; margin: 0; padding: 0 var(--space-4) var(--space-3); }
.attempt-list li { display: grid; grid-template-columns: 24px minmax(0, 1fr) auto; gap: var(--space-2); align-items: start; padding: var(--space-3) 0; border-top: 1px solid var(--border-soft); }
.attempt-index { width: 22px; height: 22px; display: inline-flex; align-items: center; justify-content: center; border-radius: 50%; background: var(--bg-hover); color: var(--text-secondary); font-size: var(--text-xs); font-variant-numeric: tabular-nums; }
.attempt-list li[data-status="success"] .attempt-index { background: color-mix(in srgb, var(--success) 14%, transparent); color: var(--success); }
.attempt-line { display: flex; justify-content: space-between; gap: var(--space-2); font-size: var(--text-sm); }
.attempt-line strong { min-width: 0; overflow-wrap: anywhere; }
.attempt-line span { flex-shrink: 0; color: var(--text-dim); font-size: var(--text-xs); }
.empty-attempt { color: var(--text-dim); justify-content: center; }
@media (max-width: 1120px) { .workspace.has-detail { grid-template-columns: 1fr; } .detail-panel { position: static; max-height: none; } }
@media (max-width: 900px) { .summary-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); } .filters { grid-template-columns: 1fr 1fr; } .search-field { grid-column: 1 / -1; } }
@media (max-width: 560px) { .summary-grid, .filters, .facts { grid-template-columns: 1fr; } .filters .btn { width: 100%; } }
</style>
