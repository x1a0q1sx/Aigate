<template>
  <div>
    <ModelRefreshModal :visible="showRefreshModal" :result="refreshResult || {}" @close="showRefreshModal = false" />

    <div class="page-head">
      <div>
        <h1>服务商管理 🔌</h1>
        <p class="muted">已启用服务商与可添加目录分开管理，支持手动添加或一键添加 API Key 类服务商。</p>
      </div>
      <div class="head-actions">
        <button class="btn btn-outline" @click="refreshAllModels" :disabled="busy.refreshModels">{{ busy.refreshModels ? '刷新中...' : '刷新模型' }}</button>
        <button class="btn btn-outline" @click="cleanOrphans" :disabled="busy.cleanOrphans">{{ busy.cleanOrphans ? '清理中...' : '清理孤儿模型' }}</button>
        <button class="btn btn-primary" @click="openAddModal">+ 添加自定义服务商</button>
      </div>
    </div>

    <div class="stats-grid">
      <div class="stat-card"><span>已添加</span><strong>{{ providers.length }}</strong></div>
      <div class="stat-card"><span>API Key</span><strong>{{ dashboardStats.apiKey }}</strong></div>
      <div class="stat-card danger"><span>异常</span><strong>{{ dashboardStats.error }}</strong></div>
    </div>

    <div class="main-tabs">
      <button :class="['main-tab', { active: pageTab === 'enabled' }]" @click="pageTab = 'enabled'">已启用服务商</button>
      <button :class="['main-tab', { active: pageTab === 'catalog' }]" @click="pageTab = 'catalog'">添加服务商</button>
    </div>

    <section v-if="pageTab === 'enabled'" class="card panel-card">
      <div class="toolbar">
        <div class="segmented">
          <button v-for="f in providerFilters" :key="f.value" :class="{ active: providerFilter === f.value }" @click="providerFilter = f.value">{{ f.label }}</button>
        </div>
        <div class="toolbar-right">
          <input v-model.trim="providerSearch" placeholder="搜索服务商 / URL" />
          <select v-model="providerSort">
            <option value="pinned">固定优先</option>
            <option value="type">按类型</option>
            <option value="models">模型数</option>
            <option value="failures">失败数</option>
            <option value="name">名称</option>
          </select>
        </div>
      </div>

      <div v-if="filteredProviders.length === 0" class="empty-state">暂无匹配服务商</div>
      <div v-else class="enabled-grid">
        <article v-for="p in filteredProviders" :key="p.id" :class="['enabled-card', { highlight: highlightProviderId === p.id }]">
          <div class="provider-topline">
            <div>
              <div class="provider-name">
                <button class="pin-btn" :class="{ pinned: isPinned(p.id) }" @click="togglePin(p.id)" :title="isPinned(p.id) ? '取消固定' : '固定常用'">★</button>
                <strong>{{ p.name }}</strong>
                <span class="cred-badge" :class="'cred-' + (p.credential_type || 'api_key')">{{ credLabel(p.credential_type) }}</span>
              </div>
              <div class="provider-url" :title="p.base_url">{{ p.base_url }}</div>
            </div>
            <span :class="['status-pill', providerStatus(p).level]">{{ providerStatus(p).text }}</span>
          </div>
          <div class="provider-meta">
            <span>模型 {{ modelCount(p.id) }}</span>
            <span v-if="(p.credential_type || 'api_key') === 'api_key'">密钥 {{ keyCount(p.id) }}</span>
            <span v-else-if="(p.credential_type || 'api_key') === 'free_tier'">无需密钥</span>
            <span v-else>OAuth {{ oauthConnectionCount(p) }}</span>
            <span><code>{{ p.api_type }}</code></span>
          </div>
          <div v-if="statusLabels(p).length" class="diagnostics">
            <span v-for="s in statusLabels(p)" :key="s" class="diag-chip">{{ s }}</span>
          </div>
          <p v-if="p.description" class="provider-desc">{{ p.description }}</p>
          <div class="card-actions">
            <button class="btn btn-outline btn-sm" @click="refreshProviderModels(p)" :disabled="busy.refreshModels">刷新模型</button>
            <button class="btn btn-outline btn-sm" @click="editProvider(p)">编辑</button>
            <button v-if="(p.credential_type || 'api_key') === 'api_key'" class="btn btn-outline btn-sm" @click="editProviderKeys(p)">密钥</button>
            <button class="btn btn-outline btn-sm" @click="openImportPricing(p)">导入定价</button>
            <button v-if="p.api_type === 'atomcode'" class="btn" :class="atomExeStatus.found ? 'btn-outline' : 'btn-warn'" @click="openAtomExeConfig(p)">配置可执行文件</button>
            <button class="btn btn-danger btn-sm" @click="deleteProvider(p)">删除</button>
          </div>
        </article>
      </div>
    </section>

    <section v-else class="card panel-card">
      <div class="toolbar catalog-toolbar">
        <input v-model.trim="catalogSearch" placeholder="搜索服务商名称 / id / base URL" />
        <div class="segmented">
          <button :class="{ active: catalogTab === 'api_key' }" @click="catalogTab = 'api_key'">API Key {{ apikeyProviders.length }}</button>
        </div>
      </div>
      <div class="catalog-grid">
        <article v-for="c in visibleCatalog" :key="catalogKey(c)" class="catalog-card" :style="{ '--brand-color': c.color || '#64748b' }">
          <div class="card-icon" :style="{ background: c.color || '#64748b' }">{{ (c.name || '?').charAt(0) }}</div>
          <div class="catalog-body">
            <div class="catalog-title">
              <strong>{{ c.name }}</strong>
              <span :class="['catalog-status', catalogStatus(c).level]">{{ catalogStatus(c).text }}</span>
            </div>
            <div class="card-id">{{ c.id || c.code }}</div>
            <div class="catalog-url" :title="c.baseUrl || c.api_base_url || c.website">{{ c.baseUrl || c.api_base_url || c.website || '-' }}</div>
            <div class="card-actions">
              <button class="btn btn-primary btn-xs" @click="addFromCatalog(c, 'api_key')" :disabled="catalogStatus(c).state === 'enabled'">添加</button>
              <button v-if="catalogStatus(c).provider" class="btn btn-outline btn-xs" @click="focusProvider(catalogStatus(c).provider)">查看</button>
              <a v-if="c.website" class="card-link" :href="c.website" target="_blank" rel="noopener">官网</a>
            </div>
          </div>
        </article>
      </div>
    </section>

    <div v-if="showModal" class="modal-overlay" @click.self="showModal = false">
      <div class="modal-content provider-modal">
        <h3>{{ isEditing ? '编辑服务商' : '添加服务商' }}</h3>
        <div class="tab-bar">
          <button :class="['tab-btn', { active: activeTab === 'basic' }]" @click="activeTab = 'basic'">基本信息</button>
          <button :class="['tab-btn', { active: activeTab === 'keys' }]" @click="activeTab = 'keys'" :disabled="!editingId">密钥管理 <span v-if="modalKeys.length" class="tab-badge">{{ modalKeys.length }}</span></button>
        </div>
        <div v-if="activeTab === 'basic'" class="modal-scroll">
          <div class="form-group"><label>名称</label><input v-model="form.name" placeholder="例如 lyclaude" /></div>
          <div class="form-group"><label>API Base URL</label><input v-model="form.base_url" placeholder="https://api.example.com/v1" /></div>
          <div class="form-group"><label>API 类型</label><select v-model="form.api_type"><option value="openai_compat">OpenAI 兼容</option><option value="codex_responses">Codex / Responses API</option><option value="anthropic">Anthropic Claude (API Key)</option><option value="github">GitHub Models</option><option value="atomcode">AtomCode (AtomGit 签名反代)</option></select></div>
          <div class="form-group"><label>凭据类型</label><select v-model="form.credential_type"><option value="api_key">API Key</option></select></div>
          <div v-if="!isEditing && form.api_type !== 'atomcode'" class="form-group"><label>API Key（可选，填了保存时一并添加）</label><input v-model="keyForm.key" :type="showKeyInput ? 'text' : 'password'" placeholder="sk-..." /><button class="btn btn-outline btn-xs input-side-btn" @click="showKeyInput = !showKeyInput">{{ showKeyInput ? '隐藏' : '显示' }}</button></div>
          <div v-if="!isEditing && form.api_type !== 'atomcode' && keyForm.key" class="form-group"><label>密钥标签（可选）</label><input v-model="keyForm.label" placeholder="例如 prod / test" /></div>
          <div class="form-group"><label>描述</label><textarea v-model="form.description" rows="3" placeholder="可选说明"></textarea></div>
          <div class="modal-actions"><button v-if="isEditing" class="btn btn-outline" @click="openImportPricingFromEdit">导入定价</button><span style="flex:1"></span><button class="btn btn-outline" @click="showModal = false">取消</button><button class="btn btn-primary" @click="saveProvider" :disabled="saving">{{ saving ? '保存中...' : '保存' }}</button></div>
        </div>
        <div v-else class="modal-scroll">
          <p class="muted">管理「{{ form.name }}」的 API Key。</p>
          <div v-if="modalKeys.length" class="key-list">
            <div class="key-item" v-for="k in modalKeys" :key="k.id">
              <div class="key-info"><strong>{{ k.label || '未命名' }}</strong><code>{{ k.key_prefix }}***</code><span :class="['badge', k.is_active ? 'ok' : 'err']">{{ k.is_active ? '可用' : '停用' }}</span></div>
              <div class="key-actions"><button class="btn btn-outline btn-xs" @click="toggleReveal(k)" :disabled="revealingId === k.id">{{ revealedKeys[k.id] ? '隐藏' : '查看' }}</button><button class="btn btn-outline btn-xs danger-text" @click="deleteKey(k)">删除</button></div>
              <div v-if="revealedKeys[k.id]" class="key-revealed"><code>{{ revealedKeys[k.id] }}</code><button class="btn btn-xs" @click="copyKey(k.id)">复制</button></div>
            </div>
          </div>
          <p v-else class="empty-state small">暂无密钥</p>
          <div class="add-key-form"><h4>添加新密钥</h4><div class="form-group"><label>API Key</label><input v-model="keyForm.key" :type="showKeyInput ? 'text' : 'password'" placeholder="sk-..." /><button class="btn btn-outline btn-xs input-side-btn" @click="showKeyInput = !showKeyInput">{{ showKeyInput ? '隐藏' : '显示' }}</button></div><div class="form-group"><label>标签</label><input v-model="keyForm.label" placeholder="例如 prod / test" /></div><button class="btn btn-primary btn-sm" @click="addKey" :disabled="keySaving">{{ keySaving ? '添加中...' : '添加密钥' }}</button></div>
          <div class="modal-actions"><button class="btn btn-outline" @click="activeTab = 'basic'">返回</button><span style="flex:1"></span><button class="btn btn-outline" @click="showModal = false">关闭</button></div>
        </div>
      </div>
    </div>

    <div v-if="showImportModal" class="modal-overlay" @click.self="showImportModal = false">
      <div class="modal-content import-modal"><h3>导入定价 — {{ importTarget ? importTarget.name : '' }}</h3><p class="muted">粘贴 newapi / one-api 的 /api/pricing JSON。</p><textarea v-model="importJson" rows="12" class="json-textarea" placeholder='{"data":[...]}'></textarea><div class="modal-actions"><button class="btn btn-outline" @click="showImportModal = false">取消</button><button class="btn btn-primary" @click="doImportPricing" :disabled="importing">{{ importing ? '导入中...' : '导入' }}</button></div></div>
    </div>

    <div v-if="importTokenModal.show" class="modal-overlay" @click.self="importTokenModal.show = false">
      <div class="modal-content import-modal"><h3>导入 {{ importTokenModal.provider_code }} token</h3><div class="form-group"><label>Access Token *</label><textarea v-model="importTokenModal.access_token" rows="3"></textarea></div><div class="form-group"><label>Refresh Token</label><textarea v-model="importTokenModal.refresh_token" rows="3"></textarea></div><div class="form-row"><div class="form-group"><label>过期秒数</label><input type="number" v-model.number="importTokenModal.expires_in" min="60" /></div><div class="form-group"><label>Owner</label><input v-model="importTokenModal.owner" /></div></div><p v-if="importTokenError" class="err-text">{{ importTokenError }}</p><div class="modal-actions"><button class="btn btn-outline" @click="importTokenModal.show = false">取消</button><button class="btn btn-primary" @click="submitImportToken" :disabled="importTokenBusy">{{ importTokenBusy ? '导入中...' : '确认导入' }}</button></div></div>
    </div>

    <div v-if="showAtomAuthModal" class="modal-overlay" @click.self="showAtomAuthModal = false">
      <div class="modal-content import-modal">
        <h3>启用 AtomCode 直连反代</h3>
        <p class="muted">AIGate 将直接对 AtomGit LLM 网关做反向代理，无需本地运行任何中间件。请粘贴你的 AtomCode 鉴权 JSON（默认读取路径 <code>~/.atomcode/auth.toml</code>，点击「从默认路径加载」可自动解析），需含 <code>access_token</code> / <code>refresh_token</code> / <code>user.id</code>。</p>
        <div class="form-group"><label>鉴权 JSON *</label><textarea v-model="atomAuthJson" rows="10" placeholder='{"access_token":"...","refresh_token":"...","user":{"id":"..."},"expires_in":7200,"created_at":1700000000}'></textarea></div>
        <p class="form-hint">必须包含 access_token；user.id 用于请求签名；refresh_token 用于自动续期。</p>
        <p v-if="atomAuthError" class="err-text">{{ atomAuthError }}</p>
        <div class="modal-actions">
          <button class="btn btn-outline" @click="showAtomAuthModal = false">取消</button>
          <button class="btn btn-secondary" @click="loadAtomAuth" :disabled="atomAuthLoading || atomAuthBusy">{{ atomAuthLoading ? '读取中...' : '从默认路径 (~/.atomcode/auth.toml) 加载' }}</button>
          <button class="btn btn-primary" @click="confirmAtomAuth" :disabled="atomAuthBusy">{{ atomAuthBusy ? '启用中...' : '确认启用' }}</button>
        </div>
      </div>
    </div>

    <div v-if="showAtomExeModal" class="modal-overlay" @click.self="showAtomExeModal = false">
      <div class="modal-content import-modal">
        <h3>配置 AtomCode 可执行文件</h3>
        <p class="muted">AIGate 的 AtomCode 适配器需要本机运行 <code>atomcode</code> 守护进程来为请求签名。当前未找到可执行文件，请先安装 AtomCode 桌面客户端，再把<strong>安装目录</strong>或 <strong>exe 路径</strong>填到下面。</p>
        <ol class="atom-steps">
          <li>从 <a href="https://atomgit.com" target="_blank" rel="noopener">AtomGit 官网</a> 下载并安装「AtomCode / AtomGit 桌面客户端」。</li>
          <li>记下它的安装目录（例如 <code>C:\Users\你\AppData\Local\AtomCode</code> 或你自定义的路径）。</li>
          <li>把该目录（或目录里的 <code>atomcode.exe</code>）粘贴到下方，点击保存。AIGate 会自动拉起守护进程并验证。</li>
        </ol>
        <div class="form-group"><label>安装目录 或 exe 路径 *</label><input v-model="atomExePath" placeholder="例如 C:\Users\你\AppData\Local\AtomCode 或 D:\atomcode\atomcode.exe" /></div>
        <p v-if="atomExeStatus.configured_path && atomExeStatus.configured_path !== atomExePath" class="form-hint">当前已保存路径：<code>{{ atomExeStatus.configured_path }}</code></p>
        <p v-if="atomExeError" class="err-text">{{ atomExeError }}</p>
        <p v-if="atomExeWarning" class="warn-text">{{ atomExeWarning }}</p>
        <div class="modal-actions">
          <button class="btn btn-outline" @click="showAtomExeModal = false">取消</button>
          <button class="btn btn-primary" @click="setAtomExePath" :disabled="atomExeBusy">{{ atomExeBusy ? '验证中...' : '保存并验证' }}</button>
        </div>
      </div>
    </div>
  </div>
