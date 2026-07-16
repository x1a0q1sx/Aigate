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
      <div class="modal-content" style="width: 640px;">
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
          <label>候选模型（拖动可调整顺序）</label>
          <p style="font-size: 12px; color: var(--text-muted); margin: 4px 0 12px;">
            渠道内的模型顺序就是 fallback / 轮询顺序。可拖拽左侧 ⠿ 手柄调整顺序，或用 ↑↓ 按钮。
          </p>

          <div class="combo-editor">
            <div class="combo-item" v-for="(m, i) in form.model_ids" :key="i"
                 draggable="true"
                 @dragstart="onDragStart(i, $event)"
                 @dragenter.prevent="onDragOver(i, $event)"
                 @dragover.prevent="onDragOver(i, $event)"
                 @drop="onDrop(i, $event)"
                 @dragend="onDragEnd"
                 :class="{ 'dragging': dragIndex === i, 'drag-over': dragOverIndex === i }">
              <span class="combo-item-idx drag-handle" title="按住拖拽排序">⠿ {{ i + 1 }}</span>
              <select v-model="m.provider" @change="onProviderChange(m)" style="flex: 1;">
                <option value="">-- 选择服务商 --</option>
                <option v-for="p in providers" :key="p.id" :value="p.name">{{ p.name }}</option>
              </select>
              <select v-model="m.model_id" style="flex: 1;">
                <option value="">-- 选择模型 --</option>
                <option v-for="mo in targetModels(m.provider)" :key="mo.id" :value="mo.model_id">{{ mo.model_id }}</option>
              </select>
              <button class="btn btn-outline btn-sm" @click="moveUp(i)" :disabled="i === 0" title="上移">↑</button>
              <button class="btn btn-outline btn-sm" @click="moveDown(i)" :disabled="i === form.model_ids.length - 1" title="下移">↓</button>
              <button class="btn btn-danger btn-sm" @click="removeItem(i)" title="移除">✕</button>
            </div>
            <button class="btn btn-outline btn-sm" @click="addItem" style="margin-top: 8px;">+ 添加候选</button>
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
      dragIndex: null,
      dragOverIndex: null,
      form: {
        name: '',
        description: '',
        strategy: 'fallback',
        model_ids: []
      }
    }
  },
  async mounted() {
    await this.loadAll()
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
    stratLabel(s) {
      return { fallback: '顺序兜底', round_robin: '轮询', fusion: '扇出合并' }[s] || s
    },
    targetModels(providerName) {
      const p = this.providers.find(x => x.name === providerName)
      if (!p) return []
      return this.models.filter(m => m.provider_id === p.id && m.enabled)
    },
    onProviderChange(m) {
      m.model_id = ''  // 切provider清空model
    },
    addItem() {
      this.form.model_ids.push({ provider: '', model_id: '' })
    },
    removeItem(i) {
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
        // 兼容 Firefox：必须设置数据才会触发拖拽
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
      this.form = { name: '', description: '', strategy: 'fallback', model_ids: [{ provider: '', model_id: '' }] }
      this.showModal = true
    },
    editCombo(c) {
      this.isEditing = true
      this.editingId = c.id
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
  width: 500px; max-width: 90vw; max-height: 90vh; overflow-y: auto; }
.modal-actions { display: flex; justify-content: flex-end; gap: 12px; margin-top: 24px; }
.combo-editor { border: 1px solid var(--border-soft); border-radius: 8px; padding: 10px; background: var(--bg-input); }
.combo-item { display: flex; gap: 6px; align-items: center; margin-bottom: 6px; }
.combo-item.dragging { opacity: 0.4; }
.combo-item.drag-over { box-shadow: inset 0 2px 0 0 var(--accent, #4f8cff); }
.combo-item-idx { width: 18px; text-align: center; font-size: 12px; color: var(--text-muted); flex-shrink: 0; }
.drag-handle { cursor: grab; user-select: none; }
.drag-handle:active { cursor: grabbing; }
.combo-item select { padding: 6px 8px; border: 1px solid var(--border-medium);
  background: var(--bg-input); color: var(--text-primary); border-radius: 4px; }
</style>
