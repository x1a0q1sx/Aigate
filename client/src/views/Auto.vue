<template>
  <div>
    <PageHeader title="Auto 选举" icon="scale" subtitle="基于速度、智力、稳定性三维评分的模型自动选举排序">
      <template #actions>
        <button class="btn btn-primary" @click="load" :disabled="loading">
          <AppIcon name="refresh" :size="14" />
          {{ loading ? '刷新中...' : '刷新排名' }}
        </button>
      </template>
    </PageHeader>

    <!-- 权重设置 -->
    <section class="card">
      <div class="card-header">
        <span class="card-title"><AppIcon name="sliders" :size="16" />权重比例</span>
      </div>
      <p class="weights-hint">速度、智力、稳定性三项合计必须为 100%。保存后会影响 Auto 模型选举排序。</p>
      <div class="weights-grid">
        <label class="weight-item">
          <span class="weight-label">
            <AppIcon name="zap" :size="14" />
            速度 {{ percent(form.w_speed) }}
          </span>
          <input type="range" min="0" max="100" step="1" v-model.number="speedPercent" @input="syncFromPercent" />
          <input type="number" min="0" max="100" step="1" v-model.number="speedPercent" @input="syncFromPercent" class="weight-number" />
        </label>
        <label class="weight-item">
          <span class="weight-label">
            <AppIcon name="cpu" :size="14" />
            智力 {{ percent(form.w_intel) }}
          </span>
          <input type="range" min="0" max="100" step="1" v-model.number="intelPercent" @input="syncFromPercent" />
          <input type="number" min="0" max="100" step="1" v-model.number="intelPercent" @input="syncFromPercent" class="weight-number" />
        </label>
        <label class="weight-item">
          <span class="weight-label">
            <AppIcon name="shield" :size="14" />
            稳定性 {{ percent(form.w_stab) }}
          </span>
          <input type="range" min="0" max="100" step="1" v-model.number="stabPercent" @input="syncFromPercent" />
          <input type="number" min="0" max="100" step="1" v-model.number="stabPercent" @input="syncFromPercent" class="weight-number" />
        </label>
      </div>
      <div class="weights-footer">
        <span class="weight-total" :class="totalPercent === 100 ? 'total-ok' : 'total-bad'">
          <AppIcon :name="totalPercent === 100 ? 'checkCircle' : 'alert'" :size="14" />
          当前合计：{{ totalPercent }}%
        </span>
        <div class="weights-actions">
          <button class="btn btn-outline btn-sm" @click="applyPreset(30, 50, 20)">默认 30/50/20</button>
          <button class="btn btn-outline btn-sm" @click="applyPreset(50, 25, 25)">速度优先</button>
          <button class="btn btn-outline btn-sm" @click="applyPreset(20, 60, 20)">智力优先</button>
          <button class="btn btn-primary btn-sm" @click="saveWeights" :disabled="saving || totalPercent !== 100">
            {{ saving ? '保存中...' : '保存权重' }}
          </button>
        </div>
      </div>
    </section>

    <!-- 模型评分排名 -->
    <section class="card">
      <div class="card-header">
        <span class="card-title"><AppIcon name="chart" :size="16" />模型评分</span>
      </div>

      <EmptyState
        v-if="ranking.length === 0"
        icon="inbox"
        title="暂无 Auto 候选评分"
        hint="配置模型并启用 Auto 路由后将显示排名数据"
        small
      />

      <table v-else>
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
          <tr v-for="(item, index) in ranking" :key="item.model_id" :class="{ 'row-excluded': item.excluded_reason }">
            <td>
              <span v-if="index === 0 && !item.excluded_reason" class="rank-badge rank-1">1</span>
              <span v-else-if="index === 1 && !item.excluded_reason" class="rank-badge rank-2">2</span>
              <span v-else-if="index === 2 && !item.excluded_reason" class="rank-badge rank-3">3</span>
              <span v-else class="rank-badge">{{ index + 1 }}</span>
            </td>
            <td>
              <strong>{{ item.display_name }}</strong>
              <div class="text-sub">{{ item.model_full_id }}</div>
            </td>
            <td>{{ item.provider }}</td>
            <td :style="{ color: speedColor(item.speed_score) }">
              {{ score(item.speed_score) }}
              <div class="text-sub">P50 {{ item.p50_ms || '-' }}ms</div>
            </td>
            <td :style="{ color: intelColor(item.intel_score) }">
              {{ score(item.intel_score) }}
              <div class="text-sub">{{ item.intel_source || '' }}</div>
            </td>
            <td :style="{ color: stabColor(item.stab_score) }">
              {{ item.stab_score != null ? score(item.stab_score) : '-' }}
              <div class="text-sub">成功率 {{ item.success_rate != null ? Number(item.success_rate).toFixed(1) + '%' : '-' }}</div>
            </td>
            <td class="score-cell" :class="item.excluded_reason ? 'score-excluded' : 'score-active'">
              {{ item.excluded_reason ? '-' : score(item.final_score) }}
            </td>
            <td>
              <span v-if="item.priority_boost > 0" class="boost-up">
                <AppIcon name="arrowUp" :size="12" /> +{{ item.priority_boost }}
              </span>
              <span v-else-if="item.priority_boost < 0" class="boost-down">
                <AppIcon name="arrowDown" :size="12" /> {{ item.priority_boost }}
              </span>
              <span v-else class="text-muted">-</span>
              <span v-if="item.cooldown_until" class="cooldown-tag" :title="'冷却至 ' + item.cooldown_until">
                <AppIcon name="clock" :size="11" /> {{ cooldownText(item.cooldown_until) }}
              </span>
              <span v-else-if="item.fail_count > 0" class="fail-tag">
                <AppIcon name="xCircle" :size="11" /> x{{ item.fail_count }}
              </span>
            </td>
            <td>
              <span v-if="item.excluded_reason" class="badge badge-warning">{{ item.excluded_reason }}</span>
              <span v-else class="badge badge-success">参与选举</span>
            </td>
          </tr>
        </tbody>
      </table>
      <div v-if="ranking.length > 0" class="table-footer">
        共 {{ totalCandidates }} 个候选 · 权重: {{ percent(form.w_speed) }} / {{ percent(form.w_intel) }} / {{ percent(form.w_stab) }}
      </div>
    </section>
  </div>
