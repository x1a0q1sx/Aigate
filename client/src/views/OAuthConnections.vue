<template>
  <div class="oauth-page">
    <PageHeader
      title="OAuth 连接"
      subtitle="连接支持 OAuth 的服务商（设备流一键连接 / 浏览器授权 / 手动导入 token）。本页不在常规界面提供入口，直接访问 /providers/oauth。"
      icon="shield"
    >
      <template #actions>
        <input v-model.trim="search" placeholder="搜索服务商" class="search-input" />
        <router-link to="/providers/checkin" class="btn btn-outline">签到监控 →</router-link>
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
          <template v-if="renamingId === c.id">
            <input v-model.trim="renameValue" class="rename-input" maxlength="100"
                   placeholder="账号名（同服务商内唯一）" @keyup.enter="submitRename(c)"
                   @keyup.esc="renamingId = null" />
            <button class="btn btn-ghost btn-xs" @click="submitRename(c)" :disabled="!!renaming">保存</button>
            <button class="btn btn-ghost btn-xs" @click="renamingId = null">取消</button>
          </template>
          <template v-else>
            <span class="conn-owner" :title="'连接 id ' + c.id + '，可改名区分多账号'">{{ c.owner }}</span>
            <span class="badge" :class="connStatus(c).cls">{{ connStatus(c).label }}</span>
            <span class="conn-expire text-xs" :title="'到期 ' + (c.expires_at || '未知')">{{ expireText(c) }}</span>
            <span class="conn-actions">
              <button class="btn btn-ghost btn-xs" @click="loadUsage(c, false)">查询余额</button>
              <button class="btn btn-ghost btn-xs" @click="startRename(c)">改名</button>
              <button class="btn btn-ghost btn-xs" @click="refreshConn(c)" :disabled="busyConn === c.id">强制刷新</button>
              <button class="btn btn-ghost btn-xs danger-text" @click="removeConn(c)">断开</button>
            </span>
          </template>
          <div v-if="c.last_error" class="conn-error text-xs" :title="c.last_error">最后错误：{{ c.last_error.slice(0, 120) }}</div>
          <div v-if="usage[c.id]" class="usage-panel">
            <div v-if="usage[c.id].loading" class="text-xs muted">额度查询中...</div>
            <template v-else>
              <div class="usage-head text-xs">
                <strong>{{ (usage[c.id].data && usage[c.id].data.plan) || c.provider_code }}</strong>
                <span v-if="usage[c.id].data && usage[c.id].data.cached" class="muted">（缓存，5 分钟）</span>
                <button v-if="usage[c.id].data && !usage[c.id].loading" class="btn btn-ghost btn-xs" @click="loadUsage(c, true)">强刷</button>
              </div>
              <div v-for="(q, name) in quotaRows(usage[c.id].data)" :key="name" class="quota-row">
                <span class="quota-name" :title="name">{{ q.display_name && q.display_name !== name ? name + ' · ' + q.display_name : name }}</span>
                <div class="quota-bar"><i :class="{ warn: quotaPct(q) > 85, hot: quotaPct(q) >= 100 }" :style="{ width: quotaPct(q) + '%' }"></i></div>
                <span class="quota-val">{{ quotaText(q) }}</span>
              </div>
              <div v-if="usageMsg(usage[c.id].data)" class="usage-msg text-xs">{{ usageMsg(usage[c.id].data) }}</div>
              <div v-if="usage[c.id].error" class="usage-msg text-xs">{{ usage[c.id].error }}</div>
            </template>
          </div>
        </div>

        <div class="oauth-card-actions">
          <button class="btn btn-primary btn-xs" @click="connect(p)" :disabled="busyCode === p.code">
            {{ busyCode === p.code ? '处理中...' : (flowOf(p).label === '导入' ? '获取引导' : (connectionsOf(p.code).length ? '新增账号' : '一键连接')) }}
          </button>
          <button v-if="connectionsOf(p.code).length" class="btn btn-outline btn-xs"
                  @click="connectAsNew(p)" :disabled="busyCode === p.code"
                  title="强制新建一个账号连接（保留现有账号不覆盖）">
            <AppIcon name="plus" :size="11" />强制新增
          </button>
          <button class="btn btn-outline btn-xs" @click="openImportTokenModal(p.code)">导入 Token</button>
        </div>
      </article>
    </div>

    <p class="foot-tip text-xs">
      提示：连接成功后系统自动登记服务商（服务商列表即可见，credential_type=oauth），在服务商页刷新模型后即可路由流量。
      「查询余额」覆盖 claude_code / codex / github_copilot / antigravity / codebuddy 国服与国际服 / qoder / u1s1
      （对齐 9router 的 usage 支持范围；其余服务商上游没有公开额度接口）。
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
import AppIcon from '../components/AppIcon.vue'

