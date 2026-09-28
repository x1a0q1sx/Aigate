<template>
  <div class="quota-panel">
    <!-- 账号级积分汇总（CodeBuddy 等：上游 30+ 个赠包逐个列会刷屏，先给总数） -->
    <div v-if="creditsSummary" class="quota-summary">
      <span class="qs-main">可用积分 <b>{{ fmt(creditsSummary.remaining) }}</b></span>
      <span class="qs-sub">总量 {{ fmt(creditsSummary.total) }} · 已用 {{ fmt(creditsSummary.used) }}</span>
      <span v-if="bonusAgg.count" class="qs-sub">赠包 {{ bonusAgg.count }} 个（合计 {{ fmt(bonusAgg.total) }}）</span>
    </div>

    <!-- 常规额度（续包/套餐/余额）逐条显示 -->
    <div v-for="[name, q] in plainRows" :key="name" class="quota-row">
      <span class="quota-name" :title="name">{{ labelOf(name, q) }}</span>
      <div class="quota-bar"><i :class="{ warn: pct(q) > 85, hot: pct(q) >= 100 }" :style="{ width: pct(q) + '%' }"></i></div>
      <span class="quota-val">{{ quotaText(q) }}</span>
    </div>

    <!-- 赠包：折叠态只显示一行聚合；展开后每包一个小 chip（紧凑、全部可见） -->
    <template v-if="bonusRows.length">
      <div class="bonus-head">
        <span class="bonus-title">赠包 {{ bonusRows.length }} 个 · 合计 {{ fmt(bonusAgg.total) }} 积分</span>
        <span class="bonus-sub">最早 {{ shortDate(bonusAgg.firstExpiry) }} 到期</span>
        <button class="btn btn-ghost btn-xs" @click="expanded = !expanded">
          {{ expanded ? '收起' : '展开每个包' }}
        </button>
      </div>
      <div v-if="expanded" class="bonus-chips">
        <span v-for="[name, q] in bonusRows" :key="name" class="bonus-chip" :title="name + ' · 到期 ' + (q.reset_at || '未知')">
          <span class="bc-name">{{ shortName(name) }}</span>
          <span class="bc-val" :class="{ zero: Number(q.remaining) <= 0 }">{{ fmt(q.remaining) }}<span class="bc-total">/{{ fmt(q.total) }}</span></span>
          <span class="bc-exp">{{ shortDate(q.reset_at) }}</span>
        </span>
      </div>
    </template>

    <div v-if="noteText" class="quota-note text-xs">{{ noteText }}</div>
    <div v-if="entry && entry.message" class="usage-msg text-xs">{{ entry.message }}</div>
  </div>
</template>

