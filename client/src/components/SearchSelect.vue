<template>
  <div ref="root" class="search-select" :class="{ 'is-open': open, 'is-disabled': disabled }">
    <div class="ss-control" role="combobox" :aria-expanded="open ? 'true' : 'false'" @click="openMenu">
      <input
        ref="input"
        class="ss-input"
        type="text"
        autocomplete="off"
        :value="open ? query : displayLabel"
        :placeholder="open ? '输入关键字过滤...' : placeholder"
        :disabled="disabled"
        @input="onInput"
        @focus="openMenu"
        @keydown.down.prevent="move(1)"
        @keydown.up.prevent="move(-1)"
        @keydown.enter.prevent="chooseActive"
        @keydown.esc.prevent="closeMenu"
        @keydown.tab="closeMenu"
      />
      <span class="ss-caret" :class="{ up: open }" aria-hidden="true">▾</span>
    </div>
    <div v-if="open" ref="menu" class="ss-menu" role="listbox">
      <div v-if="!filtered.length" class="ss-empty">{{ emptyText }}</div>
      <div
        v-for="(o, i) in filtered"
        :key="o.value"
        class="ss-option"
        :class="{ active: i === activeIndex, selected: String(o.value) === String(modelValue) }"
        role="option"
        :aria-selected="String(o.value) === String(modelValue) ? 'true' : 'false'"
        @mousedown.prevent="pick(o)"
        @mouseenter="activeIndex = i"
      >
        <span class="ss-option-label">{{ o.label }}</span>
        <span v-if="optionHint(o)" class="ss-option-hint">{{ optionHint(o) }}</span>
      </div>
    </div>
  </div>
</template>

<script>
/**
 * 可搜索下拉选择器（Playground 服务商/模型选择用）。
 *
 * 与原生 select 的差异：
 * - 输入即过滤：默认匹配 label + keywords；也可由父组件传 filterFn 自定义匹配
 *   （服务商下拉靠它实现「模型名命中 → 服务商也出现在结果里」）。
 * - hintFn(option, query) 可返回动态副标题，例如「含模型：xxx-free」。
 * - ↑/↓ 移动、Enter 选中、Esc/Tab/点击外部关闭。
 */
export default {
  name: 'SearchSelect',
  props: {
    modelValue: { type: [String, Number], default: '' },
    options: { type: Array, default: () => [] },
    placeholder: { type: String, default: '请选择' },
    emptyText: { type: String, default: '无匹配结果' },
    disabled: { type: Boolean, default: false },
    filterFn: { type: Function, default: null },
    hintFn: { type: Function, default: null },
  },
  emits: ['update:modelValue'],
  data() {
    return { open: false, query: '', activeIndex: 0 }
  },
  computed: {
    selectedOption() {
      return this.options.find((o) => String(o.value) === String(this.modelValue)) || null
    },
    displayLabel() {
      return this.selectedOption ? this.selectedOption.label : ''
    },
    filtered() {
      const q = (this.query || '').trim().toLowerCase()
      if (!q) return this.options
      if (this.filterFn) return this.options.filter((o) => this.filterFn(o, q))
      return this.options.filter((o) => this._matchText(o).includes(q))
    },
  },
  mounted() {
    document.addEventListener('mousedown', this.onDocMousedown)
  },
  beforeUnmount() {
    document.removeEventListener('mousedown', this.onDocMousedown)
  },
  methods: {
    _matchText(o) {
      return `${o.label || ''} ${o.keywords || ''}`.toLowerCase()
    },
    optionHint(o) {
      if (this.hintFn) return this.hintFn(o, this.query) || ''
      return o.hint || ''
    },
    openMenu() {
      if (this.disabled) return
      if (!this.open) {
        this.open = true
        this.query = ''
        const idx = this.filtered.findIndex((o) => String(o.value) === String(this.modelValue))
        this.activeIndex = idx >= 0 ? idx : 0
      }
      // 已展开时点控件（如箭头区域）也要把焦点落回输入框，否则无法继续键入
      this.$nextTick(() => {
        const el = this.$refs.input
        if (el) el.focus()
      })
    },
    closeMenu() {
      if (!this.open) return
      this.open = false
      this.query = ''
    },
    onInput(e) {
      this.query = e.target.value
      this.activeIndex = 0
    },
    move(step) {
      if (!this.open) { this.openMenu(); return }
      const n = this.filtered.length
      if (!n) return
      this.activeIndex = (this.activeIndex + step + n) % n
      this.$nextTick(() => {
        const menu = this.$refs.menu
        const el = menu && menu.querySelector('.ss-option.active')
        if (el && el.scrollIntoView) el.scrollIntoView({ block: 'nearest' })
      })
    },
    chooseActive() {
      const o = this.filtered[this.activeIndex]
      if (o) this.pick(o)
    },
    pick(o) {
      this.$emit('update:modelValue', o.value)
      this.closeMenu()
    },
    onDocMousedown(e) {
      const root = this.$refs.root
      if (root && !root.contains(e.target)) this.closeMenu()
    },
  },
}
</script>

<style scoped>
.search-select { position: relative; width: 100%; }
.ss-control { position: relative; }
.ss-input { padding-right: 30px; cursor: text; }
.ss-caret {
  position: absolute; right: 11px; top: 50%; transform: translateY(-50%);
  color: var(--text-muted); font-size: 11px; pointer-events: none;
  transition: transform var(--dur-fast) var(--ease-out);
}
.ss-caret.up { transform: translateY(-50%) rotate(180deg); }
.ss-menu {
  position: absolute; top: calc(100% + 4px); left: 0; right: 0; z-index: 60;
  max-height: 320px; overflow-y: auto; padding: 4px;
  background: var(--bg-elevated); border: 1px solid var(--border-medium);
  border-radius: var(--radius-md); box-shadow: var(--shadow-lg);
}
.ss-empty { padding: 10px 12px; font-size: var(--text-sm); color: var(--text-muted); }
.ss-option {
  display: flex; align-items: baseline; gap: 8px;
  padding: 7px 10px; border-radius: var(--radius-sm);
  font-size: var(--text-base); color: var(--text-primary); cursor: pointer;
}
.ss-option.active { background: var(--bg-hover); }
.ss-option.selected { color: var(--primary); }
.ss-option-label { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.ss-option-hint {
  flex-shrink: 0; max-width: 55%; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  font-size: var(--text-xs); color: var(--text-muted);
}
.is-disabled { opacity: 0.55; }
</style>
