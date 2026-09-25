// API 调用封装
const BASE_URL = ''

function _getSession() {
  return localStorage.getItem('aigate_session') || ''
}

function _addAuthHeader(headers = {}) {
  const token = _getSession()
  if (token) {
    headers['Authorization'] = `Bearer ${token}`
  }
  return headers
}

function _handle401(res) {
  if (res.status === 401) {
    localStorage.removeItem('aigate_session')
    localStorage.removeItem('aigate_username')
    if (window.location.pathname !== '/login') {
      window.location.href = '/login'
    }
    return true
  }
  return false
}

async function apiGet(path, extraHeaders = {}) {
  const res = await fetch(`${BASE_URL}${path}`, { headers: { ..._addAuthHeader(), ...extraHeaders } })
  if (_handle401(res)) throw new Error('未登录或 session 已过期')
  if (!res.ok) {
    throw new Error(`HTTP ${res.status}: ${await res.text()}`)
  }
  return res.json()
}
async function apiPost(path, data, extraHeaders = {}) {
  const res = await fetch(`${BASE_URL}${path}`, {
    method: 'POST',
    headers: { ..._addAuthHeader({ 'Content-Type': 'application/json' }), ...extraHeaders },
    body: JSON.stringify(data)
  })
  if (_handle401(res)) throw new Error('未登录或 session 已过期')
  if (!res.ok) {
    throw new Error(`HTTP ${res.status}: ${await res.text()}`)
  }
  return res.json()
}
async function apiPut(path, data) {
  const res = await fetch(`${BASE_URL}${path}`, {
    method: 'PUT',
    headers: _addAuthHeader({ 'Content-Type': 'application/json' }),
    body: JSON.stringify(data)
  })
  if (_handle401(res)) throw new Error('未登录或 session 已过期')
  if (!res.ok) {
    throw new Error(`HTTP ${res.status}: ${await res.text()}`)
  }
  return res.json()
}
async function apiDelete(path) {
  const res = await fetch(`${BASE_URL}${path}`, {
    method: 'DELETE',
    headers: _addAuthHeader()
  })
  if (_handle401(res)) throw new Error('未登录或 session 已过期')
  if (!res.ok) {
    throw new Error(`HTTP ${res.status}: ${await res.text()}`)
  }
  return res.json()
}
async function apiPatch(path, data) {
  const res = await fetch(`${BASE_URL}${path}`, {
    method: 'PATCH',
    headers: _addAuthHeader({ 'Content-Type': 'application/json' }),
    body: JSON.stringify(data)
  })
  if (_handle401(res)) throw new Error('未登录或 session 已过期')
  if (!res.ok) {
    throw new Error(`HTTP ${res.status}: ${await res.text()}`)
  }
  return res.json()
}
async function apiDownload(path) {
  const res = await fetch(`${BASE_URL}${path}`, { headers: _addAuthHeader() })
  if (_handle401(res)) throw new Error('未登录或 session 已过期')
  if (!res.ok) throw new Error(`HTTP ${res.status}`)
  const blob = await res.blob()
  const cd = res.headers.get('Content-Disposition') || ''
  const m = cd.match(/filename=(.+?)(?:;|$)/)
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = m ? m[1] : 'download'
  a.click()
  URL.revokeObjectURL(url)
}
export {
  apiGet,
  apiPost,
  apiPut,
  apiDelete,
  apiPatch,
  apiDownload
}
export default {
  // 认证
  login: (username, password) => apiPost('/admin/api/auth/login', { username, password }),
  logout: () => apiPost('/admin/api/auth/logout', {}),
  checkAuth: () => apiGet('/admin/api/auth/check'),
  changePassword: (oldPass, newPass) => apiPut('/admin/api/auth/password', { old_password: oldPass, new_password: newPass }),
  // 一键更新
  checkUpdate: () => apiGet('/admin/api/update/check'),
  applyUpdate: () => apiPost('/admin/api/update/apply', {}),
  updateStatus: () => apiGet('/admin/api/update/status'),
  getUpdateBackups: () => apiGet('/admin/api/update/backups'),
  createUpdateBackup: () => apiPost('/admin/api/update/backups', {}),
  // 服务商
  getProviders: () => apiGet('/admin/api/providers'),
  getProviderModelStats: () => apiGet('/admin/api/providers/model-stats'),
  createProvider: (data) => apiPost('/admin/api/providers', data),
  updateProvider: (id, data) => apiPut(`/admin/api/providers/${id}`, data),
  deleteProvider: (id) => apiDelete(`/admin/api/providers/${id}`),
  // 一键备份 / 恢复（全系统）
  fullBackup: (adminPassword) => apiGet('/admin/api/backup', { 'X-Admin-Password': adminPassword || '' }),
  fullRestore: (data, adminPassword) => apiPost('/admin/api/restore', data, { 'X-Admin-Password': adminPassword || '' }),
  // 服务商配置导入 / 导出（P1-21：include_keys 含明文密钥，需管理员密码二次校验）
  exportProviders: (params = {}) => {
    const qs = new URLSearchParams()
    if (params.include_keys) qs.append('include_keys', 'true')
    if (params.provider_ids) qs.append('provider_ids', params.provider_ids)
    return apiGet(`/admin/api/providers/export?${qs.toString()}`,
      params.admin_password ? { 'X-Admin-Password': params.admin_password } : {})
  },
  importProviders: (data) => apiPost('/admin/api/providers/import', data),
  // 服务商定价导入（newapi / one-api 的 /api/pricing JSON）
  importProviderPricing: (id, jsonData) => apiPost(`/admin/api/providers/${id}/import-pricing`, { json_data: jsonData }),
  // AtomCode 直连反代：从默认路径 ~/.atomcode/auth.toml 解析鉴权 JSON
  loadAtomAuth: () => apiPost('/admin/api/atomcode/load-auth', {}),
  // AtomCode 本地 daemon 可执行文件：状态查询与 UI 配置
  getAtomExeStatus: () => apiGet('/admin/api/atomcode/exe-status'),
  setAtomExePath: (path) => apiPost('/admin/api/atomcode/set-exe-path', { path }),
  // 密钥（P1-21：揭示明文需管理员密码二次校验）
  getKeys: () => apiGet('/admin/api/keys'),
  revealKey: (id, adminPassword) => apiGet(`/admin/api/keys/${id}/reveal`,
    adminPassword ? { 'X-Admin-Password': adminPassword } : {}),
  getAIGateKey: (reveal = false, adminPassword) => apiGet(
    `/admin/api/aigate-key${reveal ? '?reveal=true' : ''}`,
    adminPassword ? { 'X-Admin-Password': adminPassword } : {}),
  createKey: (data) => apiPost('/admin/api/keys', data),
  deleteKey: (id) => apiDelete(`/admin/api/keys/${id}`),
  toggleKey: (id, isActive) => apiPost(`/admin/api/keys/${id}/toggle`, { is_active: isActive }),
  // 模型
  getModels: (params) => {
    let qs = new URLSearchParams()
    if (params && params.provider_id) qs.append('provider_id', params.provider_id)
    if (params && params.is_free !== null && params.is_free !== undefined) qs.append('is_free', params.is_free)
    if (params && params.auto_enabled !== null && params.auto_enabled !== undefined) qs.append('auto_enabled', params.auto_enabled)
    if (params && params.q) qs.append('q', params.q)
    if (params && params.limit) qs.append('limit', params.limit)
    if (params && params.offset) qs.append('offset', params.offset)
    return apiGet(`/admin/api/models?${qs.toString()}`)
  },
  updateModel: (id, data) => apiPut(`/admin/api/models/${id}`, data),
  // 分组复选：Auto + 所属 combo 名称列表（一个模型可属多个分组）
  setModelGroups: (id, data) => apiPut(`/admin/api/models/${id}/groups`, data),
  deleteModel: (id) => apiDelete(`/admin/api/models/${id}`),
  cleanOrphanModels: () => apiDelete('/admin/api/models/orphans'),
  createProviderModel: (providerId, data) => apiPost(`/admin/api/providers/${providerId}/models`, data),
  refreshModels: (providerId) => {
    const qs = providerId ? `?provider_id=${providerId}` : ''
    return apiPost(`/admin/api/models/refresh${qs}`, {})
  },
  // v2.0 手动测速
  pingModel: (modelId) => apiPost(`/admin/api/models/${modelId}/ping`, {}),
  pingAllModels: () => apiPost('/admin/api/models/ping-all', {}),
  getLatencyStats: () => apiGet('/admin/api/models/latency-stats'),
  // dashboard
  getDashboard: () => apiGet('/admin/api/dashboard'),
  // health
  getHealth: () => apiGet('/admin/api/health'),
  // auto ranking
  getAutoRanking: () => apiGet('/admin/api/ranking/overall?limit=1000'),
  getLegacyAutoRanking: () => apiGet('/admin/api/auto/ranking'),
  getRoutingWeights: () => apiGet('/admin/api/routing/weights'),
  updateRoutingWeights: (data) => apiPut('/admin/api/routing/weights', data),
  // 路由决策中心
  getRouteDecisions: (params = {}) => {
    const qs = new URLSearchParams()
    Object.entries(params).forEach(([key, value]) => {
      if (value !== undefined && value !== null && value !== '') qs.append(key, value)
    })
    return apiGet(`/admin/api/route-decisions?${qs.toString()}`)
  },
  getRouteDecision: (id) => apiGet(`/admin/api/route-decisions/${id}`),
  // 请求日志
  getLogs: (params) => {
    const qs = new URLSearchParams()
    if (params.page) qs.append('page', params.page)
    if (params.page_size) qs.append('page_size', params.page_size)
    if (params.status) qs.append('status', params.status)
    if (params.provider) qs.append('provider', params.provider)
    if (params.log_type) qs.append('log_type', params.log_type)
    return apiGet(`/admin/api/logs?${qs.toString()}`)
  },
  getLogDetail: (id, full = false) => apiGet(`/admin/api/logs/${id}${full ? '?full=1' : ''}`),
  getLogProviders: () => apiGet('/admin/api/logs/providers'),
  getAnalyticsSummary: () => apiGet('/admin/api/analytics/summary'),
  resetAnalyticsSummary: () => apiPost('/admin/api/analytics/summary/reset', {}),
  // 日志归档
  listArchives: () => apiGet('/admin/api/logs/archives'),
  triggerArchive: () => apiPost('/admin/api/logs/archive', {}),
  restoreArchive: (filename) => apiPost(`/admin/api/logs/archives/${encodeURIComponent(filename)}/restore`, {}),
  deleteArchive: (filename) => apiDelete(`/admin/api/logs/archives/${encodeURIComponent(filename)}`),
  clearLogs: () => apiDelete('/admin/api/logs'),
  getCurrentModel: () => apiGet('/admin/api/current-model'),
  // playground
  playgroundChat: (data) => apiPost('/admin/api/playground', data),
  // 别名：支持 api.playground() 调用
  playground: (data) => apiPost('/admin/api/playground', data),
  // v3.0 Combos 组合 CRUD
  getCombos: () => apiGet('/admin/api/combos'),
  getCombo: (id) => apiGet(`/admin/api/combos/${id}`),
  createCombo: (data) => apiPost('/admin/api/combos', data),
  updateCombo: (id, data) => apiPut(`/admin/api/combos/${id}`, data),
  deleteCombo: (id) => apiDelete(`/admin/api/combos/${id}`),
  // v3.0 RTK Token Saver 配置
  getTokenSaver: () => apiGet('/admin/api/token-saver'),
  updateTokenSaver: (enabled, minChars) => apiPut(`/admin/api/token-saver?enabled=${enabled}&min_chars=${minChars ?? ''}`),
  previewTokenSaver: (data) => apiPost('/admin/api/token-saver/preview', data),

  // ── 用量分析（配额已合并到分析页）──
  getAnalyticsToday: (params = {}) => {
    const qs = new URLSearchParams()
    if (params.start) qs.append('start', params.start)
    if (params.end) qs.append('end', params.end)
    if (params.provider) qs.append('provider', params.provider)
    if (params.model) qs.append('model', params.model)
    const s = qs.toString()
    return apiGet(`/admin/api/analytics/summary/today${s ? '?' + s : ''}`)
  },
  getAnalyticsTrend: (days = 7) => apiGet(`/admin/api/analytics/trend?days=${days}`),
  getAnalyticsByProvider: () => apiGet('/admin/api/analytics/by-provider'),

  // ── HTTP 代理池 ──
  getProxyPool: () => apiGet('/admin/api/proxy-pool'),
  updateProxyPool: (data) => apiPut('/admin/api/proxy-pool', data),
  getKeyRotatorStatus: () => apiGet('/admin/api/keys/rotator-status'),

  // ── Headroom ──
  getHeadroom: () => apiGet('/admin/api/headroom'),
  updateHeadroom: (entries) => apiPut('/admin/api/headroom', entries),

  // ── 高级 saver (caveman / ponytail) ──
  getSaverExtra: () => apiGet('/admin/api/token-saver-extra'),
  updateSaverExtra: (data) => apiPut('/admin/api/token-saver-extra', data),

  // ── 媒体生成：图片 ──
  generateImage: (data) => apiPost('/admin/api/media/image', data),

  // ── 媒体生成：视频 ──
  generateVideo: (data) => apiPost('/admin/api/media/video', data),

  // ── 请求诊断日志开关 ──
  getDiag: () => apiGet('/admin/api/diag'),
  setDiag: (verbose) => apiPut(`/admin/api/diag?verbose=${verbose}`),
  // ── 失败罚时 / 冷却总览 ──
  getCooling: () => apiGet('/admin/api/cooling'),
  clearModelCooling: (model_id) => apiPost('/admin/api/cooling/clear' + (model_id != null ? `?model_id=${model_id}` : '')),

  // ── OAuth 接入 ──
  getOAuthProviders: () => apiGet('/admin/oauth/providers'),
  getOAuthConnections: () => apiGet('/admin/oauth/connections'),
  startOAuthAuthorize: (code, opts = {}) => {
    const qs = new URLSearchParams()
    if (opts.owner) qs.append('owner', opts.owner)
    if (opts.newAccount) qs.append('new_account', 'true')
    const q = qs.toString()
    return apiPost(`/admin/oauth/authorize/${code}${q ? '?' + q : ''}`, {})
  },
  refreshOAuthConnection: (id) => apiPost(`/admin/oauth/refresh/${id}`),
  deleteOAuthConnection: (id) => apiDelete(`/admin/oauth/connections/${id}`),
  // 编辑连接（账号名 / 启用停用）；owner 同服务商下唯一，重名返回 409
  updateOAuthConnection: (id, data) => apiPatch(`/admin/oauth/connections/${id}`, data),
  importOAuthToken: (data) => apiPost('/admin/oauth/import-token', data),
  // 手动回调（portal 把回调锁死 127.0.0.1 的 provider，如 LobsterAI）
  completeManualOAuthCallback: (data) => apiPost('/admin/oauth/complete-callback', data),
  // 额度/余额查询（usage 端点缓存 5 分钟，force=true 强刷）
  getOAuthConnectionUsage: (id, force = false) =>
    apiGet(`/admin/oauth/connections/${id}/usage${force ? '?force=true' : ''}`),

  // ── v2 路线新增 ──
  // D1 网关密钥（下游客户端钥匙）
  getGatewayKeys: () => apiGet('/admin/api/gateway-keys'),
  createGatewayKey: (data) => apiPost('/admin/api/gateway-keys', data),
  updateGatewayKey: (id, data) => apiPatch(`/admin/api/gateway-keys/${id}`, data),
  deleteGatewayKey: (id) => apiDelete(`/admin/api/gateway-keys/${id}`),
  gatewayKeyUsage: (id) => apiGet(`/admin/api/gateway-keys/${id}/usage`),
  // E1 模型别名
  getAliases: () => apiGet('/admin/api/aliases'),
  createAlias: (data) => apiPost('/admin/api/aliases', data),
  updateAlias: (id, data) => apiPatch(`/admin/api/aliases/${id}`, data),
  deleteAlias: (id) => apiDelete(`/admin/api/aliases/${id}`),
  // B3 模型批量操作
  batchModels: (ids, action) => apiPost('/admin/api/models/batch', { ids, action }),
  // B4 一键诊断
  diagnose: (data) => apiPost('/admin/api/diagnose', data),
  // B2 失败分析
  getFailures: (hours = 24) => apiGet(`/admin/api/analytics/failures?hours=${hours}`),
  // B1 实时监控
  getLiveSummary: () => apiGet('/admin/api/live/summary'),
  // B5 日志导出
  exportLogs: (params = {}) => {
    const qs = new URLSearchParams({ format: params.format || 'csv', hours: params.hours || 168 })
    if (params.status) qs.append('status', params.status)
    if (params.provider) qs.append('provider', params.provider)
    return apiDownload(`/admin/api/logs/export?${qs.toString()}`)
  },
  // D3 通知
  getNotify: () => apiGet('/admin/api/notify'),
  updateNotify: (data) => apiPut('/admin/api/notify', data),
  testNotify: () => apiPost('/admin/api/notify/test', {}),
  // 流口水自动冷却 / 候选竞速
  getDroolGuard: () => apiGet('/admin/api/drool-guard'),
  updateDroolGuard: (data) => apiPut('/admin/api/drool-guard', data),
  getRace: () => apiGet('/admin/api/race'),
  updateRace: (data) => apiPut('/admin/api/race', data),
  // 模型刷新：超时与并发配置（2026-09 并发化）
  getModelRefresh: () => apiGet('/admin/api/model-refresh'),
  updateModelRefresh: (data) => apiPut('/admin/api/model-refresh', data),
  // 每日签到（领取上游免费积分/额度）+ 额度监控
  getCheckinOverview: (withUsage = true) =>
    apiGet(`/admin/api/checkin/overview${withUsage ? '' : '?with_usage=false'}`),
  runCheckin: (providerCode) =>
    apiPost('/admin/api/checkin/run', providerCode ? { provider_code: providerCode } : {}),
  getCheckinLogs: (days = 7) => apiGet(`/admin/api/checkin/logs?days=${days}`),
  getCheckinConfig: () => apiGet('/admin/api/checkin/config'),
  updateCheckinConfig: (data) => apiPut('/admin/api/checkin/config', data),
  // OpenCode 桥接（官方 CLI sidecar）
  getOpenCodeBridge: () => apiGet('/admin/api/opencode'),
  updateOpenCodeBridge: (data) => apiPut('/admin/api/opencode', data),
  restartOpenCodeBridge: () => apiPost('/admin/api/opencode/restart', {}),
  // D4 价格健康
  getPriceHealth: () => apiGet('/admin/api/price-health'),
  // C3 数据库备份文件
  getBackupFiles: () => apiGet('/admin/api/backup/files'),
  runBackupNow: () => apiPost('/admin/api/backup/files', {}),
  // C4 版本
  getVersion: () => apiGet('/admin/api/version'),
  // A4 响应缓存
  getCacheInfo: () => apiGet('/admin/api/cache'),
  clearCache: () => apiDelete('/admin/api/cache'),
}
