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
          <select v-model="selectedModelId" @change="syncModel">
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
      <div v-if="response" class="response-box"><strong>响应:</strong><div style="margin-top: 8px;">{{ response }}</div></div>
      <div v-if="error" class="error-box"><strong>错误:</strong> {{ error }}</div>
    </div>
  </div>
</template>
<script>
import api from '../api.js'
export default {
  name: 'PlaygroundView',
  data() { return { providers: [], models: [], selectedProviderId: 'auto', selectedModelId: '', form: { model: 'auto', message: '' }, sending: false, response: '', error: '' } },
  computed: {
    providersWithModels() { return this.providers.filter(p => this.models.some(m => m.provider_id === p.id)) },
    filteredModels() { return this.models.filter(m => String(m.provider_id) === String(this.selectedProviderId)) }
  },
  async mounted() {
    try { const [providers, models] = await Promise.all([api.getProviders().catch(() => []), api.getModels().catch(() => [])]); this.providers = providers || []; this.models = models || [] }
    catch (e) { console.error('加载模型失败:', e) }
  },
  methods: {
    modelCount(pid) { return this.models.filter(m => m.provider_id === pid).length },
    fullId(m) { return m.full_id || ((m.provider_name || this.providers.find(p => p.id === m.provider_id)?.name || '') + '/' + m.model_id) },
    onProviderChange() { if (this.selectedProviderId === 'auto') { this.form.model = 'auto'; this.selectedModelId = ''; return } const first = this.filteredModels[0]; this.selectedModelId = first ? String(first.id) : ''; this.syncModel() },
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
