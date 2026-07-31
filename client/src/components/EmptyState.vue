<template>
  <div class="empty-state" :class="{ small }">
    <AppIcon v-if="icon" :name="icon" :size="small ? 22 : 30" :stroke-width="1.5" />
    <div class="empty-state-title">{{ title }}</div>
    <div v-if="hint" class="text-sm">{{ hint }}</div>
    <slot />
  </div>
</template>

<script>
import AppIcon from './AppIcon.vue'

export default {
  name: 'EmptyState',
  components: { AppIcon },
  props: {
    title: { type: String, default: '暂无数据' },
    hint: { type: String, default: '' },
    icon: { type: String, default: 'inbox' },
    small: { type: Boolean, default: false },
  },
}
</script>
