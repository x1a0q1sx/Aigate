<template>
  <div class="range-picker">
    <div class="preset-row">
      <button v-for="p in presets" :key="p.key" class="btn btn-outline btn-sm"
              :class="{ active: value && value.key === p.key }" @click="pick(p)">{{ p.label }}</button>
      <button class="btn btn-outline btn-sm" :class="{ active: value && value.key === 'custom' }"
              @click="toggleCustom">自定义</button>
    </div>
    <div v-if="customOpen" class="custom-row">
      <label>开始 <input type="datetime-local" v-model="customStart"></label>
      <label>结束 <input type="datetime-local" v-model="customEnd"></label>
      <button class="btn btn-primary btn-sm" :disabled="!customStart || !customEnd" @click="applyCustom">应用</button>
    </div>
  </div>
</template>

<script>
// 全局范围选择器：驱动分析页 ①-④ 区联动。
// emit 载荷 { key, label, bucket, start, end, hours }，start/end 为 UTC ISO（后端 _parse_dt_param 兼容）。
//
// 时间窗口新鲜度（2026-10-16 修复）：预设窗口一律在「点击的当下」经 buildPresets(now) 重算，
// 绝不缓存挂载时刻的旧窗口——此前 preset 列表放 computed 里，new Date() 无响应式依赖被
// Vue 永久缓存，导致「今日」趋势永远停在打开页面那一刻（详见 rangePresets.js 头注）。
import { buildPresets, defaultCustomWindow } from '../rangePresets.js'

export default {
  name: 'RangePicker',
  props: {
    value: { type: Object, default: null },
  },
  emits: ['change'],
  data() {
    return { customOpen: false, customStart: '', customEnd: '', presets: [] }
  },
  mounted() {
    this.refreshPresets()
    if (!this.value) this.pick(this.presets[1])   // 默认近7天
  },
  methods: {
    refreshPresets() {
      this.presets = buildPresets(new Date())
    },
    emitRange(r) {
      this.$emit('change', r)
    },
    pick(p) {
      this.customOpen = false
      // 关键：以「点击当下」重算窗口（p 是按钮渲染时刻的快照，可能已过时数小时）
      this.refreshPresets()
      const fresh = this.presets.find((x) => x.key === p.key) || p
      this.emitRange({
        key: fresh.key, label: fresh.label, bucket: fresh.bucket,
        start: fresh.start, end: fresh.end, hours: fresh.hours,
      })
    },
    toggleCustom() {
      this.customOpen = !this.customOpen
      if (this.customOpen && !this.customStart) {
        const w = defaultCustomWindow(new Date())
        this.customStart = w.start
        this.customEnd = w.end
      }
    },
    applyCustom() {
      const s = new Date(this.customStart)
      const e = new Date(this.customEnd)
      if (Number.isNaN(s.getTime()) || Number.isNaN(e.getTime()) || s >= e) return
      const spanH = Math.max(1, Math.ceil((e - s) / 3600000))
      this.emitRange({
        key: 'custom',
        label: `${this.customStart.replace('T', ' ')} ~ ${this.customEnd.replace('T', ' ')}（本地）`,
        bucket: spanH <= 72 ? 'hour' : 'day',
        start: s.toISOString(),
        end: e.toISOString(),
        hours: spanH,
      })
    },
  },
}
</script>

<style scoped>
.range-picker { display: flex; flex-direction: column; gap: 8px; }
.preset-row { display: flex; gap: 8px; flex-wrap: wrap; align-items: center; }
.custom-row { display: flex; gap: 12px; align-items: center; flex-wrap: wrap; font-size: 13px; color: var(--text-muted); }
.custom-row label { display: flex; align-items: center; gap: 6px; }
.custom-row input {
  padding: 5px 8px; border: 1px solid var(--border-soft); border-radius: 6px;
  background: var(--bg-card); color: var(--text-primary); font-size: 13px;
}
.btn.active { background: var(--primary); color: #fff; border-color: var(--primary); }
</style>
