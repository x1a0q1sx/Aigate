<template>
  <div>
    <h1 style="margin-bottom: 20px;">Playground 🎮</h1>
    <div class="card playground-card">
      <div class="model-picker">
        <div class="form-group">
          <label>服务商</label>
          <select v-model="selectedProviderId" @change="onProviderChange">
            <option value="auto">Auto（智能选举）</option>
            <option v-for="p in providersWithModels" :key="p.id" :value="String(p.id)">{{ p.name }}（{{ modelCount(p.id) }}）</option>
          </select>
        </div>
        <div class="form-group" v-if="selectedProviderId !== 'auto'">
          <label>模型</label>
          <select v-model="selectedModelId" @change="syncModel" :disabled="modelsLoading">
            <option v-if="modelsLoading" value="">模型加载中...</option>
            <option v-else-if="filteredModels.length === 0" value="">（该服务商暂无可用模型）</option>
            <option v-for="m in filteredModels" :key="m.id" :value="String(m.id)">{{ m.display_name || m.model_id }}</option>
          </select>
        </div>
      </div>
      <div v-if="form.model !== 'auto'" class="selected-model"><code>{{ form.model }}</code></div>
      <div class="form-group">
        <label>消息</label>
        <textarea v-model="form.message" rows="4" placeholder="输入你的消息..."></textarea>
      </div>
      <button class="btn btn-primary" @click="send" :disabled="sending">{{ sending ? '发送中...' : '发送 🚀' }}</button>
      <div v-if="loadError" class="error-box"><strong>加载失败:</strong> {{ loadError }}</div>
      <div v-if="response" class="response-box"><strong>响应:</strong><div style="margin-top: 8px;">{{ response }}</div></div>
      <div v-if="error" class="error-box"><strong>错误:</strong> {{ error }}</div>
    </div>
  </div>
</template>
<script>
import api from '../api.js'
export default {
  name: 'PlaygroundView',
  data() { return { providers: [], modelStats: {}, models: [], loadedPid: null, modelsLoading: false, selectedProviderId: 'auto', selectedModelId: '', form: { model: 'auto', message: '' }, sending: false, response: '', error: '', loadError: '' } },
  computed: {
    // 懒加载：服务商下拉只依赖轻量的 model-stats（每家模型数），不再等全量模型列表
    providersWithModels() { return this.providers.filter(p => (this.modelStats[p.id] || 0) > 0) },
    filteredModels() { return this.models.filter(m => String(m.provider_id) === String(this.selectedProviderId)) }
  },
  async mounted() {
    // 服务商 + 每家模型数都是轻量接口，首屏秒开；模型列表等选中服务商后再按需拉取
    const [providers, stats] = await Promise.all([
      api.getProviders().then(d => ({ ok: true, d })).catch(e => ({ ok: false, e })),
      api.getProviderModelStats().then(d => ({ ok: true, d })).catch(e => ({ ok: false, e })),
    ])
    if (providers.ok) this.providers = providers.d || []
    if (stats.ok) {
      const map = {}
      for (const s of (stats.d || [])) map[s.provider_id] = s.model_count
      this.modelStats = map
    }
    const failed = [providers, stats].filter(x => !x.ok)
    if (failed.length === 2) this.loadError = (failed[0].e?.message || String(failed[0].e)) + '（请检查网络后刷新）'
    else if (failed.length === 1) console.error('Playground 部分加载失败:', failed[0].e)
  },
  methods: {
    modelCount(pid) { return this.modelStats[pid] || 0 },
    fullId(m) { return m.full_id || ((m.provider_name || this.providers.find(p => p.id === m.provider_id)?.name || '') + '/' + m.model_id) },
    async onProviderChange() {
      if (this.selectedProviderId === 'auto') { this.form.model = 'auto'; this.selectedModelId = ''; return }
      await this.loadModelsFor(this.selectedProviderId)
    },
    async loadModelsFor(pid) {
      if (this.loadedPid === String(pid) && this.models.length) return
      this.modelsLoading = true
      this.models = []
      this.selectedModelId = ''
      try {
        const list = await api.getModels({ provider_id: pid })
        this.models = list || []
        this.loadedPid = String(pid)
        const first = this.models[0]
        this.selectedModelId = first ? String(first.id) : ''
        this.syncModel()
      } catch (e) {
        console.error('加载模型失败:', e)
        this.error = '模型加载失败: ' + (e.message || e)
      } finally {
        this.modelsLoading = false
      }
    },
    syncModel() { const m = this.models.find(x => String(x.id) === String(this.selectedModelId)); this.form.model = m ? this.fullId(m) : 'auto' },
    async send() { if (!this.form.message.trim()) return; this.sending = true; this.response = ''; this.error = ''; try { const res = await api.playground({ model: this.form.model, messages: [{ role: 'user', content: this.form.message }] }); const data = res?.data || res; this.response = data?.choices?.[0]?.message?.content || JSON.stringify(data, null, 2) } catch (e) { this.error = e.response?.data?.detail || e.message || String(e) } finally { this.sending = false } }
  }
}
</script>
<style scoped>
.playground-card { max-width: 920px; }
.model-picker { display: grid; grid-template-columns: minmax(220px, 320px) minmax(280px, 1fr); gap: 12px; align-items: end; }
.selected-model { margin: -4px 0 12px; color: var(--text-muted); font-size: 12px; }
.response-box { margin-top: 20px; padding: 16px; background: var(--chat-asst-bg); border: 1px solid var(--border-soft); border-radius: 8px; white-space: pre-wrap; color: var(--text-primary); }
.error-box { margin-top: 20px; padding: 16px; background: var(--alert-error-bg); border: 1px solid var(--alert-error-border); border-radius: 8px; color: var(--alert-error-fg); }
@media (max-width: 760px) { .model-picker { grid-template-columns: 1fr; } }
</style>
