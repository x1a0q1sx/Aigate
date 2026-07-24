<template>
  <div>
    <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 20px;">
      <h1>Auto 选举 ⚖️</h1>
      <button class="btn btn-primary" @click="load" :disabled="loading">{{ loading ? '刷新中...' : '刷新排名' }}</button>
    </div>

    <div class="card" style="margin-bottom: 16px;">
      <h2 style="margin-bottom: 12px;">权重比例</h2>
      <p style="color: var(--gray-500); font-size: 13px; margin-bottom: 16px;">速度、智力、稳定性三项合计必须为 100%。保存后会影响 Auto 模型选举排序。</p>
      <div class="weights-grid">
        <label>
          <span>速度 {{ percent(form.w_speed) }}</span>
          <input type="range" min="0" max="100" step="1" v-model.number="speedPercent" @input="syncFromPercent" />
          <input type="number" min="0" max="100" step="1" v-model.number="speedPercent" @input="syncFromPercent" />
        </label>
        <label>
          <span>智力 {{ percent(form.w_intel) }}</span>
          <input type="range" min="0" max="100" step="1" v-model.number="intelPercent" @input="syncFromPercent" />
          <input type="number" min="0" max="100" step="1" v-model.number="intelPercent" @input="syncFromPercent" />
        </label>
        <label>
          <span>稳定性 {{ percent(form.w_stab) }}</span>
          <input type="range" min="0" max="100" step="1" v-model.number="stabPercent" @input="syncFromPercent" />
          <input type="number" min="0" max="100" step="1" v-model.number="stabPercent" @input="syncFromPercent" />
        </label>
      </div>
      <div style="display: flex; justify-content: space-between; align-items: center; margin-top: 16px; gap: 12px; flex-wrap: wrap;">
        <span :style="{color: totalPercent === 100 ? 'var(--success)' : 'var(--danger)'}">当前合计：{{ totalPercent }}%</span>
        <div style="display: flex; gap: 8px;">
          <button class="btn btn-outline" @click="applyPreset(30, 50, 20)">默认 30/50/20</button>
          <button class="btn btn-outline" @click="applyPreset(50, 25, 25)">速度优先</button>
          <button class="btn btn-outline" @click="applyPreset(20, 60, 20)">智力优先</button>
          <button class="btn btn-primary" @click="saveWeights" :disabled="saving || totalPercent !== 100">{{ saving ? '保存中...' : '保存权重' }}</button>
        </div>
      </div>
    </div>

    <div class="card">
      <h2 style="margin-bottom: 12px;">模型评分</h2>
      <table>
        <thead>
          <tr>
            <th>排名</th>
            <th>模型</th>
            <th>服务商</th>
            <th>速度</th>
            <th>智力</th>
            <th>稳定性</th>
            <th>总分</th>
            <th>干预</th>
            <th>状态</th>
          </tr>
        </thead>
        <tbody>
          <tr v-if="ranking.length === 0">
            <td colspan="9" style="text-align: center; color: var(--gray-500); padding: 32px;">暂无 Auto 候选评分</td>
          </tr>
          <tr v-for="(item, index) in ranking" :key="item.model_id" :style="{opacity: item.excluded_reason ? 0.55 : 1}">
            <td>
              <span v-if="index === 0 && !item.excluded_reason" style="font-size: 20px;">🥇</span>
              <span v-else-if="index === 1 && !item.excluded_reason" style="font-size: 20px;">🥈</span>
              <span v-else-if="index === 2 && !item.excluded_reason" style="font-size: 20px;">🥉</span>
              <span v-else>{{ index + 1 }}</span>
            </td>
            <td><strong>{{ item.display_name }}</strong><div class="muted">{{ item.model_full_id }}</div></td>
            <td>{{ item.provider }}</td>
            <td :style="{color: speedColor(item.speed_score)}">{{ score(item.speed_score) }}<div class="muted">P50 {{ item.p50_ms || '-' }}ms</div></td>
            <td :style="{color: intelColor(item.intel_score)}">{{ score(item.intel_score) }}<div class="muted">{{ item.intel_source || '' }}</div></td>
            <td :style="{color: stabColor(item.stab_score)}">{{ item.stab_score != null ? score(item.stab_score) : '-' }}<div class="muted">成功率 {{ item.success_rate != null ? Number(item.success_rate).toFixed(1) + '%' : '-' }}</div></td>
            <td :style="{fontWeight: 'bold', color: item.excluded_reason ? 'var(--gray-500)' : 'var(--primary)'}">{{ item.excluded_reason ? '-' : score(item.final_score) }}</td>
            <td>
              <span v-if="item.priority_boost > 0" style="color: var(--success); font-weight: bold;">⬆️+{{ item.priority_boost }}</span>
              <span v-else-if="item.priority_boost < 0" style="color: var(--danger); font-weight: bold;">⬇️{{ item.priority_boost }}</span>
              <span v-else style="color: var(--text-muted);">-</span>
              <span v-if="item.cooldown_until" :title="'冷却至 ' + item.cooldown_until" style="color: var(--warning); font-size: 11px; display: block; margin-top: 2px;">⏳ {{ cooldownText(item.cooldown_until) }}</span>
              <span v-else-if="item.fail_count > 0" style="color: var(--gray-500); font-size: 11px; display: block; margin-top: 2px;">❌×{{ item.fail_count }}</span>
            </td>
            <td>
              <span v-if="item.excluded_reason" class="badge badge-warning">{{ item.excluded_reason }}</span>
              <span v-else class="badge badge-success">参与选举</span>
            </td>
          </tr>
        </tbody>
      </table>
      <div v-if="ranking.length > 0" style="padding: 12px; color: var(--gray-500); font-size: 13px; text-align: center;">
        共 {{ totalCandidates }} 个候选 · 权重: {{ percent(form.w_speed) }} / {{ percent(form.w_intel) }} / {{ percent(form.w_stab) }}
      </div>
    </div>
  </div>
