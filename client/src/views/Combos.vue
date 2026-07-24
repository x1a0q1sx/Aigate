<template>
  <div>
    <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 20px;">
      <div>
        <h1>组合路由 ⚡</h1>
        <p style="color: var(--text-muted); font-size: 14px; margin-top: 4px;">
          自定义模型组合名，按策略在候选模型间 fallback / 轮询。请求时用 <code>combo:组合名</code> 调用。
        </p>
      </div>
      <button class="btn btn-primary" @click="openAddModal">+ 新建组合</button>
    </div>

    <div class="card" v-if="combos.length === 0" style="text-align: center; color: var(--text-muted); padding: 40px;">
      还没有组合。点击右上角"新建组合"开始。
    </div>

    <div v-else>
      <div class="combo-card" v-for="c in combos" :key="c.id">
        <div class="combo-header">
          <span class="combo-name">combo:{{ c.name }}</span>
          <span class="cred-badge" :class="'strat-' + c.strategy" :title="'策略: ' + stratLabel(c.strategy)">
            {{ stratLabel(c.strategy) }}
          </span>
          <span class="combo-count" :title="c.model_ids.map(m => m.full_id || (m.provider + '/' + m.model_id)).join(', ')">
            {{ (c.model_ids || []).length }} 个模型
          </span>
          <span style="flex:1"></span>
          <button class="btn btn-outline btn-sm" @click="editCombo(c)">编辑</button>
          <button class="btn btn-danger btn-sm" @click="deleteCombo(c)">删除</button>
        </div>
        <div class="combo-body" v-if="c.description">{{ c.description }}</div>
        <div class="combo-tags">
          <span class="combo-tag" v-for="(m, i) in (c.model_ids || [])" :key="i">
            {{ m.provider }}/{{ m.model_id }}
          </span>
        </div>
      </div>
    </div>

    <!-- 添加/编辑 Modal -->
    <div v-if="showModal" class="modal-overlay" @click.self="showModal = false">
      <div class="modal-content" style="width: 1080px;">
        <h3>{{ isEditing ? '编辑组合' : '新建组合' }}</h3>

        <div class="form-group">
          <label>组合名（调用时用 <code>combo:组合名</code>）</label>
          <input v-model="form.name" placeholder="例如 fast-cheap" :disabled="isEditing" />
        </div>

        <div class="form-group">
          <label>策略</label>
          <select v-model="form.strategy">
            <option value="fallback">fallback 顺序兜底（第一个失败 → 下一个）</option>
            <option value="round_robin">round_robin 轮询（每次请求轮到下一个）</option>
            <option value="fusion" disabled>fusion 扇出合并（暂未实现）</option>
          </select>
        </div>

        <div class="form-group">
          <label>描述（可选）</label>
          <input v-model="form.description" placeholder="例如 免费 + 便宜模型的备份组合" />
        </div>

        <div class="form-group">
          <label>候选模型 <span style="font-size: 12px; color: var(--text-muted); font-weight: 400;">（左侧服务商默认收起，点 ▶ 展开点选；右侧顺序即 fallback / 轮询顺序）</span></label>

          <div class="template-bar">
            <span class="template-label">模板</span>
            <select class="template-select" v-model="templatePick" @change="onTemplatePick">
              <option value="">选择预设 / 模板…</option>
              <optgroup label="内置预设">
                <option value="__preset:domestic">🇨🇳 国产替代</option>
                <option value="__preset:claude">🤖 Claude 全家桶</option>
                <option value="__preset:gpt">🟢 GPT 全家桶</option>
              </optgroup>
              <optgroup label="我的模板" v-if="myTemplates.length">
                <option v-for="t in myTemplates" :key="t.name" :value="t.name">{{ t.name }} ({{ t.model_ids.length }})</option>
              </optgroup>
            </select>
            <button class="btn btn-outline btn-xs" v-if="isMyTemplateSelected" @click="deleteTemplate" title="删除当前选中的我的模板">✕</button>
            <span class="template-spacer"></span>
            <button class="btn btn-outline btn-xs" @click="saveTemplate" title="把当前已选存为模板（本浏览器）">💾存为模板</button>
          </div>

          <div class="transfer">
            <!-- 左：模型池 -->
            <div class="transfer-pane">
              <input class="transfer-search" v-model="searchQuery" placeholder="搜索服务商 / 模型名..." />
              <div class="transfer-list">
                <div class="transfer-group" v-for="g in groupedPool" :key="g.id">
                  <div class="transfer-group-head">
                    <span class="tri-box" :class="'tri-' + groupSelState(g)"
                          role="checkbox"
                          :aria-checked="groupSelState(g) === 'all' ? 'true' : groupSelState(g) === 'partial' ? 'mixed' : 'false'"
                          :title="groupSelState(g) === 'all' ? '已全选（点击取消该组）' : groupSelState(g) === 'partial' ? '部分选中（点击全选该组）' : '未选中（点击全选该组）'"
                          @click.stop="toggleGroup(g)">
                      <span class="tri-mark" v-if="groupSelState(g) === 'all'">✓</span>
                      <span class="tri-mark" v-else-if="groupSelState(g) === 'partial'">–</span>
                    </span>
                    <span class="g-toggle" @click="toggleCollapse(g)">
                      <span class="g-caret">{{ isGroupCollapsed(g) ? '▶' : '▼' }}</span>
                      <span class="g-name">{{ g.name }}</span>
                    </span>
                    <span class="g-count" @click="toggleCollapse(g)">{{ g.models.length }}</span>
                  </div>
                  <div class="transfer-group-body" v-show="!isGroupCollapsed(g)">
                    <div class="transfer-model" v-for="m in g.models" :key="m.id"
                         :class="{ 'is-sel': isSel(g.name, m.model_id) }"
                         @click="toggle(g.name, m.model_id)">
                      <span class="transfer-model-name">{{ m.display_name || m.model_id }}</span>
                      <span class="transfer-model-meta" v-if="m.is_free || m.input_price || m.avg_latency_ms">
                        {{ m.is_free ? '免费' : '¥' + fmtPrice(m.input_price) }}<template v-if="m.avg_latency_ms"> · {{ Math.round(m.avg_latency_ms) }}ms</template>
                      </span>
                      <span class="transfer-check" v-if="isSel(g.name, m.model_id)">✓</span>
                    </div>
                  </div>
                </div>
                <div v-if="groupedPool.length === 0" class="transfer-empty">无匹配模型</div>
              </div>
            </div>

            <!-- 右：已选候选 -->
            <div class="transfer-pane">
              <div class="transfer-head">
                <span class="transfer-sel-count">已选 · {{ form.model_ids.length }}</span>
                <div class="transfer-actions">
                  <button class="btn btn-outline btn-xs" @click="sortBy('name')" :disabled="form.model_ids.length < 2">按名称</button>
                  <button class="btn btn-outline btn-xs" @click="sortBy('price')" :disabled="form.model_ids.length < 2">按价格</button>
                  <button class="btn btn-outline btn-xs" @click="sortBy('latency')" :disabled="form.model_ids.length < 2">按延迟</button>
                  <button class="btn btn-outline btn-xs" @click="clearSel" :disabled="!form.model_ids.length">清空</button>
                </div>
              </div>
              <div class="transfer-list transfer-selected">
                <div class="sel-item" v-for="(m, i) in form.model_ids" :key="i"
                     draggable="true"
                     @dragstart="onDragStart(i, $event)"
                     @dragenter.prevent="onDragOver(i, $event)"
                     @dragover.prevent="onDragOver(i, $event)"
                     @drop="onDrop(i, $event)"
                     @dragend="onDragEnd"
                     :class="{ 'dragging': dragIndex === i, 'drag-over': dragOverIndex === i }">
                  <span class="sel-idx">{{ i + 1 }}</span>
                  <span class="sel-name">{{ m.provider }}/{{ m.model_id }}</span>
                  <span class="sel-meta" v-if="selMeta(m)">{{ selMeta(m) }}</span>
                  <span class="sel-spacer"></span>
                  <button class="btn btn-outline btn-xs" @click="moveUp(i)" :disabled="i === 0" title="上移">↑</button>
                  <button class="btn btn-outline btn-xs" @click="moveDown(i)" :disabled="i === form.model_ids.length - 1" title="下移">↓</button>
                  <button class="btn btn-danger btn-xs" @click="removeAt(i)" title="移除">×</button>
                </div>
                <div v-if="form.model_ids.length === 0" class="transfer-empty">从左侧点选模型加入 →</div>
              </div>
            </div>
          </div>
        </div>

        <div class="modal-actions">
          <button class="btn btn-outline" @click="showModal = false">取消</button>
          <button class="btn btn-primary" @click="saveCombo" :disabled="saving">
            {{ saving ? '保存中...' : '保存' }}
          </button>
        </div>
      </div>
    </div>
  </div>
