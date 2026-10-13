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
      <div class="btn-row">
        <button class="btn btn-primary" @click="send" :disabled="sending || pelican.running">{{ sending ? '发送中...' : '发送 🚀' }}</button>
        <button class="btn btn-outline" @click="runPelican" :disabled="sending || pelican.running"
                title="用「SVG 鹈鹕骑自行车动画」提示词测试当前模型：流式生成 → 预览 → 下载 HTML">
          {{ pelican.running ? '🐦 生成中...' : '🐦 鹈鹕一键测试' }}
        </button>
      </div>
      <div v-if="loadError" class="error-box"><strong>加载失败:</strong> {{ loadError }}</div>
      <div v-if="response" class="response-box"><strong>响应:</strong><div style="margin-top: 8px;">{{ response }}</div></div>
      <div v-if="error" class="error-box"><strong>错误:</strong> {{ error }}</div>

      <!-- 鹈鹕一键测试：流式进度 → HTML 预览（iframe sandbox）→ 下载 -->
      <div v-if="pelican.running || pelican.html || pelican.error || pelican.note" class="pelican-box">
        <div class="pelican-head">
          <strong>🐦 鹈鹕测试</strong>
          <span v-if="pelican.running" class="dim">生成中… 已接收 {{ fmtNum(pelican.chars) }} 字符</span>
          <span v-else-if="pelican.done" class="dim">✅ 完成 · {{ pelican.served }} · {{ fmtNum(pelican.chars) }} 字符 · {{ pelican.elapsed }}s</span>
          <span class="pelican-spacer"></span>
          <button v-if="pelican.running" class="btn btn-outline btn-sm" @click="cancelPelican">取消</button>
          <template v-if="pelican.url">
            <a class="btn btn-outline btn-sm" :href="pelican.url" :download="pelican.filename">⬇ 下载 {{ pelican.filename }}</a>
            <a class="btn btn-outline btn-sm" :href="pelican.url" target="_blank" rel="noopener">新标签打开</a>
          </template>
        </div>
        <p v-if="pelican.note" class="pelican-note">{{ pelican.note }}</p>
        <div v-if="pelican.error" class="error-box">{{ pelican.error }}</div>
        <iframe v-if="pelican.html" class="pelican-frame" sandbox="allow-scripts" :srcdoc="pelican.html" title="鹈鹕动画预览"></iframe>
        <details v-if="pelican.raw" class="pelican-raw-fold">
          <summary>查看模型原始输出（{{ fmtNum(pelican.raw.length) }} 字符）</summary>
          <pre class="pelican-raw">{{ pelican.raw }}</pre>
        </details>
      </div>
    </div>
  </div>
</template>
<script>
import api, { apiStream } from '../api.js'

