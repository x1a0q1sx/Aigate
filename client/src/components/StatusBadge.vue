<template>
  <span :class="badgeClass">{{ statusText }}</span>
</template>
<script>
export default {
  name: 'StatusBadge',
  props: {
    status: {
      type: String,
      required: true
    }
  },
  computed: {
    badgeClass() {
      return `badge badge-${this.status}`
    },
    statusText() {
      const map = {
        healthy: '健康',
        degraded: '延迟',
        rate_limited: '限流',
        unhealthy: '故障',
        unknown: '未知'
      }
      return map[this.status] || this.status
    }
  }
}
</script>