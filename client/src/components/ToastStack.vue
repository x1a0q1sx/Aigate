<template>
  <div class="toast-stack" role="status" aria-live="polite">
    <div v-for="t in items" :key="t.id" class="toast" :class="t.type" @click="dismiss(t.id)">
      <AppIcon class="toast-icon" :name="iconFor(t.type)" :size="16" />
      <span class="toast-text">{{ t.text }}</span>
    </div>
  </div>
</template>

<script>
import { toastState, dismiss } from '../toast.js'
import AppIcon from './AppIcon.vue'

export default {
  name: 'ToastStack',
  components: { AppIcon },
  computed: {
    items() {
      return toastState.items
    },
  },
  methods: {
    dismiss,
    iconFor(type) {
      return { success: 'checkCircle', error: 'xCircle', warning: 'alert', info: 'info' }[type] || 'info'
    },
  },
}
</script>

<style scoped>
.toast { cursor: pointer; }
</style>
