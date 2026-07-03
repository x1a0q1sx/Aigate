<template>
  <div>
    <h1 style="margin-bottom: 20px;">Playground 🎮</h1>
    <div class="card">
      <div class="form-group">
        <label>模型</label>
        <select v-model="form.model">
          <option value="auto">🤖 Auto（智能选举）</option>
          <option v-for="m in models" :key="m.id" :value="m.full_id || ((m.provider_name || m.provider?.name || '') + '/' + m.model_id)">
            {{ m.full_id || ((m.provider_name || m.provider?.name || '') + '/' + m.model_id) }}
          </option>
        </select>
      </div>
      <div class="form-group">
        <label>消息</label>
        <textarea v-model="form.message" rows="4" placeholder="输入你的消息..."></textarea>
      </div>
      <button class="btn btn-primary" @click="send" :disabled="sending">
        {{ sending ? '发送中...' : '发送 🚀' }}
      </button>
      <div v-if="response" style="margin-top: 20px; padding: 16px; background: #f8f9fa; border-radius: 8px; white-space: pre-wrap;">
        <strong>响应:</strong>
        <div style="margin-top: 8px;">{{ response }}</div>
      </div>
      <div v-if="error" style="margin-top: 20px; padding: 16px; background: #fff0f0; border-radius: 8px; color: #c0392b;">
        <strong>错误:</strong> {{ error }}
      </div>
    </div>
  </div>
</template>
<script>
import api from '../api.js'
export default {
  name: 'PlaygroundView',
  data() {
    return {
      models: [],
      form: { model: 'auto', message: '' },
      sending: false,
      response: '',
      error: ''
    }
  },
  async mounted() {
    try {
      const res = await api.getModels()
      this.models = res || []
    } catch (e) {
      console.error('加载模型失败:', e)
    }
  },
  methods: {
    async send() {
      if (!this.form.message.trim()) return
      this.sending = true
      this.response = ''
      this.error = ''
      try {
        const res = await api.playground({
          model: this.form.model,
          messages: [{ role: 'user', content: this.form.message }]
        })
        const data = res?.data || res
        this.response = data?.choices?.[0]?.message?.content || JSON.stringify(data, null, 2)
      } catch (e) {
        this.error = e.response?.data?.detail || e.message || String(e)
      } finally {
        this.sending = false
      }
    }
  }
}
</script>