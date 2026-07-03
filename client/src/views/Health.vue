<template>
  <div>
    <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 20px;">
      <h1>健康监控 💓</h1>
      <button class="btn btn-primary" @click="pingAll" :disabled="pinging">
        {{ pinging ? '测速中...' : '🚀 一键测速' }}
      </button>
    </div>
    <!-- 延迟柱状图 -->
    <div class="card" v-if="chartData.length > 0" style="margin-bottom: 20px;">
      <h2 style="margin-bottom: 16px;">📊 延迟对比 (ms)</h2>
      <div class="bar-chart">
        <div v-for="item in chartData" :key="item.model_id" class="bar-item" @click="pingSingle(item.model_id)" title="点击测速">
          <div class="bar-label" :title="item.model_full_id">{{ item.short_name }}</div>
          <div class="bar-track">
            <div class="bar-fill"
                 :style="{width: barWidth(item.latency_ms) + '%', background: barColor(item.latency_ms)}">
            </div>
            <span class="bar-value" :style="{color: barColor(item.latency_ms)}">{{ Math.round(item.latency_ms || 0) }}ms</span>
          </div>
        </div>
      </div>
    </div>
    <!-- 延迟统计 -->
    <div class="stats-grid" v-if="latencyStats" style="margin-bottom: 20px;">
      <div class="stat-card">
        <div class="stat-number" style="color: var(--success);">{{ Math.round((latencyStats.average_latency_ms) || 0) }}ms</div>
        <div class="stat-label">平均延迟</div>
      </div>
      <div class="stat-card">
        <div class="stat-number">{{ latencyStats.total_models }}</div>
        <div class="stat-label">已测速模型</div>
      </div>
    </div>
    <!-- 最快 Top5 -->
    <div class="card" v-if="latencyStats && latencyStats.fastest && latencyStats.fastest.length > 0" style="margin-bottom: 20px;">
      <h2 style="margin-bottom: 12px;">⚡ 最快模型 Top5</h2>
      <table>
        <thead>
          <tr><th>排名</th><th>模型</th><th>延迟</th><th>状态</th></tr>
        </thead>
        <tbody>
          <tr v-for="(item, idx) in latencyStats.fastest" :key="item.model_id">
            <td>{{ ['🥇','🥈','🥉','4','5'][idx] }}</td>
            <td><strong>{{ item.model_full_id }}</strong></td>
            <td :style="{color: barColor(item.latency_ms), fontWeight: 'bold'}">{{ Math.round(item.latency_ms || 0) }}ms</td>
            <td><span :class="['badge', statusBadgeClass(item.status)]">{{ statusEmoji(item.status) }} {{ item.status }}</span></td>
          </tr>
        </tbody>
      </table>
    </div>
    <!-- 健康记录表 -->
    <div class="card" v-if="healthData.length > 0">
      <h2 style="margin-bottom: 12px;">📋 最近探测记录</h2>
      <table>
        <thead>
          <tr>
            <th>模型</th>
            <th>状态</th>
            <th>延迟</th>
            <th>最后探测（北京时间）</th>
            <th>错误信息</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="h in healthData" :key="h.model_id + '-' + h.last_checked">
            <td><strong>{{ h.model_full_id }}</strong></td>
            <td>
              <span :class="['badge', statusBadgeClass(h.status)]">
                {{ statusEmoji(h.status) }} {{ statusLabel(h.status) }}
              </span>
            </td>
            <td :style="{color: h.latency_ms ? barColor(h.latency_ms) : '#999', fontWeight: 'bold'}">
              {{ h.latency_ms ? Math.round(h.latency_ms || 0) + 'ms' : '-' }}
            </td>
            <td>{{ formatDate(h.last_checked) }}</td>
            <td style="font-size: 12px; color: var(--danger); max-width: 200px; overflow: hidden; text-overflow: ellipsis;">
              {{ h.error_message || '-' }}
            </td>
          </tr>
        </tbody>
      </table>
    </div>
    <div class="card" v-else>
      <p style="text-align: center; color: #999; padding: 40px;">
        暂无健康数据，系统将在后台自动探测已配置的模型 ⏳
      </p>
    </div>
  </div>
