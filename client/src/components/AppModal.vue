<template>
  <Teleport to="body">
    <div v-if="modelValue" class="modal-overlay" @click.self="close">
      <div class="modal-content" :class="sizeClass" role="dialog" aria-modal="true" :aria-label="title">
        <div class="modal-head">
          <h3>
            <AppIcon v-if="icon" :name="icon" :size="18" />
            {{ title }}
          </h3>
          <button class="modal-close" type="button" aria-label="关闭" @click="close">
            <AppIcon name="close" :size="16" />
          </button>
        </div>

        <div class="modal-body">
          <slot />
        </div>

        <div v-if="$slots.footer" class="modal-actions">
          <slot name="footer" />
        </div>
      </div>
    </div>
  </Teleport>
</template>

<script>
import AppIcon from './AppIcon.vue'

export default {
  name: 'AppModal',
  components: { AppIcon },
  props: {
    modelValue: { type: Boolean, default: false },
    title: { type: String, default: '' },
    icon: { type: String, default: '' },
    /** sm 440 / md 560 / lg 720 / xl 1080 */
    size: { type: String, default: 'md' },
    closeOnEsc: { type: Boolean, default: true },
  },
  emits: ['update:modelValue', 'close'],
  computed: {
    sizeClass() {
      return this.size === 'md' ? '' : this.size
    },
  },
  watch: {
    modelValue(open) {
      // 打开弹窗时锁掉背景滚动，关闭后恢复
      document.body.style.overflow = open ? 'hidden' : ''
    },
  },
  mounted() {
    document.addEventListener('keydown', this.onKeydown)
  },
  beforeUnmount() {
    document.removeEventListener('keydown', this.onKeydown)
    document.body.style.overflow = ''
  },
  methods: {
    onKeydown(e) {
      if (e.key === 'Escape' && this.modelValue && this.closeOnEsc) this.close()
    },
    close() {
      this.$emit('update:modelValue', false)
      this.$emit('close')
    },
  },
}
</script>
