<template>
  <div>
    <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 20px;">
      <h1>服务商管理 🔌</h1>
      <button class="btn btn-primary" @click="openAddModal">+ 添加服务商</button>
    </div>
    <div class="card">
      <table>
        <thead>
          <tr>
            <th>ID</th>
            <th>名称</th>
            <th>Base URL</th>
            <th>API 类型</th>
            <th>密钥</th>
            <th>描述</th>
            <th>操作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="p in providers" :key="p.id">
            <td>{{ p.id }}</td>
            <td><strong>{{ p.name }}</strong></td>
            <td style="font-family: mono; font-size: 12px;">{{ p.base_url }}</td>
            <td><code>{{ p.api_type }}</code></td>
            <td>
              <span v-if="keyCount(p.id) > 0" style="color: var(--green);">{{ keyCount(p.id) }} 把 🔑</span>
              <span v-else style="color: var(--gray-400);">—</span>
            </td>
            <td>{{ p.description }}</td>
            <td>
              <button class="btn btn-outline btn-sm" @click="editProvider(p)">编辑</button>
              <button class="btn btn-danger btn-sm" @click="deleteProvider(p)">删除</button>
            </td>
          </tr>
        </tbody>
      </table>
    </div>

    <!-- 添加/编辑 Modal -->
    <div v-if="showModal" class="modal-overlay" @click.self="showModal = false">
      <div class="modal-content" style="width: 580px;">
        <h3>{{ isEditing ? '编辑服务商' : '添加服务商' }}</h3>

        <!-- Tab 切换 -->
        <div class="tab-bar">
          <button :class="['tab-btn', { active: activeTab === 'basic' }]" @click="activeTab = 'basic'">基本信息</button>
          <button :class="['tab-btn', { active: activeTab === 'keys' }]" @click="activeTab = 'keys'" :disabled="!editingId">
            密钥管理
            <span v-if="editingId && modalKeys.length > 0" class="tab-badge">{{ modalKeys.length }}</span>
          </button>
        </div>

        <!-- 基本信息 Tab -->
        <div v-if="activeTab === 'basic'">
          <div class="form-group">
            <label>名称</label>
            <input v-model="form.name" placeholder="例如 GitHub Models" />
          </div>
          <div class="form-group">
            <label>API Base URL</label>
            <input v-model="form.base_url" placeholder="https://models.inference.ai.azure.com" />
          </div>
          <div class="form-group">
            <label>API 类型</label>
            <select v-model="form.api_type">
              <option value="openai_compat">OpenAI 兼容（大多数服务商）</option>
              <option value="anthropic">Anthropic Claude</option>
              <option value="github">GitHub Models（免费模型多）</option>
            </select>
          </div>
          <div class="form-group">
            <label>描述</label>
            <textarea v-model="form.description" placeholder="可选描述" rows="2"></textarea>
          </div>
          <div class="modal-actions">
            <button v-if="isEditing" class="btn btn-outline" @click="openImportPricingFromEdit">📥 导入定价</button>
            <span style="flex:1"></span>
            <button class="btn btn-outline" @click="showModal = false">取消</button>
            <button class="btn btn-primary" @click="saveProvider" :disabled="saving">
              {{ saving ? '保存中...' : '保存' }}
            </button>
          </div>
        </div>

        <!-- 密钥管理 Tab -->
        <div v-if="activeTab === 'keys'">
          <p style="color: var(--gray-500); font-size: 13px; margin-bottom: 16px;">
            管理「{{ form.name }}」的 API 密钥
          </p>

          <!-- 现有密钥列表 -->
          <div v-if="modalKeys.length > 0" style="margin-bottom: 20px;">
            <div class="key-item" v-for="k in modalKeys" :key="k.id">
              <div class="key-info">
                <span class="key-label">{{ k.label || '未命名' }}</span>
                <code class="key-prefix">{{ k.key_prefix }}***</code>
                <span class="key-status" :class="{ inactive: !k.is_active }">{{ k.is_active ? '活跃' : '停用' }}</span>
              </div>
              <div class="key-actions">
                <button class="btn btn-outline btn-xs" @click="toggleReveal(k)" :disabled="revealingId === k.id">
                  {{ revealingId === k.id ? '...' : (revealedKeys[k.id] ? '隐藏' : '查看') }}
                </button>
                <button class="btn btn-outline btn-xs" style="color: var(--danger);" @click="deleteKey(k)">删除</button>
              </div>
              <div v-if="revealedKeys[k.id]" class="key-revealed">
                <code>{{ revealedKeys[k.id] }}</code>
                <button class="btn btn-xs copy-btn" @click="copyKey(k.id)">复制</button>
              </div>
            </div>
          </div>
          <p v-else style="text-align: center; padding: 16px; color: var(--gray-400); font-size: 13px;">暂未添加密钥</p>

          <!-- 添加密钥表单 -->
          <div class="add-key-form">
            <h4 style="margin: 0 0 12px 0; font-size: 14px;">添加新密钥</h4>
            <div class="form-group">
              <label>API Key</label>
              <input v-model="keyForm.key" placeholder="sk-..." :type="showKeyInput ? 'text' : 'password'" />
              <button class="btn btn-outline btn-xs" style="position: absolute; right: 8px; top: 32px;" @click="showKeyInput = !showKeyInput">
                {{ showKeyInput ? '👁' : '👁‍🗨' }}
              </button>
            </div>
            <div class="form-group">
              <label>标签（可选）</label>
              <input v-model="keyForm.label" placeholder="例如 prod、test" />
            </div>
            <button class="btn btn-primary btn-sm" @click="addKey" :disabled="keySaving">
              {{ keySaving ? '添加中...' : '添加密钥' }}
            </button>
          </div>

          <div class="modal-actions">
            <button class="btn btn-outline" @click="activeTab = 'basic'">← 返回基本信息</button>
            <span style="flex:1"></span>
            <button class="btn btn-outline" @click="showModal = false">关闭</button>
          </div>
        </div>
      </div>
    </div>

    <!-- 导入定价 Modal -->
    <div v-if="showImportModal" class="modal-overlay" @click.self="showImportModal = false">
      <div class="modal-content" style="max-width: 640px;">
        <h3>📥 导入定价 — {{ importTarget ? importTarget.name : '' }}</h3>
        <p style="font-size: 12px; color: var(--gray-500);">粘贴 newapi/one-api 的 /api/pricing 返回 JSON</p>
        <textarea v-model="importJson" rows="12" style="width: 100%; font-family: mono; font-size: 12px; padding: 8px;" placeholder='{"data":[{"model_name":"claude-opus-4-8","model_ratio":2.5,"completion_ratio":5,...}],...}'></textarea>
        <div class="modal-actions">
          <button class="btn btn-outline" @click="showImportModal = false">取消</button>
          <button class="btn btn-primary" @click="doImportPricing" :disabled="importing">
            {{ importing ? '导入中...' : '导入' }}
          </button>
        </div>
      </div>
    </div>
  </div>
