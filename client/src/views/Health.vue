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
    <!-- 失败罚时 / 冷却总览 -->
    <div class="card" v-if="cooling" style="margin-bottom: 20px;">
      <h2 style="margin-bottom: 12px; display: flex; align-items: center; gap: 10px;">
        ❄️ 失败罚时 / 冷却总览
        <span class="cooling-refresh" @click="loadCooling" title="立即刷新">⟳</span>
      </h2>
      <div class="cooling-summary" v-if="cooling.summary">
        <span class="cs-badge" :class="cooling.summary.model_cooling_count ? 'cs-warn' : 'cs-ok'">模型罚时 {{ cooling.summary.model_cooling_count }}</span>
        <span class="cs-badge" :class="cooling.summary.key_cooling_count ? 'cs-warn' : 'cs-ok'">密钥冷却 {{ cooling.summary.key_cooling_count }}</span>
        <span class="cs-badge" :class="cooling.summary.proxy_cooling_count ? 'cs-warn' : 'cs-ok'">代理冷却 {{ cooling.summary.proxy_cooling_count }}</span>
        <span class="cs-badge cs-neutral" v-if="!cooling.summary.proxy_enabled">代理未启用</span>
      </div>

      <div v-if="cooling.model_cooling.length" class="cooling-group">
        <h3 style="display: flex; align-items: center; gap: 10px;">
          🤖 模型失败冷却（真实流量驱动，指数退避 30s~1h）
          <button class="btn btn-outline" style="font-size: 12px; padding: 4px 10px;" @click="clearAllModelCooling" :disabled="clearingCooling">
            {{ clearingCooling ? '清除中...' : '🧹 一键清除模型冷却' }}
          </button>
        </h3>
        <table>
          <thead><tr><th>模型</th><th>连续失败</th><th>剩余冷却</th><th>状态</th><th>操作</th></tr></thead>
          <tbody>
            <tr v-for="m in cooling.model_cooling" :key="'m' + m.model_id">
              <td><strong>{{ m.model_full_id }}</strong></td>
              <td>{{ m.fail_count }}</td>
              <td class="mono">{{ m.cooling ? fmtRemain(m.remaining_sec) : '已恢复' }}</td>
              <td><span :class="['badge', m.cooling ? 'badge-warning' : 'badge-success']">{{ m.cooling ? '冷却中' : '正常' }}</span></td>
              <td><button class="btn btn-outline" style="font-size: 11px; padding: 2px 8px;" @click="clearOneModelCooling(m.model_id)">清除</button></td>
            </tr>
          </tbody>
        </table>
      </div>

      <div v-if="cooling.key_cooling.length" class="cooling-group">
        <h3>🔑 密钥冷却 / 熔断（连续 3 次失败→冷却 60s；401/403 永久禁用）</h3>
        <table>
          <thead><tr><th>服务商</th><th>连续失败</th><th>剩余冷却</th><th>状态</th></tr></thead>
          <tbody>
            <tr v-for="k in cooling.key_cooling" :key="'k' + k.api_key_id">
              <td><strong>{{ k.provider }}</strong> <span class="mono dim">#{{ k.api_key_id }}</span></td>
              <td>{{ k.fail_count }}</td>
              <td class="mono">{{ k.hard_disabled ? '永久禁用' : (k.remaining_sec > 0 ? fmtRemain(k.remaining_sec) : '已恢复') }}</td>
              <td><span :class="['badge', k.hard_disabled ? 'badge-danger' : (k.remaining_sec > 0 ? 'badge-warning' : 'badge-success')]">{{ k.hard_disabled ? '硬禁用' : (k.remaining_sec > 0 ? '冷却中' : '正常') }}</span></td>
            </tr>
          </tbody>
        </table>
      </div>

      <div v-if="cooling.proxy_cooling.length" class="cooling-group">
        <h3>🌐 代理冷却（连续 3 次失败→冷却 30s）</h3>
        <table>
          <thead><tr><th>代理</th><th>连续失败</th><th>剩余冷却</th><th>状态</th></tr></thead>
          <tbody>
            <tr v-for="p in cooling.proxy_cooling" :key="'p' + p.name">
              <td><strong>{{ p.name }}</strong> <span class="dim">{{ p.url }}</span></td>
              <td>{{ p.fail_count }}</td>
              <td class="mono">{{ p.remaining_sec > 0 ? fmtRemain(p.remaining_sec) : '已恢复' }}</td>
              <td><span :class="['badge', p.remaining_sec > 0 ? 'badge-warning' : 'badge-success']">{{ p.remaining_sec > 0 ? '冷却中' : '正常' }}</span></td>
            </tr>
          </tbody>
        </table>
      </div>

      <p v-if="!cooling.model_cooling.length && !cooling.key_cooling.length && !cooling.proxy_cooling.length"
         style="text-align: center; color: var(--text-muted); padding: 16px;">
        ✅ 当前没有模型、密钥或代理处于冷却 / 罚时状态
      </p>
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
            <td :style="{color: h.latency_ms ? barColor(h.latency_ms) : 'var(--text-muted)', fontWeight: 'bold'}">
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
      <p style="text-align: center; color: var(--text-muted); padding: 40px;">
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
      pinging: false,
      cooling: null,
      _coolingTimer: null,           // 服务端拉取定时器（慢，发请求）
      _countdownTimer: null,         // 本地倒计时定时器（1s，仅更新显示，不发请求）
      coolingRefreshMs: 10000,       // 服务端冷却总览拉取间隔（毫秒）
      clearingCooling: false,
      _coolingTick: 0
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
    await this.loadCooling()
    this.startCoolingTimer()
    document.addEventListener('visibilitychange', this.onVisibility)
  },
  beforeDestroy() {
    this.stopCoolingTimer()
    document.removeEventListener('visibilitychange', this.onVisibility)
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
    async loadCooling() {
      try {
        this.cooling = await api.getCooling()
      } catch (e) {
        console.error('加载冷却总览失败:', e)
      }
    },
    startCoolingTimer() {
      this.stopCoolingTimer()
      // 本地倒计时：每秒仅更新显示，不发起任何网络请求
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
      // 服务端校正：每 coolingRefreshMs 拉一次（捕获新冷却 / 漂移）
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
      // setInterval 不会因标签页不可见而自动暂停；切到其他标签页时停轮询，切回时恢复
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
        alert(`已清除 ${res.cleared || 0} 个模型的冷却惩罚`)
      } catch (e) {
        alert('清除失败: ' + e.message)
      } finally {
        this.clearingCooling = false
      }
    },
    async clearOneModelCooling(modelId) {
      try {
        await api.clearModelCooling(modelId)
        await this.loadCooling()
      } catch (e) {
        alert('清除失败: ' + e.message)
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
/* ── 失败罚时 / 冷却总览 ── */
.cooling-refresh {
  cursor: pointer;
  font-size: 16px;
  color: var(--gray-400);
  transition: transform 0.3s, color 0.2s;
  user-select: none;
}
.cooling-refresh:hover {
  color: var(--primary, #3b82f6);
  transform: rotate(180deg);
}
.cooling-summary {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  margin-bottom: 14px;
}
.cs-badge {
  font-size: 12px;
  font-weight: 600;
  padding: 3px 10px;
  border-radius: 999px;
  border: 1px solid transparent;
}
.cs-ok { background: rgba(34,197,94,0.12); color: #15803d; border-color: rgba(34,197,94,0.3); }
.cs-warn { background: rgba(234,179,8,0.14); color: #a16207; border-color: rgba(234,179,8,0.35); }
.cs-neutral { background: var(--gray-100); color: var(--gray-500); }
.cooling-group {
  margin-top: 14px;
  padding-top: 12px;
  border-top: 1px solid var(--gray-100);
}
.cooling-group h3 {
  font-size: 14px;
  color: var(--gray-600);
  margin-bottom: 10px;
  font-weight: 600;
}
.mono { font-family: monospace; }
.dim { color: var(--text-muted); font-size: 12px; }
</style>