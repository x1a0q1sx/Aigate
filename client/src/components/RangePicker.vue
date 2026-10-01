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
const pad = (n) => String(n).padStart(2, '0')
const toLocalInput = (d) => `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`
// datetime-local（本地时区）→ UTC ISO，避免与后端 UTC 口径错位
const localToUtcIso = (v) => (v ? new Date(v).toISOString() : '')

export default {
  name: 'RangePicker',
  props: {
    value: { type: Object, default: null },
  },
  emits: ['change'],
  data() {
    return { customOpen: false, customStart: '', customEnd: '' }
  },
  computed: {
    presets() {
      const now = new Date()
      const daysAgo = (n) => new Date(now.getTime() - n * 86400000)
      const mk = (key, label, start, end, bucket) => ({
        key, label, bucket,
        start: localToUtcIso(toLocalInput(start)),
        end: localToUtcIso(toLocalInput(end)),
        hours: Math.max(1, Math.ceil((end - start) / 3600000)),
      })
      return [
        // spec §4② 默认粒度：今日→hour、7/30 天→day、90 天→week
        mk('today', '今日', new Date(now.getFullYear(), now.getMonth(), now.getDate()), now, 'hour'),
        mk('7d', '近7天', daysAgo(7), now, 'day'),
        mk('30d', '近30天', daysAgo(30), now, 'day'),
        mk('90d', '近90天', daysAgo(90), now, 'week'),
      ]
    },
  },
  mounted() {
    if (!this.value) this.pick(this.presets[1])   // 默认近7天
  },
  methods: {
    emitRange(r) {
      this.$emit('change', r)
    },
    pick(p) {
      this.customOpen = false
      this.emitRange({ key: p.key, label: p.label, bucket: p.bucket, start: p.start, end: p.end, hours: p.hours })
    },
    toggleCustom() {
      this.customOpen = !this.customOpen
      if (this.customOpen && !this.customStart) {
        const now = new Date()
        this.customStart = toLocalInput(new Date(now.getTime() - 7 * 86400000))
        this.customEnd = toLocalInput(now)
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
