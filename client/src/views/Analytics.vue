<template>
  <div>
    <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 20px;">
      <div>
        <h1>分析 📊</h1>
        <p style="color: var(--gray-500); font-size: 14px; margin-top: 4px;">请求量、延迟、Token 用量和失败统计</p>
      </div>
      <button class="btn btn-outline" @click="loadAll">刷新</button>
    </div>

    <!-- 统计卡片 -->
    <div class="stats-grid" v-if="summary">
      <div class="stat-card">
        <div class="stat-label">总请求数</div>
        <div class="stat-number">{{ formatNum(summary.total_requests) }}</div>
        <div class="stat-sub">auto路由 {{ summary.auto_requests }} · 直连 {{ summary.direct_requests }}</div>
      </div>
      <div class="stat-card">
        <div class="stat-label">成功率</div>
        <div class="stat-number" :style="{color: summary.success_rate >= 95 ? 'var(--success)' : summary.success_rate >= 80 ? 'var(--warning)' : 'var(--danger)'}">{{ summary.success_rate }}%</div>
      </div>
      <div class="stat-card">
        <div class="stat-label">输入 Token</div>
        <div class="stat-number">{{ formatTokens(summary.total_input_tokens) }}</div>
      </div>
      <div class="stat-card">
        <div class="stat-label">输出 Token</div>
        <div class="stat-number">{{ formatTokens(summary.total_output_tokens) }}</div>
      </div>
      <div class="stat-card">
        <div class="stat-label">平均延迟</div>
        <div class="stat-number">{{ (summary.avg_latency_ms / 1000).toFixed(1) }}s</div>
      </div>
      <div class="stat-card">
        <div class="stat-label">总请求数</div>
        <div class="stat-number">{{ formatNum(summary.success_count) }}</div>
        <div class="stat-sub">成功请求</div>
      </div>
    </div>

    <!-- 请求日志 -->
    <div class="card" style="margin-top: 20px;">
      <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 12px;">
        <h2>请求日志</h2>
        <select v-model="filterStatus" @change="loadPage(1)" style="width: auto;">
          <option value="">全部</option>
          <option value="success">成功</option>
          <option value="error">失败</option>
        </select>
      </div>
      <table>
        <thead>
          <tr>
            <th>时间</th>
            <th>请求模型</th>
            <th>路由到</th>
            <th>状态</th>
            <th>延迟</th>
            <th>Token</th>
            <th>操作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-if="items.length === 0">
            <td colspan="7" style="text-align: center; padding: 32px; color: var(--gray-500);">暂无请求日志</td>
          </tr>
          <tr v-for="r in items" :key="r.id">
            <td style="font-size: 12px; white-space: nowrap;">{{ fmtTime(r.created_at) }}</td>
            <td style="font-family: monospace; font-size: 12px;">{{ r.requested_model || '-' }}</td>
            <td style="font-family: monospace; font-size: 12px;">
              <span v-if="r.routed_provider">{{ r.routed_provider }}/{{ r.routed_model }}</span>
              <span v-else style="color: #999;">-</span>
            </td>
            <td>
              <span :class="['badge', r.status === 'success' ? 'badge-success' : 'badge-danger']" style="font-size: 11px;">{{ r.status === 'success' ? '成功' : '失败' }}</span>
            </td>
            <td style="font-family: monospace;">{{ r.latency_ms ? (r.latency_ms / 1000).toFixed(1) + 's' : '-' }}</td>
            <td style="font-family: monospace; font-size: 12px;">{{ r.prompt_tokens || 0 }}/{{ r.completion_tokens || 0 }}</td>
            <td><button class="btn btn-outline btn-sm" @click="showDetail(r)">详情</button></td>
          </tr>
        </tbody>
      </table>
      <div style="display: flex; justify-content: space-between; align-items: center; padding: 12px 0;">
        <span style="color: var(--gray-500); font-size: 13px;">共 {{ total }} 条 · 第 {{ page }} / {{ totalPages }} 页</span>
        <div style="display: flex; gap: 8px;">
          <button class="btn btn-outline btn-sm" :disabled="page <= 1" @click="loadPage(page - 1)">上一页</button>
          <button class="btn btn-outline btn-sm" :disabled="page >= totalPages" @click="loadPage(page + 1)">下一页</button>
        </div>
      </div>
    </div>

    <!-- 日志归档管理 -->
    <div class="card" style="margin-top: 20px;">
      <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 12px;">
        <h2>日志归档管理 📦</h2>
        <div style="display: flex; gap: 8px;">
          <button class="btn btn-outline btn-sm" :disabled="archiveBusy" @click="doArchive">
            {{ archiveBusy ? '归档中...' : '📥 手动归档' }}
          </button>
          <button class="btn btn-sm" style="background: var(--danger); color: #fff;" :disabled="archiveBusy" @click="doClearLogs">
            🗑 清空日志
          </button>
        </div>
      </div>
      <p style="color: var(--gray-500); font-size: 13px; margin-bottom: 12px;">
        每天凌晨 2:00 自动将昨日日志归档为 gzip 压缩文件。也可手动归档，随时解压恢复或永久删除。
      </p>
      <table v-if="archives.length > 0">
        <thead>
          <tr>
            <th>归档文件</th>
            <th>日期范围</th>
            <th>记录数</th>
            <th>文件大小</th>
            <th>归档时间</th>
            <th>操作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="a in archives" :key="a.filename">
            <td style="font-family: monospace; font-size: 12px;">{{ a.filename }}</td>
            <td>{{ a.date_from }} ~ {{ a.date_to }}</td>
            <td>{{ formatNum(a.count) }}</td>
            <td>{{ formatSize(a.size_bytes) }}</td>
            <td style="font-size: 12px;">{{ fmtTime(a.archived_at) }}</td>
            <td>
              <div style="display: flex; gap: 4px;">
                <button class="btn btn-outline btn-sm" @click="doRestore(a)" :disabled="archiveBusy">恢复</button>
                <button class="btn btn-outline btn-sm" style="color: var(--danger);" @click="doDeleteArchive(a)" :disabled="archiveBusy">删除</button>
              </div>
            </td>
          </tr>
        </tbody>
      </table>
      <p v-else style="text-align: center; padding: 24px; color: var(--gray-500); font-size: 13px;">暂无归档文件</p>
    </div>

    <!-- 详情弹窗 -->
    <div v-if="detailRow" class="modal-overlay" @click.self="detailRow = null">
      <div class="modal-content" style="max-width: 900px;">
        <h3>请求详情</h3>
        <table style="width: 100%; margin: 16px 0;">
          <tr><td style="padding: 6px 8px; color: var(--gray-500); width: 100px;">时间</td><td>{{ fmtTime(detailRow.created_at) }}</td></tr>
          <tr><td style="padding: 6px 8px; color: var(--gray-500);">请求模型</td><td>{{ detailRow.requested_model || '-' }}</td></tr>
          <tr><td style="padding: 6px 8px; color: var(--gray-500);">路由服务商</td><td>{{ detailRow.routed_provider || '-' }}</td></tr>
          <tr><td style="padding: 6px 8px; color: var(--gray-500);">路由模型</td><td>{{ detailRow.routed_model || '-' }}</td></tr>
          <tr><td style="padding: 6px 8px; color: var(--gray-500);">状态</td><td>{{ detailRow.status }}</td></tr>
          <tr><td style="padding: 6px 8px; color: var(--gray-500);">延迟</td><td>{{ detailRow.latency_ms ? (detailRow.latency_ms / 1000).toFixed(1) + 's' : '-' }}</td></tr>
          <tr><td style="padding: 6px 8px; color: var(--gray-500);">Prompt Token</td><td>{{ detailRow.prompt_tokens || 0 }}</td></tr>
          <tr><td style="padding: 6px 8px; color: var(--gray-500);">Completion Token</td><td>{{ detailRow.completion_tokens || 0 }}</td></tr>
          <tr><td style="padding: 6px 8px; color: var(--gray-500);">回退次数</td><td>{{ detailRow.fallback_count || 0 }}</td></tr>
          <tr><td style="padding: 6px 8px; color: var(--gray-500);">IP</td><td>{{ detailRow.user_ip || '-' }}</td></tr>
          <tr v-if="detailRow.error_type"><td style="padding: 6px 8px; color: var(--gray-500);">错误类型</td><td>{{ detailRow.error_type }}</td></tr>
          <tr v-if="detailRow.error_msg"><td style="padding: 6px 8px; color: var(--gray-500);">错误信息</td><td style="word-break: break-all; max-width: 600px;">{{ detailRow.error_msg }}</td></tr>
        </table>
        <!-- 请求内容 + 返回内容 并排展示 -->
        <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 16px; margin-top: 16px;">
          <div>
            <h4 style="margin: 0 0 8px; color: var(--primary);">📤 请求内容</h4>
            <div v-if="reqInfo" class="code-block" style="max-height: 500px;">
              <div v-if="reqInfo.model"><strong>模型:</strong> {{ reqInfo.model }}</div>
              <div v-if="reqInfo.stream != null"><strong>流式:</strong> {{ reqInfo.stream ? '是' : '否' }}</div>
              <div v-if="reqInfo.temperature != null"><strong>温度:</strong> {{ reqInfo.temperature }}</div>
              <div v-if="reqInfo.max_tokens"><strong>Max Tokens:</strong> {{ reqInfo.max_tokens }}</div>
              <hr style="margin: 8px 0; border-color: #334155;" />
              <div v-for="(m, i) in (reqInfo.messages || [])" :key="i" style="margin-bottom: 8px; padding: 6px 8px; background: #1e293b; border-radius: 4px; border-left: 3px solid;" :style="{borderLeftColor: m.role === 'system' ? '#94a3b8' : m.role === 'user' ? '#3b82f6' : m.role === 'assistant' ? '#22c55e' : '#f59e0b'}">
                <span style="font-size: 11px; color: #64748b; text-transform: uppercase; font-weight: bold;">{{ m.role }}</span>
                <div v-if="m.tool_calls" style="margin: 4px 0;">
                  <div v-for="tc in m.tool_calls" :key="tc.id || tc.index" style="margin-bottom: 4px; padding: 4px 8px; background: #0f172a; border-radius: 4px; font-size: 11px;">
                    <span style="color: #f59e0b;">🔧 {{ tc.function?.name || tc.type || 'function' }}</span>
                    <pre style="margin: 4px 0 0; white-space: pre-wrap; word-break: break-word; font-size: 11px; color: #94a3b8; max-height: 200px; overflow-y: auto;">{{ tc.function?.arguments || JSON.stringify(tc) }}</pre>
                  </div>
                </div>
                <pre v-else-if="m.content" style="margin: 4px 0 0; white-space: pre-wrap; word-break: break-word; font-size: 12px; color: #e2e8f0; max-height: 300px; overflow-y: auto;">{{ m.content }}</pre>
                <pre v-else style="margin: 4px 0 0; white-space: pre-wrap; word-break: break-word; font-size: 12px; color: #94a3b8; max-height: 300px; overflow-y: auto;">(空)</pre>
              </div>
            </div>
            <p v-else style="color: var(--gray-500); font-size: 13px;">无数据</p>
          </div>
          <div>
            <h4 style="margin: 0 0 8px; color: var(--success);">📥 返回内容</h4>
            <div v-if="respInfo" class="code-block" style="max-height: 500px;">
              <div v-if="respInfo.model"><strong>模型:</strong> {{ respInfo.model }}</div>
              <div><strong>Token:</strong> 输入 {{ detailRow.prompt_tokens || 0 }} / 输出 {{ detailRow.completion_tokens || 0 }}</div>
              <div v-if="respInfo.finish_reason"><strong>结束原因:</strong> {{ respInfo.finish_reason }}</div>
              <hr style="margin: 8px 0; border-color: #334155;" />
              <div v-if="(respInfo.choices || []).length">
                <div v-for="(c, i) in respInfo.choices" :key="i" style="margin-bottom: 6px;">
                  <span style="font-size: 11px; color: #64748b;">Choice {{ i + 1 }} <span v-if="c.finish_reason" style="color: #22c55e;">({{ c.finish_reason }})</span></span>
                  <pre style="margin: 4px 0 0; white-space: pre-wrap; word-break: break-word; font-size: 12px; color: #e2e8f0; max-height: 300px; overflow-y: auto;">{{ c.content || '(空)' }}</pre>
                </div>
              </div>
              <div v-else-if="respInfo.error" style="color: #f87171;">
                <strong>错误:</strong> {{ respInfo.error }}
              </div>
            </div>
            <p v-else style="color: var(--gray-500); font-size: 13px;">无数据</p>
          </div>
        </div>
        <div class="modal-actions">
          <button class="btn btn-outline" @click="detailRow = null">关闭</button>
        </div>
      </div>
    </div>
  </div>
