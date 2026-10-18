<template>
  <div>
    <h1 style="margin-bottom: 20px;">Playground 🎮</h1>
    <div class="card playground-card">
      <div class="model-picker">
        <div class="form-group">
          <label>服务商</label>
          <!-- 可搜索：服务商名或旗下任一模型名命中都会出现在结果里（如搜 free → OpenCode） -->
          <SearchSelect
            :model-value="selectedProviderId"
            :options="providerOptions"
            placeholder="搜索服务商 / 模型名..."
            empty-text="无匹配服务商"
            :filter-fn="providerFilter"
            :hint-fn="providerHint"
            @update:model-value="onProviderPick"
          />
        </div>
        <div class="form-group" v-if="selectedProviderId !== 'auto'">
          <label>模型</label>
          <SearchSelect
            :model-value="selectedModelId"
            :options="modelOptions"
            :placeholder="modelOptions.length ? '搜索模型...' : '（该服务商暂无可用模型）'"
            empty-text="（该服务商暂无可用模型）"
            @update:model-value="onModelPick"
          />
        </div>
      </div>
      <div v-if="form.model !== 'auto'" class="selected-model"><code>{{ form.model }}</code></div>
      <div class="form-group">
        <label>消息</label>
        <textarea ref="msgInput" v-model="form.message" rows="4" placeholder="输入你的消息..."></textarea>
      </div>
      <!-- 智商测试题库：只显示图标，悬停展示题目/考察点/标准答案，点击填入输入框 -->
      <div class="iq-row">
        <span class="iq-title">🧠 智商测试</span>
        <div class="iq-icons">
          <button v-for="t in iqTests" :key="t.id" type="button" class="iq-icon"
                  :class="{ active: iqActive === t.id }" :aria-label="t.name" :title="t.name"
                  @mouseenter="iqActive = t.id" @mouseleave="iqActive = ''"
                  @focus="iqActive = t.id" @blur="iqActive = ''"
                  @click="fillIqTest(t)">{{ t.emoji }}</button>
        </div>
        <span class="iq-caption dim">悬停看题与答案 · 点击填入输入框</span>
      </div>
      <div v-if="iqCurrent" class="iq-detail">
        <div class="iq-detail-head">
          <strong>{{ iqCurrent.emoji }} {{ iqCurrent.name }}</strong>
          <span class="iq-tag">{{ iqCurrent.note }}</span>
          <span class="dim">标准答案：{{ iqCurrent.answer }}</span>
        </div>
        <div class="iq-question">{{ iqCurrent.prompt }}</div>
      </div>
      <div class="btn-row">
        <button class="btn btn-primary" @click="send" :disabled="!form.message.trim()">发送 🚀</button>
        <button class="btn btn-outline" @click="runPelican"
                title="用「SVG 鹈鹕骑自行车动画」提示词测试当前模型：流式生成 → 预览 → 下载 HTML">
          🐦 鹈鹕一键测试
        </button>
      </div>
      <div v-if="loadError" class="error-box"><strong>加载失败:</strong> {{ loadError }}</div>
    </div>

    <!-- 测试记录：连续下发互不阻塞（每条独立流式、可单独取消）；仅存本浏览器会话 -->
    <div class="card playground-card history-card">
      <div class="history-head">
        <strong>测试记录</strong>
        <span class="dim">{{ runs.length }} / {{ maxRuns }} 条 · 仅保存在本浏览器（切页/切标签不丢，重新登录后清空）</span>
        <span class="pelican-spacer"></span>
        <button v-if="runs.length" class="btn btn-outline btn-sm" @click="clearHistory">清空</button>
      </div>
      <div v-if="!runs.length" class="dim history-empty">暂无记录：发送消息或鹈鹕测试后，每次结果都会追加在这里</div>
      <div v-for="run in runs" :key="run.id" class="run-item">
        <div class="run-head">
          <span class="run-badge" :class="runBadgeCls(run)">{{ runBadge(run) }}</span>
          <span class="run-kind" :title="run.kind === 'pelican' ? '鹈鹕一键测试' : '对话测试'">{{ run.kind === 'pelican' ? '🐦' : '💬' }}</span>
          <code class="run-model">{{ run.served || run.reqModel }}</code>
          <span class="dim">{{ fmtClock(run.ts) }}</span>
          <span v-if="run.status === 'running'" class="dim">已接收 {{ fmtNum(run.chars) }} 字符…</span>
          <span v-else-if="run.elapsedMs" class="dim">{{ (run.elapsedMs / 1000).toFixed(1) }}s · {{ fmtNum(run.chars) }} 字符<span v-if="run.usage && run.usage.total_tokens"> · {{ fmtNum(run.usage.total_tokens) }} tok</span></span>
          <span class="pelican-spacer"></span>
          <button v-if="run.status === 'running'" class="btn btn-outline btn-sm" @click="cancelRun(run)">取消</button>
          <button v-if="run.status !== 'running' && (run.text || run.error)" class="btn btn-outline btn-sm" @click="run.expanded = !run.expanded">{{ run.expanded ? '收起' : '展开' }}</button>
          <button v-if="run.text && run.status !== 'running'" class="btn btn-outline btn-sm" @click="copyRun(run)">复制</button>
          <a v-if="run.kind === 'pelican' && run.html && run.status !== 'running'" class="btn btn-outline btn-sm" :href="pelicanUrl(run)" :download="run.filename">⬇ 下载</a>
          <button class="btn btn-outline btn-sm" @click="removeRun(run)">删除</button>
        </div>
        <div v-if="run.prompt" class="run-prompt dim">{{ run.prompt }}</div>
        <div v-if="run.note" class="run-note">{{ run.note }}</div>
        <div v-if="run.error" class="error-box run-error">{{ run.error }}</div>
        <template v-if="run.expanded">
          <iframe v-if="run.kind === 'pelican' && run.html" class="pelican-frame" sandbox="allow-scripts" :srcdoc="run.html" title="鹈鹕动画预览"></iframe>
          <details v-if="run.kind === 'pelican'" class="pelican-raw-fold">
            <summary>查看模型原始输出（{{ fmtNum((run.text || '').length) }} 字符）</summary>
            <pre class="pelican-raw">{{ run.text }}</pre>
          </details>
          <pre v-else class="run-text">{{ run.text }}</pre>
        </template>
        <div v-else-if="run.text" class="run-preview">{{ run.text }}</div>
      </div>
    </div>
  </div>
