<template>
  <div>
    <PageHeader title="模型管理" icon="cpu" subtitle="管理所有服务商的模型列表，调整 Auto 选举、定价、测速与干预策略">
      <template #actions>
        <button class="btn btn-outline" @click="refreshAll" :disabled="refreshing">
          <AppIcon name="refresh" :size="14" />
          {{ refreshing ? '刷新中...' : '刷新模型列表' }}
        </button>
        <button class="btn btn-outline" @click="pingAll" :disabled="pingingAll">
          <AppIcon name="zap" :size="14" />
          {{ pingingAll ? '测速中...' : '一键测速' }}
        </button>
        <button class="btn btn-primary" @click="showAddModel = true">
          <AppIcon name="plus" :size="14" />添加模型
        </button>
      </template>
    </PageHeader>

    <ModelRefreshModal :visible="showRefreshModal" :result="refreshResult || {}" @close="showRefreshModal = false" />

    <!-- 测速进度条 -->
    <div v-if="pingingAll" class="card ping-progress">
      <div class="ping-row">
        <AppIcon name="clock" :size="14" />
        <span class="text-sm">正在测速...</span>
        <div class="progress">
          <div class="progress-bar" :style="{ width: pingProgress + '%' }"></div>
        </div>
        <span class="text-sm text-muted tabular">{{ pingDone }}/{{ pingTotal }}</span>
      </div>
    </div>

    <!-- 筛选工具栏 -->
    <div class="card filter-bar">
      <div class="input-group">
        <AppIcon name="search" :size="14" />
        <input v-model.trim="searchQuery" @keyup.enter="load" placeholder="搜索模型 / 显示名 / 服务商" />
      </div>
      <button class="btn btn-outline btn-sm" @click="load">
        <AppIcon name="search" :size="12" />搜索
      </button>
      <button v-if="searchQuery" class="btn btn-ghost btn-sm" @click="clearSearch">
        <AppIcon name="close" :size="12" />清空
      </button>
      <select v-model="filterProvider" @change="load">
        <option value="">全部服务商</option>
        <option v-for="p in providers" :key="p.id" :value="p.id">{{ p.name }}</option>
      </select>
      <label class="checkbox-label text-sm">
        <input type="checkbox" v-model="filterFree" @change="load" /> 仅免费
      </label>
      <label class="checkbox-label text-sm">
        <input type="checkbox" v-model="filterAuto" @change="load" /> 仅Auto候选
      </label>
    </div>

    <!-- 模型表格 -->
    <div class="card">
      <div class="table-wrap">
        <table>
          <thead>
            <tr>
              <th>模型 ID</th>
              <th class="sortable" @click="toggleSort('display_name')">
                显示名
                <AppIcon v-if="sortKey === 'display_name'" :name="sortOrder === 'asc' ? 'chevronUp' : 'chevronDown'" :size="12" />
              </th>
              <th>服务商</th>
              <th>价格 (输入/输出)</th>
              <th class="sortable" @click="toggleSort('success_rate')">
                成功率
                <AppIcon v-if="sortKey === 'success_rate'" :name="sortOrder === 'asc' ? 'chevronUp' : 'chevronDown'" :size="12" />
              </th>
              <th>免费</th>
              <th>Auto</th>
              <th>延迟/TPS</th>
              <th>干预</th>
              <th>操作</th>
            </tr>
          </thead>
          <tbody>
            <tr v-if="models.length === 0">
              <td colspan="10">
                <EmptyState icon="cpu" title="没有匹配的模型" small />
              </td>
            </tr>
            <tr v-for="m in sortedModels" :key="m.id" :class="{ 'row-disabled': !m.enabled }">
              <td class="mono text-xs">{{ m.model_id }}</td>
              <td><strong>{{ m.display_name || m.model_id }}</strong></td>
              <td><strong>{{ getProviderName(m.provider_id) }}</strong></td>
              <td>
                <div class="mono text-xs">{{ formatPrice(m.input_price) }}/{{ formatPrice(m.output_price) }} /M</div>
                <div v-if="m.cache_read_input_price || m.cache_write_input_price" class="mono text-xs" style="color: var(--accent, #2b8aef);" title="缓存价 (读/写)">
                  缓存 {{ formatPrice(m.cache_read_input_price) }}/{{ formatPrice(m.cache_write_input_price) }} /M
                </div>
                <div v-if="m.pricing_source" class="text-xs text-muted" :title="m.pricing_source">
                  {{ pricingSourceHost(m.pricing_source) }}
                </div>
              </td>
              <td>
                <span v-if="m.success_rate != null" class="tabular" :style="{ color: successRateColor(m.success_rate), fontWeight: 600 }">
                  {{ Number(m.success_rate).toFixed(2) }}%
                </span>
                <span v-else class="text-muted">-</span>
                <div v-if="m.avg_ttft_ms" class="text-xs text-muted">TTFT {{ Math.round(m.avg_ttft_ms) }}ms</div>
              </td>
              <td>
                <span class="badge" :class="m.is_free ? 'badge-success' : 'badge-neutral'">
                  {{ m.is_free ? '免费' : '付费' }}
                </span>
              </td>
              <td>
                <label class="switch">
                  <input type="checkbox" :checked="m.auto_enabled" @change="toggleAuto(m)" />
                  <span class="slider"></span>
                </label>
                <span v-if="m.auto_excluded" class="badge badge-danger text-xs" style="margin-left: 4px">排除</span>
                <span v-if="m.cooldown_until" class="badge badge-warning text-xs" style="margin-left: 4px" :title="'冷却至 ' + m.cooldown_until">
                  <AppIcon name="clock" :size="10" /> {{ cooldownRemaining(m) }}
                </span>
                <span v-else-if="m.fail_count > 0" class="text-xs text-muted" style="margin-left: 4px">
                  ×{{ m.fail_count }}
                </span>
              </td>
              <td>
                <span v-if="m.latency_ms" class="tabular" :style="{ color: latencyColor(m.latency_ms), fontWeight: 600 }">
                  {{ Math.round(m.latency_ms || 0) }}ms
                </span>
                <span v-else class="text-muted">-</span>
                <span v-if="m.health_status" class="badge" :class="statusBadgeClass(m.health_status)" style="margin-left: 4px; font-size: 10px">
                  {{ statusLabel(m.health_status) }}
                </span>
                <div v-if="m.avg_tps" class="text-xs text-muted">{{ Number(m.avg_tps).toFixed(2) }} t/s</div>
              </td>
              <td>
                <div class="action-row">
                  <button class="btn btn-outline btn-xs" @click="boostModel(m, 10)" title="置顶 +10">
                    <AppIcon name="chevronUp" :size="11" />
                  </button>
                  <button class="btn btn-outline btn-xs" @click="boostModel(m, -10)" title="降权 -10">
                    <AppIcon name="chevronDown" :size="11" />
                  </button>
                  <button class="btn btn-xs" :class="m.auto_excluded ? 'btn-danger' : 'btn-outline'" @click="toggleExclude(m)" title="排除/恢复">
                    <AppIcon name="ban" :size="11" />
                  </button>
                </div>
                <span v-if="m.priority_boost !== 0" class="text-xs" :style="{ color: m.priority_boost > 0 ? 'var(--success)' : 'var(--danger)' }">
                  {{ m.priority_boost > 0 ? '+' : '' }}{{ m.priority_boost }}
                </span>
              </td>
              <td>
                <div class="action-row">
                  <button class="btn btn-outline btn-xs" @click="pingModel(m)" :disabled="m._pinging" title="测速">
                    <AppIcon :name="m._pinging ? 'clock' : 'zap'" :size="11" />
                  </button>
                  <button class="btn btn-outline btn-xs" @click="editModel(m)" title="编辑">
                    <AppIcon name="edit" :size="11" />
                  </button>
                  <button class="btn btn-danger btn-xs" @click="deleteModel(m)" title="删除">
                    <AppIcon name="trash" :size="11" />
                  </button>
                </div>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
    </div>

    <!-- 编辑模型 -->
    <AppModal v-model="showEditModal" :title="'编辑模型: ' + (editForm.display_name || editForm.model_id)" icon="edit" size="lg">
      <div class="form-group">
        <label class="form-label">显示名称</label>
        <input v-model="editForm.display_name" />
      </div>
      <div class="form-row">
        <div class="form-group">
          <label class="form-label">输入价格 ($/M tokens)</label>
          <input v-model.number="editForm.input_price" type="number" step="0.001" />
        </div>
        <div class="form-group">
          <label class="form-label">输出价格 ($/M tokens)</label>
          <input v-model.number="editForm.output_price" type="number" step="0.001" />
        </div>
      </div>
      <div class="form-row">
        <div class="form-group">
          <label class="form-label">缓存读价格 ($/M tokens)</label>
          <input v-model.number="editForm.cache_read_input_price" type="number" step="0.001" />
        </div>
        <div class="form-group">
          <label class="form-label">缓存写价格 ($/M tokens)</label>
          <input v-model.number="editForm.cache_write_input_price" type="number" step="0.001" />
        </div>
      </div>
      <div class="form-row">
        <label class="checkbox-label">
          <input type="checkbox" v-model="editForm.is_free" /> 免费模型
        </label>
        <label class="checkbox-label">
          <input type="checkbox" v-model="editForm.auto_enabled" /> 参与 Auto 选举
        </label>
        <label class="checkbox-label">
          <input type="checkbox" v-model="editForm.enabled" /> 启用
        </label>
        <label class="checkbox-label">
          <input type="checkbox" v-model="editForm.auto_excluded" /> 强制排除 Auto
        </label>
      </div>
      <div class="form-group">
        <label class="form-label">优先级加成 (-100 ~ 100)</label>
        <input v-model.number="editForm.priority_boost" type="number" min="-100" max="100" />
      </div>

      <div class="form-divider">高级：请求覆盖</div>
      <div class="form-group">
        <label class="form-label">模型别名</label>
        <input v-model.trim="editForm.model_alias" placeholder="留空使用原始模型 ID" />
      </div>
      <div class="form-group">
        <label class="form-label">自定义 Headers (JSON)</label>
        <textarea v-model="overrideHeadersText" rows="4" spellcheck="false" placeholder='{"anthropic-beta":"context-1m-2025-08-07"}'></textarea>
      </div>
      <div class="form-group">
        <label class="form-label">Body Patch (JSON)</label>
        <textarea v-model="overrideBodyPatchText" rows="4" spellcheck="false" placeholder='{"max_tokens":1024}'></textarea>
      </div>
      <template #footer>
        <button class="btn btn-outline" @click="showEditModal = false">取消</button>
        <button class="btn btn-primary" @click="saveEdit" :disabled="saving">
          {{ saving ? '保存中...' : '保存' }}
        </button>
      </template>
    </AppModal>

    <!-- 添加模型 -->
    <AppModal v-model="showAddModel" title="手动添加模型" icon="plus" size="md">
      <div class="form-group">
        <label class="form-label">服务商 *</label>
        <select v-model="addModelForm.provider_id">
          <option :value="null">-- 选择服务商 --</option>
          <option v-for="p in providers" :key="p.id" :value="p.id">{{ p.name }}</option>
        </select>
      </div>
      <div class="form-group">
        <label class="form-label">模型 ID *</label>
        <input v-model="addModelForm.model_id" placeholder="例如: gpt-4" />
      </div>
      <div class="form-group">
        <label class="form-label">显示名称（可选）</label>
        <input v-model="addModelForm.display_name" placeholder="留空则使用模型 ID" />
      </div>
      <div class="form-row">
        <div class="form-group">
          <label class="form-label">输入价格 /M</label>
          <input v-model.number="addModelForm.input_price" type="number" step="0.0001" min="0" />
        </div>
        <div class="form-group">
          <label class="form-label">输出价格 /M</label>
          <input v-model.number="addModelForm.output_price" type="number" step="0.0001" min="0" />
        </div>
      </div>
      <div class="form-row">
        <div class="form-group">
          <label class="form-label">缓存读价 /M</label>
          <input v-model.number="addModelForm.cache_read_input_price" type="number" step="0.0001" min="0" />
        </div>
        <div class="form-group">
          <label class="form-label">缓存写价 /M</label>
          <input v-model.number="addModelForm.cache_write_input_price" type="number" step="0.0001" min="0" />
        </div>
      </div>
      <template #footer>
        <button class="btn btn-outline" @click="showAddModel = false">取消</button>
        <button class="btn btn-primary" @click="addModel" :disabled="addingModel">
          {{ addingModel ? '添加中...' : '添加' }}
        </button>
      </template>
    </AppModal>
  </div>