export default {
  name: 'OAuthConnections',
  components: { PageHeader, AppModal, AppIcon },
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
      usage: {},          // connectionId -> { loading, data?, error? }
      renamingId: null,   // 正在改名的连接 id
      renameValue: '',
      renaming: false,
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
    // 强制新建账号（已有账号时，不覆盖现有连接）
    async connectAsNew(p) {
      this.busyCode = p.code
      try {
        const r = await api.startOAuthAuthorize(p.code, { newAccount: true })
        if (r.device_poll && r.login_url) {
          window.open(r.login_url, '_blank', 'noopener,noreferrer,width=900,height=720')
          toast.info('请在新窗口登录【另一个账号】，成功后自动新增一条连接')
          this.watchDeviceFlow(p.code)
        } else if (r.authorize_url) {
          window.open(r.authorize_url, '_blank', 'noopener,noreferrer,width=900,height=720')
          toast.info('请在新窗口用【另一个账号】授权，授权后返回本页')
        } else {
          toast.error('该服务商没有可用的授权入口，请使用「导入 Token」方式')
        }
      } catch (e) {
        toast.error('发起授权失败：' + e.message)
      } finally {
        this.busyCode = null
      }
    },
    // 账号改名
    startRename(c) {
      this.renamingId = c.id
      this.renameValue = c.owner
    },
    async submitRename(c) {
      const name = (this.renameValue || '').trim()
      if (!name) { toast.error('账号名不能为空'); return }
      if (name === c.owner) { this.renamingId = null; return }
      this.renaming = true
      try {
        await api.updateOAuthConnection(c.id, { owner: name })
        this.renamingId = null
        await this.load()
        toast.success('已改名为「' + name + '」')
      } catch (e) {
        toast.error('改名失败：' + e.message)
      } finally {
        this.renaming = false
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
        delete this.usage[c.id]
        await this.load()
        toast.success('已断开')
      } catch (e) {
        toast.error('断开失败：' + e.message)
      }
    },
    // ── 余额/额度面板 ──
    async loadUsage(c, force) {
      this.usage = { ...this.usage, [c.id]: { loading: true, data: this.usage[c.id]?.data || null } }
      try {
        const data = await api.getOAuthConnectionUsage(c.id, !!force)
        this.usage = { ...this.usage, [c.id]: { loading: false, data } }
      } catch (e) {
        this.usage = { ...this.usage, [c.id]: { loading: false, data: null, error: '查询失败：' + e.message } }
      }
    },
    quotaRows(entry) {
      return (entry && entry.quotas) || {}
    },
    usageMsg(entry) {
      return (entry && entry.message) || ''
    },
    quotaPct(q) {
      if (q.unlimited) return 0
      const total = Number(q.total) || 0
      if (!total) return 0
      return Math.max(0, Math.min(100, (Number(q.used) || 0) / total * 100))
    },
    quotaText(q) {
      if (q.unlimited) return '不限量'
      const money = (v) => '$' + (Number(v) || 0).toFixed(2)
      if (q.unit === 'USD') {
        const bal = q.remaining != null ? q.remaining : q.total
        return `剩 ${money(bal)}` + (q.display_name ? `（${q.display_name}）` : '')
      }
      const isPct = Math.round(Number(q.total) || 0) === 100 && !q.unit
      let text
      if (isPct) text = `已用 ${Math.round(Number(q.used) || 0)}%`
      else {
        const u = Math.round(Number(q.used) || 0)
        const t = Math.round(Number(q.total) || 0)
        text = `${u.toLocaleString()}/${t.toLocaleString()}` + (q.unit ? ` ${q.unit}` : '')
        if (q.display_name && !isPct && q.unit !== 'USD') text += `（${q.display_name}）`
      }
      if (q.reset_at) {
        const tl = this._timeLeft(q.reset_at)
        text += ` · ${q.recurring ? '重置' : '到期'} ${tl}`
      }
      return text
    },
    _timeLeft(iso) {
      const ms = new Date(iso).getTime() - Date.now()
      if (!Number.isFinite(ms)) return iso
      if (ms <= 0) return '已过'
      const d = Math.floor(ms / 86400000)
      const h = Math.floor((ms % 86400000) / 3600000)
      const m = Math.floor((ms % 3600000) / 60000)
      if (d >= 7) return d + ' 天后'
      if (d > 0) return d + ' 天 ' + h + ' 时后'
      if (h > 0) return h + ' 时 ' + m + ' 分后'
      return m + ' 分后'
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
  /* --card-color 并不存在，旧代码命中深色 fallback 导致白天模式卡片恒为黑底 */
  background: var(--bg-card, var(--bg-surface, #0f1623));
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
.rename-input {
  flex: 1 1 140px;
  min-width: 120px;
  padding: 3px 8px;
  font-size: 12px;
  border: 1px solid var(--border-color, #1c2839);
  border-radius: 4px;
  background: var(--bg-input, var(--bg-card, #0f1623));
  color: inherit;
}
.foot-tip { margin-top: 18px; color: var(--text-muted, #7e8ea9); line-height: 1.7; }
.muted { color: var(--text-muted, #7e8ea9); font-weight: 400; }
.usage-panel {
  width: 100%;
  border-top: 1px dashed var(--border-color, #1c2839);
  margin-top: 4px;
  padding-top: 8px;
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.usage-head { display: flex; align-items: center; gap: 8px; }
.quota-row {
  display: grid;
  grid-template-columns: minmax(80px, 1.4fr) 1fr minmax(120px, auto);
  align-items: center;
  gap: 8px;
  font-size: 12px;
}
.quota-name {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  color: var(--text-muted, #a9b7cd);
}
.quota-bar {
  height: 6px;
  border-radius: 3px;
  background: rgba(126, 142, 169, 0.18);
  overflow: hidden;
}
.quota-bar i {
  display: block;
  height: 100%;
  background: var(--primary, #5b8dff);
  border-radius: 3px;
  transition: width 0.25s;
}
.quota-bar i.warn { background: #fbbf24; }
.quota-bar i.hot { background: #f87171; }
.quota-val { text-align: right; white-space: nowrap; }
.usage-msg { color: var(--text-muted, #7e8ea9); }
</style>