</template>
<script>
import api from '../api.js'
export default {
  name: 'Providers',
  data() {
    return {
      providers: [],
      allKeys: [],          // 所有密钥（用于列表统计）
      loading: false,
      showModal: false,
      saving: false,
      isEditing: false,
      editingId: null,
      activeTab: 'basic',   // basic | keys
      form: {
        name: '',
        base_url: '',
        api_type: 'openai_compat',
        description: ''
      },
      // 密钥
      modalKeys: [],        // 当前编辑服务商的密钥列表
      keyForm: { key: '', label: '' },
      keySaving: false,
      showKeyInput: false,  // 密钥输入框是否明文
      revealedKeys: {},     // { keyId: fullKey }
      revealingId: null,    // 正在加载的密钥 id
      // 导入定价
      showImportModal: false,
      importTarget: null,
      importJson: '',
      importing: false
    }
  },
  methods: {
    async load() {
      this.loading = true
      try {
        const [providers, keysData] = await Promise.all([
          api.getProviders(),
          api.getKeys()
        ])
        this.providers = providers
        this.allKeys = keysData || []
      } catch (e) {
        console.error(e)
        alert('加载失败: ' + e.message)
      } finally {
        this.loading = false
      }
    },
    keyCount(providerId) {
      return this.allKeys.filter(k => k.provider_id === providerId).length
    },
    getProviderKeys(providerId) {
      return this.allKeys.filter(k => k.provider_id === providerId)
    },
    openAddModal() {
      this.isEditing = false
      this.editingId = null
      this.resetForm()
      this.activeTab = 'basic'
      this.modalKeys = []
      this.showModal = true
    },
    editProvider(p) {
      this.isEditing = true
      this.editingId = p.id
      this.form = {
        name: p.name,
        base_url: p.base_url,
        api_type: p.api_type,
        description: p.description || ''
      }
      this.modalKeys = this.getProviderKeys(p.id)
      this.activeTab = 'basic'
      this.keyForm = { key: '', label: '' }
      this.showKeyInput = false
      this.revealedKeys = {}
      this.showModal = true
    },
    async saveProvider() {
      if (!this.form.name || !this.form.base_url) {
        alert('请填写名称和 Base URL')
        return
      }
      this.saving = true
      try {
        if (this.isEditing) {
          await api.updateProvider(this.editingId, this.form)
        } else {
          const created = await api.createProvider(this.form)
          // 新建成功后设置 editingId，并切到密钥 tab
          this.editingId = created.id || (await api.getProviders()).find(p => p.name === this.form.name)?.id
          this.isEditing = true
          this.modalKeys = []
          // 刷新列表获取新服务商
          await this.load()
          this.activeTab = 'keys'
        }
        // 刷新密钥列表
        if (!this.isEditing || this.activeTab === 'keys') {
          await this.loadAllKeys()
          this.modalKeys = this.getProviderKeys(this.editingId)
        }
        if (!this.isEditing || this.activeTab !== 'keys') {
          // 非新建场景关闭弹窗
          await this.load()
          this.showModal = false
        }
      } catch (e) {
        alert('保存失败: ' + e.message)
      } finally {
        this.saving = false
      }
    },
    async loadAllKeys() {
      try {
        const data = await api.getKeys()
        this.allKeys = data || []
      } catch (e) {
        console.error('加载密钥失败', e)
      }
    },
    async addKey() {
      if (!this.keyForm.key.trim()) {
        alert('请填写 API Key')
        return
      }
      this.keySaving = true
      try {
        await api.createKey({
          provider_id: this.editingId,
          key: this.keyForm.key.trim(),
          label: this.keyForm.label.trim() || ''
        })
        this.keyForm = { key: '', label: '' }
        this.showKeyInput = false
        await this.loadAllKeys()
        this.modalKeys = this.getProviderKeys(this.editingId)
      } catch (e) {
        alert('添加密钥失败: ' + e.message)
      } finally {
        this.keySaving = false
      }
    },
    async toggleReveal(k) {
      if (this.revealedKeys[k.id]) {
        // 隐藏
        const copy = { ...this.revealedKeys }
        delete copy[k.id]
        this.revealedKeys = copy
        return
      }
      this.revealingId = k.id
      try {
        const data = await api.revealKey(k.id)
        this.revealedKeys = { ...this.revealedKeys, [k.id]: data.key || data }
      } catch (e) {
        alert('查看密钥失败: ' + e.message)
      } finally {
        this.revealingId = null
      }
    },
    async copyKey(keyId) {
      const key = this.revealedKeys[keyId]
      if (!key) return
      try {
        await navigator.clipboard.writeText(key)
        alert('已复制到剪贴板')
      } catch {
        // fallback
        const ta = document.createElement('textarea')
        ta.value = key
        document.body.appendChild(ta)
        ta.select()
        document.execCommand('copy')
        document.body.removeChild(ta)
        alert('已复制到剪贴板')
      }
    },
    async deleteKey(k) {
      if (!confirm(`确定删除密钥 "${k.label || k.key_prefix + '***'}"？`)) return
      try {
        await api.deleteKey(k.id)
        await this.loadAllKeys()
        this.modalKeys = this.getProviderKeys(this.editingId)
        // 清除已查看的密钥
        if (this.revealedKeys[k.id]) {
          const copy = { ...this.revealedKeys }
          delete copy[k.id]
          this.revealedKeys = copy
        }
      } catch (e) {
        alert('删除失败: ' + e.message)
      }
    },
    async deleteProvider(p) {
      if (!confirm(`确认删除服务商 "${p.name}"？关联的密钥和模型也会被删除。`)) return
      try {
        await api.deleteProvider(p.id)
        await this.load()
      } catch (e) {
        alert('删除失败: ' + e.message)
      }
    },
    resetForm() {
      this.form = {
        name: '',
        base_url: '',
        api_type: 'openai_compat',
        description: ''
      }
      this.isEditing = false
      this.editingId = null
      this.keyForm = { key: '', label: '' }
      this.showKeyInput = false
      this.revealedKeys = {}
    },
    openImportPricing(p) {
      this.importTarget = p
      this.importJson = ''
      this.showImportModal = true
    },
    openImportPricingFromEdit() {
      this.showModal = false
      this.importTarget = { id: this.editingId, name: this.form.name }
      this.importJson = ''
      this.showImportModal = true
    },
    async doImportPricing() {
      if (!this.importJson.trim()) {
        alert('请粘贴定价 JSON')
        return
      }
      this.importing = true
      try {
        const resp = await fetch(`/admin/api/providers/${this.importTarget.id}/import-pricing`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ json_data: this.importJson })
        })
        const data = await resp.json()
        if (!resp.ok) throw new Error(data.detail || '导入失败')
        this.showImportModal = false
        alert(`✅ 已更新 ${data.updated} 个，新建 ${data.created || 0} 个 / 共 ${data.total_models} 个模型`)
      } catch (e) {
        alert('导入失败: ' + e.message)
      } finally {
        this.importing = false
      }
    }
  },
  mounted() {
    this.load()
  }
}
</script>
<style scoped>
code {
  background: var(--gray-100);
  padding: 2px 6px;
  border-radius: 4px;
  font-size: 12px;
}
.modal-overlay {
  position: fixed;
  top: 0;
  left: 0;
  right: 0;
  bottom: 0;
  background: rgba(0,0,0,0.5);
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 1000;
}
.modal-content {
  background: white;
  padding: 24px;
  border-radius: 12px;
  width: 500px;
  max-width: 90vw;
}
.modal-actions {
  display: flex;
  justify-content: flex-end;
  gap: 12px;
  margin-top: 24px;
}
/* Tab */
.tab-bar {
  display: flex;
  gap: 0;
  margin-bottom: 20px;
  border-bottom: 2px solid var(--gray-200);
}
.tab-btn {
  padding: 8px 20px;
  border: none;
  background: none;
  font-size: 14px;
  color: var(--gray-500);
  cursor: pointer;
  border-bottom: 2px solid transparent;
  margin-bottom: -2px;
  transition: all 0.2s;
}
.tab-btn:hover:not(:disabled) {
  color: var(--gray-700);
}
.tab-btn.active {
  color: var(--primary);
  border-bottom-color: var(--primary);
  font-weight: 600;
}
.tab-btn:disabled {
  opacity: 0.4;
  cursor: not-allowed;
}
.tab-badge {
  display: inline-block;
  background: var(--primary);
  color: white;
  font-size: 11px;
  padding: 1px 7px;
  border-radius: 10px;
  margin-left: 6px;
  vertical-align: middle;
}
/* Key items */
.key-item {
  padding: 10px 12px;
  margin-bottom: 8px;
  background: var(--gray-50);
  border-radius: 8px;
  border: 1px solid var(--gray-200);
}
.key-info {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-bottom: 4px;
}
.key-label {
  font-weight: 600;
  font-size: 13px;
  min-width: 60px;
}
.key-prefix {
  font-size: 12px;
  background: var(--gray-200);
}
.key-status {
  font-size: 11px;
  color: var(--green);
  font-weight: 600;
}
.key-status.inactive {
  color: var(--gray-400);
}
.key-actions {
  display: flex;
  gap: 6px;
  margin-top: 6px;
}
.key-revealed {
  margin-top: 8px;
  padding: 8px;
  background: #fff8e1;
  border-radius: 6px;
  display: flex;
  align-items: center;
  gap: 8px;
}
.key-revealed code {
  flex: 1;
  font-size: 12px;
  word-break: break-all;
  background: none;
}
.copy-btn {
  white-space: nowrap;
}
.btn-xs {
  padding: 2px 10px;
  font-size: 12px;
}
/* Add key form */
.add-key-form {
  padding: 16px;
  background: var(--gray-50);
  border-radius: 8px;
  border: 1px dashed var(--gray-300);
}
.add-key-form .form-group {
  position: relative;
}
</style>