</template>

<script>
import api from '../api.js'
import toast from '../toast.js'
import AppIcon from '../components/AppIcon.vue'
import PageHeader from '../components/PageHeader.vue'
import AppModal from '../components/AppModal.vue'
import EmptyState from '../components/EmptyState.vue'
import ModelRefreshModal from '../components/ModelRefreshModal.vue'

export default {
  name: 'ModelsView',
  components: { AppIcon, PageHeader, AppModal, EmptyState, ModelRefreshModal },
  data() {
    return {
      models: [],
      providers: [],
      filterProvider: '',
      filterFree: false,
      filterAuto: false,
      searchQuery: '',
      sortKey: '',
      sortOrder: 'asc',
      refreshing: false,
      pingingAll: false,
      pingTotal: 0,
      pingDone: 0,
      showEditModal: false,
      saving: false,
      editForm: {},
      overrideHeadersText: '',
      overrideBodyPatchText: '',
      showAddModel: false,
      addingModel: false,
      addModelForm: { provider_id: null, model_id: '', display_name: '', input_price: 0, output_price: 0, cache_read_input_price: 0, cache_write_input_price: 0 },
      showRefreshModal: false,
      refreshResult: null,
    }
  },
  computed: {
    pingProgress() {
      if (this.pingTotal === 0) return 0
      return Math.round((this.pingDone / this.pingTotal) * 100)
    },
    sortedModels() {
      let list = this.models || []
      if (!this.sortKey) return list
      const order = this.sortOrder === 'asc' ? 1 : -1
      const key = this.sortKey
      const arr = list.slice()
      arr.sort((a, b) => {
        let av, bv
        if (key === 'display_name') {
          av = (a.display_name || a.model_id || '').toString().toLowerCase()
          bv = (b.display_name || b.model_id || '').toString().toLowerCase()
        } else {
          if (a.success_rate == null) return 1
          if (b.success_rate == null) return -1
          av = Number(a.success_rate)
          bv = Number(b.success_rate)
        }
        if (av < bv) return -1 * order
        if (av > bv) return 1 * order
        return 0
      })
      return arr
    },
  },
  methods: {
    parseJsonObject(text, label) {
      const trimmed = (text || '').trim()
      if (!trimmed) return {}
      let parsed
      try {
        parsed = JSON.parse(trimmed)
      } catch (e) {
        throw new Error(`${label} 不是合法 JSON: ${e.message}`)
      }
      if (!parsed || Array.isArray(parsed) || typeof parsed !== 'object') {
        throw new Error(`${label} 必须是 JSON 对象`)
      }
      return parsed
    },
    cleanRequestOverrides() {
      const headers = this.parseJsonObject(this.overrideHeadersText, 'Headers')
      const bodyPatch = this.parseJsonObject(this.overrideBodyPatchText, 'Body Patch')
      const out = {}
      if (this.editForm.model_alias) out.model_alias = this.editForm.model_alias
      if (Object.keys(headers).length) out.headers = headers
      if (Object.keys(bodyPatch).length) out.body_patch = bodyPatch
      return Object.keys(out).length ? out : null
    },
    async load() {
      try {
        this.providers = await api.getProviders()
        const params = {}
        if (this.filterProvider) params.provider_id = this.filterProvider
        if (this.filterAuto) params.auto_enabled = true
        if (this.searchQuery) params.q = this.searchQuery
        const loadedModels = await api.getModels(params)
        this.models = this.filterFree ? (loadedModels || []).filter((m) => m.is_free) : loadedModels || []
        this.models.forEach((m) => {
          if (m._pinging === undefined) m._pinging = false
        })
      } catch (e) {
        toast.error('加载失败: ' + e.message)
      }
    },
    clearSearch() {
      this.searchQuery = ''
      this.load()
    },
    toggleSort(key) {
      if (this.sortKey === key) {
        this.sortOrder = this.sortOrder === 'asc' ? 'desc' : 'asc'
      } else {
        this.sortKey = key
        this.sortOrder = key === 'success_rate' ? 'desc' : 'asc'
      }
    },
    getProviderName(pid) {
      const p = this.providers.find((x) => x.id === pid)
      return p ? p.name : '未知'
    },
    formatPrice(value) {
      const n = Number(value || 0)
      if (n === 0) return '$0'
      if (n < 0.0001) return `$${n.toExponential(2)}`
      if (n < 0.01) return `$${n.toFixed(6)}`
      if (n < 1) return `$${n.toFixed(4)}`
      return `$${n.toFixed(2)}`
    },
    pricingSourceHost(source) {
      try {
        return new URL(source).host
      } catch {
        return source || ''
      }
    },
    successRateColor(rate) {
      const n = Number(rate)
      if (n >= 99.9) return 'var(--success)'
      if (n >= 95) return 'var(--warning)'
      return 'var(--danger)'
    },
    latencyColor(ms) {
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
    cooldownRemaining(m) {
      if (!m.cooldown_until) return ''
      const diff = new Date(m.cooldown_until) - Date.now()
      if (diff <= 0) return '0s'
      const s = Math.ceil(diff / 1000)
      if (s < 60) return s + 's'
      if (s < 3600) return Math.ceil(s / 60) + 'm'
      return Math.ceil(s / 3600) + 'h'
    },
    async toggleAuto(model) {
      try {
        await api.updateModel(model.id, { auto_enabled: !model.auto_enabled })
        model.auto_enabled = !model.auto_enabled
      } catch (e) {
        toast.error('更新失败: ' + e.message)
      }
    },
    async boostModel(model, delta) {
      const newBoost = (model.priority_boost || 0) + delta
      try {
        await api.updateModel(model.id, { priority_boost: newBoost })
        model.priority_boost = newBoost
      } catch (e) {
        toast.error('更新失败: ' + e.message)
      }
    },
    async toggleExclude(model) {
      try {
        await api.updateModel(model.id, { auto_excluded: !model.auto_excluded })
        model.auto_excluded = !model.auto_excluded
      } catch (e) {
        toast.error('更新失败: ' + e.message)
      }
    },
    async pingModel(model) {
      model._pinging = true
      try {
        const result = await api.pingModel(model.id)
        model.latency_ms = result.latency_ms
        model.health_status = result.status
      } catch (e) {
        toast.error('测速失败: ' + e.message)
      } finally {
        model._pinging = false
      }
    },
    async pingAll() {
      this.pingingAll = true
      const autoModels = this.models.filter((m) => m.auto_enabled)
      this.pingTotal = autoModels.length
      this.pingDone = 0
      try {
        const result = await api.pingAllModels()
        for (const r of result.results) {
          const model = autoModels.find((m) => m.id === r.model_id)
          if (model) {
            model.latency_ms = r.latency_ms
            model.health_status = r.status
          }
          this.pingDone++
        }
        toast.success(`测速完成！健康: ${result.healthy}, 延迟: ${result.degraded}, 故障: ${result.unhealthy}`)
      } catch (e) {
        toast.error('批量测速失败: ' + e.message)
      } finally {
        this.pingingAll = false
      }
    },
    async refreshAll() {
      this.refreshing = true
      try {
        const res = await api.refreshModels()
        this.refreshResult = res
        this.showRefreshModal = true
        await this.load()
      } catch (e) {
        this.refreshResult = { error: e.message }
        this.showRefreshModal = true
      } finally {
        this.refreshing = false
      }
    },
    editModel(m) {
      const overrides = m.request_overrides || {}
      this.editForm = {
        id: m.id,
        model_id: m.model_id,
        display_name: m.display_name,
        input_price: m.input_price,
        output_price: m.output_price,
        cache_read_input_price: m.cache_read_input_price || 0,
        cache_write_input_price: m.cache_write_input_price || 0,
        success_rate: m.success_rate,
        is_free: m.is_free,
        auto_enabled: m.auto_enabled,
        enabled: m.enabled,
        priority_boost: m.priority_boost || 0,
        auto_excluded: m.auto_excluded || false,
        model_alias: overrides.model_alias || '',
      }
      this.overrideHeadersText = overrides.headers ? JSON.stringify(overrides.headers, null, 2) : ''
      this.overrideBodyPatchText = overrides.body_patch ? JSON.stringify(overrides.body_patch, null, 2) : ''
      this.showEditModal = true
    },
    async saveEdit() {
      this.saving = true
      try {
        const requestOverrides = this.cleanRequestOverrides()
        const payload = { ...this.editForm, request_overrides: requestOverrides }
        delete payload.model_alias
        await api.updateModel(this.editForm.id, payload)
        this.showEditModal = false
        await this.load()
        toast.success('模型已更新')
      } catch (e) {
        toast.error('保存失败: ' + e.message)
      } finally {
        this.saving = false
      }
    },
    async addModel() {
      if (!this.addModelForm.provider_id || !this.addModelForm.model_id.trim()) {
        toast.error('请选择服务商并填写模型 ID')
        return
      }
      this.addingModel = true
      try {
        await api.createProviderModel(this.addModelForm.provider_id, {
          model_id: this.addModelForm.model_id.trim(),
          display_name: this.addModelForm.display_name.trim(),
          input_price: this.addModelForm.input_price || 0,
          output_price: this.addModelForm.output_price || 0,
          cache_read_input_price: this.addModelForm.cache_read_input_price || 0,
          cache_write_input_price: this.addModelForm.cache_write_input_price || 0,
        })
        this.showAddModel = false
        this.addModelForm = { provider_id: null, model_id: '', display_name: '', input_price: 0, output_price: 0, cache_read_input_price: 0, cache_write_input_price: 0 }
        await this.load()
        toast.success('模型已添加')
      } catch (e) {
        toast.error('添加失败: ' + e.message)
      } finally {
        this.addingModel = false
      }
    },
    async deleteModel(m) {
      if (!confirm(`确定要删除模型 "${m.display_name || m.model_id}" 吗？此操作不可撤销。`)) return
      try {
        await api.deleteModel(m.id)
        await this.load()
        toast.success('已删除')
      } catch (e) {
        toast.error('删除失败: ' + e.message)
      }
    },
  },
  mounted() {
    this.load()
  },
}
</script>

<style scoped>
/* 进度条 */
.ping-progress {
  margin-bottom: var(--space-4);
}
.ping-row {
  display: flex;
  align-items: center;
  gap: var(--space-3);
}

/* 筛选工具栏 */
.filter-bar {
  display: flex;
  gap: var(--space-3);
  flex-wrap: wrap;
  align-items: center;
  margin-bottom: var(--space-4);
}
.input-group {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  background: var(--surface-2);
  padding: 0 var(--space-3);
  border-radius: var(--radius-md);
  border: 1px solid var(--border-base);
  flex: 1;
  min-width: 200px;
  max-width: 320px;
}
.input-group input {
  border: 0;
  background: transparent;
  padding: 6px 0;
}
.filter-bar select {
  width: auto;
  min-width: 140px;
}
.checkbox-label {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  cursor: pointer;
  white-space: nowrap;
}
.checkbox-label input {
  width: auto;
  margin: 0;
}

/* 表格 */
.table-wrap {
  overflow-x: auto;
}
.row-disabled {
  opacity: 0.45;
}
.sortable {
  cursor: pointer;
  user-select: none;
  white-space: nowrap;
}
.sortable:hover {
  color: var(--primary);
}
.action-row {
  display: flex;
  gap: 4px;
}

/* 开关 */
.switch {
  position: relative;
  display: inline-block;
  width: 36px;
  height: 20px;
}
.switch input {
  opacity: 0;
  width: 0;
  height: 0;
}
.slider {
  position: absolute;
  cursor: pointer;
  inset: 0;
  background-color: var(--border-medium);
  transition: 0.2s;
  border-radius: 20px;
}
.slider:before {
  position: absolute;
  content: '';
  height: 14px;
  width: 14px;
  left: 3px;
  bottom: 3px;
  background-color: #ffffff;
  transition: 0.2s;
  border-radius: 50%;
}
input:checked + .slider {
  background-color: var(--primary);
}
input:checked + .slider:before {
  transform: translateX(16px);
}

/* 表单 */
.form-row {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
  gap: var(--space-3);
  margin-bottom: var(--space-3);
}
.form-divider {
  margin: var(--space-4) 0 var(--space-3);
  padding-top: var(--space-3);
  border-top: 1px solid var(--border-base);
  font-weight: 600;
  color: var(--text-primary);
  font-size: var(--text-base);
}

@media (max-width: 900px) {
  .filter-bar {
    flex-direction: column;
    align-items: stretch;
  }
  .input-group {
    max-width: none;
  }
}
</style>