</template>

<script>
import api from '../api.js'
export default {
  name: 'CombosView',
  data() {
    return {
      combos: [],
      providers: [],
      models: [],   // 所有模型扁平列表，前端二次过滤
      showModal: false,
      isEditing: false,
      editingId: null,
      saving: false,
      searchQuery: '',
      collapsedGroups: {},   // provider_id → true 表示收起（默认全部收起）
      dragIndex: null,
      dragOverIndex: null,
      myTemplates: [],
      templatePick: '',
      form: {
        name: '',
        description: '',
        strategy: 'fallback',
        model_ids: []
      }
    }
  },
  async mounted() {
    this.loadMyTemplates()
    await this.loadAll()
  },
  computed: {
    // 按 provider 分组 + 搜索过滤后的模型池
    groupedPool() {
      const q = (this.searchQuery || '').trim().toLowerCase()
      const idToName = {}
      this.providers.forEach(p => { idToName[p.id] = p.name })
      const groups = []
      for (const p of this.providers) {
        const ms = this.models.filter(m =>
          m.provider_id === p.id &&
          m.enabled !== false &&
          (!q ||
            (m.model_id || '').toLowerCase().includes(q) ||
            (m.display_name || '').toLowerCase().includes(q) ||
            p.name.toLowerCase().includes(q))
        )
        if (ms.length) groups.push({ id: p.id, name: p.name, models: ms })
      }
      return groups
    },
    // provider 名 + model_id → 模型对象（用于价格/延迟展示与排序）
    selLookup() {
      const idToName = {}
      this.providers.forEach(p => { idToName[p.id] = p.name })
      const map = {}
      this.models.forEach(m => {
        const pn = idToName[m.provider_id]
        if (pn) map[`${pn}::${m.model_id}`] = m
      })
      return map
    },
    selectedSet() {
      return new Set(this.form.model_ids.map(m => `${m.provider}::${m.model_id}`))
    },
    isMyTemplateSelected() {
      return !!this.templatePick && !this.templatePick.startsWith('__preset:')
    }
  },
  methods: {
    async loadAll() {
      try {
        const [combosRes, providers, models] = await Promise.all([
          api.getCombos(),
          api.getProviders(),
          api.getModels()
        ])
        this.combos = combosRes.items || []
        this.providers = providers || []
        this.models = models || []
      } catch (e) {
        alert('加载失败: ' + e.message)
      }
    },
    loadMyTemplates() {
      try {
        const raw = localStorage.getItem('aigate_combo_templates')
        this.myTemplates = raw ? JSON.parse(raw) : []
        if (!Array.isArray(this.myTemplates)) this.myTemplates = []
      } catch (_) {
        this.myTemplates = []
      }
    },
    // 一键填充：根据模型字段生成预设候选
    applyPreset(kind) {
      const idToName = {}
      this.providers.forEach(p => { idToName[p.id] = p.name })
      const toEntry = (m) => {
        const pn = idToName[m.provider_id]
        return pn ? { provider: pn, model_id: m.model_id } : null
      }
      let list = []
      if (kind === 'free') {
        list = this.models.filter(m => {
          if (m.enabled === false) return false
          if (m.is_free === true) return true
          return Number(m.input_price || 0) === 0 && Number(m.output_price || 0) === 0
        }).map(toEntry)
      } else if (kind === 'cheap') {
        list = this.models
          .filter(m => {
            if (m.enabled === false) return false
            if (m.is_free === true) return false
            return Number(m.input_price || m.output_price || 0) > 0
          })
          .sort((a, b) => (Number(a.input_price || a.output_price || 0)) - (Number(b.input_price || b.output_price || 0)))
          .slice(0, 12)
          .map(toEntry)
      } else if (kind === 'domestic') {
        const kw = ['国', '通义', '千问', 'qwen', '智谱', 'glm', 'chatglm', 'deepseek', '豆包',
          'doubao', 'kimi', 'moonshot', '百川', 'baichuan', '文心', 'ernie', '混元',
          'hunyuan', '讯飞', '星火', 'spark', 'minimax', '阶跃', 'step-', 'abab', 'yi-', '零一', '天工', 'skywork',
          'baidu', 'aliyun', 'alibaba', 'tencent', 'bytedance', 'zhipu', 'sensenova',
          'internlm', '书生', 'kling', '可灵']
        list = this.models.filter(m => {
          if (m.enabled === false) return false
          const pn = (idToName[m.provider_id] || '').toLowerCase()
          const s = ((m.model_id || '') + ' ' + (m.display_name || '') + ' ' + pn).toLowerCase()
          return kw.some(k => s.includes(k.toLowerCase()))
        }).map(toEntry)
      } else if (kind === 'claude') {
        list = this.models.filter(m => {
          if (m.enabled === false) return false
          const pn = (idToName[m.provider_id] || '').toLowerCase()
          const s = ((m.model_id || '') + ' ' + (m.display_name || '') + ' ' + pn).toLowerCase()
          return s.includes('claude')
        }).map(toEntry)
      } else if (kind === 'gpt') {
        const kw = ['gpt', 'openai', 'chatgpt', 'o1', 'o3', 'o4', 'o1-', 'o3-', 'o4-', 'gpt-4', 'gpt-3', 'gpt-4o', 'gpt-5']
        list = this.models.filter(m => {
          if (m.enabled === false) return false
          const pn = (idToName[m.provider_id] || '').toLowerCase()
          const s = ((m.model_id || '') + ' ' + (m.display_name || '') + ' ' + pn).toLowerCase()
          return kw.some(k => s.includes(k))
        }).map(toEntry)
      }
      list = list.filter(Boolean)
      if (!list.length) { alert('没有匹配到可用模型'); return }
      this.form.model_ids = list
    },
    saveTemplate() {
      const cleaned = this.form.model_ids.filter(m => m.provider && m.model_id)
      if (!cleaned.length) { alert('请先选择至少一个模型再存为模板'); return }
      const name = window.prompt('模板名称：')
      if (!name) return
      const trimmed = name.trim()
      if (!trimmed) return
      const arr = this.myTemplates.filter(t => t.name !== trimmed)
      arr.push({ name: trimmed, model_ids: cleaned })
      try {
        localStorage.setItem('aigate_combo_templates', JSON.stringify(arr))
        this.myTemplates = arr
        alert('已保存模板：' + trimmed)
      } catch (e) {
        alert('保存失败（浏览器存储不可用）：' + e.message)
      }
    },
    onTemplatePick() {
      if (!this.templatePick) return
      if (this.templatePick.startsWith('__preset:')) {
        const kind = this.templatePick.slice('__preset:'.length)
        this.applyPreset(kind)
      } else {
        const t = this.myTemplates.find(x => x.name === this.templatePick)
        if (t) {
          this.form.model_ids = (t.model_ids || []).map(m => ({ provider: m.provider, model_id: m.model_id }))
        }
      }
      this.templatePick = ''
    },
    deleteTemplate() {
      if (!this.templatePick) return
      if (!confirm('删除模板 "' + this.templatePick + '"？')) return
      const arr = this.myTemplates.filter(t => t.name !== this.templatePick)
      try {
        localStorage.setItem('aigate_combo_templates', JSON.stringify(arr))
        this.myTemplates = arr
      } catch (_) {}
      this.templatePick = ''
    },
    stratLabel(s) {
      return { fallback: '顺序兜底', round_robin: '轮询', fusion: '扇出合并' }[s] || s
    },
    isSel(provider, model_id) {
      return this.selectedSet.has(`${provider}::${model_id}`)
    },
    // 分组是否收起：默认收起；搜索时自动全部展开，便于看到过滤结果
    isGroupCollapsed(g) {
      if (this.searchQuery) return false
      return this.collapsedGroups[g.id] !== false
    },
    toggleCollapse(g) {
      const next = !this.isGroupCollapsed(g)
      this.collapsedGroups = { ...this.collapsedGroups, [g.id]: next }
    },
    toggle(provider, model_id) {
      const key = `${provider}::${model_id}`
      const idx = this.form.model_ids.findIndex(m => `${m.provider}::${m.model_id}` === key)
      if (idx >= 0) this.form.model_ids.splice(idx, 1)
      else this.form.model_ids.push({ provider, model_id })
    },
    // 分组选择三态：none 一个都没选 / partial 选了部分 / all 全选
    groupSelState(g) {
      if (!g.models.length) return 'none'
      let n = 0
      for (const m of g.models) if (this.isSel(g.name, m.model_id)) n++
      if (n === 0) return 'none'
      if (n === g.models.length) return 'all'
      return 'partial'
    },
    toggleGroup(g) {
      if (this.groupSelState(g) === 'all') {
        // 取消该组全部（仅移除当前过滤出的）
        const keys = new Set(g.models.map(m => `${g.name}::${m.model_id}`))
        this.form.model_ids = this.form.model_ids.filter(m => !keys.has(`${m.provider}::${m.model_id}`))
      } else {
        // 加入该组缺失的（partial / none 都补成全选）
        g.models.forEach(m => {
          if (!this.isSel(g.name, m.model_id)) this.form.model_ids.push({ provider: g.name, model_id: m.model_id })
        })
      }
    },
    selMeta(m) {
      const mo = this.selLookup[`${m.provider}::${m.model_id}`]
      if (!mo) return ''
      const parts = []
      if (mo.is_free) parts.push('免费')
      else if (mo.input_price != null) parts.push('¥' + this.fmtPrice(mo.input_price))
      if (mo.avg_latency_ms != null) parts.push(Math.round(mo.avg_latency_ms) + 'ms')
      return parts.join(' · ')
    },
    fmtPrice(p) {
      const n = Number(p || 0)
      return n === 0 ? '0' : n.toFixed(2)
    },
    sortBy(field) {
      const arr = this.form.model_ids.slice()
      arr.sort((a, b) => {
        if (field === 'name') {
          const ka = `${a.provider}::${a.model_id}`.toLowerCase()
          const kb = `${b.provider}::${b.model_id}`.toLowerCase()
          return ka.localeCompare(kb, 'zh')
        }
        const ma = this.selLookup[`${a.provider}::${a.model_id}`]
        const mb = this.selLookup[`${b.provider}::${b.model_id}`]
        const va = ma && ma[field] != null ? ma[field] : Infinity
        const vb = mb && mb[field] != null ? mb[field] : Infinity
        return va - vb
      })
      this.form.model_ids = arr
    },
    clearSel() {
      this.form.model_ids = []
    },
    removeAt(i) {
      this.form.model_ids.splice(i, 1)
    },
    moveUp(i) {
      if (i === 0) return
      const arr = this.form.model_ids
      const t = arr[i - 1]; arr[i - 1] = arr[i]; arr[i] = t
    },
    moveDown(i) {
      const arr = this.form.model_ids
      if (i >= arr.length - 1) return
      const t = arr[i + 1]; arr[i + 1] = arr[i]; arr[i] = t
    },
    // ── 拖拽排序 ──
    onDragStart(i, e) {
      this.dragIndex = i
      if (e.dataTransfer) {
        e.dataTransfer.effectAllowed = 'move'
        try { e.dataTransfer.setData('text/plain', String(i)) } catch (_) {}
      }
    },
    onDragOver(i, e) {
      if (this.dragIndex === null || this.dragIndex === i) return
      this.dragOverIndex = i
    },
    onDrop(i, e) {
      if (e && e.preventDefault) e.preventDefault()
      const from = this.dragIndex
      const to = i
      if (from === null || to === null || from === to) { this.resetDrag(); return }
      const arr = this.form.model_ids
      const moved = arr.splice(from, 1)[0]
      arr.splice(to, 0, moved)
      this.resetDrag()
    },
    onDragEnd() {
      this.resetDrag()
    },
    resetDrag() {
      this.dragIndex = null
      this.dragOverIndex = null
    },
    openAddModal() {
      this.isEditing = false
      this.editingId = null
      this.searchQuery = ''
      this.form = { name: '', description: '', strategy: 'fallback', model_ids: [] }
      this.showModal = true
    },
    editCombo(c) {
      this.isEditing = true
      this.editingId = c.id
      this.searchQuery = ''
      this.form = {
        name: c.name,
        description: c.description || '',
        strategy: c.strategy,
        model_ids: (c.model_ids || []).map(m => ({
          provider: m.provider || (m.full_id ? m.full_id.split('/')[0] : ''),
          model_id: m.model_id || (m.full_id ? m.full_id.split('/')[1] : '')
        }))
      }
      this.showModal = true
    },
    async saveCombo() {
      if (!this.form.name.trim()) { alert('请填组合名'); return }
      const cleaned = this.form.model_ids.filter(m => m.provider && m.model_id)
      if (cleaned.length === 0) { alert('至少添加一个候选模型'); return }
      this.saving = true
      try {
        const payload = {
          name: this.form.name.trim(),
          description: this.form.description.trim(),
          strategy: this.form.strategy,
          model_ids: cleaned,
          priority: 0,
          enabled: true
        }
        if (this.isEditing) {
          await api.updateCombo(this.editingId, payload)
        } else {
          await api.createCombo(payload)
        }
        this.showModal = false
        await this.loadAll()
      } catch (e) {
        alert('保存失败: ' + e.message)
      } finally {
        this.saving = false
      }
    },
    async deleteCombo(c) {
      if (!confirm(`确定删除组合 "combo:${c.name}"？`)) return
      try {
        await api.deleteCombo(c.id)
        await this.loadAll()
      } catch (e) {
        alert('删除失败: ' + e.message)
      }
    }
  }
}
</script>
<style scoped>
.combo-card {
  background: var(--bg-card);
  border: 1px solid var(--border-soft);
  border-radius: 8px;
  padding: 16px 18px;
  margin-bottom: 14px;
}
.combo-header {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-bottom: 8px;
}
.combo-name {
  font-weight: 600;
  color: var(--text-primary);
  font-family: ui-monospace, monospace;
  font-size: 14px;
}
.combo-count {
  font-size: 12px;
  color: var(--text-muted);
  cursor: help;
}
.combo-body {
  font-size: 13px;
  color: var(--text-secondary);
  margin-bottom: 10px;
}
.combo-tags {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}
.combo-tag {
  font-size: 12px;
  font-family: ui-monospace, monospace;
  padding: 2px 8px;
  border-radius: 4px;
  background: var(--bg-code);
  border: 1px solid var(--border-soft);
}
.cred-badge {
  display: inline-block;
  font-size: 11px;
  font-weight: 500;
  padding: 1px 8px;
  border-radius: 10px;
  border: 1px solid transparent;
}
.cred-badge.strat-fallback {
  background: var(--alert-info-bg);
  color: var(--alert-info-fg);
  border-color: var(--alert-info-border);
}
.cred-badge.strat-round_robin {
  background: var(--badge-free-bg);
  color: var(--badge-free-fg);
  border-color: var(--badge-free-fg);
}
.cred-badge.strat-fusion {
  background: var(--badge-paid-bg);
  color: var(--badge-paid-fg);
  border-color: var(--badge-paid-fg);
}
.modal-overlay { position: fixed; inset: 0; background: rgba(0,0,0,0.5);
  display: flex; align-items: center; justify-content: center; z-index: 1000; }
