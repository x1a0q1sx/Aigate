<template>
  <div>
    <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 16px;">
      <div>
        <h1>分析 📊</h1>
        <p style="color: var(--gray-500); font-size: 14px; margin-top: 4px;">范围统计、请求日志、趋势、用量分析与失败分析</p>
      </div>
      <div style="display: flex; align-items: center; gap: 14px;">
        <label style="display: flex; align-items: center; gap: 6px; font-size: 13px; color: var(--gray-500); cursor: pointer;" :title="diagVerbose ? '全量输出所有诊断阶段（控制台日志会变多）' : '仅输出关键里程碑，关闭多余日志'">
          <input type="checkbox" v-model="diagVerbose" @change="toggleDiag" style="cursor: pointer; width: 15px; height: 15px;" />
          请求诊断日志
        </label>
        <button class="btn btn-outline" @click="refreshAll">刷新</button>
      </div>
    </div>

    <!-- 全局范围选择器：驱动 ①-④ -->
    <div class="range-bar">
      <RangePicker :value="range" @change="onRangeChange" />
      <span class="muted" v-if="range">{{ range.label }}（本地时间）</span>
    </div>

    <!-- ① 统计卡行（跟随范围） -->
    <div class="stats-grid" v-if="todayData">
      <div class="stat-card">
        <div class="stat-label">请求数</div>
        <div class="stat-number">{{ formatNum(todayData.requests) }}</div>
        <div class="stat-sub">成功 {{ formatNum(todayData.success_requests) }}</div>
      </div>
      <div class="stat-card">
        <div class="stat-label">成功率</div>
        <div class="stat-number" :style="{color: todayData.success_rate >= 95 ? 'var(--success)' : todayData.success_rate >= 80 ? 'var(--warning)' : 'var(--danger)'}">{{ todayData.success_rate }}%</div>
      </div>
      <div class="stat-card">
        <div class="stat-label">Token</div>
        <div class="stat-number">{{ formatTokens(todayData.total_tokens) }}</div>
        <div class="stat-sub">输入 {{ formatTokens(todayData.prompt_tokens) }} · 输出 {{ formatTokens(todayData.completion_tokens) }}</div>
      </div>
      <div class="stat-card">
        <div class="stat-label">成本</div>
        <div class="stat-number">${{ todayData.cost_usd.toFixed(4) }}</div>
        <div class="stat-sub">按模型单价估算</div>
      </div>
      <div class="stat-card" :title="`缓存读 ${formatNum(todayData.cache_read_tokens || 0)} / 输入 ${formatNum(todayData.prompt_tokens || 0)}`">
        <div class="stat-label">缓存命中率</div>
        <div class="stat-number" style="color: #2b8aef;">{{ todayData.cache_hit_rate != null ? todayData.cache_hit_rate + '%' : '--' }}</div>
        <div class="stat-sub">缓存读 {{ formatTokens(todayData.cache_read_tokens || 0) }}</div>
      </div>
      <div class="stat-card">
        <div class="stat-label">平均延迟</div>
        <div class="stat-number">{{ todayData.avg_latency_ms != null ? (todayData.avg_latency_ms / 1000).toFixed(1) + 's' : '--' }}</div>
      </div>
      <div class="stat-card">
        <div class="stat-label">平均首字</div>
        <div class="stat-number">{{ todayData.avg_ttft_ms != null ? (todayData.avg_ttft_ms / 1000).toFixed(1) + 's' : '--' }}</div>
        <div class="stat-sub">流式采样 {{ formatNum(todayData.ttft_samples || 0) }} 条</div>
      </div>
    </div>
    <p v-else style="color: var(--gray-500); font-size: 13px; padding: 8px 0;">统计加载中...</p>

    <!-- ② 请求日志 + 归档（折叠，均在 LogsPanel 内）——置顶于趋势之上（2026-10-12 用户偏好） -->
    <div style="margin-top: 20px;">
      <LogsPanel ref="logs" />
    </div>

    <!-- ③ 用量趋势（跟随范围 + 粒度可切换） -->
    <div class="card" style="margin-top: 20px;">
      <div class="usage-header" style="margin-bottom: 12px;">
        <h2>用量趋势</h2>
        <div style="display: flex; gap: 8px;">
          <button v-for="b in bucketOptions" :key="b.v" class="btn btn-outline btn-sm"
                  :class="{active: trendBucket === b.v}" @click="setBucket(b.v)">{{ b.t }}</button>
        </div>
      </div>
      <TrendChart v-if="trendData.length" :rows="trendData" :bucket="trendBucket" />
      <p v-else style="text-align: center; padding: 16px; color: var(--gray-500); font-size: 13px;">当前范围暂无趋势数据</p>
    </div>

    <!-- ④ 用量分析（跟随范围；可按服务商 / 服务商×模型 / 服务商×账号 切换视角） -->
    <div class="card" style="margin-top: 20px;" v-if="providerData.length">
      <div class="usage-header" style="margin-bottom: 12px;">
        <h2>用量分析</h2>
        <div style="display: flex; align-items: center; gap: 8px; flex-wrap: wrap;">
          <div class="dim-switch" role="group" aria-label="用量视角">
            <button v-for="d in dimensionOptions" :key="d.v" class="dim-btn"
                    :class="{active: usageDimension === d.v}" :title="d.hint"
                    @click="setDimension(d.v)">{{ d.t }}</button>
          </div>
          <button v-if="usageDimension !== 'provider'" class="btn btn-outline btn-sm"
                  @click="toggleExpandAll">{{ allExpanded ? '收起全部' : '展开全部' }}</button>
          <span class="muted">{{ usageSummary }}</span>
        </div>
      </div>
      <table>
        <thead>
          <tr>
            <th>{{ usageDimension === 'provider_model' ? '服务商 / 模型'
                  : usageDimension === 'provider_account' ? '服务商 / 账号' : '服务商' }}</th>
            <th>请求</th>
            <th>Token</th>
            <th style="min-width: 170px;">占比</th>
            <th>成本</th>
            <th></th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="r in usageRows" :key="r.key" :class="{ 'usage-sub': r.kind === 'sub' }">
            <td :style="r.kind === 'sub' ? 'padding-left: 30px;' : 'font-weight: 600;'">
              <template v-if="r.kind === 'provider'">
                <button v-if="r.children.length" class="expand-caret" :title="isExpanded(r.key) ? '收起' : '展开细分'"
                        @click="toggleExpand(r.key)">{{ isExpanded(r.key) ? '▾' : '▸' }}</button>
                {{ r.row.provider_name }}
                <span v-if="r.row.orphan_kind === 'deleted'" class="muted"
                      title="服务商已删除或改名，仅按日志中的名称归集">（已删除）</span>
                <span v-else-if="r.row.orphan_kind === 'unattributed'" class="muted"
                      title="请求在选定服务商之前就结束了（路由失败 / 在途中断 / 未产生用量），日志里没有服务商名称，无从归属">（未归属）</span>
                <span v-if="r.children.length" class="muted" style="margin-left: 6px;">{{ r.children.length }} 项</span>
              </template>
              <template v-else>
                <span style="font-family: monospace; font-size: 12px;">{{ r.subLabel }}</span>
              </template>
            </td>
            <td>{{ formatNum(r.row.requests) }}</td>
            <td>{{ formatTokens(r.row.tokens) }}</td>
            <td>
              <div class="bar"><div class="bar-fill" :style="{width: sharePct(r.row) + '%'}"></div></div>
              <span class="muted">{{ sharePct(r.row) }}%</span>
            </td>
            <td>${{ r.row.cost_usd.toFixed(4) }}</td>
            <td>
              <button v-if="r.row.orphan_kind !== 'unattributed'" class="btn btn-outline btn-sm"
                      @click="seeLogs(r)">看日志</button>
            </td>
          </tr>
        </tbody>
      </table>
    </div>

    <!-- ⑤ 失败分析（跟随范围换算 hours，可手动覆盖） -->
    <div class="card" style="margin-top: 20px;">
      <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 14px;">
        <h2 style="margin: 0;">失败分析 🔍</h2>
        <div style="display: flex; align-items: center; gap: 10px;">
          <select v-model.number="failuresHours" @change="loadFailures" style="width: auto;" title="跟随上方范围自动换算，可手动覆盖">
            <option :value="6">近 6 小时</option>
            <option :value="24">近 24 小时</option>
            <option :value="72">近 3 天</option>
            <option :value="168">近 7 天</option>
          </select>
          <button class="btn btn-outline btn-sm" @click="loadFailures">刷新</button>
        </div>
      </div>
      <template v-if="failures">
        <div style="display: flex; gap: 12px; align-items: center; margin-bottom: 14px; flex-wrap: wrap;">
          <span class="badge" :class="failures.total_errors > 0 ? 'badge-danger' : 'badge-success'">
            失败 {{ failures.total_errors }} 次
          </span>
          <span v-if="failures.latest_error_sample" class="text-xs" style="color: var(--gray-500); overflow: hidden; text-overflow: ellipsis; max-width: 640px; white-space: nowrap;" :title="failures.latest_error_sample">
            最近错误：{{ failures.latest_error_sample }}
          </span>
        </div>
        <div style="display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap: 16px;">
          <div>
            <h3 style="font-size: 13px; color: var(--gray-500); margin: 0 0 8px;">按错误类型</h3>
            <table v-if="failures.by_error_type.length" style="width: 100%; font-size: 13px;">
              <tbody>
                <tr v-for="g in failures.by_error_type" :key="g.error_type">
                  <td><code>{{ g.error_type }}</code></td>
                  <td style="text-align: right; font-weight: 600;">{{ g.count }}</td>
                </tr>
              </tbody>
            </table>
            <p v-else style="color: var(--gray-500); font-size: 13px;">无失败记录</p>
          </div>
          <div>
            <h3 style="font-size: 13px; color: var(--gray-500); margin: 0 0 8px;">按服务商</h3>
            <table v-if="failures.by_provider.length" style="width: 100%; font-size: 13px;">
              <tbody>
                <tr v-for="g in failures.by_provider" :key="g.provider">
                  <td>{{ g.provider }}</td>
                  <td style="text-align: right; font-weight: 600;">{{ g.count }}</td>
                </tr>
              </tbody>
            </table>
            <p v-else style="color: var(--gray-500); font-size: 13px;">无失败记录</p>
          </div>
          <div>
            <h3 style="font-size: 13px; color: var(--gray-500); margin: 0 0 8px;">失败最多的模型 TOP10</h3>
            <table v-if="failures.by_model.length" style="width: 100%; font-size: 13px;">
              <tbody>
                <tr v-for="(g, i) in failures.by_model.slice(0, 10)" :key="i">
                  <td style="max-width: 200px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;" :title="g.provider + '/' + g.model">{{ g.provider }}/{{ g.model }}</td>
                  <td style="text-align: right; font-weight: 600;">{{ g.count }}</td>
                </tr>
              </tbody>
            </table>
            <p v-else style="color: var(--gray-500); font-size: 13px;">无失败记录</p>
          </div>
        </div>
      </template>
      <p v-else style="text-align: center; padding: 16px; color: var(--gray-500); font-size: 13px;">加载中...</p>
    </div>
  </div>
