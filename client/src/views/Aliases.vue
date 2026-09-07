<template>
  <div class="alias-page">
    <div class="page-header">
      <div>
        <h1>模型别名</h1>
        <p>客户端请求别名 → 自动改写为真实目标（provider/model 或 combo:xxx），公益站命名五花八门也不怕。</p>
      </div>
      <div class="header-actions">
        <button class="btn btn-outline" @click="load" :disabled="loading">刷新</button>
        <button class="btn btn-primary" @click="openCreate">新建别名</button>
      </div>
    </div>

    <div class="hint-card">
      <p><strong>示例</strong>：<code>deepseek</code> → <code>魔塔AI/deepseek-ai/DeepSeek-V4-Pro</code>；<code>my-claude</code> → <code>combo:claude-fast</code>。别名命中后，思考强度后缀（如 <code>-high</code>）照常可用。别名也会出现在 <code>/v1/models</code> 清单里。</p>
    </div>

    <div v-if="showEdit" class="modal-mask" @click.self="showEdit = false">
      <div class="modal">
        <h3>{{ editing.id ? '编辑别名' : '新建别名' }}</h3>
        <div class="form-grid">
          <label>别名（客户端请求的模型名）<input v-model="editing.alias" placeholder="deepseek" /></label>
          <label>目标（真实路由目标）<input v-model="editing.target" placeholder="服务商/模型 或 combo:名称" /></label>
          <label>备注<input v-model="editing.note" placeholder="可选" /></label>
          <label class="check-line"><input type="checkbox" v-model="editing.enabled" /> 启用</label>
        </div>
        <p v-if="editError" class="err-text">{{ editError }}</p>
        <div class="modal-actions">
          <button class="btn btn-outline" @click="showEdit = false">取消</button>
          <button class="btn btn-primary" @click="save" :disabled="saving">{{ saving ? '保存中...' : '保存' }}</button>
        </div>
      </div>
    </div>

    <div class="table-card">
      <table v-if="aliases.length">
        <thead><tr><th>别名</th><th>目标</th><th>备注</th><th>状态</th><th>操作</th></tr></thead>
        <tbody>
          <tr v-for="a in aliases" :key="a.id">
            <td><code class="alias-name">{{ a.alias }}</code></td>
            <td><code>{{ a.target }}</code></td>
            <td class="dim">{{ a.note || '—' }}</td>
            <td><span class="pill" :class="a.enabled ? 'ok' : 'muted'">{{ a.enabled ? '启用' : '停用' }}</span></td>
            <td class="action-cell">
              <button class="btn btn-outline btn-sm" @click="openEdit(a)">编辑</button>
              <button class="btn btn-danger btn-sm" @click="remove(a)">删除</button>
            </td>
          </tr>
        </tbody>
      </table>
      <div v-else class="empty">暂无别名。别名在请求入口按精确名匹配，命中即改写。</div>
    </div>
  </div>
</template>

<script>
import api from '../api'
import toast from '../toast'

export default {
  name: 'Aliases',
  data() {
    return {
      aliases: [], loading: false, showEdit: false, saving: false, editError: '',
      editing: { id: null, alias: '', target: '', note: '', enabled: true },
    }
  },
  mounted() { this.load() },
  methods: {
    async load() {
      this.loading = true
      try {
        const r = await api.getAliases()
        this.aliases = r.aliases || []
      } catch (e) { toast.error('加载失败: ' + e.message) }
      finally { this.loading = false }
    },
    openCreate() {
      this.editing = { id: null, alias: '', target: '', note: '', enabled: true }
      this.editError = ''
      this.showEdit = true
    },
    openEdit(a) {
      this.editing = { ...a }
      this.editError = ''
      this.showEdit = true
    },
    async save() {
      if (!this.editing.alias.trim() || !this.editing.target.trim()) {
        this.editError = '别名和目标都不能为空'
        return
      }
      this.saving = true
      this.editError = ''
      try {
        if (this.editing.id) {
          await api.updateAlias(this.editing.id, {
            alias: this.editing.alias, target: this.editing.target,
            note: this.editing.note, enabled: this.editing.enabled,
          })
        } else {
          await api.createAlias({
            alias: this.editing.alias, target: this.editing.target,
            note: this.editing.note, enabled: this.editing.enabled,
          })
        }
        this.showEdit = false
        this.load()
      } catch (e) { this.editError = e.message }
      finally { this.saving = false }
    },
    async remove(a) {
      if (!confirm(`删除别名「${a.alias}」？正在使用它的客户端请求将恢复原名解析。`)) return
      try { await api.deleteAlias(a.id); this.load() }
      catch (e) { toast.error('删除失败: ' + e.message) }
    },
  },
}
</script>

