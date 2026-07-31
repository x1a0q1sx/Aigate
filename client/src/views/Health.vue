<template>
  <div>
    <PageHeader title="健康监控" icon="activity" subtitle="模型测速、延迟统计与失败冷却总览">
      <template #actions>
        <button class="btn btn-primary" @click="pingAll" :disabled="pinging">
          <AppIcon name="zap" :size="14" />
          {{ pinging ? '测速中...' : '一键测速' }}
        </button>
      </template>
    </PageHeader>

    <!-- 延迟柱状图 -->
    <section v-if="chartData.length > 0" class="card">
      <div class="card-header">
        <span class="card-title"><AppIcon name="chart" :size="16" />延迟对比 (ms)</span>
      </div>
      <div class="bar-chart">
        <div v-for="item in chartData" :key="item.model_id" class="bar-item" @click="pingSingle(item.model_id)" title="点击测速">
          <div class="bar-label mono" :title="item.model_full_id">{{ item.short_name }}</div>
          <div class="bar-track">
            <div class="bar-fill" :style="{ width: barWidth(item.latency_ms) + '%', background: barColor(item.latency_ms) }"></div>
            <span class="bar-value" :style="{ color: barColor(item.latency_ms) }">{{ Math.round(item.latency_ms || 0) }}ms</span>
          </div>
        </div>
      </div>
    </section>

    <!-- 延迟统计 -->
    <div v-if="latencyStats" class="stats-grid">
      <StatCard label="平均延迟" icon="clock" tone="ok" :value="Math.round(latencyStats.average_latency_ms || 0) + 'ms'" />
      <StatCard label="已测速模型" icon="cpu" :value="latencyStats.total_models" />
    </div>

    <!-- 最快 Top5 -->
    <section v-if="latencyStats && latencyStats.fastest && latencyStats.fastest.length > 0" class="card">
      <div class="card-header">
        <span class="card-title"><AppIcon name="zap" :size="16" />最快模型 Top5</span>
      </div>
      <table>
        <thead>
          <tr>
            <th>排名</th>
            <th>模型</th>
            <th>延迟</th>
            <th>状态</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="(item, idx) in latencyStats.fastest" :key="item.model_id">
            <td>
              <span class="rank-badge" :class="'rank-' + (idx + 1)">{{ idx + 1 }}</span>
            </td>
            <td><strong>{{ item.model_full_id }}</strong></td>
            <td>
              <span class="tabular" :style="{ color: barColor(item.latency_ms), fontWeight: 600 }">
                {{ Math.round(item.latency_ms || 0) }}ms
              </span>
            </td>
            <td>
              <span class="badge" :class="statusBadgeClass(item.status)">{{ statusLabel(item.status) }}</span>
            </td>
          </tr>
        </tbody>
      </table>
    </section>

    <!-- 失败罚时 / 冷却总览 -->
    <section v-if="cooling" class="card">
      <div class="card-header">
        <span class="card-title">
          <AppIcon name="snowflake" :size="16" />失败罚时 / 冷却总览
        </span>
        <button class="btn btn-ghost btn-xs" @click="loadCooling" title="立即刷新">
          <AppIcon name="refresh" :size="13" />
        </button>
      </div>

      <div v-if="cooling.summary" class="cooling-summary">
        <span class="badge" :class="cooling.summary.model_cooling_count ? 'badge-warning' : 'badge-success'">
          模型罚时 {{ cooling.summary.model_cooling_count }}
        </span>
        <span class="badge" :class="cooling.summary.key_cooling_count ? 'badge-warning' : 'badge-success'">
          密钥冷却 {{ cooling.summary.key_cooling_count }}
        </span>
        <span class="badge" :class="cooling.summary.proxy_cooling_count ? 'badge-warning' : 'badge-success'">
          代理冷却 {{ cooling.summary.proxy_cooling_count }}
        </span>
        <span v-if="!cooling.summary.proxy_enabled" class="badge badge-neutral">代理未启用</span>
      </div>

      <!-- 模型冷却 -->
      <div v-if="cooling.model_cooling.length" class="cooling-group">
        <div class="cooling-group-head">
          <h4><AppIcon name="cpu" :size="14" />模型失败冷却（指数退避 30s~1h）</h4>
          <button class="btn btn-outline btn-xs" @click="clearAllModelCooling" :disabled="clearingCooling">
            <AppIcon name="broom" :size="11" />
            {{ clearingCooling ? '清除中...' : '一键清除' }}
          </button>
        </div>
        <table>
          <thead>
            <tr><th>模型</th><th>连续失败</th><th>剩余冷却</th><th>状态</th><th>操作</th></tr>
          </thead>
          <tbody>
            <tr v-for="m in cooling.model_cooling" :key="'m' + m.model_id">
              <td><strong>{{ m.model_full_id }}</strong></td>
              <td class="tabular">{{ m.fail_count }}</td>
              <td class="mono">{{ m.cooling ? fmtRemain(m.remaining_sec) : '已恢复' }}</td>
              <td>
                <span class="badge" :class="m.cooling ? 'badge-warning' : 'badge-success'">
                  {{ m.cooling ? '冷却中' : '正常' }}
                </span>
              </td>
              <td>
                <button class="btn btn-outline btn-xs" @click="clearOneModelCooling(m.model_id)">清除</button>
              </td>
            </tr>
          </tbody>
        </table>
      </div>

      <!-- 密钥冷却 -->
      <div v-if="cooling.key_cooling.length" class="cooling-group">
        <h4><AppIcon name="key" :size="14" />密钥冷却 / 熔断（连续 3 次→60s；401/403 永久禁用）</h4>
        <table>
          <thead>
            <tr><th>服务商</th><th>连续失败</th><th>剩余冷却</th><th>状态</th></tr>
          </thead>
          <tbody>
            <tr v-for="k in cooling.key_cooling" :key="'k' + k.api_key_id">
              <td><strong>{{ k.provider }}</strong> <span class="mono text-xs text-muted">#{{ k.api_key_id }}</span></td>
              <td class="tabular">{{ k.fail_count }}</td>
              <td class="mono">{{ k.hard_disabled ? '永久禁用' : (k.remaining_sec > 0 ? fmtRemain(k.remaining_sec) : '已恢复') }}</td>
              <td>
                <span class="badge" :class="k.hard_disabled ? 'badge-danger' : (k.remaining_sec > 0 ? 'badge-warning' : 'badge-success')">
                  {{ k.hard_disabled ? '硬禁用' : (k.remaining_sec > 0 ? '冷却中' : '正常') }}
                </span>
              </td>
            </tr>
          </tbody>
        </table>
      </div>

      <!-- 代理冷却 -->
      <div v-if="cooling.proxy_cooling.length" class="cooling-group">
        <h4><AppIcon name="globe" :size="14" />代理冷却（连续 3 次失败→冷却 30s）</h4>
        <table>
          <thead>
            <tr><th>代理</th><th>连续失败</th><th>剩余冷却</th><th>状态</th></tr>
          </thead>
          <tbody>
            <tr v-for="p in cooling.proxy_cooling" :key="'p' + p.name">
              <td><strong>{{ p.name }}</strong> <span class="text-xs text-muted">{{ p.url }}</span></td>
              <td class="tabular">{{ p.fail_count }}</td>
              <td class="mono">{{ p.remaining_sec > 0 ? fmtRemain(p.remaining_sec) : '已恢复' }}</td>
              <td>
                <span class="badge" :class="p.remaining_sec > 0 ? 'badge-warning' : 'badge-success'">
                  {{ p.remaining_sec > 0 ? '冷却中' : '正常' }}
                </span>
              </td>
            </tr>
          </tbody>
        </table>
      </div>

      <EmptyState
        v-if="!cooling.model_cooling.length && !cooling.key_cooling.length && !cooling.proxy_cooling.length"
        icon="checkCircle"
        title="当前没有模型、密钥或代理处于冷却 / 罚时状态"
        small
      />
    </section>

    <!-- 健康记录表 -->
    <section v-if="healthData.length > 0" class="card">
      <div class="card-header">
        <span class="card-title"><AppIcon name="archive" :size="16" />最近探测记录</span>
      </div>
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
              <span class="badge" :class="statusBadgeClass(h.status)">{{ statusLabel(h.status) }}</span>
            </td>
            <td>
              <span v-if="h.latency_ms" class="tabular" :style="{ color: barColor(h.latency_ms), fontWeight: 600 }">
                {{ Math.round(h.latency_ms || 0) }}ms
              </span>
              <span v-else class="text-muted">-</span>
            </td>
            <td class="text-sm">{{ formatDate(h.last_checked) }}</td>
            <td class="error-cell text-xs">{{ h.error_message || '-' }}</td>
          </tr>
        </tbody>
      </table>
    </section>

    <section v-else class="card">
      <EmptyState icon="activity" title="暂无健康数据" hint="系统将在后台自动探测已配置的模型" />
    </section>
  </div>
