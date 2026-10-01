<template>
  <div>
    <div class="trend-wrap" @mousemove="onMove" @mouseleave="hover = -1">
      <svg :viewBox="'0 0 ' + W + ' ' + H" width="100%" style="display:block; overflow:visible;">
        <line :x1="pad" :y1="H - pad" :x2="W - pad" :y2="H - pad" stroke="#334155" stroke-width="1"/>
        <path :d="tokArea" fill="rgba(59,130,246,0.12)"/>
        <path :d="tokLine" fill="none" stroke="#3b82f6" stroke-width="2"/>
        <path :d="costLine" fill="none" stroke="#10b981" stroke-width="2" stroke-dasharray="4 3"/>
        <g v-if="rows.length <= 120">
          <circle v-for="(d, i) in rows" :key="'t' + i" :cx="x(i)" :cy="yTok(d)" r="2.6" fill="#3b82f6"/>
          <circle v-for="(d, i) in rows" :key="'c' + i" :cx="x(i)" :cy="yCost(d)" r="2.6" fill="#10b981"/>
        </g>
        <g v-for="(d, i) in rows" :key="'l' + i">
          <text v-if="showLabel(i)" :x="x(i)" :y="H - 10" font-size="9" fill="#94a3b8" text-anchor="middle">{{ label(d) }}</text>
        </g>
        <line v-if="hover >= 0" :x1="x(hover)" :y1="pad" :x2="x(hover)" :y2="H - pad" stroke="#64748b" stroke-width="1" stroke-dasharray="3 3"/>
      </svg>
      <div v-if="hover >= 0" class="trend-tip" :style="tipStyle">
        <div class="trend-tip-day">{{ rows[hover].day }}</div>
        <div>Token：{{ fmtTok(rows[hover].tokens) }}</div>
        <div>成本：${{ (rows[hover].cost_usd || 0).toFixed(4) }}</div>
        <div>请求：{{ rows[hover].requests }}</div>
      </div>
    </div>
    <div class="trend-legend">
      <span><i class="dot token"></i>Token</span>
      <span><i class="dot cost"></i>成本 (USD)</span>
    </div>
  </div>
</template>

<script>
// 用量趋势图（原 Analytics 内联 SVG 实现抽出，逻辑不变；不引图表库）。
// bucket 只影响 X 轴 label：hour 槽显示 "HH:MM"，day/week 显示 "MM-DD"，month 显示 "YYYY-MM"。
export default {
  name: 'TrendChart',
  props: {
    rows: { type: Array, default: () => [] },
    bucket: { type: String, default: 'day' },
  },
  data() {
    return { W: 680, H: 210, pad: 32, hover: -1, tipX: 0, tipY: 0 }
  },
  computed: {
    scale() {
      const data = this.rows || []
      const n = data.length
      const maxTok = Math.max(1, ...data.map(d => d.tokens || 0))
      const maxCost = Math.max(0.0001, ...data.map(d => d.cost_usd || 0))
      const W = this.W, H = this.H, pad = this.pad
      const xOf = i => pad + (W - 2 * pad) * (n <= 1 ? 0.5 : i / (n - 1))
      const yTok = v => H - pad - (H - 2 * pad) * (v / maxTok)
      const yCost = v => H - pad - (H - 2 * pad) * (v / maxCost)
      return { data, n, xOf, yTok, yCost }
    },
    tokArea() {
      const s = this.scale
      if (!s.n) return ''
      const { data, n, xOf, yTok } = s
      return `M ${xOf(0).toFixed(1)},${(this.H - this.pad).toFixed(1)} ` +
        data.map((d, i) => `L ${xOf(i).toFixed(1)},${yTok(d.tokens || 0).toFixed(1)}`).join(' ') +
        ` L ${xOf(n - 1).toFixed(1)},${(this.H - this.pad).toFixed(1)} Z`
    },
    tokLine() {
      const s = this.scale
      if (!s.n) return ''
      const { data, xOf, yTok } = s
      return 'M ' + data.map((d, i) => `${xOf(i).toFixed(1)},${yTok(d.tokens || 0).toFixed(1)}`).join(' L ')
    },
    costLine() {
      const s = this.scale
      if (!s.n) return ''
      const { data, xOf, yCost } = s
      return 'M ' + data.map((d, i) => `${xOf(i).toFixed(1)},${yCost(d.cost_usd || 0).toFixed(1)}`).join(' L ')
    },
    tipStyle() {
      return { left: this.tipX + 'px', top: this.tipY + 'px' }
    },
  },
  methods: {
    x(i) { return this.scale.xOf(i) },
    yTok(d) { return this.scale.yTok(d.tokens || 0) },
    yCost(d) { return this.scale.yCost(d.cost_usd || 0) },
    showLabel(i) {
      const n = this.scale.n
      const step = Math.max(1, Math.ceil(n / 14))
      return n <= 14 || i % step === 0 || i === n - 1
    },
    label(d) {
      const k = String(d.day)
      if (this.bucket === 'hour') return k.slice(11, 16)
      if (this.bucket === 'month') return k.slice(0, 7)
      return k.slice(5)   // day / week（周一槽位）
    },
    onMove(e) {
      const rect = e.currentTarget.getBoundingClientRect()
      const xPx = e.clientX - rect.left
      const yPx = e.clientY - rect.top
      const xView = xPx / rect.width * this.W
      const n = this.scale.n
      let i = n <= 1 ? 0 : Math.round((xView - this.pad) / (this.W - 2 * this.pad) * (n - 1))
      i = Math.max(0, Math.min(n - 1, i))
      this.hover = i
      this.tipX = xPx
      this.tipY = yPx
    },
    fmtTok(n) {
      if (!n || n < 1000) return String(n || 0)
      if (n < 1e6) return (n / 1000).toFixed(1) + 'K'
      return (n / 1e6).toFixed(1) + 'M'
    },
  },
}
</script>

<style scoped>
.trend-wrap { position: relative; }
.trend-legend { display: flex; gap: 16px; margin-top: 8px; font-size: 12px; color: var(--text-muted); }
.trend-legend .dot { display: inline-block; width: 10px; height: 10px; border-radius: 2px; margin-right: 4px; vertical-align: middle; }
.trend-legend .dot.token { background: #3b82f6; }
.trend-legend .dot.cost { background: #10b981; }
.trend-tip {
  position: absolute;
  transform: translate(-50%, calc(-100% - 14px));
  background: #0f172a; color: #e2e8f0;
  border: 1px solid #334155; border-radius: 8px;
  padding: 8px 10px; font-size: 12px; line-height: 1.7;
  pointer-events: none; white-space: nowrap;
  box-shadow: 0 4px 12px rgba(0,0,0,.35); z-index: 5;
}
.trend-tip-day { font-weight: 700; color: #93c5fd; margin-bottom: 2px; }
</style>
