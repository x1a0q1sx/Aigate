<template>
  <span class="badge" :class="toneClass">
    <span class="dot"></span>{{ statusText }}
  </span>
</template>

<script>
const TONE = {
  healthy: 'badge-success',
  degraded: 'badge-warning',
  rate_limited: 'badge-info',
  unhealthy: 'badge-danger',
  unknown: 'badge-neutral',
}
const LABEL = {
  healthy: '健康',
  degraded: '延迟',
  rate_limited: '限流',
  unhealthy: '故障',
  unknown: '未知',
}

export default {
  name: 'StatusBadge',
  props: {
    status: { type: String, required: true },
  },
  computed: {
    toneClass() {
      return TONE[this.status] || 'badge-neutral'
    },
    statusText() {
      return LABEL[this.status] || this.status
    },
  },
}

export { TONE as STATUS_TONE, LABEL as STATUS_LABEL }
</script>