</template>
<script>
import { reactive, markRaw } from 'vue'
import api, { apiStream } from '../api.js'
import toast from '../toast.js'
import SearchSelect from '../components/SearchSelect.vue'

const PELICAN_PROMPT = '创建一个HTML，内容是SVG绘制一个鹈鹕骑自行车的2D动画，你不需要任何测试'

// 智商测试题库（GitHub 上大模型推理测试的经典题目）：
// 只渲染图标，悬停/聚焦显示题目与标准答案，点击把题干填入输入框。
// 每题都带「常见错误答案」，用来判断模型是推理对了还是踩了直觉陷阱。
const IQ_TESTS = [
  {
    id: 'candy', emoji: '🍬', name: '糖果保底', note: '最坏情况推理',
    answer: '21 颗（9 颗圆形 + 12 颗五角星形）；常见错误答案 29',
    prompt: '一个黑袋子里有三种口味的糖果：苹果味、桃子味、西瓜味。每种口味都有圆形和五角星形两种形状，形状可以靠手感摸出来，但口味摸不出来。袋子中各种糖果的数量如下：苹果味圆形 7 颗、苹果味五角星形 7 颗；桃子味圆形 9 颗、桃子味五角星形 6 颗；西瓜味圆形 8 颗、西瓜味五角星形 4 颗。你可以在不看的情况下，按形状挑选糖果。请问：最少要取出多少颗糖果，才能保证手中一定同时拥有「不同形状的苹果味和桃子味」——即要么有圆形苹果味 + 五角星形桃子味，要么有圆形桃子味 + 五角星形苹果味？请给出最少数量，并简要说明理由。最后一行只写答案。',
  },
  {
    id: 'strawberry', emoji: '🍓', name: '草莓数 r', note: '字符级计数',
    answer: '3 个；常见错误答案 2',
    prompt: '单词 strawberry 里有几个字母 r？请只回答数字。',
  },
  {
    id: 'batball', emoji: '⚾', name: '蝙蝠球', note: '认知反射 CRT',
    answer: '0.05 美元；常见错误答案 0.10',
    prompt: '一根球棒和一个球一共 1.10 美元。球棒比球贵 1.00 美元。球多少钱？请给出金额。',
  },
  {
    id: 'decimal', emoji: '🔢', name: '小数比大小', note: '数值位值',
    answer: '9.9 更大（9.90 > 9.11）',
    prompt: '按数值比较，9.11 和 9.9 哪个更大？请直接回答。',
  },
  {
    id: 'siblings', emoji: '👧', name: '兄弟姐妹', note: '视角转换',
    answer: '1 个；常见错误答案 2 或 3',
    prompt: 'Sally 有 3 个兄弟。她的每个兄弟都有 2 个姐妹。Sally 有几个姐妹？',
  },
  {
    id: 'snail', emoji: '🐌', name: '蜗牛爬井', note: '边界条件',
    answer: '第 8 天；常见错误答案 10 天',
    prompt: '一只蜗牛在一口 10 米深的井底。它白天向上爬 3 米，晚上滑下去 2 米。请问它第几天能爬出井口？',
  },
  {
    id: 'clock', emoji: '🕒', name: '时钟角度', note: '角速度/相对运动',
    answer: '7.5°；常见错误答案 0°',
    prompt: '钟表显示 3 点 15 分时，时针和分针之间的夹角是多少度？',
  },
  {
    id: 'potion', emoji: '🧪', name: '千瓶药水', note: '二进制编码',
    answer: '10 只，按二进制编号喂药；常见错误答案 1000/500/32',
    prompt: '有 1000 瓶药水，其中恰好 1 瓶有毒。喝下毒药后 24 小时内会死亡。你只有 10 只小白鼠和 24 小时，如何一次找出有毒的那瓶？请说明方法。',
  },
  {
    id: 'date', emoji: '📅', name: '日期推理', note: '月末进位',
    answer: '05/01/2021',
    prompt: '昨天是 2021 年 4 月 30 日。今天是几月几日？请用 MM/DD/YYYY 格式回答。',
  },
  {
    id: 'sallyanne', emoji: '🧠', name: '莎莉安妮', note: '心智理论',
    answer: '篮子（Sally 没看到 Anne 移动弹珠）',
    prompt: 'Sally 把一个弹珠放进篮子里，然后离开了房间。Sally 不在时，Anne 把弹珠从篮子里拿出来，放进了盒子里。Sally 回来后，她会先去哪里找她的弹珠？',
  },
]
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