</template>

<script>
import api from '../api.js'
import { FREE_TIER_PROVIDERS, OAUTH_PROVIDERS as OAUTH_CATALOG, APIKEY_PROVIDERS } from '../data/providersConstants.js'
import ModelRefreshModal from '../components/ModelRefreshModal.vue'
const PIN_KEY = 'aigate:pinnedProviders'
export default {
  name: 'Providers',
  components: { ModelRefreshModal },
  data() { return {
    providers: [], allKeys: [], models: [], oauthProvidersState: [], oauthConnections: [],
    pageTab: 'enabled', providerFilter: 'all', providerSearch: '', providerSort: 'pinned', catalogTab: 'api_key', catalogSearch: '',
    providerFilters: [{ value: 'all', label: '全部' }, { value: 'api_key', label: 'API Key' }, { value: 'free_tier', label: 'Free Tier' }, { value: 'oauth', label: 'OAuth' }, { value: 'error', label: '异常' }],
    freeTierProviders: FREE_TIER_PROVIDERS, apikeyProviders: APIKEY_PROVIDERS,
    pinnedIds: [], highlightProviderId: null,
    showModal: false, saving: false, isEditing: false, editingId: null, activeTab: 'basic',
    form: { name: '', base_url: '', api_type: 'openai_compat', credential_type: 'api_key', oauth_code: null, description: '' },
    modalKeys: [], keyForm: { key: '', label: '' }, keySaving: false, showKeyInput: false, revealedKeys: {}, revealingId: null,
    showImportModal: false, importTarget: null, importJson: '', importing: false,
    showAtomAuthModal: false, atomAuthTarget: null, atomAuthJson: '', atomAuthBusy: false, atomAuthError: '', atomAuthLoading: false,
    atomExeStatus: { found: true, exe_path: null, configured_path: null },
    showAtomExeModal: false, atomExePath: '', atomExeBusy: false, atomExeError: '', atomExeWarning: '',
    importTokenModal: { show: false, provider_code: '', access_token: '', refresh_token: '', expires_in: 3600, owner: '__default' }, importTokenBusy: false, importTokenError: '', pendingCode: null,
    busy: { refreshModels: false, cleanOrphans: false, enableCode: '' },
    showRefreshModal: false, refreshResult: null
  }},
  computed: {
    oauthProviders() { if (this.oauthProvidersState?.length) return this.oauthProvidersState; return OAUTH_CATALOG.map(p => ({ ...p, code: p.id, api_base_url: p.baseUrl, use_pkce: !['codebuddy_cn', 'kimchi', 'qoder'].includes(p.id), extra_params: {} })) },
    dashboardStats() { const s = { apiKey: 0, freeTier: 0, oauth: 0, error: 0 }; for (const p of this.providers) { const t = p.credential_type || 'api_key'; if (t === 'free_tier') s.freeTier++; else if (t === 'oauth') s.oauth++; else s.apiKey++; if (this.providerStatus(p).level === 'error') s.error++ } return s },
    filteredProviders() { let rows = [...this.providers]; const q = this.providerSearch.toLowerCase(); if (q) rows = rows.filter(p => [p.name, p.base_url, p.oauth_code, p.api_type].some(v => String(v || '').toLowerCase().includes(q))); if (this.providerFilter !== 'all') { if (this.providerFilter === 'error') rows = rows.filter(p => this.providerStatus(p).level === 'error'); else rows = rows.filter(p => (p.credential_type || 'api_key') === this.providerFilter) } const byName = (a,b) => a.name.localeCompare(b.name); rows.sort((a,b) => { if (this.providerSort === 'pinned') return Number(this.isPinned(b.id)) - Number(this.isPinned(a.id)) || byName(a,b); if (this.providerSort === 'type') return (a.credential_type || 'api_key').localeCompare(b.credential_type || 'api_key') || byName(a,b); if (this.providerSort === 'models') return this.modelCount(b.id) - this.modelCount(a.id) || byName(a,b); if (this.providerSort === 'failures') return this.failCount(b.id) - this.failCount(a.id) || byName(a,b); return byName(a,b) }); return rows },
    visibleCatalog() { const source = this.catalogTab === 'free_tier' ? this.freeTierProviders : this.catalogTab === 'oauth' ? this.oauthProviders : this.apikeyProviders; const q = this.catalogSearch.toLowerCase(); return source.filter(c => !q || [c.name, c.id, c.code, c.alias, c.baseUrl, c.api_base_url, c.website].some(v => String(v || '').toLowerCase().includes(q))) }
  },
  mounted() { this.pinnedIds = JSON.parse(localStorage.getItem(PIN_KEY) || '[]'); this.load() },
  methods: {
    async load() { const [providers, keys, models, oauthProviders, oauthConnections, atomExe] = await Promise.all([api.getProviders(), api.getKeys().catch(() => []), api.getModels().catch(() => []), api.getOAuthProviders().catch(() => []), api.getOAuthConnections().catch(() => []), api.getAtomExeStatus().catch(() => ({ found: true }))]); this.providers = providers || []; this.allKeys = keys || []; this.models = models || []; this.oauthProvidersState = oauthProviders || []; this.oauthConnections = oauthConnections || []; this.atomExeStatus = atomExe && atomExe.found !== undefined ? atomExe : { found: true } },
    normalizeUrl(v) { return String(v || '').replace(/\/+$/, '').toLowerCase() },
    catalogKey(c) { return c.id || c.code || c.name },
    matchProvider(c) { const code = c.id || c.code; const base = this.normalizeUrl(c.baseUrl || c.api_base_url); return this.providers.find(p => (code && p.oauth_code === code) || p.name === c.name || (base && this.normalizeUrl(p.base_url) === base)) },
    catalogStatus(c) { const p = this.matchProvider(c); if (!p) return { state: 'not_added', text: '未添加', level: 'idle', provider: null }; const st = this.providerStatus(p); if (st.level === 'error') return { state: 'error', text: '异常', level: 'error', provider: p }; return { state: 'enabled', text: '已启用', level: 'ok', provider: p } },
    keyCount(id) { return this.allKeys.filter(k => k.provider_id === id).length },
    getProviderKeys(id) { return this.allKeys.filter(k => k.provider_id === id) },
    modelCount(id) { return this.models.filter(m => m.provider_id === id).length },
    failCount(id) { return this.models.filter(m => m.provider_id === id).reduce((n,m) => n + (Number(m.fail_count) || 0), 0) },
    oauthConnectionCount(p) { return this.oauthConnections.filter(c => c.provider_code === p.oauth_code || c.provider_code === p.name).length },
    statusLabels(p) { const labels = []; const type = p.credential_type || 'api_key'; if (type === 'api_key' && this.keyCount(p.id) === 0 && p.api_type !== 'atomcode') labels.push('缺密钥'); if (p.api_type === 'atomcode' && !this.atomExeStatus.found) labels.push('需配置 AtomCode'); if (this.modelCount(p.id) === 0) labels.push('无模型'); if (type === 'oauth' && this.oauthConnectionCount(p) === 0) labels.push('OAuth 未连接'); if (!/^https?:\/\//i.test(p.base_url || '') && type !== 'free_tier') labels.push('URL 异常'); if (this.failCount(p.id) > 0) labels.push(`失败 ${this.failCount(p.id)}`); return labels },
    providerStatus(p) { const labels = this.statusLabels(p); if (labels.some(x => ['缺密钥','无模型','OAuth 未连接','URL 异常','需配置 AtomCode'].includes(x))) return { text: labels[0], level: 'error' }; if (labels.length) return { text: '需关注', level: 'warn' }; return { text: '可用', level: 'ok' } },
    credLabel(t) { return { api_key: 'API Key', free_tier: 'Free', oauth: 'OAuth' }[t || 'api_key'] || 'API Key' },
    credHint(t) { return { api_key: '需要在密钥管理里添加 sk-... 等密钥。' }[t || 'api_key'] || '需要在密钥管理里添加 sk-... 等密钥。' },
    isPinned(id) { return this.pinnedIds.includes(id) },
    togglePin(id) { this.pinnedIds = this.isPinned(id) ? this.pinnedIds.filter(x => x !== id) : [...this.pinnedIds, id]; localStorage.setItem(PIN_KEY, JSON.stringify(this.pinnedIds)) },
    focusProvider(p) { this.pageTab = 'enabled'; this.providerSearch = p.name; this.highlightProviderId = p.id; setTimeout(() => { this.highlightProviderId = null }, 3000) },
    openAddModal() { this.resetForm(); this.showModal = true },
    editProvider(p) { this.form = { name: p.name, base_url: p.base_url, api_type: p.api_type, credential_type: p.credential_type || 'api_key', oauth_code: p.oauth_code || null, description: p.description || '' }; this.isEditing = true; this.editingId = p.id; this.activeTab = 'basic'; this.modalKeys = this.getProviderKeys(p.id); this.showModal = true },
    editProviderKeys(p) { this.editProvider(p); this.activeTab = 'keys' },
    resetForm() { this.form = { name: '', base_url: '', api_type: 'openai_compat', credential_type: 'api_key', oauth_code: null, description: '' }; this.isEditing = false; this.editingId = null; this.activeTab = 'basic'; this.modalKeys = []; this.keyForm = { key: '', label: '' }; this.revealedKeys = {} },
    async saveProvider() { if (!this.form.name.trim() || !this.form.base_url.trim()) return alert('请填写名称和 Base URL'); this.saving = true; try { const base = { name: this.form.name.trim(), base_url: this.form.base_url.trim(), api_type: this.form.api_type, description: this.form.description || '' }; if (this.isEditing) { await api.updateProvider(this.editingId, base); await this.load(); } else { const saved = await api.createProvider({ ...base, credential_type: 'api_key', oauth_code: null }); const k = (this.keyForm.key || '').trim(); if (k && this.form.api_type !== 'atomcode') { try { await api.createKey({ provider_id: saved.id, key: k, label: (this.keyForm.label || '').trim() }); } catch (e) { alert('服务商已创建，但密钥添加失败：' + e.message); } } await this.load(); this.editingId = saved.id; this.isEditing = true; this.keyForm = { key: '', label: '' }; } this.showModal = false } catch (e) { alert('保存失败: ' + e.message) } finally { this.saving = false } },
    async deleteProvider(p) { if (!confirm(`确认删除服务商「${p.name}」？关联密钥和模型也会删除。`)) return; try { await api.deleteProvider(p.id); await this.load() } catch (e) { alert('删除失败: ' + e.message) } },
    async enableFreeTier(c) {
      const existing = this.matchProvider(c);
      if (existing) return this.focusProvider(existing);
      // 需要粘贴 AtomCode 鉴权 JSON（含 access_token / refresh_token / user.id）
      if (c.authInput) {
        this.atomAuthTarget = c;
        this.atomAuthJson = '';
        this.atomAuthError = '';
        this.atomAuthLoading = false;
        this.showAtomAuthModal = true;
        this.loadAtomAuth();
        return;
      }
      this.busy.enableCode = c.id;
      try {
        const baseUrl = c.baseUrl || c.api_base_url || c.website || '';
        let provider, modelIds;
        if (c.needsKey) {
          // 标准 OpenAI 兼容 + 静态 key（如部分本地服务），非 free_tier 免密协议
          provider = await api.createProvider({ name: c.name, base_url: baseUrl, api_type: 'openai_compat', credential_type: 'api_key', oauth_code: c.id || c.code, description: `${c.name}` });
          if (c.defaultKey) { try { await api.createKey({ provider_id: provider.id, key: c.defaultKey, label: c.name + ' 密钥' }) } catch (e) { if (!String(e.message).includes('409')) throw e } }
          modelIds = (c.models && c.models.length) ? c.models : [`${c.id || 'free'}-auto`];
        } else {
          provider = await api.createProvider({ name: c.name, base_url: baseUrl, api_type: 'openai_compat', credential_type: 'free_tier', oauth_code: c.id || c.code, description: `${c.name}` });
          modelIds = (c.id === 'mimo-free') ? ['mimo-auto'] : [`${c.id || 'free'}-auto`];
        }
        for (const mid of modelIds) { try { await api.createProviderModel(provider.id, { model_id: mid, display_name: mid, input_price: 0, output_price: 0 }) } catch (e) { if (!String(e.message).includes('409')) throw e } }
        await this.load();
        this.focusProvider(provider);
        alert(`${c.name} 已启用。可在 Playground 使用 ${provider.name}/${modelIds[0]}`)
      } catch (e) { alert('启用失败: ' + e.message) } finally { this.busy.enableCode = '' }
    },
    async loadAtomAuth() {
      const c = this.atomAuthTarget;
      if (!c) return;
      this.atomAuthLoading = true;
      this.atomAuthError = '';
      try {
        const r = await api.loadAtomAuth();
        this.atomAuthJson = JSON.stringify(r.auth, null, 2);
      } catch (e) {
        // 文件不存在 / 解析失败时仅提示，不阻断手动粘贴
        this.atomAuthError = '自动读取默认路径失败：' + e.message + '（可手动粘贴 JSON）';
      } finally {
        this.atomAuthLoading = false;
      }
    },
    async confirmAtomAuth() {
      const c = this.atomAuthTarget;
      if (!c) return;
      let parsed;
      try { parsed = JSON.parse(this.atomAuthJson); } catch (e) { this.atomAuthError = 'JSON 解析失败：' + e.message; return; }
      if (!parsed || !parsed.access_token) { this.atomAuthError = '鉴权 JSON 必须包含 access_token 字段'; return; }
      this.atomAuthBusy = true;
      try {
        const provider = await api.createProvider({ name: c.name, base_url: c.baseUrl, api_type: c.api_type || 'atomcode', credential_type: 'api_key', oauth_code: c.id || c.code, description: `AtomCode 直连反代` });
        try { await api.createKey({ provider_id: provider.id, key: this.atomAuthJson, label: c.name + ' 鉴权' }) } catch (e) { if (!String(e.message).includes('409')) throw e }
        const modelIds = (c.models && c.models.length) ? c.models : ['deepseek-v4-flash'];
        for (const mid of modelIds) { try { await api.createProviderModel(provider.id, { model_id: mid, display_name: mid, input_price: 0, output_price: 0 }) } catch (e) { if (!String(e.message).includes('409')) throw e } }
        this.showAtomAuthModal = false;
        this.atomAuthJson = '';
        await this.load();
        this.focusProvider(provider);
        alert(`${c.name} 已启用（AIGate 直连 AtomGit 网关）。可在 Playground 使用 ${provider.name}/${modelIds[0]}`)
      } catch (e) { this.atomAuthError = '启用失败: ' + e.message } finally { this.atomAuthBusy = false }
    },
    openAtomExeConfig(p) {
      this.atomExePath = this.atomExeStatus.configured_path || (p && (p._atomExeDir || '')) || '';
      this.atomExeError = '';
      this.atomExeWarning = '';
      this.atomExeBusy = false;
      this.showAtomExeModal = true;
    },
    async setAtomExePath() {
      if (!this.atomExePath.trim()) { this.atomExeError = '请填写安装目录或 exe 路径'; return }
      this.atomExeBusy = true;
      this.atomExeError = '';
      this.atomExeWarning = '';
      try {
        const r = await api.setAtomExePath(this.atomExePath.trim());
        await this.load();
        if (r.warning) {
          this.atomExeWarning = r.warning;
          alert('路径已保存，但守护进程拉起失败：' + r.warning);
        } else {
          this.showAtomExeModal = false;
          alert('AtomCode 可执行文件已配置，守护进程已启动 ✓')
        }
      } catch (e) { this.atomExeError = '配置失败: ' + e.message } finally { this.atomExeBusy = false }
    },
    addFromCatalog(c, credentialType) { const baseUrl = c.baseUrl || c.api_base_url || c.website || ''; this.form = { name: c.name, base_url: baseUrl, api_type: (c.id === 'codex' || c.alias === 'cx') ? 'codex_responses' : 'openai_compat', credential_type: 'api_key', oauth_code: null, description: '' }; this.isEditing = false; this.editingId = null; this.activeTab = 'basic'; this.showModal = true },
    async refreshAllModels() { this.busy.refreshModels = true; try { const r = await api.refreshModels(); await this.load(); this.refreshResult = r; this.showRefreshModal = true } catch (e) { this.refreshResult = { error: e.message }; this.showRefreshModal = true } finally { this.busy.refreshModels = false } },
    async refreshProviderModels(p) { this.busy.refreshModels = true; try { const r = await api.refreshModels(p.id); await this.load(); this.refreshResult = r; this.showRefreshModal = true } catch (e) { this.refreshResult = { error: e.message }; this.showRefreshModal = true } finally { this.busy.refreshModels = false } },
    async cleanOrphans() { if (!confirm('清理没有服务商归属的孤儿模型？')) return; this.busy.cleanOrphans = true; try { const r = await api.cleanOrphanModels(); await this.load(); alert(`已清理 ${r.deleted || 0} 个孤儿模型`) } catch (e) { alert('清理失败: ' + e.message) } finally { this.busy.cleanOrphans = false } },
    async addKey() { if (!this.keyForm.key.trim()) return alert('请填写 API Key'); this.keySaving = true; try { await api.createKey({ provider_id: this.editingId, key: this.keyForm.key.trim(), label: this.keyForm.label.trim() }); this.keyForm = { key: '', label: '' }; this.showKeyInput = false; await this.load(); this.modalKeys = this.getProviderKeys(this.editingId) } catch (e) { alert('添加密钥失败: ' + e.message) } finally { this.keySaving = false } },
    async toggleReveal(k) { if (this.revealedKeys[k.id]) { const x = { ...this.revealedKeys }; delete x[k.id]; this.revealedKeys = x; return } this.revealingId = k.id; try { const d = await api.revealKey(k.id); this.revealedKeys = { ...this.revealedKeys, [k.id]: d.key || d } } catch (e) { alert('查看密钥失败: ' + e.message) } finally { this.revealingId = null } },
    async copyKey(id) { await navigator.clipboard?.writeText(this.revealedKeys[id] || '') },
    async deleteKey(k) { if (!confirm(`确认删除密钥「${k.label || k.key_prefix + '***'}」？`)) return; try { await api.deleteKey(k.id); await this.load(); this.modalKeys = this.getProviderKeys(this.editingId) } catch (e) { alert('删除失败: ' + e.message) } },
    openImportPricing(p) { this.importTarget = p; this.importJson = ''; this.showImportModal = true },
    openImportPricingFromEdit() { this.showModal = false; this.openImportPricing({ id: this.editingId, name: this.form.name }) },
    async doImportPricing() { if (!this.importJson.trim()) return alert('请粘贴定价 JSON'); this.importing = true; try { const resp = await fetch(`/admin/api/providers/${this.importTarget.id}/import-pricing`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ json_data: this.importJson }) }); const data = await resp.json(); if (!resp.ok) throw new Error(data.detail || '导入失败'); this.showImportModal = false; await this.load(); alert(`已更新 ${data.updated} 个，新建 ${data.created || 0} 个`) } catch (e) { alert('导入失败: ' + e.message) } finally { this.importing = false } },
    isImportProvider(code) { return ['codebuddy_cn', 'kimchi', 'qoder', 'kimi_coding'].includes(code) },
    async startAuthorize(code) { if (this.isImportProvider(code)) return this.openImportModal(code); this.pendingCode = code; try { const r = await api.startOAuthAuthorize(code); if (r.login_url) window.open(r.login_url, '_blank', 'noopener,noreferrer,width=900,height=720'); else if (r.authorize_url) window.open(r.authorize_url, '_blank', 'noopener,noreferrer,width=720,height=720'); setTimeout(() => this.load(), 5000) } catch (e) { alert('授权失败: ' + e.message) } finally { this.pendingCode = null } },
    openImportModal(code) { this.importTokenError = ''; this.importTokenModal = { show: true, provider_code: code, access_token: '', refresh_token: '', expires_in: 3600, owner: '__default' } },
    async submitImportToken() { if (!this.importTokenModal.access_token.trim()) { this.importTokenError = 'access_token 不能为空'; return } this.importTokenBusy = true; try { await api.importOAuthToken({ provider_code: this.importTokenModal.provider_code, access_token: this.importTokenModal.access_token.trim(), refresh_token: this.importTokenModal.refresh_token.trim(), expires_in: this.importTokenModal.expires_in || 3600, owner: this.importTokenModal.owner || '__default' }); this.importTokenModal.show = false; await this.load(); alert('导入成功') } catch (e) { this.importTokenError = '导入失败: ' + e.message } finally { this.importTokenBusy = false } },
    async refreshOAuth(id) { try { await api.refreshOAuthConnection(id); await this.load() } catch (e) { alert('刷新失败: ' + e.message) } },
    async removeOAuth(id) { if (!confirm('确认删除该 OAuth 连接？')) return; try { await api.deleteOAuthConnection(id); await this.load() } catch (e) { alert('删除失败: ' + e.message) } },
    formatTime(t) { return t ? String(t).replace('T', ' ').replace('Z', '').slice(0, 19) : '-' }
  }
}
</script>
<style scoped>
.page-head,.toolbar,.provider-topline,.provider-name,.provider-meta,.card-actions,.head-actions,.form-row{display:flex;gap:12px;align-items:center}.page-head{justify-content:space-between;margin-bottom:16px}.head-actions{flex-wrap:wrap}.muted{color:var(--text-muted);font-size:13px}.stats-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));gap:10px;margin-bottom:16px}.stat-card{background:var(--bg-card);border:1px solid var(--border-soft);border-radius:8px;padding:12px}.stat-card span{display:block;color:var(--text-muted);font-size:12px}.stat-card strong{font-size:22px;color:var(--primary)}.stat-card.danger strong{color:var(--danger)}.main-tabs{display:flex;gap:8px;margin-bottom:12px}.main-tab{border:1px solid var(--border-soft);background:var(--bg-elevated);color:var(--text-secondary);padding:8px 14px;border-radius:8px;cursor:pointer}.main-tab.active{background:var(--primary);border-color:var(--primary);color:#fff}.panel-card{padding:16px}.toolbar{justify-content:space-between;margin-bottom:14px;flex-wrap:wrap}.toolbar-right{display:flex;gap:8px;align-items:center}.toolbar input,.toolbar select{width:auto;min-width:180px}.segmented{display:flex;gap:4px;flex-wrap:wrap}.segmented button{border:1px solid var(--border-soft);background:var(--bg-elevated);color:var(--text-secondary);padding:6px 10px;border-radius:6px;cursor:pointer}.segmented button.active{background:var(--primary);border-color:var(--primary);color:#fff}.enabled-grid,.catalog-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(320px,1fr));gap:12px}.enabled-card,.catalog-card{border:1px solid var(--border-soft);background:var(--bg-elevated);border-radius:8px;padding:14px;transition:.18s border-color,.18s box-shadow}.enabled-card.highlight{border-color:var(--primary);box-shadow:0 0 0 3px rgba(59,130,246,.18)}.provider-topline{justify-content:space-between;align-items:flex-start}.provider-name{gap:6px}.pin-btn{border:0;background:transparent;color:var(--text-dim);cursor:pointer;padding:0;font-size:15px}.pin-btn.pinned{color:#f59e0b}.provider-url,.catalog-url{font-family:monospace;font-size:12px;color:var(--text-muted);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:420px}.provider-meta{flex-wrap:wrap;margin-top:10px;color:var(--text-muted);font-size:12px}.provider-desc{color:var(--text-muted);font-size:12px;margin:10px 0 0}.cred-badge,.status-pill,.diag-chip,.catalog-status,.badge{display:inline-block;border-radius:999px;padding:2px 8px;font-size:11px;border:1px solid transparent}.cred-api_key{background:var(--badge-paid-bg);color:var(--badge-paid-fg);border-color:var(--badge-paid-fg)}.cred-free_tier,.status-pill.ok,.catalog-status.ok,.badge.ok{background:var(--badge-free-bg);color:var(--badge-free-fg);border-color:var(--badge-free-fg)}.cred-oauth{background:var(--alert-info-bg);color:var(--alert-info-fg);border-color:var(--alert-info-border)}.status-pill.error,.catalog-status.error,.badge.err{background:var(--alert-error-bg);color:var(--alert-error-fg);border-color:var(--alert-error-border)}.status-pill.warn{background:rgba(245,158,11,.15);color:#f59e0b;border-color:#f59e0b}.catalog-status.idle{background:var(--bg-hover);color:var(--text-muted);border-color:var(--border-soft)}.diagnostics{display:flex;gap:6px;flex-wrap:wrap;margin-top:10px}.diag-chip{background:var(--bg-hover);color:var(--text-muted);border-color:var(--border-soft)}.catalog-card{display:flex;gap:12px;position:relative}.catalog-card:before{content:'';position:absolute;left:0;top:0;bottom:0;width:3px;background:var(--brand-color);border-radius:8px 0 0 8px}.card-icon{width:34px;height:34px;min-width:34px;border-radius:8px;color:#fff;display:flex;align-items:center;justify-content:center;font-weight:700}.catalog-body{min-width:0;flex:1}.catalog-title{display:flex;justify-content:space-between;gap:8px}.card-id{font-size:11px;color:var(--text-dim);margin-top:2px}.card-link{font-size:12px;color:var(--primary);text-decoration:none}.empty-state{text-align:center;color:var(--text-muted);padding:30px}.empty-state.small{padding:12px}.oauth-conn-table{margin-top:16px}.error-cell{max-width:280px;word-break:break-all;color:var(--danger);font-size:12px}.btn-xs{padding:3px 9px;font-size:11px}.modal-overlay{position:fixed;inset:0;background:rgba(0,0,0,.52);display:flex;align-items:center;justify-content:center;z-index:1000}.modal-content{background:var(--bg-elevated);color:var(--text-primary);border:1px solid var(--border-soft);border-radius:12px;padding:22px;width:560px;max-width:92vw;max-height:88vh;overflow:hidden}.provider-modal{display:flex;flex-direction:column}.import-modal{width:680px}.modal-scroll{overflow:auto;padding-right:4px}.tab-bar{display:flex;border-bottom:1px solid var(--border-soft);margin-bottom:14px}.tab-btn{border:0;background:transparent;color:var(--text-muted);padding:8px 14px;cursor:pointer;border-bottom:2px solid transparent}.tab-btn.active{color:var(--primary);border-bottom-color:var(--primary)}.tab-badge{background:var(--primary);color:#fff;border-radius:999px;padding:1px 6px;margin-left:4px;font-size:10px}.form-group{margin-bottom:12px;position:relative}.form-group label{display:block;font-size:12px;color:var(--text-muted);margin-bottom:5px}.form-group input,.form-group select,.form-group textarea,.json-textarea{width:100%;box-sizing:border-box}.form-hint{font-size:12px;color:var(--text-muted);margin:5px 0 0}.modal-actions{display:flex;justify-content:flex-end;gap:10px;margin-top:16px}.key-item{border:1px solid var(--border-soft);border-radius:8px;padding:10px;margin-bottom:8px;background:var(--bg-hover)}.key-info,.key-actions,.key-revealed{display:flex;gap:8px;align-items:center}.key-revealed{margin-top:8px;background:var(--bg-code);padding:8px;border-radius:6px}.key-revealed code{flex:1;word-break:break-all}.add-key-form{border:1px dashed var(--border-medium);border-radius:8px;padding:14px;background:var(--bg-hover)}.input-side-btn{position:absolute;right:6px;top:27px}.danger-text{color:var(--danger)}.err-text{color:var(--danger);font-size:12px}.json-textarea{font-family:monospace;font-size:12px}
.btn-warn{background:var(--warn,#f59e0b);border:1px solid var(--warn,#f59e0b);color:#1a1206}.btn-warn:hover{filter:brightness(1.05)}.warn-text{color:var(--warn,#f59e0b);font-size:13px;margin:0 0 8px}.atom-steps{margin:10px 0 14px;padding-left:20px;color:var(--text-secondary);font-size:13px;line-height:1.7}.atom-steps code{background:var(--bg-card);padding:1px 5px;border-radius:4px}
</style>