.modal-content { background: var(--bg-elevated); color: var(--text-primary);
  border: 1px solid var(--border-soft); border-radius: 12px; padding: 24px;
  width: 500px; max-width: 92vw; max-height: 92vh; overflow-y: auto; }
.modal-actions { display: flex; justify-content: flex-end; gap: 12px; margin-top: 24px; }

/* ── 双栏穿梭 ── */
.transfer { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }
.transfer-pane {
  border: 1px solid var(--border-soft);
  border-radius: 8px;
  background: var(--bg-input);
  display: flex; flex-direction: column;
  min-height: 380px;
}
.transfer-search {
  margin: 8px; padding: 7px 10px;
  border: 1px solid var(--border-medium); border-radius: 6px;
  background: var(--bg-input); color: var(--text-primary);
  font-size: 13px;
}
.transfer-search::placeholder { color: var(--text-muted); }
.transfer-list { flex: 1; overflow-y: auto; padding: 0 8px 8px; }
.transfer-group { margin-bottom: 2px; border-bottom: 1px solid var(--border-soft); }
.transfer-group-head {
  display: flex; align-items: center; gap: 6px;
  padding: 5px 4px; position: sticky; top: 0;
  background: var(--bg-input); cursor: pointer;
}
.g-toggle { display: flex; align-items: center; gap: 6px; flex: 1; min-width: 0; }
.g-name {
  font-size: 13px; font-weight: 500; color: var(--text-primary);
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
}
.g-caret { font-size: 10px; color: var(--text-muted); flex-shrink: 0; }
.transfer-group-count, .g-count { font-size: 11px; color: var(--text-muted); flex-shrink: 0; cursor: pointer; }
/* 分组“全选”三态按钮（固定在最左列，所有服务商对齐） */
.tri-box {
  flex-shrink: 0;
  width: 16px; height: 16px;
  display: inline-flex; align-items: center; justify-content: center;
  border: 1.5px solid var(--border, #ccc);
  border-radius: 4px;
  background: var(--surface, #fff);
  color: var(--text-primary);
  font-size: 12px; font-weight: 700; line-height: 1;
  cursor: pointer; user-select: none;
  transition: background .12s, border-color .12s, color .12s;
}
.tri-box:hover { border-color: var(--accent, #4f8cff); }
.tri-box.tri-none { background: var(--surface, #fff); border-color: var(--border, #ccc); color: transparent; }
.tri-box.tri-partial { background: var(--accent-soft, rgba(79,140,255,0.18)); border-color: var(--accent, #4f8cff); color: var(--accent, #4f8cff); }
.tri-box.tri-all { background: var(--accent, #4f8cff); border-color: var(--accent, #4f8cff); color: #fff; }
.tri-mark { display: block; transform: translateY(-0.5px); }
.transfer-group-body { margin: 2px 0 4px; }
.transfer-model {
  display: flex; align-items: center; gap: 6px;
  padding: 5px 8px; border-radius: 6px; cursor: pointer;
  font-size: 13px; color: var(--text-secondary);
}
.transfer-model:hover { background: var(--bg-code); color: var(--text-primary); }
.transfer-model.is-sel { background: var(--accent-soft, rgba(79,140,255,0.12)); color: var(--text-primary); }
.transfer-model-name { flex: 1; font-family: ui-monospace, monospace; }
.transfer-model-meta { font-size: 11px; color: var(--text-muted); }
.transfer-check { color: var(--accent, #4f8cff); font-weight: 600; }
.transfer-empty { text-align: center; color: var(--text-muted); font-size: 13px; padding: 24px 0; }

.transfer-head {
  display: flex; align-items: center; justify-content: space-between;
  padding: 8px; border-bottom: 1px solid var(--border-soft);
}
.transfer-sel-count { font-size: 13px; font-weight: 500; color: var(--text-primary); }
.transfer-actions { display: flex; gap: 6px; }
.template-bar {
  display: flex; flex-wrap: nowrap; align-items: center; gap: 4px;
  margin-bottom: 10px; overflow-x: auto;
}
.template-label { font-size: 14px; color: var(--text-secondary); flex-shrink: 0; }
.template-spacer { flex: 1; }
.template-sep { width: 1px; height: 16px; background: var(--border-soft); margin: 0 2px; flex-shrink: 0; }
.template-select {
  padding: 5px 8px; font-size: 14px; max-width: 220px;
  border: 1px solid var(--border-medium); border-radius: 6px;
  background: var(--bg-input); color: var(--text-primary); flex-shrink: 0;
}
.btn-xs { padding: 5px 10px; font-size: 14px; }
.transfer-selected { padding-top: 8px; }
.sel-item {
  display: flex; align-items: center; gap: 6px;
  padding: 5px 8px; border-radius: 6px; margin-bottom: 4px;
  background: var(--bg-code); border: 1px solid var(--border-soft);
  font-size: 13px; cursor: grab;
}
.sel-item.dragging { opacity: 0.4; }
.sel-item.drag-over { box-shadow: inset 0 2px 0 0 var(--accent, #4f8cff); }
.sel-item:active { cursor: grabbing; }
.sel-idx { width: 16px; text-align: center; font-size: 11px; color: var(--text-muted); flex-shrink: 0; }
.sel-name { font-family: ui-monospace, monospace; color: var(--text-primary); }
.sel-meta { font-size: 11px; color: var(--text-muted); }
.sel-spacer { flex: 1; }
</style>