</template>
<script>
import api from '../api.js'
export default {
  name: 'HealthView',
  data() {
    return {
      healthData: [],
      latencyStats: null,
      pinging: false
    }
  },
  computed: {
    chartData() {
      // 从 healthData 中提取每个模型的最新延迟
      const map = {}
      for (const h of this.healthData) {
        if (!map[h.model_full_id] || new Date(h.last_checked) > new Date(map[h.model_full_id].last_checked)) {
          map[h.model_full_id] = h
        }
      }
      return Object.values(map)
        .filter(h => h.latency_ms && h.latency_ms > 0)
        .sort((a, b) => a.latency_ms - b.latency_ms)
        .map(h => ({
          ...h,
          short_name: h.model_full_id.length > 25 ? h.model_full_id.substring(0, 22) + '...' : h.model_full_id
        }))
    }
  },
  async mounted() {
    await this.loadAll()
  },
  methods: {
    async loadAll() {
      try {
        const [healthRes, statsRes] = await Promise.all([
          api.getHealth(),
          api.getLatencyStats()
        ])
        this.healthData = healthRes.items || []
        this.latencyStats = statsRes
      } catch (e) {
        console.error('加载健康数据失败:', e)
      }
    },
    barWidth(ms) {
      if (!ms || ms <= 0) return 1
      const max = Math.max(...this.chartData.map(h => h.latency_ms || 1), 1)
      return Math.max(2, Math.round((ms / max) * 100))
    },
    barColor(ms) {
      if (ms < 500) return 'var(--success)'
      if (ms < 2000) return 'var(--warning)'
      return 'var(--danger)'
    },
    statusBadgeClass(s) {
      const map = { healthy: 'badge-success', degraded: 'badge-warning', rate_limited: 'badge-info', unhealthy: 'badge-danger' }
      return map[s] || 'badge-neutral'
    },
    statusEmoji(s) {
      const map = { healthy: '✅', degraded: '⚠️', rate_limited: '⏸️', unhealthy: '❌' }
      return map[s] || '❓'
    },
    statusLabel(s) {
      const map = { healthy: '健康', degraded: '延迟', rate_limited: '限流', unhealthy: '故障' }
      return map[s] || s
    },
    formatDate(d) {
      if (!d) return '-'
      const normalized = typeof d === 'string' && !/[zZ]|[+-]\d{2}:?\d{2}$/.test(d) ? `${d}Z` : d
      const date = new Date(normalized)
      if (Number.isNaN(date.getTime())) return d
      return date.toLocaleString('zh-CN', { timeZone: 'Asia/Shanghai', hour12: false })
    },
    async pingSingle(modelId) {
      try {
        await api.pingModel(modelId)
        await this.loadAll()
      } catch (e) {
        console.error('测速失败:', e)
      }
    },
    async pingAll() {
      this.pinging = true
      try {
        await api.pingAllModels()
        await this.loadAll()
      } catch (e) {
        alert('批量测速失败: ' + e.message)
      } finally {
        this.pinging = false
      }
    }
  }
}
</script>
<style scoped>
.bar-chart {
  display: flex;
  flex-direction: column;
  gap: 10px;
}
.bar-item {
  display: flex;
  align-items: center;
  gap: 12px;
  cursor: pointer;
  padding: 4px 8px;
  border-radius: 6px;
  transition: background 0.2s;
}
.bar-item:hover {
  background: var(--gray-50);
}
.bar-label {
  width: 180px;
  font-size: 12px;
  font-family: monospace;
  text-align: right;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.bar-track {
  flex: 1;
  height: 24px;
  background: var(--gray-100);
  border-radius: 12px;
  position: relative;
  overflow: visible;
}
.bar-fill {
  height: 100%;
  border-radius: 12px;
  transition: width 0.5s ease;
  min-width: 4px;
  opacity: 0.7;
}
.bar-value {
  position: absolute;
  right: 8px;
  top: 50%;
  transform: translateY(-50%);
  font-size: 12px;
  font-weight: 600;
}
</style>