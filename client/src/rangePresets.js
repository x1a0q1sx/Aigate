// 分析页时间范围工具（2026-10-16 自 RangePicker 抽取）。
//
// 背景（修复的 bug）：预设列表原先放在组件 computed 里，且内部调用 new Date()
// 没有任何响应式依赖 → Vue 会永久缓存首次求值结果，时间窗口冻结在页面挂载时刻。
// 表现：早上打开分析页，晚上 22 点「今日」趋势仍只画到早上 10 点；点「刷新」
// 也只是重放挂载时的旧窗口，永不前进。
// 约定：组件不得自行取时刻；所有预设窗口都经 buildPresets(now)/freshRange(range, now)
// 在「取数/点击的当下」重算（now 可注入，便于测试）。

const pad = (n) => String(n).padStart(2, '0')

export const toLocalInput = (d) =>
  `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`

// datetime-local（本地时区）→ UTC ISO，避免与后端 UTC 口径错位
export const localToUtcIso = (v) => (v ? new Date(v).toISOString() : '')

// 生成四个预设（spec §4② 默认粒度：今日→hour、7/30 天→day、90 天→week）。
export function buildPresets(now = new Date()) {
  const daysAgo = (n) => new Date(now.getTime() - n * 86400000)
  const mk = (key, label, start, end, bucket) => ({
    key, label, bucket,
    start: localToUtcIso(toLocalInput(start)),
    end: localToUtcIso(toLocalInput(end)),
    hours: Math.max(1, Math.ceil((end - start) / 3600000)),
  })
  return [
    mk('today', '今日', new Date(now.getFullYear(), now.getMonth(), now.getDate()), now, 'hour'),
    mk('7d', '近7天', daysAgo(7), now, 'day'),
    mk('30d', '近30天', daysAgo(30), now, 'day'),
    mk('90d', '近90天', daysAgo(90), now, 'week'),
  ]
}

// 按 key 在当前时刻重算既有 range 的 start/end/hours；custom（绝对窗口）原样返回。
export function freshRange(range, now = new Date()) {
  if (!range || !range.key || range.key === 'custom') return range
  const p = buildPresets(now).find((x) => x.key === range.key)
  return p ? { ...range, start: p.start, end: p.end, hours: p.hours } : range
}

// 自定义范围面板的默认初值（近 7 天 → 现在，本地时区字符串）
export function defaultCustomWindow(now = new Date()) {
  return {
    start: toLocalInput(new Date(now.getTime() - 7 * 86400000)),
    end: toLocalInput(now),
  }
}