</template>

<script>
import api from '../api.js'
import RangePicker from '../components/RangePicker.vue'
import TrendChart from '../components/TrendChart.vue'
import LogsPanel from '../components/LogsPanel.vue'
import { freshRange } from '../rangePresets.js'

// 编排层：全局范围状态（range）驱动 ①-④ 联动；
// 日志区自管轮询/筛选（子组件内部状态）。实时区已删（2026-10-13 用户要求）。
export default {
  name: 'AnalyticsView',
  components: { RangePicker, TrendChart, LogsPanel },
  data() {
    return {
      range: null,          // {key,label,bucket,start,end,hours}（RangePicker emit）
      todayData: null,
      trendData: [],
      trendBucket: 'day',
      providerData: [],
      usageDimension: 'provider',   // provider | provider_model | provider_account
      usageCollapsed: {},           // 分组默认展开；这里只记「被用户收起」的 key
      failures: null,
      failuresHours: 24,    // 默认近 24h；范围变化时按 range.hours 换算覆盖
      diagVerbose: false,
      bucketOptions: [
        { v: 'hour', t: '小时' },
        { v: 'day', t: '天' },
        { v: 'week', t: '周' },
        { v: 'month', t: '月' },
      ],
      dimensionOptions: [
        { v: 'provider', t: '按服务商', hint: '只看每家服务商的合计用量' },
        { v: 'provider_model', t: '按服务商-模型', hint: '展开看每家服务商下各实际路由模型的用量' },
        { v: 'provider_account', t: '按服务商-账号', hint: '展开看每家服务商下各 OAuth 账号的用量' },
      ],
    }
  },
  computed: {
    providerTotal() {
      return (this.providerData || []).reduce((s, p) => s + (p.tokens || 0), 0)
    },
    // 模型/账号视角：把扁平的 (服务商, 子项) 行按服务商分组
    usageGroups() {
      const groups = new Map()
      for (const it of this.providerData || []) {
        const gk = it.provider_id != null ? `id:${it.provider_id}` : `name:${it.provider_name}`
        let g = groups.get(gk)
        if (!g) {
          g = {
            key: gk, kind: 'provider', children: [],
            row: {
              provider_id: it.provider_id, provider_name: it.provider_name,
              orphan_kind: it.orphan_kind, requests: 0, tokens: 0, cost_usd: 0,
            },
          }
          groups.set(gk, g)
        }
        g.row.requests += it.requests || 0
        g.row.tokens += it.tokens || 0
        g.row.cost_usd += it.cost_usd || 0
        const sub = this.usageDimension === 'provider_model' ? it.model : it.account
        g.children.push({
          key: `${gk}|${sub || ''}`,
          kind: 'sub',
          row: it,
          parentKey: gk,
          // null 表示这批请求没记录账号/模型（非 OAuth 服务商、或路由前就失败）
          subLabel: sub || '（未记录）',
        })
      }
      // 服务端已按 token 降序，这里按分组合计重排（分组后顺序可能变化）
      const list = Array.from(groups.values())
      for (const g of list) g.row.cost_usd = +g.row.cost_usd.toFixed(4)
      list.sort((a, b) => b.row.tokens - a.row.tokens)
      return list
    },
    usageRows() {
      if (this.usageDimension === 'provider') {
        return (this.providerData || []).map((p, i) => ({
          key: p.provider_id != null ? `id:${p.provider_id}` : `name:${p.provider_name}:${i}`,
          kind: 'provider', row: p, children: [],
        }))
      }
      const out = []
      for (const g of this.usageGroups) {
        out.push(g)
        if (!this.usageCollapsed[g.key]) out.push(...g.children)
      }
      return out
    },
    allExpanded() {
      return this.usageGroups.length > 0 && this.usageGroups.every((g) => !this.usageCollapsed[g.key])
    },
    usageSummary() {
      const tokens = this.formatTokens(this.providerTotal)
      if (this.usageDimension === 'provider') {
        return `${this.providerData.length} 家 · 共 ${tokens} tokens`
      }
      const n = this.usageGroups.length
      const items = (this.providerData || []).length
      return `${n} 家 / ${items} 项 · 共 ${tokens} tokens`
    },
  },
  mounted() {
    this.loadDiag()
    // RangePicker mounted 时会自动 emit 默认范围（近7天）→ onRangeChange 拉取 ①-④
  },
  methods: {
    onRangeChange(r) {
      this.range = r
      this.trendBucket = r.bucket || 'day'
      // 失败分析跟随范围：小时数 = 范围跨度（向上取整，钳制到端点上限 720）
      this.failuresHours = Math.min(720, Math.max(1, r.hours || 24))
      this.loadToday()
      this.loadTrend()
      this.loadByProvider()
      this.loadFailures()
    },
    setBucket(b) {
      this.trendBucket = b
      this.syncRange()   // 切粒度同样按当前时刻推进窗口（自定义范围不动）
      this.loadTrend()
    },
    // 预设范围按当前时刻重算窗口（2026-10-16）：此前 range 是挂载时刻快照，
    // 页面开久了点「刷新」/切粒度也只是重放旧窗口（趋势停在打开那一刻）。
    syncRange() {
      if (this.range) this.range = freshRange(this.range)
    },
    async loadToday() {
      if (!this.range) return
      try {
        this.todayData = await api.getAnalyticsToday({ start: this.range.start, end: this.range.end })
      } catch (e) { console.error('today load failed', e) }
    },
    async loadTrend() {
      if (!this.range) return
      try {
        this.trendData = await api.getAnalyticsTrend({
          bucket: this.trendBucket, start: this.range.start, end: this.range.end,
        })
      } catch (e) { console.error('trend load failed', e) }
    },
    async loadByProvider() {
      if (!this.range) return
      try {
        const d = await api.getAnalyticsByProvider({
          start: this.range.start, end: this.range.end, dimension: this.usageDimension,
        })
        this.providerData = d.providers || []
      } catch (e) { console.error('by-provider load failed', e) }
    },
    setDimension(d) {
      if (this.usageDimension === d) return
      this.usageDimension = d
      this.usageCollapsed = {}
      this.loadByProvider()
    },
    isExpanded(key) {
      return !this.usageCollapsed[key]
    },
    toggleExpand(key) {
      this.usageCollapsed = { ...this.usageCollapsed, [key]: !this.usageCollapsed[key] }
    },
    toggleExpandAll() {
      if (this.allExpanded) {
        const next = {}
        for (const g of this.usageGroups) next[g.key] = true
        this.usageCollapsed = next
      } else {
        this.usageCollapsed = {}
      }
    },
    sharePct(row) {
      if (row && row.share_pct != null) return row.share_pct
      return Math.round(((row.tokens || 0) / (this.providerTotal || 1)) * 1000) / 10
    },
    async loadFailures() {
      try { this.failures = await api.getFailures(this.failuresHours) }
      catch (e) { console.error('failures load failed', e) }
    },
    seeLogs(r) {
      if (!this.$refs.logs) return
      // 点「看日志」把当前视角的筛选条件一并带过去：服务商名始终带上，
      // 模型/账号视角再叠加对应的子项，否则看日志只会得到该服务商的全部记录。
      const row = r.row || r
      const f = { provider: row.provider_name || '' }
      if (this.usageDimension === 'provider_model') f.model = row.model || ''
      else if (this.usageDimension === 'provider_account') f.account = row.account || ''
      this.$refs.logs.setFilters(f)
      // 日志区已移至本表上方：滚动过去让用户看到筛选结果
      this.$refs.logs.$el?.scrollIntoView({ behavior: 'smooth', block: 'start' })
    },
    async loadDiag() {
      try { this.diagVerbose = !!(await api.getDiag()).verbose } catch (e) { console.error('diag load failed', e) }
    },
    async toggleDiag() {
      try {
        await api.setDiag(this.diagVerbose)
      } catch (e) {
        this.diagVerbose = !this.diagVerbose  // 失败回滚
        console.error('diag toggle failed', e)
      }
    },
    async refreshAll() {
      this.loadDiag()
      this.syncRange()
      await Promise.all([
        this.loadToday(), this.loadTrend(), this.loadByProvider(), this.loadFailures(),
        this.$refs.logs ? this.$refs.logs.reload() : Promise.resolve(),
      ])
    },
    formatNum(n) { return (n || 0).toLocaleString() },
    formatTokens(n) {
      if (!n || n < 1000) return String(n || 0)
      if (n < 1e6) return (n / 1000).toFixed(1) + 'K'
      return (n / 1e6).toFixed(1) + 'M'
    },
  },
}
</script>