// 记录仅存 sessionStorage：切换路由/切换浏览器标签仍在（同标签会话内），
// 关标签即消失；键里带 admin session 尾号，重新登录（换 token）后自动清空。
const PG_KEY = 'pg_runs'
const MAX_RUNS = 30
const MAX_STORED_TEXT = 60000   // 单条记录的正文/HTML 上限，防会话存储超配额

export default {
  name: 'PlaygroundView',
  components: { SearchSelect },
  data() {
    return {
      providers: [], modelStats: {}, lightModels: [],
      selectedProviderId: 'auto', selectedModelId: '',
      form: { model: 'auto', message: '' },
      loadError: '',
      runs: [],
      seq: 0,
      maxRuns: MAX_RUNS,
      iqTests: IQ_TESTS,
      iqActive: '',
    }
  },
  computed: {
    iqCurrent() { return this.iqTests.find((t) => t.id === this.iqActive) || null },

    // 已关闭服务商隐藏（2026-10-13 用户要求）：路由层跳过它们，选了也必失败。
    providersWithModels() { return this.providers.filter(p => p.enabled !== false && this.modelCount(p.id) > 0) },
    // 轻量全量目录（9 字段）：模型下拉与「按模型名搜服务商」共用同一份数据
    providerModelCount() {
      const map = {}
      for (const m of this.lightModels) {
        if (m.enabled === false) continue
        map[m.provider_id] = (map[m.provider_id] || 0) + 1
      }
      return map
    },
    providerModelNames() {
      const map = {}
      for (const m of this.lightModels) {
        if (m.enabled === false) continue
        const arr = map[m.provider_id] || (map[m.provider_id] = [])
        if (m.model_id) arr.push(m.model_id)
        if (m.display_name && m.display_name !== m.model_id) arr.push(m.display_name)
      }
      return map
    },
    providerOptions() {
      const opts = [{ value: 'auto', label: 'Auto（智能选举）', modelNames: [] }]
      for (const p of this.providersWithModels) {
        opts.push({ value: String(p.id), label: `${p.name}（${this.modelCount(p.id)}）`, modelNames: this.providerModelNames[p.id] || [] })
      }
      return opts
    },
    filteredModels() {
      return this.lightModels.filter(m => String(m.provider_id) === String(this.selectedProviderId) && m.enabled !== false)
    },
    modelOptions() {
      return this.filteredModels.map(m => ({
        value: String(m.id),
        label: m.display_name || m.model_id,
        keywords: `${m.model_id || ''} ${m.display_name || ''}`,
      }))
    },
  },
  mounted() {
    this.runs = this.loadHistory()
    this.loadInitial()
  },
  beforeUnmount() {
    // 不 abort 在跑的任务：交换出去的那条流继续跑完并写回 sessionStorage，
    // 切回来能看到完整结果（切标签页保留的语义）。只回收 blob URL。
    for (const r of this.runs) { if (r.url) URL.revokeObjectURL(r.url) }
    this.persist()
  },
  methods: {
    modelCount(pid) { return this.modelStats[pid] || this.providerModelCount[pid] || 0 },
    fullId(m) { return m.full_id || ((m.provider_name || this.providers.find(p => p.id === m.provider_id)?.name || '') + '/' + m.model_id) },
    /** 服务商搜索：命中服务商名，或旗下任一模型名/显示名命中（如搜 free → OpenCode）。 */
    providerFilter(opt, q) {
      if (opt.value === 'auto') return 'auto'.includes(q) || '智能选举'.includes(q)
      if ((opt.label || '').toLowerCase().includes(q)) return true
      return (opt.modelNames || []).some(n => String(n).toLowerCase().includes(q))
    },
    /** 服务商行右侧提示：输入命中模型时列出命中的模型，让「为什么这家会出现」一目了然。 */
    providerHint(opt, query) {
      const q = (query || '').trim().toLowerCase()
      if (!q || opt.value === 'auto') return ''
      const hits = (opt.modelNames || []).filter(n => String(n).toLowerCase().includes(q))
      if (!hits.length || (opt.label || '').toLowerCase().includes(q)) return ''
      const uniq = [...new Set(hits)]
      return '含模型：' + uniq.slice(0, 3).join('、') + (uniq.length > 3 ? ` 等 ${uniq.length} 个` : '')
    },
    onProviderPick(val) {
      const same = String(val) === String(this.selectedProviderId)
      this.selectedProviderId = val
      if (val === 'auto') { this.form.model = 'auto'; this.selectedModelId = ''; return }
      // 重选同一服务商时保留当前模型，只切服务商才落到第一家
      if (!same) {
        const first = this.filteredModels[0]
        this.selectedModelId = first ? String(first.id) : ''
      }
      this.syncModel()
    },
    onModelPick(val) {
      this.selectedModelId = val
      this.syncModel()
    },
    syncModel() { const m = this.filteredModels.find(x => String(x.id) === String(this.selectedModelId)); this.form.model = m ? this.fullId(m) : 'auto' },

    async loadInitial() {
      // 服务商 + 每家模型数 + 轻量全量模型目录并行拉取：
      // 后两者让「服务商/模型」两个下拉都能本地即时搜索（含按模型名命中服务商），不再逐服务商懒加载。
      const [providers, stats, light] = await Promise.all([
        api.getProviders().then(d => ({ ok: true, d })).catch(e => ({ ok: false, e })),
        api.getProviderModelStats().then(d => ({ ok: true, d })).catch(e => ({ ok: false, e })),
        api.getModelsLight().then(d => ({ ok: true, d })).catch(e => ({ ok: false, e })),
      ])
      if (providers.ok) this.providers = providers.d || []
      if (stats.ok) {
        const map = {}
        for (const s of (stats.d || [])) map[s.provider_id] = s.model_count
        this.modelStats = map
      }
      if (light.ok) this.lightModels = light.d || []
      const failed = [providers, stats, light].filter(x => !x.ok)
      if (failed.length === 3) this.loadError = (failed[0].e?.message || String(failed[0].e)) + '（请检查网络后刷新）'
      else if (failed.length) console.error('Playground 部分加载失败:', failed.map(x => x.e))
    },

    fmtNum(n) { return Number(n || 0).toLocaleString('zh-CN') },
    fmtClock(ts) {
      const d = new Date(ts), p = n => String(n).padStart(2, '0')
      return `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`
    },
    runBadge(r) {
      return { running: '生成中', done: '完成', error: '失败', cancelled: '已取消', interrupted: '已中断' }[r.status] || r.status
    },
    runBadgeCls(r) {
      if (r.status === 'done') return r.note && !r.error ? 'warn' : 'ok'
      if (r.status === 'running') return 'info'
      return 'err'
    },

    // ── 连续下发：每次发送/鹈鹕测试追加一条独立记录（互不阻塞、各自可取消） ──
    send() {
      const msg = this.form.message.trim()
      if (!msg) return
      this.newRun('chat', this.form.model || 'auto', msg, [{ role: 'user', content: msg }])
    },
    runPelican() {
      this.newRun('pelican', this.form.model || 'auto', PELICAN_PROMPT, [{ role: 'user', content: PELICAN_PROMPT }])
    },
    /** 点击智商测试图标：题干填入输入框（不直接发送，用户可先改模型/追加要求）。 */
    fillIqTest(t) {
      this.form.message = t.prompt
      this.$nextTick(() => {
        const el = this.$refs.msgInput
        if (el && el.focus) {
          el.focus()
          el.setSelectionRange(el.value.length, el.value.length)
        }
      })
    },
    newRun(kind, reqModel, promptText, messages) {
      // 用 reactive 包一层：流式回调（reader.read 的 await 之后）改 run.chars/text 时
      // 才能触发重渲染。直接 push 裸对象进 this.runs，Vue3 只对数组本身做响应式，
      // 不会代理元素 → 只有切换模型/重渲染才会「忽然」看到结果。
      const run = reactive({
        id: `r${Date.now()}-${++this.seq}`,
        ts: Date.now(),
        kind, reqModel, served: '',
        status: 'running',
        chars: 0, elapsedMs: 0,
        text: '', html: '', filename: '',
        error: '', note: '', usage: null,
        prompt: promptText.length > 120 ? promptText.slice(0, 120) + '…' : promptText,
        expanded: false,
        abortCtl: null, url: '',
      })
      this.runs.unshift(run)
      if (this.runs.length > MAX_RUNS) this.runs.splice(MAX_RUNS)
      this.persist()
      this.streamRun(run, { model: reqModel, messages, stream: true })
    },
    /** 单条记录的流式执行：SSE 逐块累积；完成后按类型收尾（对话=正文；鹈鹕=提取 HTML）。 */
    async streamRun(run, payload) {
      const startedAt = Date.now()
      const abort = new AbortController()
      run.abortCtl = markRaw(abort)   // 原生对象不能被 reactive 代理（abort() 会 Illegal invocation）
      let buf = '', served = '', reasoning = '', streamError = ''
      try {
        const stream = await apiStream('/admin/api/playground', payload, abort.signal)
        const reader = stream.getReader()
        const decoder = new TextDecoder('utf-8')
        let pending = ''
        const consume = (chunkText) => {
          pending += chunkText
          // 按 SSE 事件切分：双换行分隔；每行 data: 载荷
          const parts = pending.split('\n\n')
          pending = parts.pop() || ''
          for (const part of parts) {
            for (const line of part.split('\n')) {
              if (!line.startsWith('data:')) continue
              const pl = line.slice(5).trim()
              if (!pl || pl === '[DONE]') continue
              let obj = null
              try { obj = JSON.parse(pl) } catch (e) { continue }
              if (obj && obj.error) {
                streamError = typeof obj.error === 'string' ? obj.error : (obj.error.message || JSON.stringify(obj.error))
                continue
              }
              if (obj && obj.model) served = obj.model
              if (obj && obj.usage) run.usage = obj.usage
              const delta = obj?.choices?.[0]?.delta || {}
              if (delta.content) {
                buf += delta.content
                run.chars = buf.length
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
        run.served = served || run.reqModel
        run.elapsedMs = Date.now() - startedAt
        if (!buf.trim() && !reasoning) {
          // 什么都没有：优先用流里的错误（OpenCode 类慢模型常是撞超时/无内容）
          throw new Error(streamError || '模型没有返回任何内容')
        }
        if (run.kind === 'pelican') {
          run.text = buf || reasoning
          const html = extractHtml(buf)
          run.html = html
          if (streamError) {
            run.note = '⚠️ 流中途报错（' + streamError + '），以下为已接收部分'
          } else if (!buf.trim()) {
            run.note = `模型只输出思考、没有正文（${this.fmtNum(reasoning.length)} 字符思考，可能是生成太慢撞到超时）`
          } else if (!/<\s*(html|svg|body|div|style|script)/i.test(html)) {
            run.note = '⚠️ 回复里没有找到 HTML/SVG 代码（展开可查看原始输出）'
          } else if (!/<\/html>\s*$/i.test(html.trim()) && !/<\/svg>\s*$/i.test(html.trim())) {
            run.note = 'ℹ️ 提取到的代码可能不完整（截断/围栏缺失），预览与下载以提取结果为准'
          }
          const [prov, ...rest] = String(run.served).split('/')
          const modelPart = rest.length ? rest.join('/') : run.reqModel
          run.filename = `${sanitizeName(prov)}-${sanitizeName(modelPart)}-${stamp()}.html`
        } else {
          run.text = buf || reasoning
          if (streamError) run.note = '⚠️ 流中途报错（' + streamError + '），以上为已接收部分'
          else if (!buf.trim()) run.note = `模型只输出思考、没有正文（${this.fmtNum(reasoning.length)} 字符思考，可能是生成太慢撞到超时）`
        }
        run.status = 'done'
      } catch (e) {
        if (e?.name === 'AbortError') {
          run.status = 'cancelled'
          run.note = '已取消（已接收 ' + this.fmtNum((buf || reasoning).length) + ' 字符）'
          run.text = run.text || buf || reasoning
        } else {
          run.status = 'error'
          run.error = String(e?.message || e)
          run.text = buf || reasoning || run.text
          run.elapsedMs = run.elapsedMs || (Date.now() - startedAt)
        }
      } finally {
        run.abortCtl = null
        this.persist()
      }
    },
    cancelRun(run) { if (run.abortCtl) run.abortCtl.abort() },
    copyRun(run) {
      const text = run.text || ''
      if (!text) return
      if (navigator.clipboard?.writeText) {
        navigator.clipboard.writeText(text).then(() => toast.success('已复制到剪贴板')).catch(() => toast.error('复制失败'))
      } else {
        toast.error('当前浏览器不支持剪贴板 API')
      }
    },
    pelicanUrl(run) {
      if (!run.html) return '#'
      if (!run.url) run.url = URL.createObjectURL(new Blob([run.html], { type: 'text/html;charset=utf-8' }))
      return run.url
    },
    removeRun(run) {
      if (run.abortCtl) run.abortCtl.abort()
      if (run.url) URL.revokeObjectURL(run.url)
      const i = this.runs.indexOf(run)
      if (i !== -1) this.runs.splice(i, 1)
      this.persist()
    },
    clearHistory() {
      for (const r of this.runs) {
        if (r.abortCtl) r.abortCtl.abort()
        if (r.url) URL.revokeObjectURL(r.url)
      }
      this.runs = []
      this.persist()
    },

    // ── 持久化（sessionStorage；仅前端，不入库） ──
    _sessionSuffix() { return (localStorage.getItem('aigate_session') || '').slice(-8) },
    persist() {
      const sid = this._sessionSuffix()
      const ser = (r) => ({
        id: r.id, ts: r.ts, kind: r.kind, reqModel: r.reqModel, served: r.served,
        status: r.status, chars: r.chars, elapsedMs: r.elapsedMs,
        text: (r.text || '').slice(0, MAX_STORED_TEXT),
        html: (r.html || '').slice(0, MAX_STORED_TEXT),
        filename: r.filename, error: r.error, note: r.note, prompt: r.prompt,
        usage: r.usage || null, expanded: !!r.expanded,
      })
      let list = this.runs.slice(0, MAX_RUNS)
      for (;;) {
        try {
          sessionStorage.setItem(PG_KEY, JSON.stringify({ sid, runs: list.map(ser) }))
          return
        } catch (e) {
          if (!list.length) return          // 配额不足：丢最旧的重试（只影响持久化，不动界面）
          list = list.slice(0, list.length - 1)
        }
      }
    },
    loadHistory() {
      try {
        const d = JSON.parse(sessionStorage.getItem(PG_KEY) || 'null')
        if (!d || d.sid !== this._sessionSuffix() || !Array.isArray(d.runs)) return []
        return d.runs.slice(0, MAX_RUNS).map((r) => ({
          ...r,
          usage: r.usage || null,
          abortCtl: null, url: '',
          // 刷新/关页时还在跑的记录不可能再收尾 → 标记已中断
          status: r.status === 'running' ? 'interrupted' : r.status,
          note: r.status === 'running' ? '已中断（页面刷新或离开，流未完成）' : r.note,
        }))
      } catch (e) { return [] }
    },
  }
}
</script>
<style scoped>
.playground-card { max-width: 920px; }
.model-picker { display: grid; grid-template-columns: minmax(220px, 320px) minmax(280px, 1fr); gap: 12px; align-items: end; }
.selected-model { margin: -4px 0 12px; color: var(--text-muted); font-size: 12px; }
/* 智商测试题库：图标行 + 悬停展开的题目详情 */
.iq-row { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; margin: -4px 0 10px; }
.iq-title { font-size: 12px; color: var(--text-muted); }
.iq-icons { display: flex; gap: 4px; flex-wrap: wrap; }
.iq-icon { width: 32px; height: 32px; padding: 0; font-size: 17px; line-height: 1; cursor: pointer; background: var(--surface-1, #0f1622); border: 1px solid var(--border-soft); border-radius: 8px; transition: border-color .15s, transform .15s; }
.iq-icon:hover, .iq-icon.active, .iq-icon:focus-visible { border-color: var(--accent, #6366f1); transform: translateY(-1px); outline: none; }
.iq-caption { font-size: 12px; }
.iq-detail { margin: 0 0 12px; padding: 10px 12px; background: var(--surface-1, #0f1622); border: 1px solid var(--border-soft); border-radius: 8px; }
.iq-detail-head { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; font-size: 12px; }
.iq-detail-head .dim { font-size: 12px; }
.iq-tag { font-size: 11px; padding: 1px 8px; border-radius: 999px; border: 1px solid var(--border-soft); color: var(--text-muted); }
.iq-question { margin-top: 6px; font-size: 12px; color: var(--text-primary); line-height: 1.6; white-space: pre-wrap; }
.btn-row { display: flex; gap: 10px; flex-wrap: wrap; }
.error-box { margin-top: 20px; padding: 16px; background: var(--alert-error-bg); border: 1px solid var(--alert-error-border); border-radius: 8px; color: var(--alert-error-fg); }
.pelican-frame { width: 100%; height: 420px; border: 1px solid var(--border-soft); border-radius: 8px; background: #fff; }
.pelican-spacer { flex: 1; }
.pelican-raw-fold { margin-top: 10px; font-size: 12px; color: var(--text-muted); }
.pelican-raw { max-height: 260px; overflow: auto; white-space: pre-wrap; word-break: break-all; background: var(--surface-1, #0f1622); border: 1px solid var(--border-soft); border-radius: 6px; padding: 10px; font-family: var(--font-mono, monospace); font-size: 12px; color: var(--text-primary); }
a.btn { text-decoration: none; display: inline-flex; align-items: center; }
/* 测试记录 */
.history-card { margin-top: 16px; }
.history-head { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; margin-bottom: 8px; }
.history-head .dim { font-size: 12px; }
.history-empty { padding: 4px 0 2px; font-size: 12px; }
.run-item { padding: 10px 0; border-top: 1px solid var(--border-soft); }
.run-item:first-of-type { border-top: none; }
.run-head { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.run-head .dim { font-size: 12px; }
.run-badge { font-size: 11px; padding: 1px 8px; border-radius: 999px; border: 1px solid var(--border-soft); }
.run-badge.info { color: var(--text-muted); }
.run-badge.ok { color: var(--success, #22c55e); border-color: var(--success, #22c55e); }
.run-badge.warn { color: var(--warning, #f59e0b); border-color: var(--warning, #f59e0b); }
.run-badge.err { color: var(--alert-error-fg); border-color: var(--alert-error-border); }
.run-model { font-size: 12px; color: var(--text-primary); }
.run-prompt { font-size: 12px; margin-top: 6px; }
.run-note { font-size: 12px; color: var(--warning, #f59e0b); margin-top: 6px; }
.run-error { margin-top: 8px; padding: 10px 12px; font-size: 12px; }
.run-preview { margin-top: 6px; font-size: 12px; color: var(--text-muted); white-space: pre-wrap; word-break: break-word; max-height: 4.8em; overflow: hidden; }
.run-text { margin-top: 8px; max-height: 420px; overflow: auto; white-space: pre-wrap; word-break: break-word; background: var(--surface-1, #0f1622); border: 1px solid var(--border-soft); border-radius: 6px; padding: 10px; font-family: var(--font-mono, monospace); font-size: 12px; color: var(--text-primary); }
@media (max-width: 760px) { .model-picker { grid-template-columns: 1fr; } }
</style>