</template>

<script>
import api from '../api.js'
import toast from '../toast.js'
import AppIcon from '../components/AppIcon.vue'
import PageHeader from '../components/PageHeader.vue'
import StatCard from '../components/StatCard.vue'
import EmptyState from '../components/EmptyState.vue'

export default {
  name: 'HealthView',
  components: { AppIcon, PageHeader, StatCard, EmptyState },
  data() {
    return {
      healthData: [],
      latencyStats: null,
      pinging: false,
      cooling: null,
      _coolingTimer: null,
      _countdownTimer: null,
      coolingRefreshMs: 10000,
      clearingCooling: false,
    }
  },
  computed: {
    chartData() {
      const map = {}
      for (const h of this.healthData) {
        if (!map[h.model_full_id] || new Date(h.last_checked) > new Date(map[h.model_full_id].last_checked)) {
          map[h.model_full_id] = h
        }
      }
      return Object.values(map)
        .filter((h) => h.latency_ms && h.latency_ms > 0)
        .sort((a, b) => a.latency_ms - b.latency_ms)
        .map((h) => ({
          ...h,
          short_name: h.model_full_id.length > 25 ? h.model_full_id.substring(0, 22) + '...' : h.model_full_id,
        }))
    },
  },
  async mounted() {
    await this.loadAll()
    await this.loadCooling()
    this.startCoolingTimer()
    document.addEventListener('visibilitychange', this.onVisibility)
  },
  beforeUnmount() {
    this.stopCoolingTimer()
    document.removeEventListener('visibilitychange', this.onVisibility)
  },
  methods: {
    async loadAll() {
      try {
        const [healthRes, statsRes] = await Promise.all([api.getHealth(), api.getLatencyStats()])
        this.healthData = healthRes.items || []
        this.latencyStats = statsRes
      } catch (e) {
        toast.error('加载健康数据失败: ' + e.message)
      }
    },
    async loadCooling() {
      try {
        this.cooling = await api.getCooling()
      } catch (e) {
        console.error('加载冷却总览失败:', e)
      }
    },
    startCoolingTimer() {
      this.stopCoolingTimer()
      this._countdownTimer = setInterval(() => {
        if (!this.cooling) return
        const dec = (arr) => {
          if (!arr) return
          for (const it of arr) {
            if (it.remaining_sec > 0) it.remaining_sec = Math.max(0, it.remaining_sec - 1)
          }
        }
        dec(this.cooling.model_cooling)
        dec(this.cooling.key_cooling)
        dec(this.cooling.proxy_cooling)
      }, 1000)
      this._coolingTimer = setInterval(() => {
        this.loadCooling()
      }, this.coolingRefreshMs)
    },
    stopCoolingTimer() {
      if (this._coolingTimer) {
        clearInterval(this._coolingTimer)
        this._coolingTimer = null
      }
      if (this._countdownTimer) {
        clearInterval(this._countdownTimer)
        this._countdownTimer = null
      }
    },
    onVisibility() {
      if (document.hidden) {
        this.stopCoolingTimer()
      } else {
        this.startCoolingTimer()
      }
    },
    fmtRemain(sec) {
      sec = Math.max(0, Math.floor(sec || 0))
      if (sec >= 60) {
        const m = Math.floor(sec / 60)
        const s = sec % 60
        return `${m}分${s.toString().padStart(2, '0')}秒`
      }
      return `${sec}秒`
    },
    async clearAllModelCooling() {
      if (!confirm('确定清除所有模型的失败冷却惩罚？')) return
      this.clearingCooling = true
      try {
        const res = await api.clearModelCooling(null)
        await this.loadCooling()
        toast.success(`已清除 ${res.cleared || 0} 个模型的冷却惩罚`)
      } catch (e) {
        toast.error('清除失败: ' + e.message)
      } finally {
        this.clearingCooling = false
      }
    },
    async clearOneModelCooling(modelId) {
      try {
        await api.clearModelCooling(modelId)
        await this.loadCooling()
        toast.success('已清除')
      } catch (e) {
        toast.error('清除失败: ' + e.message)
      }
    },
    barWidth(ms) {
      if (!ms || ms <= 0) return 1
      const max = Math.max(...this.chartData.map((h) => h.latency_ms || 1), 1)
      return Math.max(2, Math.round((ms / max) * 100))
    },
    barColor(ms) {
      if (ms < 500) return 'var(--success)'
      if (ms < 2000) return 'var(--warning)'
      return 'var(--danger)'
    },
    statusBadgeClass(s) {
      return { healthy: 'badge-success', degraded: 'badge-warning', rate_limited: 'badge-info', unhealthy: 'badge-danger' }[s] || 'badge-neutral'
    },
    statusLabel(s) {
      return { healthy: '健康', degraded: '延迟', rate_limited: '限流', unhealthy: '故障' }[s] || s
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
        toast.error('测速失败: ' + e.message)
      }
    },
    async pingAll() {
      this.pinging = true
      try {
        await api.pingAllModels()
        await this.loadAll()
        toast.success('批量测速完成')
      } catch (e) {
        toast.error('批量测速失败: ' + e.message)
      } finally {
        this.pinging = false
      }
    },
  },
}
</script>

