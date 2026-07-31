<template>
  <header class="page-head">
    <div class="page-head-titles">
      <h1>
        <AppIcon v-if="icon" :name="icon" :size="22" :stroke-width="1.7" class="page-head-icon" />
        {{ title }}
      </h1>
      <p v-if="subtitle" class="page-sub">{{ subtitle }}</p>
      <slot name="sub" />
    </div>
    <div v-if="$slots.actions" class="page-head-actions">
      <slot name="actions" />
    </div>
  </header>
</template>

<script>
import AppIcon from './AppIcon.vue'

export default {
  name: 'PageHeader',
  components: { AppIcon },
  props: {
    title: { type: String, required: true },
    subtitle: { type: String, default: '' },
    icon: { type: String, default: '' },
  },
}
</script>

<style scoped>
.page-head-icon { color: var(--primary); }
</style>
