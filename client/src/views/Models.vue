<template>
  <div>
    <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 20px;">
      <h1>模型管理 🤖</h1>
      <div style="display: flex; gap: 12px;">
        <button class="btn btn-outline" @click="refreshAll" :disabled="refreshing">
          {{ refreshing ? '刷新中...' : '🔄 刷新模型列表' }}
        </button>
        <button class="btn btn-primary" @click="pingAll" :disabled="pingingAll">
          {{ pingingAll ? '测速中...' : '🚀 一键测速' }}
        </button>
        <button class="btn btn-outline" @click="showAddModel = true">➕ 添加模型</button>
      </div>
    </div>
    <!-- 测速进度条 -->
    <div v-if="pingingAll" class="card" style="margin-bottom: 16px;">
      <div style="display: flex; align-items: center; gap: 12px;">
        <span>⏳ 正在测速...</span>
        <div style="flex: 1; height: 8px; background: var(--gray-100); border-radius: 4px; overflow: hidden;">
          <div :style="{width: pingProgress + '%', height: '100%', background: 'var(--primary)', transition: 'width 0.3s'}"></div>
        </div>
        <span style="font-size: 13px; color: var(--gray-500);">{{ pingDone }}/{{ pingTotal }}</span>
      </div>
    </div>
    <!-- 筛选 -->
    <div class="card" style="margin-bottom: 16px; display: flex; gap: 12px; flex-wrap: wrap; align-items: center;">
      <input v-model.trim="searchQuery" @keyup.enter="load" placeholder="搜索模型 / 显示名 / 服务商" style="min-width: 260px;" />
      <button class="btn btn-outline" @click="load">搜索</button>
      <button v-if="searchQuery" class="btn btn-outline" @click="clearSearch">清空</button>
      <select v-model="filterProvider" @change="load" style="width: auto; min-width: 160px;">
        <option value="">全部服务商</option>
        <option v-for="p in providers" :key="p.id" :value="p.id">{{ p.name }}</option>
      </select>
      <label style="display: flex; align-items: center; gap: 6px; font-size: 14px;">
        <input type="checkbox" v-model="filterFree" @change="load" /> 仅免费
      </label>
      <label style="display: flex; align-items: center; gap: 6px; font-size: 14px;">
        <input type="checkbox" v-model="filterAuto" @change="load" /> 仅Auto候选
      </label>
    </div>
    <div class="card">
      <table>
        <thead>
          <tr>
            <th>模型 ID</th>
            <th>显示名</th>
            <th class="provider-col">服务商</th>
            <th>价格 (输入/输出)</th>
            <th>成功率</th>
            <th>免费</th>
            <th>Auto</th>
            <th>延迟/TPS</th>
            <th>干预</th>
            <th>操作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-if="models.length === 0">
            <td colspan="10" style="text-align: center; color: var(--gray-500); padding: 32px;">没有匹配的模型</td>
          </tr>
          <tr v-for="m in models" :key="m.id" :style="{opacity: m.enabled ? 1 : 0.5}">
            <td style="font-family: monospace; font-size: 12px;">{{ m.model_id }}</td>
            <td><strong>{{ m.display_name || m.model_id }}</strong></td>
            <td class="provider-col"><strong>{{ getProviderName(m.provider_id) }}</strong></td>
            <td>
              <div style="font-family: monospace; font-size: 12px;">{{ formatPrice(m.input_price) }}/{{ formatPrice(m.output_price) }} /M</div>
              <div v-if="m.pricing_source" style="font-size: 11px; color: var(--text-muted);" :title="m.pricing_source">
                {{ pricingSourceHost(m.pricing_source) }}
              </div>
            </td>
            <td>
              <span v-if="m.success_rate !== null && m.success_rate !== undefined" :style="{color: successRateColor(m.success_rate), fontWeight: 'bold'}">
                {{ Number(m.success_rate).toFixed(2) }}%
              </span>
              <span v-else style="color: var(--text-muted);">-</span>
              <div v-if="m.avg_ttft_ms" style="font-size: 11px; color: var(--text-muted);">TTFT {{ Math.round(m.avg_ttft_ms) }}ms</div>
            </td>
            <td>{{ m.is_free ? '🆓' : '💰' }}</td>
            <td>
              <label class="switch">
                <input type="checkbox" :checked="m.auto_enabled" @change="toggleAuto(m)" />
                <span class="slider"></span>
              </label>
              <span v-if="m.auto_excluded" style="color: var(--danger); font-size: 11px; margin-left: 4px;">🚫排除</span>
              <span v-if="m.cooldown_until" :title="'冷却至 ' + m.cooldown_until" style="color: var(--warning); font-size: 11px; margin-left: 4px; display: inline-flex; align-items: center; gap: 2px;">
                ⏳ {{ cooldownRemaining(m) }}
              </span>
              <span v-else-if="m.fail_count > 0" style="color: var(--gray-500); font-size: 11px; margin-left: 4px;">
                ❌×{{ m.fail_count }}
              </span>
            </td>
            <td>
              <span v-if="m.latency_ms" :style="{color: latencyColor(m.latency_ms), fontWeight: 'bold'}">
                {{ Math.round(m.latency_ms || 0) }}ms
              </span>
              <span v-else style="color: var(--text-muted);">-</span>
              <span v-if="m.health_status" :class="['badge', statusBadgeClass(m.health_status)]" style="margin-left: 4px; font-size: 10px;">
                {{ statusEmoji(m.health_status) }}
              </span>
              <div v-if="m.avg_tps" style="font-size: 11px; color: var(--gray-500);">{{ Number(m.avg_tps).toFixed(2) }} t/s</div>
            </td>
            <td>
              <div style="display: flex; gap: 4px;">
                <button class="btn btn-sm btn-outline"
                        @click="boostModel(m, 10)" title="置顶 +10">⬆️</button>
                <button class="btn btn-sm btn-outline"
                        @click="boostModel(m, -10)" title="降权 -10">⬇️</button>
                <button class="btn btn-sm" :class="m.auto_excluded ? 'btn-danger' : 'btn-outline'"
                        @click="toggleExclude(m)" title="排除/恢复">🚫</button>
              </div>
              <span v-if="m.priority_boost !== 0" style="font-size: 11px;" :style="{color: m.priority_boost > 0 ? 'var(--success)' : 'var(--danger)'}">
                {{ m.priority_boost > 0 ? '+' : '' }}{{ m.priority_boost }}
              </span>
            </td>
            <td>
              <div style="display: flex; gap: 4px;">
                <button class="btn btn-outline btn-sm" @click="pingModel(m)" :disabled="m._pinging">
                  {{ m._pinging ? '⏳' : '🔍' }}
                </button>
                <button class="btn btn-outline btn-sm" @click="editModel(m)">✏️</button>
                <button class="btn btn-outline btn-sm" style="color: var(--danger); border-color: var(--danger);" @click="deleteModel(m)">🗑️</button>
              </div>
            </td>
          </tr>
        </tbody>
      </table>
    </div>
    <!-- 编辑 Modal -->
    <div v-if="showEditModal" class="modal-overlay" @click.self="showEditModal = false">
      <div class="modal-content">
        <h3>编辑模型: {{ editForm.display_name || editForm.model_id }}</h3>
        <div class="form-group">
          <label>显示名称</label>
          <input v-model="editForm.display_name" />
        </div>
        <div class="form-group">
          <label>输入价格 ($/M tokens)</label>
          <input v-model.number="editForm.input_price" type="number" step="0.001" />
        </div>
        <div class="form-group">
          <label>输出价格 ($/M tokens)</label>
          <input v-model.number="editForm.output_price" type="number" step="0.001" />
        </div>
        <div class="form-group">
          <label>
            <input type="checkbox" v-model="editForm.is_free" /> 免费模型
          </label>
        </div>
        <div class="form-group">
          <label>
            <input type="checkbox" v-model="editForm.auto_enabled" /> 参与 Auto 选举
          </label>
        </div>
        <div class="form-group">
          <label>
            <input type="checkbox" v-model="editForm.enabled" /> 启用
          </label>
        </div>
        <div class="form-group">
          <label>优先级加成 (-100 ~ 100)</label>
          <input v-model.number="editForm.priority_boost" type="number" min="-100" max="100" />
        </div>
        <div class="form-group">
          <label>
            <input type="checkbox" v-model="editForm.auto_excluded" /> 强制排除 Auto 选举
          </label>
        </div>

        <div class="form-section-title">????</div>
        <div class="form-group">
          <label>??????</label>
          <input v-model.trim="editForm.model_alias" placeholder="????????? ID" />
        </div>
        <div class="form-group">
          <label>??? Headers (JSON)</label>
          <textarea v-model="overrideHeadersText" rows="4" spellcheck="false" placeholder='{"anthropic-beta":"context-1m-2025-08-07"}'></textarea>
        </div>
        <div class="form-group">
          <label>Body Patch (JSON)</label>
          <textarea v-model="overrideBodyPatchText" rows="4" spellcheck="false" placeholder='{"max_tokens":1024}'></textarea>
        </div>
        <div class="modal-actions">
          <button class="btn btn-outline" @click="showEditModal = false">取消</button>
          <button class="btn btn-primary" @click="saveEdit" :disabled="saving">
            {{ saving ? '保存中...' : '保存' }}
          </button>
        </div>
      </div>
    </div>

    <!-- 添加模型弹窗 -->
    <div v-if="showAddModel" class="modal-overlay" @click.self="showAddModel = false">
      <div class="modal-content" style="max-width: 500px;">
        <h3>➕ 手动添加模型</h3>
        <div style="margin: 12px 0;">
          <label>服务商</label>
          <select v-model="addModelForm.provider_id" style="width: 100%; padding: 8px;">
            <option :value="null">-- 选择服务商 --</option>
            <option v-for="p in providers" :key="p.id" :value="p.id">{{ p.name }}</option>
          </select>
        </div>
        <div style="margin: 12px 0;">
          <label>模型 ID</label>
          <input v-model="addModelForm.model_id" placeholder="例如: gpt-4" style="width: 100%; padding: 8px;" />
        </div>
        <div style="margin: 12px 0;">
          <label>显示名称（可选）</label>
          <input v-model="addModelForm.display_name" placeholder="留空则使用模型 ID" style="width: 100%; padding: 8px;" />
        </div>
        <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 12px; margin: 12px 0;">
          <div>
            <label>输入价格 /M</label>
            <input v-model.number="addModelForm.input_price" type="number" step="0.0001" min="0" style="width: 100%; padding: 8px;" />
          </div>
          <div>
            <label>输出价格 /M</label>
            <input v-model.number="addModelForm.output_price" type="number" step="0.0001" min="0" style="width: 100%; padding: 8px;" />
          </div>
        </div>
        <div class="modal-actions">
          <button class="btn btn-outline" @click="showAddModel = false">取消</button>
          <button class="btn btn-primary" @click="addModel" :disabled="addingModel">{{ addingModel ? '添加中...' : '添加' }}</button>
        </div>
      </div>
    </div>
  </div>