<style scoped>
/* 柱状图 */
.bar-chart {
  display: flex;
  flex-direction: column;
  gap: var(--space-3);
}
.bar-item {
  display: flex;
  align-items: center;
  gap: var(--space-3);
  cursor: pointer;
  padding: 4px var(--space-2);
  border-radius: var(--radius-sm);
  transition: background 0.15s;
}
.bar-item:hover {
  background: var(--surface-2);
}
.bar-label {
  width: 180px;
  font-size: var(--text-xs);
  text-align: right;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.bar-track {
  flex: 1;
  height: 24px;
  background: var(--surface-2);
  border-radius: var(--radius-lg);
  position: relative;
  overflow: visible;
}
.bar-fill {
  height: 100%;
  border-radius: var(--radius-lg);
  transition: width 0.5s ease;
  min-width: 4px;
  opacity: 0.7;
}
.bar-value {
  position: absolute;
  right: 8px;
  top: 50%;
  transform: translateY(-50%);
  font-size: var(--text-xs);
  font-weight: 600;
}

/* 排名徽章 */
.rank-badge {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 24px;
  height: 24px;
  border-radius: 50%;
  font-weight: 700;
  font-size: var(--text-xs);
  background: var(--surface-3);
  color: var(--text-secondary);
}
.rank-1 {
  background: linear-gradient(135deg, #fbbf24 0%, #f59e0b 100%);
  color: #1a1006;
}
.rank-2 {
  background: linear-gradient(135deg, #94a3b8 0%, #64748b 100%);
  color: #fff;
}
.rank-3 {
  background: linear-gradient(135deg, #d97706 0%, #b45309 100%);
  color: #fff;
}

/* 冷却 */
.cooling-summary {
  display: flex;
  flex-wrap: wrap;
  gap: var(--space-2);
  margin-bottom: var(--space-4);
}
.cooling-group {
  margin-top: var(--space-4);
  padding-top: var(--space-3);
  border-top: 1px solid var(--border-base);
}
.cooling-group h4 {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  font-size: var(--text-sm);
  color: var(--text-secondary);
  margin-bottom: var(--space-3);
  font-weight: 600;
}
.cooling-group-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: var(--space-3);
}
.cooling-group-head h4 {
  margin-bottom: 0;
}

/* 错误列 */
.error-cell {
  max-width: 200px;
  overflow: hidden;
  text-overflow: ellipsis;
  color: var(--danger);
}

@media (max-width: 900px) {
  .bar-label {
    width: 100px;
  }
}
</style>