const PELICAN_PROMPT = '创建一个HTML，内容是SVG绘制一个鹈鹕骑自行车的2D动画，你不需要任何测试'
// 只取回复中的 HTML 代码：优先 ``` 围栏（含 html/xml 等语言标记），
// 其次从首个 <!DOCTYPE/<html 截到最后一个 </html>，再退 <svg…</svg>；都找不到则原样返回
function extractHtml(text) {
  if (!text) return ''
  const fence = text.match(/```(?:html)?\s*([\s\S]*?)```/i)
  if (fence && /<(html|svg|body|div|style)\b/i.test(fence[1])) {
    // 围栏可能带其它语言标记（如 ```xml）→ 去掉首行裸单词
    return fence[1].replace(/^\s*[a-z]{2,10}\s*\n/i, '').trim()
  }
  const idx = text.search(/<!DOCTYPE\s+html|<html[\s>]/i)
  if (idx >= 0) {
    const rest = text.slice(idx)
    // 截到最后一个 </html>（防尾随说明文字混入下载文件）
    const end = rest.toLowerCase().lastIndexOf('</html>')
    return (end >= 0 ? rest.slice(0, end + 7) : rest).trim()
  }
  const svgIdx = text.search(/<svg[\s>]/i)
  if (svgIdx >= 0) {
    // 截到最后一个 </svg>（防尾随说明文字混入下载文件）
    const rest = text.slice(svgIdx)
    const end = rest.toLowerCase().lastIndexOf('</svg>')
    return (end >= 0 ? rest.slice(0, end + 6) : rest).trim()
  }
  return text.trim()
}
function stamp() {
  const d = new Date(), p = n => String(n).padStart(2, '0')
  return `${d.getFullYear()}${p(d.getMonth() + 1)}${p(d.getDate())}-${p(d.getHours())}${p(d.getMinutes())}${p(d.getSeconds())}`
}
function sanitizeName(s) {
  return String(s || '').replace(/[\\/:*?"<>|\s]+/g, '_').replace(/_+/g, '_').replace(/^_|_$/g, '') || 'unknown'
}

export default {
  name: 'PlaygroundView',
  data() { return { providers: [], modelStats: {}, models: [], loadedPid: null, modelsLoading: false, selectedProviderId: 'auto', selectedModelId: '', form: { model: 'auto', message: '' }, sending: false, response: '', error: '', loadError: '', pelican: { running: false, done: false, chars: 0, raw: '', html: '', error: '', note: '', url: '', filename: '', served: '', elapsed: 0, abort: null } } },
  computed: {
    // 懒加载：服务商下拉只依赖轻量的 model-stats（每家模型数），不再等全量模型列表。
    // 已关闭服务商隐藏（2026-10-13 用户要求）：路由层跳过它们，选了也必失败。
    providersWithModels() { return this.providers.filter(p => p.enabled !== false && (this.modelStats[p.id] || 0) > 0) },
    filteredModels() { return this.models.filter(m => String(m.provider_id) === String(this.selectedProviderId)) }
  },
  mounted() {
    this.loadInitial()
  },
  beforeUnmount() {
    if (this.pelican.abort) this.pelican.abort.abort()
    if (this.pelican.url) URL.revokeObjectURL(this.pelican.url)
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
    async send() { if (!this.form.message.trim()) return; this.sending = true; this.response = ''; this.error = ''; try { const res = await api.playground({ model: this.form.model, messages: [{ role: 'user', content: this.form.message }] }); const data = res?.data || res; this.response = data?.choices?.[0]?.message?.content || JSON.stringify(data, null, 2) } catch (e) { this.error = e.response?.data?.detail || e.message || String(e) } finally { this.sending = false } },

    async loadInitial() {
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

    fmtNum(n) { return Number(n || 0).toLocaleString('zh-CN') },
    resetPelican() {
      if (this.pelican.url) URL.revokeObjectURL(this.pelican.url)
      this.pelican = { running: false, done: false, chars: 0, raw: '', html: '', error: '', note: '', url: '', filename: '', served: '', elapsed: 0, abort: null }
    },
    cancelPelican() {
      if (this.pelican.abort) this.pelican.abort.abort()
    },
    /** 鹈鹕一键测试：流式生成 → 累积文本 → 提取 HTML → iframe 预览 + Blob 下载。 */
    async runPelican() {
      if (this.pelican.running || this.sending) return
      const reqModel = this.form.model || 'auto'
      this.resetPelican()
      const st = this.pelican
      st.running = true
      const startedAt = Date.now()
      const abort = new AbortController()
      st.abort = abort
      let buf = ''
      let served = ''
      let reasoning = ''
      try {
        const stream = await apiStream('/admin/api/playground', {
          model: reqModel,
          messages: [{ role: 'user', content: PELICAN_PROMPT }],
          stream: true,
        }, abort.signal)
        const reader = stream.getReader()
        const decoder = new TextDecoder('utf-8')
        let pending = ''
        let streamError = ''
        const consume = (chunkText) => {
          pending += chunkText
          // 按 SSE 事件切分：双换行分隔；每行 data: 载荷
          const parts = pending.split('\n\n')
          pending = parts.pop() || ''
          for (const part of parts) {
            for (const line of part.split('\n')) {
              if (!line.startsWith('data:')) continue
              const payload = line.slice(5).trim()
              if (!payload || payload === '[DONE]') continue
              let obj = null
              try { obj = JSON.parse(payload) } catch (e) { continue }
              if (obj && obj.error) {
                streamError = typeof obj.error === 'string' ? obj.error : (obj.error.message || JSON.stringify(obj.error))
                continue
              }
              if (obj && obj.model) served = obj.model
              const delta = obj?.choices?.[0]?.delta || {}
              if (delta.content) {
                buf += delta.content
                st.chars = buf.length
              }
              if (delta.reasoning_content) reasoning += delta.reasoning_content
            }
          }
        }
        for (;;) {
          const { done, value } = await reader.read()
          if (done) break
          consume(decoder.decode(value, { stream: true }))
        }
        consume(decoder.decode())          // 冲刷解码器尾字节
        if (pending) consume('\n\n')       // 末事件可能无尾随空行
        st.served = served || reqModel
        st.raw = buf || reasoning
        st.elapsed = ((Date.now() - startedAt) / 1000).toFixed(1)
        if (!buf.trim()) {
          // 只有思考没有正文：OpenCode 类慢模型常见（长时间规划后撞上网关超时）
          throw new Error(streamError || (reasoning
            ? `模型只输出思考、没有正文（${this.fmtNum(reasoning.length)} 字符思考，可能是生成太慢撞到超时）。可查看下方原始输出，或换一个模型重试`
            : '模型没有返回任何内容'))
        }
        const html = extractHtml(buf)
        st.html = html
        if (streamError) {
          st.note = '⚠️ 流中途报错（' + streamError + '），以下为已接收部分'
        } else if (!/<\s*(html|svg|body|div|style|script)/i.test(html)) {
          st.note = '⚠️ 回复里没有找到 HTML/SVG 代码（下方原始输出可查看全文）'
        } else if (!/<\/html>\s*$/i.test(html.trim()) && !/<\/svg>\s*$/i.test(html.trim())) {
          st.note = 'ℹ️ 提取到的代码可能不完整（截断/围栏缺失），预览与下载以提取结果为准'
        }
        const [prov, ...rest] = String(st.served).split('/')
        const modelPart = rest.length ? rest.join('/') : reqModel
        st.filename = `${sanitizeName(prov)}-${sanitizeName(modelPart)}-${stamp()}.html`
        st.url = URL.createObjectURL(new Blob([html], { type: 'text/html;charset=utf-8' }))
        st.done = true
      } catch (e) {
        if (e?.name === 'AbortError') {
          st.note = '已取消（已生成 ' + this.fmtNum(buf.length || reasoning.length) + ' 字符）'
          st.raw = buf || reasoning
        } else {
          st.error = String(e?.message || e)
        }
      } finally {
        st.running = false
        st.abort = null
      }
    },
  }
}
</script>
<style scoped>
.playground-card { max-width: 920px; }
.model-picker { display: grid; grid-template-columns: minmax(220px, 320px) minmax(280px, 1fr); gap: 12px; align-items: end; }
.selected-model { margin: -4px 0 12px; color: var(--text-muted); font-size: 12px; }
.btn-row { display: flex; gap: 10px; flex-wrap: wrap; }
.response-box { margin-top: 20px; padding: 16px; background: var(--chat-asst-bg); border: 1px solid var(--border-soft); border-radius: 8px; white-space: pre-wrap; color: var(--text-primary); }
.error-box { margin-top: 20px; padding: 16px; background: var(--alert-error-bg); border: 1px solid var(--alert-error-border); border-radius: 8px; color: var(--alert-error-fg); }
.pelican-box { margin-top: 20px; padding: 16px; background: var(--surface-2, var(--bg-card)); border: 1px solid var(--border-soft); border-radius: 8px; }
.pelican-head { display: flex; align-items: center; gap: 12px; flex-wrap: wrap; margin-bottom: 10px; }
.pelican-spacer { flex: 1; }
.pelican-head .dim { color: var(--text-muted); font-size: 12px; }
.pelican-note { margin: 0 0 10px; font-size: 12px; color: var(--warning, #f59e0b); }
.pelican-frame { width: 100%; height: 420px; border: 1px solid var(--border-soft); border-radius: 8px; background: #fff; }
.pelican-raw-fold { margin-top: 10px; font-size: 12px; color: var(--text-muted); }
.pelican-raw { max-height: 260px; overflow: auto; white-space: pre-wrap; word-break: break-all; background: var(--surface-1, #0f1622); border: 1px solid var(--border-soft); border-radius: 6px; padding: 10px; font-family: var(--font-mono, monospace); font-size: 12px; color: var(--text-primary); }
a.btn { text-decoration: none; display: inline-flex; align-items: center; }
@media (max-width: 760px) { .model-picker { grid-template-columns: 1fr; } }
</style>
