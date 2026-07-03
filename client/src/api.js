// API 调用封装
const BASE_URL = ''
async function apiGet(path) {
  const res = await fetch(`${BASE_URL}${path}`)
  if (!res.ok) {
    throw new Error(`HTTP ${res.status}: ${await res.text()}`)
  }
  return res.json()
}
async function apiPost(path, data) {
  const res = await fetch(`${BASE_URL}${path}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(data)
  })
  if (!res.ok) {
    throw new Error(`HTTP ${res.status}: ${await res.text()}`)
  }
  return res.json()
}
async function apiPut(path, data) {
  const res = await fetch(`${BASE_URL}${path}`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(data)
  })
  if (!res.ok) {
    throw new Error(`HTTP ${res.status}: ${await res.text()}`)
  }
  return res.json()
}
async function apiDelete(path) {
  const res = await fetch(`${BASE_URL}${path}`, {
    method: 'DELETE'
  })
  if (!res.ok) {
    throw new Error(`HTTP ${res.status}: ${await res.text()}`)
  }
  return res.json()
}
export {
  apiGet,
  apiPost,
  apiPut,
  apiDelete
}
export default {
  // 服务商
  getProviders: () => apiGet('/admin/api/providers'),
  createProvider: (data) => apiPost('/admin/api/providers', data),
  updateProvider: (id, data) => apiPut(`/admin/api/providers/${id}`, data),
  deleteProvider: (id) => apiDelete(`/admin/api/providers/${id}`),
  // 密钥
  getKeys: () => apiGet('/admin/api/keys'),
  revealKey: (id) => apiGet(`/admin/api/keys/${id}/reveal`),
  getAIGateKey: (reveal = false) => apiGet(`/admin/api/aigate-key${reveal ? '?reveal=true' : ''}`),
  createKey: (data) => apiPost('/admin/api/keys', data),
  deleteKey: (id) => apiDelete(`/admin/api/keys/${id}`),
  // 模型
  getModels: (params) => {
    let qs = new URLSearchParams()
    if (params && params.provider_id) qs.append('provider_id', params.provider_id)
    if (params && params.is_free !== null && params.is_free !== undefined) qs.append('is_free', params.is_free)
    if (params && params.auto_enabled !== null && params.auto_enabled !== undefined) qs.append('auto_enabled', params.auto_enabled)
    if (params && params.q) qs.append('q', params.q)
    return apiGet(`/admin/api/models?${qs.toString()}`)
  },
  updateModel: (id, data) => apiPut(`/admin/api/models/${id}`, data),
  deleteModel: (id) => apiDelete(`/admin/api/models/${id}`),
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
  getAutoRanking: () => apiGet('/admin/api/ranking/overall'),
  getLegacyAutoRanking: () => apiGet('/admin/api/auto/ranking'),
  getRoutingWeights: () => apiGet('/admin/api/routing/weights'),
  updateRoutingWeights: (data) => apiPut('/admin/api/routing/weights', data),
  // 请求日志
  getLogs: (params) => {
    const qs = new URLSearchParams()
    if (params.page) qs.append('page', params.page)
    if (params.page_size) qs.append('page_size', params.page_size)
    if (params.status) qs.append('status', params.status)
    return apiGet(`/admin/api/logs?${qs.toString()}`)
  },
  getLogDetail: (id) => apiGet(`/admin/api/logs/${id}`),
  getAnalyticsSummary: () => apiGet('/admin/api/analytics/summary'),
  // 日志归档
  listArchives: () => apiGet('/admin/api/logs/archives'),
  triggerArchive: () => apiPost('/admin/api/logs/archive', {}),
  restoreArchive: (filename) => apiPost(`/admin/api/logs/archives/${encodeURIComponent(filename)}/restore`, {}),
  deleteArchive: (filename) => apiDelete(`/admin/api/logs/archives/${encodeURIComponent(filename)}`),
  clearLogs: () => apiDelete('/admin/api/logs'),
  getCurrentModel: () => apiGet('/admin/api/current-model'),
  getHealthConfig: () => apiGet('/admin/api/health-config'),
  updateHealthConfig: (data) => apiPut('/admin/api/health-config', data),
  // playground
  playgroundChat: (data) => apiPost('/admin/api/playground', data),
  // 别名：支持 api.playground() 调用
  playground: (data) => apiPost('/admin/api/playground', data)
}