</template>

<script>
import api from '../api.js'
import toast from '../toast.js'
import AppIcon from '../components/AppIcon.vue'
import PageHeader from '../components/PageHeader.vue'
import EmptyState from '../components/EmptyState.vue'

export default {
  name: 'AutoView',
  components: { AppIcon, PageHeader, EmptyState },
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
        toast.error('加载 Auto 排名失败: ' + e.message)
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
        toast.success('权重已保存')
        await this.load()
      } catch (e) {
        toast.error('保存权重失败: ' + e.message)
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
/* Weights section */
.weights-hint {
  color: var(--text-muted);
  font-size: var(--text-sm);
  margin-bottom: var(--space-4);
}
.weights-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
  gap: var(--space-4);
}
.weight-item {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
}
.weight-label {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  font-weight: 600;
  font-size: var(--text-sm);
}
.weight-number {
  width: 100px;
}
.weights-footer {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-top: var(--space-4);
  gap: var(--space-3);
  flex-wrap: wrap;
}
.weight-total {
  display: flex;
  align-items: center;
  gap: var(--space-1);
  font-weight: 600;
  font-size: var(--text-sm);
}
.total-ok {
  color: var(--success);
}
.total-bad {
  color: var(--danger);
}
.weights-actions {
  display: flex;
  gap: var(--space-2);
  flex-wrap: wrap;
}

/* Ranking table */
.row-excluded {
  opacity: 0.55;
}
.text-sub {
  color: var(--text-muted);
  font-size: var(--text-xs);
  margin-top: 2px;
}

/* Rank badges */
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

/* Score cells */
.score-cell {
  font-weight: 700;
}
.score-active {
  color: var(--primary);
}
.score-excluded {
  color: var(--text-muted);
}

/* Intervention indicators */
.boost-up {
  display: inline-flex;
  align-items: center;
  gap: 2px;
  color: var(--success);
  font-weight: 700;
  font-size: var(--text-sm);
}
.boost-down {
  display: inline-flex;
  align-items: center;
  gap: 2px;
  color: var(--danger);
  font-weight: 700;
  font-size: var(--text-sm);
}
.cooldown-tag {
  display: flex;
  align-items: center;
  gap: 2px;
  color: var(--warning);
  font-size: var(--text-xs);
  margin-top: 2px;
}
.fail-tag {
  display: flex;
  align-items: center;
  gap: 2px;
  color: var(--text-muted);
  font-size: var(--text-xs);
  margin-top: 2px;
}

/* Table footer */
.table-footer {
  padding: var(--space-3);
  color: var(--text-muted);
  font-size: var(--text-sm);
  text-align: center;
}
</style>