<style scoped>
.range-bar { display: flex; align-items: center; gap: 14px; flex-wrap: wrap; margin-bottom: 16px; }
.stats-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
  gap: 12px;
}
.stat-card {
  background: var(--bg-card);
  border: 1px solid var(--border-soft);
  border-radius: 12px;
  padding: 16px;
}
.stat-label {
  font-size: 12px;
  color: var(--text-muted);
  text-transform: uppercase;
  margin-bottom: 4px;
}
.stat-number {
  font-size: 22px;
  font-weight: 700;
  color: var(--primary);
}
.stat-sub {
  font-size: 11px;
  color: var(--text-muted);
  margin-top: 2px;
}
.usage-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 12px; }
.usage-header h2 { margin: 0; }
.muted { color: var(--text-muted); font-size: 13px; }
.bar { height: 8px; background: var(--border-soft); border-radius: 4px; overflow: hidden; display: inline-block; width: 120px; vertical-align: middle; margin-right: 8px; }
.bar-fill { height: 100%; background: linear-gradient(90deg, #3b82f6, #10b981); border-radius: 4px; transition: width 0.3s ease; }
.btn.active { background: var(--primary); color: #fff; border-color: var(--primary); }

/* 视角切换（分段控件） */
.dim-switch { display: inline-flex; border: 1px solid var(--border-medium); border-radius: var(--radius-md); overflow: hidden; }
.dim-btn {
  border: none; background: transparent; cursor: pointer; padding: 5px 11px;
  font-size: var(--text-sm); color: var(--text-muted); white-space: nowrap;
}
.dim-btn + .dim-btn { border-left: 1px solid var(--border-medium); }
.dim-btn:hover { background: var(--bg-hover); color: var(--text-primary); }
.dim-btn.active { background: var(--primary); color: #fff; }
/* 二级行（模型/账号）视觉降级，避免和所属服务商抢视线 */
.usage-sub td { background: rgba(148, 163, 184, 0.06); font-size: 13px; }
.expand-caret {
  border: none; background: transparent; cursor: pointer; padding: 0 4px 0 0;
  color: var(--text-muted); font-size: 12px;
}
.expand-caret:hover { color: var(--text-primary); }
</style>
