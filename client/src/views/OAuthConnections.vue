<template>
  <div class="oauth-page">
    <PageHeader
      title="OAuth 连接"
      subtitle="连接支持 OAuth 的服务商（设备流一键连接 / 浏览器授权 / 手动导入 token）。本页不在常规界面提供入口，直接访问 /providers/oauth。"
      icon="shield"
    >
      <template #actions>
        <input v-model.trim="search" placeholder="搜索服务商" class="search-input" />
        <button class="btn btn-outline" @click="load" :disabled="loading">刷新</button>
      </template>
    </PageHeader>

    <div v-if="errorText" class="alert alert-error">{{ errorText }}</div>

    <div v-if="!filtered.length" class="empty-hint">没有匹配的 OAuth 服务商。</div>

    <div class="oauth-grid">
      <article v-for="p in filtered" :key="p.code" class="oauth-card" :class="{ connected: connectionsOf(p.code).length }">
        <div class="oauth-card-head">
          <div>
            <strong>{{ p.name }}</strong>
            <span class="badge badge-neutral code-tag">{{ p.code }}</span>
            <span class="badge" :class="flowOf(p).cls">{{ flowOf(p).label }}</span>
          </div>
          <span v-if="connectionsOf(p.code).length" class="badge badge-success">已连接 {{ connectionsOf(p.code).length }}</span>
          <span v-else class="badge badge-neutral">未连接</span>
        </div>
        <div class="oauth-meta mono text-xs">{{ p.api_base_url || '-' }}</div>
        <div v-if="p.notes" class="oauth-notes text-xs">{{ p.notes }}</div>

        <div v-for="c in connectionsOf(p.code)" :key="c.id" class="conn-row">
          <span class="conn-owner">{{ c.owner }}</span>
          <span class="badge" :class="connStatus(c).cls">{{ connStatus(c).label }}</span>
          <span class="conn-expire text-xs" :title="'到期 ' + (c.expires_at || '未知')">{{ expireText(c) }}</span>
          <span class="conn-actions">
            <button class="btn btn-ghost btn-xs" @click="refreshConn(c)" :disabled="busyConn === c.id">强制刷新</button>
            <button class="btn btn-ghost btn-xs danger-text" @click="removeConn(c)">断开</button>
          </span>
          <div v-if="c.last_error" class="conn-error text-xs" :title="c.last_error">最后错误：{{ c.last_error.slice(0, 120) }}</div>
        </div>

        <div class="oauth-card-actions">
          <button class="btn btn-primary btn-xs" @click="connect(p)" :disabled="busyCode === p.code">
            {{ busyCode === p.code ? '处理中...' : (flowOf(p).label === '导入' ? '获取引导' : '一键连接') }}
          </button>
          <button class="btn btn-outline btn-xs" @click="openImportTokenModal(p.code)">导入 Token</button>
        </div>
      </article>
    </div>

    <p class="foot-tip text-xs">
      提示：连接成功后，需在「服务商」页新建服务商（credential_type=oauth，通过 API 创建），或直接导入含 OAuth 连接的备份。
      设备流/导入类服务商的 token 长期有效时页面显示"长效"。
    </p>

    <!-- 导入 token -->
    <AppModal v-model="importModal.show" :title="'导入 ' + importModal.provider_code + ' token'" icon="shield" size="md">
      <div class="form-group">
        <label class="form-label">Access Token *</label>
        <textarea v-model="importModal.access_token" rows="3"></textarea>
      </div>
      <div class="form-group">
        <label class="form-label">Refresh Token（可留空）</label>
        <textarea v-model="importModal.refresh_token" rows="2"></textarea>
      </div>
      <div class="form-row">
        <div class="form-group">
          <label class="form-label">过期秒数</label>
          <input type="number" v-model.number="importModal.expires_in" min="60" />
        </div>
        <div class="form-group">
          <label class="form-label">Owner</label>
          <input v-model="importModal.owner" />
        </div>
      </div>
      <p v-if="importModal.error" class="alert alert-error text-sm">{{ importModal.error }}</p>
      <template #footer>
        <button class="btn btn-outline" @click="importModal.show = false">取消</button>
        <button class="btn btn-primary" @click="submitImportToken" :disabled="importModal.busy">
          {{ importModal.busy ? '导入中...' : '确认导入' }}
        </button>
      </template>
    </AppModal>
  </div>
