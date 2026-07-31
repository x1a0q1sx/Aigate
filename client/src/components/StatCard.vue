<template>
  <div class="stat-card">
    <div class="stat-label">
      <AppIcon v-if="icon" :name="icon" :size="13" />
      {{ label }}
    </div>
    <div class="stat-value" :class="tone">{{ value }}</div>
    <div v-if="sub" class="stat-sub">{{ sub }}</div>
    <slot />
  </div>
</template>

<script>
import AppIcon from './AppIcon.vue'

export default {
  name: 'StatCard',
  components: { AppIcon },
  props: {
    label: { type: String, required: true },
    value: { type: [String, Number], default: '-' },
    sub: { type: String, default: '' },
    icon: { type: String, default: '' },
    /** '' | accent | ok | warn | bad */
    tone: { type: String, default: 'accent' },
  },
}
</script>
