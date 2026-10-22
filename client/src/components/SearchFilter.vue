<template>
  <div ref="root" class="search-filter" :class="{ 'is-open': open }">
    <div class="sf-control">
      <input
        ref="input"
        class="sf-input"
        type="text"
        autocomplete="off"
        :value="text"
        :placeholder="placeholder"
        @input="onInput"
        @focus="openMenu"
        @keydown.down.prevent="move(1)"
        @keydown.up.prevent="move(-1)"
        @keydown.enter.prevent="commit"
        @keydown.esc.prevent="closeMenu"
      />
      <button v-if="text" class="sf-clear" type="button" :title="'清除' + label" @click="clear">×</button>
      <span v-else class="sf-caret" aria-hidden="true">▾</span>
    </div>
    <div v-if="open" ref="menu" class="sf-menu" role="listbox">
      <div class="sf-option" :class="{ active: activeIndex === 0 }"
           role="option" @mousedown.prevent="pick('')" @mouseenter="activeIndex = 0">
        <span class="sf-option-label">全部{{ label }}</span>
      </div>
      <div v-if="!filtered.length" class="sf-empty">{{ emptyText }}</div>
      <div v-for="(o, i) in filtered" :key="o" class="sf-option"
           :class="{ active: activeIndex === i + 1, selected: o === text }"
           role="option" @mousedown.prevent="pick(o)" @mouseenter="activeIndex = i + 1">
        <span class="sf-option-label">{{ o }}</span>
      </div>
    </div>
  </div>
</template>

<script>
/**
 * 可输入搜索的筛选框（分析页日志筛选：服务商 / 模型 / 账号）。
 *
 * 与 SearchSelect 的区别：SearchSelect 是「选择器」（必须命中候选项），
 * 这里允许自由输入——输入即作为筛选词实时生效（防抖 300ms），候选列表只是
 * 用来提示哪些值真的存在。因此输一半也能筛（服务端做子串匹配）。
 */
export default {
  name: 'SearchFilter',
  props: {
    modelValue: { type: String, default: '' },
    options: { type: Array, default: () => [] },
    label: { type: String, default: '项' },
    placeholder: { type: String, default: '' },
    emptyText: { type: String, default: '无匹配项' },
    debounceMs: { type: Number, default: 300 },
  },
  emits: ['update:modelValue', 'change'],
  data() {
    return { open: false, activeIndex: 0, text: this.modelValue || '' }
  },
  computed: {
    filtered() {
      const q = this.text.trim().toLowerCase()
      const list = this.options || []
      if (!q) return list
      return list.filter((o) => String(o).toLowerCase().includes(q))
    },
  },
  watch: {
    modelValue(v) {
      // 父级复位（如切换日志类型）时同步输入框，避免显示与筛选不一致
      if ((v || '') !== this.text) this.text = v || ''
    },
  },
  mounted() {
    document.addEventListener('mousedown', this.onDocMousedown)
  },
  beforeUnmount() {
    document.removeEventListener('mousedown', this.onDocMousedown)
    if (this._timer) clearTimeout(this._timer)
  },
  methods: {
    openMenu() {
      this.open = true
      const idx = this.filtered.findIndex((o) => o === this.text)
      this.activeIndex = idx >= 0 ? idx + 1 : 0
    },
    closeMenu() { this.open = false },
    onDocMousedown(e) {
      const root = this.$refs.root
      if (root && !root.contains(e.target)) this.closeMenu()
    },
    onInput(e) {
      this.text = e.target.value
      this.activeIndex = 0
      this.open = true
      if (this._timer) clearTimeout(this._timer)
      this._timer = setTimeout(() => this.emit(), this.debounceMs)
    },
    move(step) {
      if (!this.open) { this.openMenu(); return }
      const n = this.filtered.length + 1        // +1 = 「全部」
      this.activeIndex = (this.activeIndex + step + n) % n
      this.$nextTick(() => {
        const menu = this.$refs.menu
        const el = menu && menu.querySelector('.sf-option.active')
        if (el && el.scrollIntoView) el.scrollIntoView({ block: 'nearest' })
      })
    },
    commit() {
      if (this.open) {
        const o = this.filtered[this.activeIndex - 1]
        if (o !== undefined && this.activeIndex > 0) { this.pick(o); return }
      }
      this.emit()
      this.closeMenu()
    },
    pick(o) {
      this.text = o || ''
      this.emit()
      this.closeMenu()
    },
    clear() {
      this.text = ''
      this.emit()
      this.closeMenu()
    },
    emit() {
      if (this._timer) { clearTimeout(this._timer); this._timer = null }
      const v = this.text
      if (v !== this.modelValue) {
        this.$emit('update:modelValue', v)
        this.$emit('change', v)
      }
    },
  },
}
</script>

<style scoped>
.search-filter { position: relative; width: 170px; }
.sf-control { position: relative; }
.sf-input { padding-right: 26px; }
.sf-caret {
  position: absolute; right: 10px; top: 50%; transform: translateY(-50%);
  color: var(--text-muted); font-size: 11px; pointer-events: none;
}
.sf-clear {
  position: absolute; right: 4px; top: 50%; transform: translateY(-50%);
  border: none; background: transparent; color: var(--text-muted);
  font-size: 16px; line-height: 1; cursor: pointer; padding: 2px 6px;
}
.sf-clear:hover { color: var(--text-primary); }
.sf-menu {
  position: absolute; top: calc(100% + 4px); left: 0; right: 0; z-index: 60;
  max-height: 300px; overflow-y: auto; padding: 4px;
  background: var(--bg-elevated); border: 1px solid var(--border-medium);
  border-radius: var(--radius-md); box-shadow: var(--shadow-lg);
}
.sf-empty { padding: 8px 10px; font-size: var(--text-sm); color: var(--text-muted); }
.sf-option {
  padding: 6px 10px; border-radius: var(--radius-sm); cursor: pointer;
  font-size: var(--text-base); color: var(--text-primary);
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.sf-option.active { background: var(--bg-hover); }
.sf-option.selected { color: var(--primary); }
.sf-option-label { display: block; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
</style>