<script>
// 统一额度面板：OAuth 连接页 / 签到页 / 服务商详情页三处复用，
// 保证「积分总量 + 赠包压缩」口径完全一致（此前三处各自渲染，赠包多时都刷屏）。
export default {
  name: 'QuotaPanel',
  props: {
    // 额度查询结果 {plan, quotas:{名: {used,total,remaining,unit,...}}, message, extra}
    entry: { type: Object, default: null },
    // 折叠态最多显示多少行常规额度（超出部分也并入折叠逻辑由赠包承接）
    maxPlain: { type: Number, default: 6 },
  },
  data() {
    return { expanded: false }
  },
  computed: {
    quotas() {
      return (this.entry && this.entry.quotas) || {}
    },
    plainRows() {
      return Object.entries(this.quotas).filter(([name, q]) => !this.isBonus(name, q))
    },
    bonusRows() {
      return Object.entries(this.quotas).filter(([name, q]) => this.isBonus(name, q))
    },
    creditsSummary() {
      const ex = (this.entry && this.entry.extra) || {}
      if (ex.credits_remaining == null && ex.credits_total == null) return null
      return {
        remaining: Number(ex.credits_remaining) || 0,
        total: Number(ex.credits_total) || 0,
        used: Number(ex.credits_used) || 0,
      }
    },
    // 上游档位/可用范围的说明（如 Freebuff limited 档只放行哪几个模型）。
    // 放这里而不是各页各写一份：三处复用同一口径（与「积分总量」同理）。
    noteText() {
      const ex = (this.entry && this.entry.extra) || {}
      return ex.note || ''
    },
    bonusAgg() {
      let total = 0, remaining = 0, first = null
      for (const [, q] of this.bonusRows) {
        total += Number(q.total) || 0
        remaining += Number(q.remaining) || 0
        if (q.reset_at && (first === null || q.reset_at < first)) first = q.reset_at
      }
      return { count: this.bonusRows.length, total, remaining, firstExpiry: first }
    },
  },
  methods: {
    isBonus(name, q) {
      // kind 是后端新加的显式标记；老响应没有它时按名字兜底（Bonus Pack N）
      if (q && q.kind) return q.kind === 'bonus'
      return /^Bonus Pack/.test(String(name || ''))
    },
    labelOf(name, q) {
      return q.display_name && q.display_name !== name ? name + ' · ' + q.display_name : name
    },
    shortName(name) {
      // "Bonus Pack 12" → "#12"（chip 里空间宝贵）
      const m = /^Bonus Pack (\d+)$/.exec(name)
      return m ? '#' + m[1] : name
    },
    shortDate(iso) {
      if (!iso) return '—'
      const d = new Date(iso)
      if (!Number.isFinite(d.getTime())) return '—'
      return `${d.getMonth() + 1}-${String(d.getDate()).padStart(2, '0')}`
    },
    fmt(v) {
      const n = Number(v) || 0
      return n.toLocaleString('zh-CN', { maximumFractionDigits: 0 })
    },
    pct(q) {
      if (q.unlimited) return 0
      const total = Number(q.total) || 0
      if (!total) return 0
      return Math.max(0, Math.min(100, (Number(q.used) || 0) / total * 100))
    },
    quotaText(q) {
      if (q.unlimited) return '不限量'
      const money = (v) => '$' + (Number(v) || 0).toFixed(2)
      if (q.unit === 'USD') {
        const bal = q.remaining != null ? q.remaining : q.total
        return `剩 ${money(bal)}` + (q.display_name ? `（${q.display_name}）` : '')
      }
      const isPct = Math.round(Number(q.total) || 0) === 100 && !q.unit
      let text
      if (isPct) text = `已用 ${Math.round(Number(q.used) || 0)}%`
      else {
        const u = Math.round(Number(q.used) || 0)
        const t = Math.round(Number(q.total) || 0)
        text = `${u.toLocaleString()}/${t.toLocaleString()}` + (q.unit ? ` ${q.unit}` : '')
        if (q.display_name && !isPct && q.unit !== 'USD') text += `（${q.display_name}）`
      }
      if (q.reset_at) {
        const tl = this._timeLeft(q.reset_at)
        text += ` · ${q.recurring ? '重置' : '到期'} ${tl}`
      }
      return text
    },
    _timeLeft(iso) {
      const ms = new Date(iso).getTime() - Date.now()
      if (!Number.isFinite(ms)) return iso
      if (ms <= 0) return '已过'
      const d = Math.floor(ms / 86400000)
      const h = Math.floor((ms % 86400000) / 3600000)
      const m = Math.floor((ms % 3600000) / 60000)
      if (d >= 7) return d + ' 天后'
      if (d > 0) return d + ' 天 ' + h + ' 时后'
      if (h > 0) return h + ' 时 ' + m + ' 分后'
      return m + ' 分后'
    },
  },
}
</script>

<style scoped>
.quota-panel { display: flex; flex-direction: column; gap: 6px; }
.quota-summary {
  display: flex; flex-wrap: wrap; align-items: baseline; gap: 4px 12px;
  font-size: 12px; padding: 4px 0 2px;
}
.qs-main { font-size: 13px; }
.qs-main b { color: var(--primary, #5b8dff); font-size: 15px; }
.qs-sub { color: var(--text-muted, #7e8ea9); }
.quota-row {
  display: grid;
  grid-template-columns: minmax(80px, 1.4fr) 1fr minmax(120px, auto);
  align-items: center; gap: 8px; font-size: 12px;
}
.quota-name { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: var(--text-muted, #a9b7cd); }
.quota-bar { height: 6px; border-radius: 3px; background: rgba(126, 142, 169, 0.18); overflow: hidden; }
.quota-bar i { display: block; height: 100%; background: var(--primary, #5b8dff); border-radius: 3px; transition: width 0.25s; }
.quota-bar i.warn { background: #fbbf24; }
.quota-bar i.hot { background: #f87171; }
.quota-val { text-align: right; white-space: nowrap; }
.bonus-head {
  display: flex; align-items: center; gap: 10px; flex-wrap: wrap;
  font-size: 12px; padding-top: 2px;
}
.bonus-title { color: var(--text-muted, #a9b7cd); }
.bonus-sub { color: var(--text-muted, #7e8ea9); font-size: 11px; }
.bonus-chips { display: flex; flex-wrap: wrap; gap: 4px; }
.bonus-chip {
  display: inline-flex; align-items: baseline; gap: 4px;
  border: 1px solid var(--border-color, #1c2839); border-radius: 6px;
  padding: 1px 6px; font-size: 11px; line-height: 1.7;
  background: var(--bg-card, rgba(15, 22, 35, 0.4));
}
.bc-name { color: var(--text-muted, #7e8ea9); }
.bc-val { font-weight: 600; }
.bc-val.zero { color: var(--text-muted, #7e8ea9); font-weight: 400; }
.bc-total { font-weight: 400; color: var(--text-muted, #7e8ea9); }
.bc-exp { color: var(--text-muted, #7e8ea9); font-size: 10px; }
.usage-msg { color: var(--text-muted, #7e8ea9); }
/* 档位/可用范围说明：与 message 区分开（message 是错误态，note 是事实说明） */
.quota-note { color: var(--text-muted, #7e8ea9); margin-top: 4px; line-height: 1.5; }
</style>