</template>

<script>
import api from '../api.js'
import toast from '../toast.js'
import PageHeader from '../components/PageHeader.vue'
import AppModal from '../components/AppModal.vue'

export default {
  name: 'OAuthConnections',
  components: { PageHeader, AppModal },
  data() {
    return {
      providers: [],
      connections: [],
      search: '',
      loading: false,
      errorText: '',
      busyCode: null,
      busyConn: null,
      importModal: { show: false, provider_code: '', access_token: '', refresh_token: '', expires_in: 3600, owner: '__default', busy: false, error: '' },
      _deviceWatcher: null,
    }
  },
  computed: {
    filtered() {
      const q = this.search.toLowerCase()
      if (!q) return this.providers
      return this.providers.filter((p) =>
        [p.code, p.name, p.api_base_url, p.notes].some((v) => String(v || '').toLowerCase().includes(q))
      )
    },
  },
  mounted() {
    this.load()
    // 浏览器授权/拒绝后回调自动落回本页（/providers/oauth?oauth=success|error&msg=...）
    try {
      const params = new URLSearchParams(window.location.search)
      const flag = params.get('oauth')
      if (flag === 'success') {
        toast.success('OAuth 授权完成，token 已保存（稍后请求即自动使用）')
        window.history.replaceState({}, '', window.location.pathname)
        this.load()
      } else if (flag) {
        toast.error('OAuth 授权失败：' + (params.get('msg') || flag))
        window.history.replaceState({}, '', window.location.pathname)
      }
    } catch (e) { /* 老浏览器忽略 */ }
  },
  beforeUnmount() {
    if (this._deviceWatcher) clearInterval(this._deviceWatcher)
  },
  methods: {
    async load() {
      this.loading = true
      this.errorText = ''
      try {
        const [providers, connections] = await Promise.all([
          api.getOAuthProviders(),
          api.getOAuthConnections(),
        ])
        this.providers = providers || []
        this.connections = connections || []
      } catch (e) {
        this.errorText = '加载失败：' + e.message
      } finally {
        this.loading = false
      }
    },
    connectionsOf(code) {
      return this.connections.filter((c) => c.provider_code === code)
    },
    flowOf(p) {
      const ep = p.extra_params || {}
      if (ep.auth_mode === 'device_poll' || ep.auth_mode === 'u1s1_device') return { label: '设备流', cls: 'badge-info' }
      if (ep.device_code_only) return { label: '导入', cls: 'badge-warning' }
      return { label: '浏览器授权', cls: 'badge-neutral' }
    },
    connStatus(c) {
      if (!c.is_active) return { label: '已停用', cls: 'badge-error' }
      if (c.expires_at && new Date(c.expires_at).getTime() < Date.now()) return { label: '已过期', cls: 'badge-warning' }
      return { label: '正常', cls: 'badge-success' }
    },
    expireText(c) {
      if (!c.expires_at) return '长效'
      const ms = new Date(c.expires_at).getTime() - Date.now()
      if (ms < 0) return '已过期'
      const d = Math.floor(ms / 86400000)
      const h = Math.floor((ms % 86400000) / 3600000)
      const m = Math.floor((ms % 3600000) / 60000)
      if (d >= 7) return '剩余 ' + d + ' 天'
      if (d > 0) return '剩余 ' + d + ' 天 ' + h + ' 时'
      if (h > 0) return '剩余 ' + h + ' 时 ' + m + ' 分'
      return '剩余 ' + m + ' 分'
    },
    async connect(p) {
      this.busyCode = p.code
      try {
        const r = await api.startOAuthAuthorize(p.code)
        if (r.device_poll && r.login_url) {
          window.open(r.login_url, '_blank', 'noopener,noreferrer,width=900,height=720')
          toast.info(r.message || '请在新窗口完成登录，成功后系统自动收取 token')
          this.watchDeviceFlow(p.code)
        } else if (r.authorize_url) {
          window.open(r.authorize_url, '_blank', 'noopener,noreferrer,width=900,height=720')
          toast.info('请在新窗口完成授权，授权后将自动返回本页')
        } else if (r.device_code) {
          toast.info(r.message || '该服务商走设备码流程，请从官方客户端获取 token 后用「导入 Token」录入')
        } else {
          toast.error('该服务商没有可用的授权入口，请使用「导入 Token」方式')
        }
      } catch (e) {
        toast.error('发起授权失败：' + e.message)
      } finally {
        this.busyCode = null
      }
    },
    // 设备流：后端在轮询上游，前端只需盯连接数变化
    watchDeviceFlow(code) {
      const before = this.connectionsOf(code).length
      if (this._deviceWatcher) clearInterval(this._deviceWatcher)
      const deadline = Date.now() + 6 * 60 * 1000
      this._deviceWatcher = setInterval(async () => {
        if (Date.now() > deadline) {
          clearInterval(this._deviceWatcher)
          this._deviceWatcher = null
          toast.error('设备流超时：未在 6 分钟内检测到新连接，可重试或用导入 Token')
          return
        }
        try {
          const rows = await api.getOAuthConnections()
          const now = (rows || []).filter((c) => c.provider_code === code).length
          if (now > before) {
            clearInterval(this._deviceWatcher)
            this._deviceWatcher = null
            this.connections = rows || this.connections
            toast.success(code + ' 连接成功，token 已自动保存')
          }
        } catch (e) { /* 轮询失败等下一轮 */ }
      }, 3000)
    },
    openImportTokenModal(code) {
      this.importModal = { show: true, provider_code: code, access_token: '', refresh_token: '', expires_in: 3600, owner: '__default', busy: false, error: '' }
    },
    async submitImportToken() {
      if (!this.importModal.access_token.trim()) {
        this.importModal.error = 'access_token 不能为空'
        return
      }
      this.importModal.busy = true
      this.importModal.error = ''
      try {
        await api.importOAuthToken({
          provider_code: this.importModal.provider_code,
          access_token: this.importModal.access_token.trim(),
          refresh_token: this.importModal.refresh_token.trim(),
          expires_in: this.importModal.expires_in || 3600,
          owner: this.importModal.owner || '__default',
        })
        this.importModal.show = false
        await this.load()
        toast.success('导入成功')
      } catch (e) {
        this.importModal.error = '导入失败：' + e.message
      } finally {
        this.importModal.busy = false
      }
    },
    async refreshConn(c) {
      this.busyConn = c.id
      try {
        const r = await api.refreshOAuthConnection(c.id)
        await this.load()
        if (r && r.ok) toast.success('刷新成功：' + (r.message || 'OK'))
        else toast.error('刷新失败：' + ((r && r.message) || 'unknown'))
      } catch (e) {
        toast.error('刷新失败：' + e.message)
      } finally {
        this.busyConn = null
      }
    },
    async removeConn(c) {
      if (!confirm(`确认断开 ${c.provider_code}（owner=${c.owner}）的连接？token 将被删除。`)) return
      try {
        await api.deleteOAuthConnection(c.id)
        await this.load()
        toast.success('已断开')
      } catch (e) {
        toast.error('断开失败：' + e.message)
      }
    },
  },
}
</script>