</template>
<script>
import api from '../api.js'
export default {
  name: 'ModelsView',
  data() {
    return {
      models: [],
      providers: [],
      filterProvider: '',
      filterFree: false,
      filterAuto: false,
      searchQuery: '',
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
      addModelForm: { provider_id: null, model_id: '', display_name: '', input_price: 0, output_price: 0 }
    }
  },
  computed: {
    pingProgress() {
      if (this.pingTotal === 0) return 0
      return Math.round((this.pingDone / this.pingTotal) * 100)
    }
  },
  methods: {
    parseJsonObject(text, label) {
      const trimmed = (text || '').trim()
      if (!trimmed) return {}
      let parsed
      try {
        parsed = JSON.parse(trimmed)
      } catch (e) {
        throw new Error(`${label} ???? JSON: ${e.message}`)
      }
      if (!parsed || Array.isArray(parsed) || typeof parsed !== 'object') {
        throw new Error(`${label} ??? JSON ??`)
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
        // ?????????????????????????
        if (this.filterAuto) params.auto_enabled = true
        if (this.searchQuery) params.q = this.searchQuery
        const loadedModels = await api.getModels(params)
        this.models = this.filterFree ? (loadedModels || []).filter(m => m.is_free) : (loadedModels || [])
        // 初始化 _pinging 标记
        this.models.forEach(m => { if (m._pinging === undefined) m._pinging = false })
      } catch (e) {
        console.error(e)
        alert('加载失败: ' + e.message)
      }
    },
    clearSearch() {
      this.searchQuery = ''
      this.load()
    },
    getProviderName(pid) {
      const p = this.providers.find(x => x.id === pid)
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
      } catch (e) {
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
      const map = { healthy: 'badge-success', degraded: 'badge-warning', rate_limited: 'badge-info', unhealthy: 'badge-danger' }
      return map[s] || 'badge-neutral'
    },
    statusEmoji(s) {
      const map = { healthy: '✅', degraded: '⚠️', rate_limited: '⏸️', unhealthy: '❌' }
      return map[s] || '❓'
    },
    cooldownRemaining(m) {
      if (!m.cooldown_until) return ''
      const until = new Date(m.cooldown_until)
      const diff = until - Date.now()
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
        alert('更新失败: ' + e.message)
      }
    },
    async boostModel(model, delta) {
      // 每次点击在当前基础上累加/递减
      const newBoost = (model.priority_boost || 0) + delta
      try {
        await api.updateModel(model.id, { priority_boost: newBoost })
        model.priority_boost = newBoost
      } catch (e) {
        alert('更新失败: ' + e.message)
      }
    },
    async toggleExclude(model) {
      try {
        await api.updateModel(model.id, { auto_excluded: !model.auto_excluded })
        model.auto_excluded = !model.auto_excluded
      } catch (e) {
        alert('更新失败: ' + e.message)
      }
    },
    async pingModel(model) {
      model._pinging = true
      try {
        const result = await api.pingModel(model.id)
        model.latency_ms = result.latency_ms
        model.health_status = result.status
      } catch (e) {
        alert('测速失败: ' + e.message)
      } finally {
        model._pinging = false
      }
    },
    async pingAll() {
      this.pingingAll = true
      const autoModels = this.models.filter(m => m.auto_enabled)
      this.pingTotal = autoModels.length
      this.pingDone = 0
      try {
        const result = await api.pingAllModels()
        // 后端只测 auto 模型，前端进度条同步只统计 auto
        for (const r of result.results) {
          const model = autoModels.find(m => m.id === r.model_id)
          if (model) {
            model.latency_ms = r.latency_ms
            model.health_status = r.status
          }
          this.pingDone++
        }
        alert(`测速完成！健康: ${result.healthy}, 延迟: ${result.degraded}, 故障: ${result.unhealthy}`)
      } catch (e) {
        alert('批量测速失败: ' + e.message)
      } finally {
        this.pingingAll = false
      }
    },
    async refreshAll() {
      this.refreshing = true
      try {
        const res = await api.refreshModels()
        alert(`刷新完成！新增 ${res.added}，更新 ${res.updated}，价格 ${res.pricing_updated || 0}，成功率 ${res.metric_updated || 0}，共 ${res.total} 个模型`)
        await this.load()
      } catch (e) {
        alert('刷新失败: ' + e.message)
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
        success_rate: m.success_rate,
        is_free: m.is_free,
        auto_enabled: m.auto_enabled,
        enabled: m.enabled,
        priority_boost: m.priority_boost || 0,
        auto_excluded: m.auto_excluded || false,
        model_alias: overrides.model_alias || ''
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
      } catch (e) {
        alert('????: ' + e.message)
      } finally {
        this.saving = false
      }
    },
    async addModel() {
      if (!this.addModelForm.provider_id || !this.addModelForm.model_id.trim()) {
        alert('请选择服务商并填写模型 ID')
        return
      }
      this.addingModel = true
      try {
        const resp = await fetch(`/admin/api/providers/${this.addModelForm.provider_id}/models`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            model_id: this.addModelForm.model_id.trim(),
            display_name: this.addModelForm.display_name.trim(),
            input_price: this.addModelForm.input_price || 0,
            output_price: this.addModelForm.output_price || 0
          })
        })
        if (!resp.ok) {
          const err = await resp.json()
          throw new Error(err.detail || '添加失败')
        }
        this.showAddModel = false
        this.addModelForm = { provider_id: null, model_id: '', display_name: '', input_price: 0, output_price: 0 }
        await this.load()
      } catch (e) {
        alert('添加失败: ' + e.message)
      } finally {
        this.addingModel = false
      }
    },
    async deleteModel(m) {
      if (!confirm(`确定要删除模型 "${m.display_name || m.model_id}" 吗？此操作不可撤销。`)) return
      try {
        await api.deleteModel(m.id)
        await this.load()
      } catch (e) {
        alert('删除失败: ' + e.message)
      }
    }
  },
  mounted() {
    this.load()
  }
}
</script>
<style scoped>
.switch {
  position: relative;
  display: inline-block;
  width: 36px;
  height: 20px;
}
.switch input { opacity: 0; width: 0; height: 0; }
.slider {
  position: absolute;
  cursor: pointer;
  top: 0; left: 0; right: 0; bottom: 0;
  background-color: var(--border-medium);
  transition: .3s;
  border-radius: 20px;
}
.slider:before {
  position: absolute;
  content: "";
  height: 14px; width: 14px;
  left: 3px; bottom: 3px;
  background-color: #ffffff;
  transition: .3s;
  border-radius: 50%;
}
input:checked + .slider { background-color: var(--primary); }
input:checked + .slider:before { transform: translateX(16px); }
.btn-sm { padding: 2px 6px; font-size: 12px; }
.btn-warning { background: var(--warning); color: #ffffff; border-color: var(--warning); }
.provider-col { min-width: 160px; max-width: 260px; white-space: nowrap; }
.modal-overlay {
  position: fixed;
  top: 0; left: 0; right: 0; bottom: 0;
  background: rgba(0,0,0,0.5);
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 1000;
}
.modal-content {
  background: var(--bg-elevated);
  border: 1px solid var(--border-soft);
  color: var(--text-primary);
  padding: 24px;
  border-radius: 12px;
  width: 640px;
  max-width: 90vw;
  max-height: 88vh;
  overflow-y: auto;
}
.modal-actions {
  display: flex;
  justify-content: flex-end;
  gap: 12px;
  margin-top: 24px;
}

.form-section-title {
  margin: 18px 0 10px;
  padding-top: 12px;
  border-top: 1px solid var(--border-soft);
  font-weight: 700;
  color: var(--text-primary);
}
.form-group textarea {
  width: 100%;
  min-height: 88px;
  padding: 8px;
  border: 1px solid var(--border-soft);
  border-radius: 6px;
  background: var(--bg-primary);
  color: var(--text-primary);
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-size: 12px;
  resize: vertical;
}

</style>