</template>

<script>
import api from '../api.js'

function safeParseJSON(str) {
  if (!str) return null
  try { return JSON.parse(str) } catch (e) { return null }
}

export default {
  name: 'AnalyticsView',
  data() {
    return {
      summary: null,
      items: [],
      page: 1,
      total: 0,
      totalPages: 1,
      filterStatus: '',
      detailRow: null,
      detailLoading: false,
      archives: [],
      archiveBusy: false
    }
  },
  mounted() {
    this.loadAll()
  },
  computed: {
    reqInfo() {
      return safeParseJSON(this.detailRow?.request_body)
    },
    respInfo() {
      const raw = safeParseJSON(this.detailRow?.response_body)
      if (!raw) {
        // [stream] 或无数据
        const s = this.detailRow?.response_body
        if (typeof s === 'string' && s !== '[stream]') return { error: s }
        return null
      }
      // 流式：chunk 数组 → 合并
      if (Array.isArray(raw)) {
        const merged = { choices: [], usage: null, model: '' }
        const contentMap = {}
        for (const ck of raw) {
          if (ck.model) merged.model = ck.model
          if (ck.usage && Object.keys(ck.usage).length) merged.usage = ck.usage
          for (const ch of (ck.choices || [])) {
            const idx = ch.index ?? 0
            if (!contentMap[idx]) contentMap[idx] = ''
            const delta = ch.delta || ch.message || {}
            let text = delta.content || delta.reasoning_content || delta.reasoning || ''
            if (!text && delta.tool_calls) {
              text = JSON.stringify(delta.tool_calls)
            }
            contentMap[idx] += text || ch.text || ''
          }
        }
        for (const [idx, text] of Object.entries(contentMap)) {
          merged.choices.push({ index: Number(idx), content: text })
        }
        merged.choices.sort((a, b) => a.index - b.index)
        return merged
      }
      // 非流式：标准 {choices: [...], usage: {...}}
      if (raw.choices) {
        return {
          model: raw.model || '',
          usage: raw.usage,
          choices: raw.choices.map(c => ({
            index: c.index,
            finish_reason: c.finish_reason,
            content: (c.message?.content || c.message?.reasoning_content || c.delta?.content || c.delta?.reasoning_content || (c.message?.tool_calls && JSON.stringify(c.message.tool_calls)) || (c.delta?.tool_calls && JSON.stringify(c.delta.tool_calls)) || c.text || JSON.stringify(c.message || c.delta || c))
          }))
        }
      }
      return { error: typeof raw === 'string' ? raw : JSON.stringify(raw) }
    }
  },
  methods: {
    async loadAll() {
      await Promise.all([this.loadSummary(), this.loadPage(this.page), this.loadArchives()])
    },
    async loadSummary() {
      try {
        this.summary = await api.getAnalyticsSummary()
      } catch (e) { console.error('summary load failed', e) }
    },
    async loadPage(p) {
      this.page = p
      const params = { page: p, page_size: 10 }
      if (this.filterStatus) params.status = this.filterStatus
      try {
        const data = await api.getLogs(params)
        this.items = data.items || []
        this.total = data.total || 0
        this.totalPages = data.total_pages || 1
      } catch (e) { alert('加载日志失败: ' + e.message) }
    },
    showDetail(r) {
      this.detailLoading = true
      this.detailRow = { ...r }  // 先显示已有字段
      api.getLogDetail(r.id).then(full => {
        this.detailRow = full
      }).catch(e => {
        console.error('加载详情失败', e)
      }).finally(() => {
        this.detailLoading = false
      })
    },
    async loadArchives() {
      try {
        const data = await api.listArchives()
        this.archives = data.archives || []
      } catch (e) { console.error('加载归档列表失败', e) }
    },
    async doArchive() {
      if (!confirm('确定归档所有当前日志吗？归档后这些记录将从数据库移出，保存为压缩文件。')) return
      this.archiveBusy = true
      try {
        const r = await api.triggerArchive()
        alert(r.archived_count > 0 ? `成功归档 ${r.archived_count} 条记录 → ${r.filename}` : r.message || '暂无需要归档的日志')
        await this.loadArchives()
        await this.loadSummary()
        await this.loadPage(this.page)
      } catch (e) { alert('归档失败: ' + e.message) }
      finally { this.archiveBusy = false }
    },
    async doRestore(a) {
      if (!confirm(`确定恢复归档 "${a.filename}" 吗？\n\n${this.formatNum(a.count)} 条记录将被重新导入数据库，归档文件将被删除。`)) return
      this.archiveBusy = true
      try {
        const r = await api.restoreArchive(a.filename)
        alert(r.message || '恢复完成')
        await this.loadArchives()
        await this.loadSummary()
        await this.loadPage(this.page)
      } catch (e) { alert('恢复失败: ' + e.message) }
      finally { this.archiveBusy = false }
    },
    async doDeleteArchive(a) {
      if (!confirm(`⚠️ 永久删除归档 "${a.filename}"？\n\n此操作不可撤销！${this.formatNum(a.count)} 条日志将被永久删除。`)) return
      this.archiveBusy = true
      try {
        await api.deleteArchive(a.filename)
        await this.loadArchives()
        alert('归档文件已删除')
      } catch (e) { alert('删除失败: ' + e.message) }
      finally { this.archiveBusy = false }
    },
    async doClearLogs() {
      if (!confirm('⚠️ 确定清空所有当前请求日志？\n\n此操作不可撤销！建议先手动归档保留历史记录。')) return
      if (!confirm('再次确认：清空所有请求日志？')) return
      this.archiveBusy = true
      try {
        const r = await api.clearLogs()
        alert(r.message || '日志已清空')
        await this.loadSummary()
        await this.loadPage(1)
      } catch (e) { alert('清空失败: ' + e.message) }
      finally { this.archiveBusy = false }
    },
    formatJson(s) {
      try { return JSON.stringify(JSON.parse(s), null, 2) } catch (e) { return s }
    },
    formatNum(n) { return (n || 0).toLocaleString() },
    formatTokens(n) {
      if (!n || n < 1000) return String(n || 0)
      if (n < 1e6) return (n / 1000).toFixed(1) + 'K'
      return (n / 1e6).toFixed(1) + 'M'
    },
    formatSize(bytes) {
      if (!bytes || bytes < 1024) return (bytes || 0) + ' B'
      if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + ' KB'
      return (bytes / 1024 / 1024).toFixed(1) + ' MB'
    },
    fmtTime(ts) {
      if (!ts) return '-'
      // 判断是否已有时区信息：Z 结尾 或 +/-HH:MM 结尾
      const hasTZ = /[zZ]$|[+-]\d{2}:\d{2}$/.test(ts)
      const d = new Date(hasTZ ? ts : ts + 'Z')
      if (Number.isNaN(d.getTime())) return ts
      return d.toLocaleString('zh-CN', { timeZone: 'Asia/Shanghai', hour12: false })
    }
  }
}
</script>

<style scoped>
.stats-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
  gap: 12px;
}
.stat-card {
  background: white;
  border: 1px solid var(--gray-200);
  border-radius: 12px;
  padding: 16px;
}
.stat-label {
  font-size: 12px;
  color: var(--gray-500);
  text-transform: uppercase;
  margin-bottom: 4px;
}
.stat-number {
  font-size: 22px;
  font-weight: 700;
}
.stat-sub {
  font-size: 11px;
  color: var(--gray-500);
  margin-top: 2px;
}
.modal-overlay {
  position: fixed; inset: 0; background: rgba(0,0,0,0.4);
  display: flex; align-items: center; justify-content: center; z-index: 100;
}
.modal-content {
  background: white; border-radius: 12px; padding: 24px; max-width: 640px; width: 90%;
}
.modal-actions { display: flex; justify-content: flex-end; gap: 8px; margin-top: 16px; }
.code-block { background: #1e293b; color: #e2e8f0; padding: 16px; border-radius: 8px; font-size: 13px; font-family: monospace; max-height: 500px; overflow-y: auto; white-space: pre-wrap; word-break: break-word; line-height: 1.6; }
.code-block strong { color: #93c5fd; }
</style>