</template>

<script>
import api from '../api.js'

export default {
  name: 'AutoView',
  data() {
    return {
      loading: false,
      saving: false,
      ranking: [],
      totalCandidates: 0,
      form: { w_speed: 0.3, w_intel: 0.5, w_stab: 0.2 },
      speedPercent: 30,
      intelPercent: 50,
      stabPercent: 20
    }
  },
  computed: {
    totalPercent() {
      return Number(this.speedPercent || 0) + Number(this.intelPercent || 0) + Number(this.stabPercent || 0)
    }
  },
  async mounted() {
    await this.load()
  },
  methods: {
    async load() {
      this.loading = true
      try {
        const data = await api.getAutoRanking()
        this.ranking = data.ranking || []
        this.totalCandidates = (typeof data.total_candidates === 'number') ? data.total_candidates : (data.ranking ? data.ranking.length : 0)
        const weights = data.weights || await api.getRoutingWeights()
        this.setWeights(weights)
      } catch (e) {
        alert('加载 Auto 排名失败: ' + e.message)
      } finally {
        this.loading = false
      }
    },
    setWeights(weights) {
      this.form = {
        w_speed: Number(weights.w_speed ?? 0.3),
        w_intel: Number(weights.w_intel ?? 0.5),
        w_stab: Number(weights.w_stab ?? 0.2)
      }
      this.speedPercent = Math.round(this.form.w_speed * 100)
      this.intelPercent = Math.round(this.form.w_intel * 100)
      this.stabPercent = Math.round(this.form.w_stab * 100)
    },
    syncFromPercent() {
      this.form = {
        w_speed: Number(this.speedPercent || 0) / 100,
        w_intel: Number(this.intelPercent || 0) / 100,
        w_stab: Number(this.stabPercent || 0) / 100
      }
    },
    applyPreset(speed, intel, stab) {
      this.speedPercent = speed
      this.intelPercent = intel
      this.stabPercent = stab
      this.syncFromPercent()
    },
    async saveWeights() {
      if (this.totalPercent !== 100) return
      this.saving = true
      try {
        await api.updateRoutingWeights(this.form)
        await this.load()
      } catch (e) {
        alert('保存权重失败: ' + e.message)
      } finally {
        this.saving = false
      }
    },
    percent(value) {
      return `${Math.round(Number(value || 0) * 100)}%`
    },
    score(value) {
      if (value === null || value === undefined) return '-'
      return Number(value).toFixed(1)
    },
    speedColor(v) {
      if (!v || v === 0) return 'var(--text-muted)'
      if (v >= 80) return 'var(--success)'
      if (v >= 50) return 'var(--warning)'
      return 'var(--danger)'
    },
    intelColor(v) {
      if (!v) return 'var(--text-muted)'
      if (v >= 80) return 'var(--success)'
      if (v >= 60) return 'var(--warning)'
      return 'var(--danger)'
    },
    stabColor(v) {
      if (v === null || v === undefined) return 'var(--text-muted)'
      if (v >= 95) return 'var(--success)'
      if (v >= 80) return 'var(--warning)'
      return 'var(--danger)'
    },
    cooldownText(until) {
      if (!until) return ''
      const diff = new Date(until) - Date.now()
      if (diff <= 0) return '0s'
      const s = Math.ceil(diff / 1000)
      if (s < 60) return s + 's'
      if (s < 3600) return Math.ceil(s / 60) + 'm'
      return Math.ceil(s / 3600) + 'h'
    }
  }
}
</script>

<style scoped>
.weights-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
  gap: 16px;
}
.weights-grid label {
  display: flex;
  flex-direction: column;
  gap: 8px;
  font-weight: 600;
}
.weights-grid input[type="number"] {
  width: 100px;
}
.muted {
  color: var(--gray-500);
  font-size: 12px;
  margin-top: 2px;
}
</style>