<style scoped>
.alias-page { display: flex; flex-direction: column; gap: var(--space-4); }
.page-header { display: flex; justify-content: space-between; align-items: flex-start; gap: var(--space-3); }
.page-header h1 { margin: 0 0 4px; font-size: var(--text-2xl, 1.5rem); }
.page-header p { margin: 0; color: var(--text-muted); font-size: var(--text-sm); }
.header-actions { display: flex; gap: var(--space-2); }
.hint-card { background: var(--surface-2); border: 1px solid var(--border-base); border-radius: var(--radius-md); padding: var(--space-3) var(--space-4); font-size: var(--text-sm); color: var(--text-muted); }
.hint-card code { background: var(--surface-1); padding: 1px 6px; border-radius: 4px; }
.table-card { background: var(--surface-2); border: 1px solid var(--border-base); border-radius: var(--radius-lg); overflow-x: auto; }
table { width: 100%; border-collapse: collapse; font-size: var(--text-sm); }
th, td { padding: 10px 14px; text-align: left; border-bottom: 1px solid var(--border-base); white-space: nowrap; }
th { color: var(--text-muted); font-weight: 500; font-size: var(--text-xs); }
.alias-name { font-weight: 600; }
code { font-family: var(--font-mono, monospace); font-size: var(--text-xs); }
.action-cell { display: flex; gap: 6px; }
.pill { padding: 2px 8px; border-radius: 999px; font-size: var(--text-xs); }
.pill.ok { background: rgba(34, 197, 94, .15); color: #22c55e; }
.pill.muted { background: rgba(148, 163, 184, .15); color: #94a3b8; }
.empty { padding: var(--space-6); text-align: center; color: var(--text-dim); }
.dim { color: var(--text-dim); }
.modal-mask { position: fixed; inset: 0; background: rgba(0, 0, 0, .45); display: flex; align-items: center; justify-content: center; z-index: 100; }
.modal { background: var(--surface-2); border: 1px solid var(--border-base); border-radius: var(--radius-lg); padding: var(--space-5); width: min(520px, 92vw); }
.modal h3 { margin: 0 0 var(--space-4); }
.form-grid { display: flex; flex-direction: column; gap: var(--space-3); }
.form-grid label { display: flex; flex-direction: column; gap: 4px; font-size: var(--text-sm); color: var(--text-muted); }
.form-grid input { background: var(--surface-1); border: 1px solid var(--border-base); border-radius: 6px; padding: 8px 10px; color: var(--text-primary); font-size: var(--text-sm); }
.check-line { flex-direction: row !important; align-items: center; gap: 8px !important; }
.err-text { color: #ef4444; font-size: var(--text-sm); }
.modal-actions { display: flex; justify-content: flex-end; gap: var(--space-2); margin-top: var(--space-4); }
.btn { cursor: pointer; border: 1px solid transparent; border-radius: 6px; padding: 7px 14px; font-size: var(--text-sm); }
.btn-primary { background: var(--primary, #2f5fe0); color: #fff; }
.btn-outline { background: transparent; border-color: var(--border-base); color: var(--text-primary); }
.btn-danger { background: rgba(239, 68, 68, .12); color: #ef4444; }
.btn-sm { padding: 4px 10px; font-size: var(--text-xs); }
.btn:disabled { opacity: .5; cursor: not-allowed; }
</style>