<style scoped>
.search-input {
  min-width: 180px;
}
.empty-hint {
  color: var(--text-muted, #7e8ea9);
  padding: 24px 0;
}
.oauth-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(320px, 1fr));
  gap: 14px;
  margin-top: 14px;
}
.oauth-card {
  border: 1px solid var(--border-color, #1c2839);
  border-radius: 10px;
  background: var(--card-color, #0f1623);
  padding: 14px;
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.oauth-card.connected { border-color: rgba(52, 211, 153, 0.35); }
.oauth-card-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  flex-wrap: wrap;
}
.code-tag { margin-left: 6px; }
.oauth-meta { color: var(--text-muted, #7e8ea9); word-break: break-all; }
.oauth-notes { color: var(--text-muted, #7e8ea9); }
.conn-row {
  border-top: 1px dashed var(--border-color, #1c2839);
  padding-top: 8px;
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}
.conn-owner { font-weight: 600; }
.conn-expire { color: var(--text-muted, #7e8ea9); }
.conn-actions { margin-left: auto; display: flex; gap: 4px; }
.conn-error {
  width: 100%;
  color: #f87171;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.oauth-card-actions {
  display: flex;
  gap: 8px;
  border-top: 1px dashed var(--border-color, #1c2839);
  padding-top: 10px;
}
.danger-text { color: #f87171; }
.foot-tip { margin-top: 18px; color: var(--text-muted, #7e8ea9); line-height: 1.7; }
</style>
