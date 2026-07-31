<template>
  <div>
    <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 20px;">
      <div>
        <h1>分析 📊</h1>
        <p style="color: var(--gray-500); font-size: 14px; margin-top: 4px;">请求量、延迟、Token 用量和失败统计</p>
      </div>
      <div style="display: flex; align-items: center; gap: 14px;">
        <label style="display: flex; align-items: center; gap: 6px; font-size: 13px; color: var(--gray-500); cursor: pointer;" :title="diagVerbose ? '全量输出所有诊断阶段（控制台日志会变多）' : '仅输出关键里程碑，关闭多余日志'">
          <input type="checkbox" v-model="diagVerbose" @change="toggleDiag" style="cursor: pointer; width: 15px; height: 15px;" />
          请求诊断日志
        </label>
        <button class="btn btn-outline" @click="loadAll">刷新</button>
      </div>
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
        <div class="stat-label">成功请求</div>
        <div class="stat-number">{{ formatNum(summary.success_count) }}</div>
        <div class="stat-sub">占总请求比</div>
      </div>
    </div>

    <!-- 请求日志 -->
    <div class="card" style="margin-top: 20px;">
      <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 12px;">
        <h2>请求日志</h2>
        <div style="display: flex; gap: 8px;">
          <select v-model="filterStatus" @change="loadPage(1)" style="width: auto;">
            <option value="">全部状态</option>
            <option value="success">成功</option>
            <option value="error">失败</option>
          </select>
          <select v-model="filterProvider" @change="loadPage(1)" style="width: auto;">
            <option value="">全部服务商</option>
            <option v-for="p in logProviders" :key="p" :value="p">{{ p }}</option>
          </select>
        </div>
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
            <th>代理</th>
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
              <span v-else style="color: var(--text-muted);">-</span>
            </td>
            <td>
              <span :class="['badge', r.status === 'success' ? 'badge-success' : 'badge-danger']" style="font-size: 11px;">{{ r.status === 'success' ? '成功' : '失败' }}</span>
            </td>
            <td style="font-family: monospace;">{{ r.latency_ms ? (r.latency_ms / 1000).toFixed(1) + 's' : '-' }}</td>
            <td style="font-family: monospace; font-size: 12px;">{{ r.prompt_tokens || 0 }}/{{ r.completion_tokens || 0 }}</td>
            <td>
              <span v-if="r.used_proxy" style="color: #22c55e; font-size: 12px;">🟢 代理</span>
              <span v-else style="color: var(--gray-500); font-size: 12px;">⚪ 直连</span>
            </td>
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

    <!-- 今日用量概览（原配额追踪并入） -->
    <div class="usage-section" v-if="todayData">
      <div class="usage-header">
        <h2>今日用量</h2>
        <span class="muted">{{ todayData.day }}</span>
      </div>
      <div class="stats-grid">
        <div class="stat-card">
          <div class="stat-label">今日请求</div>
          <div class="stat-number">{{ formatNum(todayData.requests) }}</div>
          <div class="stat-sub">成功 {{ formatNum(todayData.success_requests) }}</div>
        </div>
        <div class="stat-card">
          <div class="stat-label">今日成功率</div>
          <div class="stat-number" :style="{color: todayData.success_rate >= 95 ? 'var(--success)' : todayData.success_rate >= 80 ? 'var(--warning)' : 'var(--danger)'}">{{ todayData.success_rate }}%</div>
        </div>
        <div class="stat-card">
          <div class="stat-label">今日 Token</div>
          <div class="stat-number">{{ formatTokens(todayData.total_tokens) }}</div>
          <div class="stat-sub">输入 {{ formatTokens(todayData.prompt_tokens) }} · 输出 {{ formatTokens(todayData.completion_tokens) }}</div>
        </div>
        <div class="stat-card">
          <div class="stat-label">今日成本</div>
          <div class="stat-number">${{ todayData.cost_usd.toFixed(4) }}</div>
          <div class="stat-sub">按模型单价估算</div>
        </div>
      </div>
    </div>

    <!-- 用量趋势（手写 SVG，双指标 + 鼠标悬停提示） -->
    <div class="card" style="margin-top: 20px;" v-if="trendData.length">
      <div class="usage-header" style="margin-bottom: 12px;">
        <h2>用量趋势</h2>
        <div style="display: flex; gap: 8px;">
          <button class="btn btn-outline btn-sm" :class="{active: trendDays === 7}" @click="changeTrendDays(7)">近 7 天</button>
          <button class="btn btn-outline btn-sm" :class="{active: trendDays === 30}" @click="changeTrendDays(30)">近 30 天</button>
        </div>
      </div>
      <div class="trend-wrap" @mousemove="onTrendMove" @mouseleave="trendHover = -1">
        <svg :viewBox="'0 0 ' + trendW + ' ' + trendH" width="100%" style="display:block; overflow:visible;">
          <line :x1="trendPad" :y1="trendH - trendPad" :x2="trendW - trendPad" :y2="trendH - trendPad" stroke="#334155" stroke-width="1"/>
          <path :d="trendTokArea" fill="rgba(59,130,246,0.12)"/>
          <path :d="trendTokLine" fill="none" stroke="#3b82f6" stroke-width="2"/>
          <path :d="trendCostLine" fill="none" stroke="#10b981" stroke-width="2" stroke-dasharray="4 3"/>
          <g v-for="(d, i) in trendData" :key="i">
            <circle :cx="trendX(i)" :cy="trendYTok(d)" r="2.6" fill="#3b82f6"/>
            <circle :cx="trendX(i)" :cy="trendYCost(d)" r="2.6" fill="#10b981"/>
            <text v-if="trendShowLabel(i)" :x="trendX(i)" :y="trendH - 10" font-size="9" fill="#94a3b8" text-anchor="middle">{{ trendLabel(d) }}</text>
          </g>
          <line v-if="trendHover >= 0" :x1="trendX(trendHover)" :y1="trendPad" :x2="trendX(trendHover)" :y2="trendH - trendPad" stroke="#64748b" stroke-width="1" stroke-dasharray="3 3"/>
        </svg>
        <div v-if="trendHover >= 0" class="trend-tip" :style="trendTipStyle">
          <div class="trend-tip-day">{{ trendData[trendHover].day }}</div>
          <div>Token：{{ formatTokens(trendData[trendHover].tokens) }}</div>
          <div>成本：${{ trendData[trendHover].cost_usd.toFixed(4) }}</div>
          <div>请求：{{ trendData[trendHover].requests }}</div>
        </div>
      </div>
      <div class="trend-legend">
        <span><i class="dot token"></i>Token</span>
        <span><i class="dot cost"></i>成本 (USD)</span>
      </div>
    </div>

    <!-- 按服务商用量（占比进度条） -->
    <div class="card" style="margin-top: 20px;" v-if="providerData.length">
      <div class="usage-header" style="margin-bottom: 12px;">
        <h2>按服务商用量</h2>
        <span class="muted">今日共 {{ formatTokens(providerTotal) }} tokens</span>
      </div>
      <table>
        <thead>
          <tr>
            <th>服务商</th>
            <th>请求</th>
            <th>Token</th>
            <th style="min-width: 170px;">占比</th>
            <th>成本</th>
            <th></th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="p in providerData" :key="p.provider_id || p.provider_name">
            <td style="font-weight: 600;">{{ p.provider_name }}</td>
            <td>{{ formatNum(p.requests) }}</td>
            <td>{{ formatTokens(p.tokens) }}</td>
            <td>
              <div class="bar"><div class="bar-fill" :style="{width: p.share_pct + '%'}"></div></div>
              <span class="muted">{{ p.share_pct }}%</span>
            </td>
            <td>${{ p.cost_usd.toFixed(4) }}</td>
            <td><button class="btn btn-outline btn-sm" @click="filterByProvider(p.provider_name)">看日志</button></td>
          </tr>
        </tbody>
      </table>
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
      <div class="modal-content detail-modal">
        <h3>请求详情</h3>
        <table class="detail-meta" style="width: 100%; margin: 16px 0; table-layout: fixed;">
          <tr>
            <td class="k">时间</td><td class="v">{{ fmtTime(detailRow.created_at) }}</td>
            <td class="k">状态</td><td class="v">{{ detailRow.status }}</td>
          </tr>
          <tr>
            <td class="k">请求模型</td><td class="v">{{ detailRow.requested_model || '-' }}</td>
            <td class="k">路由模型</td><td class="v">{{ detailRow.routed_model || '-' }}</td>
          </tr>
          <tr>
            <td class="k">路由服务商</td><td class="v">{{ detailRow.routed_provider || '-' }}</td>
            <td class="k">延迟</td><td class="v">{{ detailRow.latency_ms ? (detailRow.latency_ms / 1000).toFixed(1) + 's' : '-' }}</td>
          </tr>
          <tr>
            <td class="k">Prompt Token</td><td class="v">{{ detailRow.prompt_tokens || 0 }}</td>
            <td class="k">Completion Token</td><td class="v">{{ detailRow.completion_tokens || 0 }}</td>
          </tr>
          <tr>
            <td class="k">回退次数</td><td class="v">{{ detailRow.fallback_count || 0 }}</td>
            <td class="k">IP</td><td class="v">{{ detailRow.user_ip || '-' }}</td>
          </tr>
          <tr>
            <td class="k">代理</td>
            <td class="v" colspan="3">
              <span v-if="detailRow.used_proxy" style="color: #22c55e; font-weight: 500;">🟢 走代理</span>
              <span v-else style="color: var(--gray-500);">⚪ 直连</span>
              <span v-if="detailRow.proxy_url" style="font-family: monospace; font-size: 12px; margin-left: 6px; color: #94a3b8;">{{ maskProxy(detailRow.proxy_url) }}</span>
            </td>
          </tr>
          <tr v-if="detailRow.error_type">
            <td class="k">错误类型</td><td class="v" colspan="3">{{ detailRow.error_type }}</td>
          </tr>
          <tr v-if="detailRow.error_msg">
            <td class="k">错误信息</td><td class="v" colspan="3" style="word-break: break-all;">{{ detailRow.error_msg }}</td>
          </tr>
        </table>
        <!-- 请求内容 + 返回内容 并排展示 -->
        <div class="detail-grid">
          <div>
            <div style="display: flex; justify-content: space-between; align-items: center; margin: 0 0 8px;">
              <h4 style="margin: 0; color: var(--primary);">📤 请求内容</h4>
              <button class="btn btn-outline btn-sm" @click="copy(prettyRequestBody)">复制</button>
            </div>
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
                    <pre style="margin: 4px 0 0; white-space: pre-wrap; word-break: break-word; font-size: 11px; color: #94a3b8; max-height: 260px; overflow-y: auto;">{{ formatRichContent(tc.function?.arguments || JSON.stringify(tc)) }}</pre>
                  </div>
                </div>
                <pre v-else-if="m.content" style="margin: 4px 0 0; white-space: pre-wrap; word-break: break-word; font-size: 12px; color: #e2e8f0; max-height: 300px; overflow-y: auto;">{{ formatRichContent(m.content) }}</pre>
                <pre v-else style="margin: 4px 0 0; white-space: pre-wrap; word-break: break-word; font-size: 12px; color: #94a3b8; max-height: 300px; overflow-y: auto;">(空)</pre>
              </div>
            </div>
            <p v-else style="color: var(--gray-500); font-size: 13px;">无数据</p>
            <details v-if="prettyRequestBody" style="margin-top: 8px;">
              <summary style="cursor: pointer; color: var(--gray-500); font-size: 12px;">原始请求 JSON（美化）</summary>
              <pre class="code-block" style="margin-top: 6px; max-height: 360px;">{{ prettyRequestBody }}</pre>
            </details>
          </div>
          <div>
            <div style="display: flex; justify-content: space-between; align-items: center; margin: 0 0 8px;">
              <h4 style="margin: 0; color: var(--success);">📥 返回内容</h4>
              <button class="btn btn-outline btn-sm" @click="copy(prettyResponseBody)">复制</button>
            </div>
            <div v-if="respInfo || responseToolCalls.length" class="code-block" style="max-height: 500px;">
              <div v-if="respInfo">
                <div v-if="respInfo.model"><strong>模型:</strong> {{ respInfo.model }}</div>
                <div><strong>Token:</strong> 输入 {{ detailRow.prompt_tokens || 0 }} / 输出 {{ detailRow.completion_tokens || 0 }}</div>
                <div v-if="respInfo.finish_reason"><strong>结束原因:</strong> {{ respInfo.finish_reason }}</div>
                <hr style="margin: 8px 0; border-color: #334155;" />
                <div v-if="(respInfo.choices || []).length">
                  <div v-for="(c, i) in respInfo.choices" :key="i" style="margin-bottom: 6px;">
                    <span style="font-size: 11px; color: #64748b;">Choice {{ i + 1 }} <span v-if="c.finish_reason" style="color: #22c55e;">({{ c.finish_reason }})</span></span>
                    <pre style="margin: 4px 0 0; white-space: pre-wrap; word-break: break-word; font-size: 12px; color: #e2e8f0; max-height: 300px; overflow-y: auto;">{{ collapseBlank(c.content) || '(空)' }}</pre>
                  </div>
                </div>
                <div v-else-if="respInfo.error" style="color: #f87171;">
                  <strong>错误:</strong> {{ respInfo.error }}
                </div>
              </div>
              <div v-if="responseToolCalls.length" style="margin-top: 12px;">
                <div style="font-size: 13px; font-weight: 500; margin-bottom: 8px; color: #94a3b8;">工具调用 · {{ responseToolCalls.length }} 个</div>
                <div v-for="tc in responseToolCalls" :key="tc.id || tc.index" style="margin-bottom: 10px; padding: 10px 12px; background: #0f172a; border: 0.5px solid #334155; border-radius: 6px;">
                  <div style="display: flex; align-items: center; gap: 8px; margin-bottom: 8px;">
                    <span style="font-size: 13px; font-weight: 500; color: #f59e0b;">🔧 {{ tc.name }}</span>
                    <span style="font-size: 11px; color: #64748b; font-family: monospace;">{{ tc.id }}</span>
                  </div>
                  <div v-for="(v, k) in tc.args" :key="k" style="margin-bottom: 4px; font-size: 12px;">
                    <span style="color: #94a3b8;">{{ k }}:</span>
                    <pre style="margin: 2px 0 0; white-space: pre-wrap; word-break: break-word; font-size: 12px; color: #e2e8f0; background: #020617; padding: 6px 8px; border-radius: 4px; max-height: 320px; overflow-y: auto;">{{ argText(v) }}</pre>
                  </div>
                </div>
              </div>
            </div>
            <p v-else style="color: var(--gray-500); font-size: 13px;">无数据</p>
            <details v-if="prettyResponseBody" style="margin-top: 8px;">
              <summary style="cursor: pointer; color: var(--gray-500); font-size: 12px;">原始返回 JSON（美化）</summary>
              <pre class="code-block" style="margin-top: 6px; max-height: 360px;">{{ prettyResponseBody }}</pre>
            </details>
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
import toast from '../toast.js'

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
      archiveBusy: false,
      todayData: null,
      trendDays: 7,
      trendData: [],
      providerData: [],
      filterProvider: '',
      logProviders: [],
      trendW: 680,
      trendH: 210,
      trendPad: 32,
      trendHover: -1,
      trendTipX: 0,
      trendTipY: 0,
      diagVerbose: false
    }
  },
    mounted() {
    this.loadAll()
  },
  computed: {
    providerTotal() {
      return (this.providerData || []).reduce((s, p) => s + (p.tokens || 0), 0)
    },
    trendScale() {
      const data = this.trendData || []
      const n = data.length
      const maxTok = Math.max(1, ...data.map(d => d.tokens || 0))
      const maxCost = Math.max(0.0001, ...data.map(d => d.cost_usd || 0))
      const W = this.trendW, H = this.trendH, pad = this.trendPad
      const xOf = i => pad + (W - 2 * pad) * (n <= 1 ? 0.5 : i / (n - 1))
      const yTok = v => H - pad - (H - 2 * pad) * (v / maxTok)
      const yCost = v => H - pad - (H - 2 * pad) * (v / maxCost)
      return { data, n, maxTok, maxCost, xOf, yTok, yCost }
    },
    trendTokArea() {
      const s = this.trendScale
      if (!s.n) return ''
      const { data, n, xOf, yTok } = s
      return `M ${xOf(0).toFixed(1)},${(this.trendH - this.trendPad).toFixed(1)} ` +
        data.map((d, i) => `L ${xOf(i).toFixed(1)},${yTok(d.tokens || 0).toFixed(1)}`).join(' ') +
        ` L ${xOf(n - 1).toFixed(1)},${(this.trendH - this.trendPad).toFixed(1)} Z`
    },
    trendTokLine() {
      const s = this.trendScale
      if (!s.n) return ''
      const { data, xOf, yTok } = s
      return 'M ' + data.map((d, i) => `${xOf(i).toFixed(1)},${yTok(d.tokens || 0).toFixed(1)}`).join(' L ')
    },
    trendCostLine() {
      const s = this.trendScale
      if (!s.n) return ''
      const { data, xOf, yCost } = s
      return 'M ' + data.map((d, i) => `${xOf(i).toFixed(1)},${yCost(d.cost_usd || 0).toFixed(1)}`).join(' L ')
    },
    trendTipStyle() {
      return { left: this.trendTipX + 'px', top: this.trendTipY + 'px' }
    },
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
        const tcOnly = {}
        for (const ck of raw) {
          if (ck.model) merged.model = ck.model
          if (ck.usage && Object.keys(ck.usage).length) merged.usage = ck.usage
          for (const ch of (ck.choices || [])) {
            const idx = ch.index ?? 0
            if (!contentMap[idx]) contentMap[idx] = ''
            const delta = ch.delta || ch.message || {}
            // 工具调用已在下方「工具调用」卡片中解析展示，主内容区不再塞原始 JSON
            const text = delta.content || delta.reasoning_content || delta.reasoning || ch.text || ''
            if (text) contentMap[idx] += text
            else if (delta.tool_calls) tcOnly[idx] = true
          }
        }
        for (const [idx, text] of Object.entries(contentMap)) {
          merged.choices.push({
            index: Number(idx),
            content: text || (tcOnly[idx] ? '(工具调用见下方卡片)' : ''),
          })
        }
        merged.choices.sort((a, b) => a.index - b.index)
        return merged
      }
      // 非流式：标准 {choices: [...], usage: {...}}
      if (raw.choices) {
        return {
          model: raw.model || '',
          usage: raw.usage,
          choices: raw.choices.map(c => {
            // 工具调用已在下方「工具调用」卡片中解析展示，主内容区不再塞原始 JSON
            const tc = c.message?.tool_calls || c.delta?.tool_calls
            const content = (c.message?.content || c.message?.reasoning_content || c.delta?.content || c.delta?.reasoning_content || c.text
              || (tc && '(工具调用见下方卡片)')
              || JSON.stringify(c.message || c.delta || c))
            return { index: c.index, finish_reason: c.finish_reason, content }
          })
        }
      }
      return { error: typeof raw === 'string' ? raw : JSON.stringify(raw) }
    },
    // 解析 response_body 中的 tool_calls，兼容三种存储形态：
    //   A. 顶层干净数组 [{index,id,type:"function",function:{name,arguments}}]
    //   B. 流式 SSE delta 累积数组 [{id,choices:[{index,delta:{tool_calls:[...]}}]}]
    //   C. 非流式对象 {choices:[{message:{tool_calls:[...]}}]}
    // 对形态 B 的增量 arguments 按 index 合并
    responseToolCalls() {
      const parseArgs = (a) => {
        if (typeof a !== 'string') return a
        try { return JSON.parse(a) } catch (e) { return a }
      }
      const normalizeTC = (tc, i) => {
        const fn = tc.function || {}
        return {
          index: tc.index ?? i ?? 0,
          id: tc.id || '',
          name: fn.name || tc.name || tc.type || 'function',
          args: parseArgs(fn.arguments ?? tc.arguments),
        }
      }
      const raw = safeParseJSON(this.detailRow?.response_body)
      if (!raw) return []
      // 形态 C：非流式对象
      if (!Array.isArray(raw)) {
        const out = []
        for (const ch of raw.choices || []) {
          const tcs = (ch.message || {}).tool_calls
          if (Array.isArray(tcs)) for (const tc of tcs) out.push(normalizeTC(tc))
        }
        return out
      }
      if (!raw.length) return []
      // 形态 B：流式 delta 数组（每个元素带 choices）
      if (Array.isArray(raw[0]?.choices)) {
        const acc = {}
        for (const el of raw) {
          const d = ((el.choices || [])[0] || {}).delta
          const tcs = d && d.tool_calls
          if (!Array.isArray(tcs)) continue
          for (const tc of tcs) {
            const i = tc.index ?? 0
            if (!acc[i]) acc[i] = { id: tc.id, type: tc.type, name: '', args: '' }
            if (tc.id) acc[i].id = tc.id
            if (tc.type) acc[i].type = tc.type
            if (tc.function && tc.function.name) acc[i].name = tc.function.name
            if (tc.function && typeof tc.function.arguments === 'string') acc[i].args += tc.function.arguments
          }
        }
        return Object.keys(acc).sort((a, b) => a - b).map(i => ({
          index: Number(i),
          id: acc[i].id || '',
          name: acc[i].name || acc[i].type || 'function',
          args: parseArgs(acc[i].args),
        }))
      }
      // 形态 A：干净的 tool_calls 数组
      if (raw.every(x => x && (x.function || x.type === 'function'))) {
        return raw.map((x, i) => normalizeTC(x, i))
      }
      return []
    },
    prettyRequestBody() {
      const raw = this.detailRow?.request_body
      if (!raw) return ''
      const parsed = safeParseJSON(raw)
      return parsed ? this.deepUnescape(parsed) : raw
    },
    prettyResponseBody() {
      const raw = this.detailRow?.response_body
      if (!raw) return ''
      const parsed = safeParseJSON(raw)
      return parsed ? this.deepUnescape(parsed) : raw
    }
  },
  methods: {
    async loadAll() {
      await Promise.all([
        this.loadSummary(), this.loadPage(this.page), this.loadArchives(),
        this.loadToday(), this.loadTrend(), this.loadByProvider(), this.loadLogProviders(),
      ])
      this.loadDiag()
    },
    async loadDiag() {
      try { this.diagVerbose = !!(await api.getDiag()).verbose } catch (e) { console.error('diag load failed', e) }
    },
    async toggleDiag() {
      try {
        await api.setDiag(this.diagVerbose)
        toast.info(this.diagVerbose ? '请求诊断日志：已开启（全量输出）' : '请求诊断日志：已关闭（仅关键里程碑）')
      } catch (e) {
        this.diagVerbose = !this.diagVerbose  // 失败回滚
        toast.error('切换失败：' + e.message)
      }
    },
    async loadSummary() {
      try {
        this.summary = await api.getAnalyticsSummary()
      } catch (e) { console.error('summary load failed', e) }
    },
    async loadToday() {
      try { this.todayData = await api.getAnalyticsToday() }
      catch (e) { console.error('today load failed', e) }
    },
    async loadTrend() {
      try { this.trendData = await api.getAnalyticsTrend(this.trendDays) }
      catch (e) { console.error('trend load failed', e) }
    },
    async loadByProvider() {
      try {
        const d = await api.getAnalyticsByProvider()
        this.providerData = d.providers || []
      } catch (e) { console.error('by-provider load failed', e) }
    },
    changeTrendDays(d) {
      this.trendDays = d
      this.loadTrend()
    },
    filterByProvider(name) {
      this.filterProvider = name
      this.loadPage(1)
    },
    clearProviderFilter() {
      this.filterProvider = ''
      this.loadPage(1)
    },
    loadLogProviders() {
      api.getLogProviders().then(d => { this.logProviders = (d && d.providers) || [] }).catch(() => {})
    },
    trendX(i) { return this.trendScale.xOf(i) },
    trendYTok(d) { return this.trendScale.yTok(d.tokens || 0) },
    trendYCost(d) { return this.trendScale.yCost(d.cost_usd || 0) },
    trendShowLabel(i) {
      const n = this.trendScale.n
      const step = Math.max(1, Math.ceil(n / 14))
      return n <= 14 || i % step === 0 || i === n - 1
    },
    trendLabel(d) { return String(d.day).slice(5) },
    onTrendMove(e) {
      const rect = e.currentTarget.getBoundingClientRect()
      const xPx = e.clientX - rect.left
      const yPx = e.clientY - rect.top
      const xView = xPx / rect.width * this.trendW
      const n = this.trendScale.n
      let i = n <= 1 ? 0 : Math.round((xView - this.trendPad) / (this.trendW - 2 * this.trendPad) * (n - 1))
      i = Math.max(0, Math.min(n - 1, i))
      this.trendHover = i
      this.trendTipX = xPx
      this.trendTipY = yPx
    },
    async loadPage(p) {
      this.page = p
      const params = { page: p, page_size: 10 }
      if (this.filterStatus) params.status = this.filterStatus
      if (this.filterProvider) params.provider = this.filterProvider
      try {
        const data = await api.getLogs(params)
        this.items = data.items || []
        this.total = data.total || 0
        this.totalPages = data.total_pages || 1
      } catch (e) { toast.error('加载日志失败: ' + e.message) }
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
        toast.success(r.archived_count > 0 ? `成功归档 ${r.archived_count} 条记录 → ${r.filename}` : r.message || '暂无需要归档的日志')
        await this.loadArchives()
        await this.loadSummary()
        await this.loadPage(this.page)
      } catch (e) { toast.error('归档失败: ' + e.message) }
      finally { this.archiveBusy = false }
    },
    async doRestore(a) {
      if (!confirm(`确定恢复归档 "${a.filename}" 吗？\n\n${this.formatNum(a.count)} 条记录将被重新导入数据库，归档文件将被删除。`)) return
      this.archiveBusy = true
      try {
        const r = await api.restoreArchive(a.filename)
        toast.success(r.message || '恢复完成')
        await this.loadArchives()
        await this.loadSummary()
        await this.loadPage(this.page)
      } catch (e) { toast.error('恢复失败: ' + e.message) }
      finally { this.archiveBusy = false }
    },
    async doDeleteArchive(a) {
      if (!confirm(`⚠️ 永久删除归档 "${a.filename}"？\n\n此操作不可撤销！${this.formatNum(a.count)} 条日志将被永久删除。`)) return
      this.archiveBusy = true
      try {
        await api.deleteArchive(a.filename)
        await this.loadArchives()
        toast.success('归档文件已删除')
      } catch (e) { toast.error('删除失败: ' + e.message) }
      finally { this.archiveBusy = false }
    },
    async doClearLogs() {
      if (!confirm('⚠️ 确定清空所有当前请求日志？\n\n此操作不可撤销！建议先手动归档保留历史记录。')) return
      if (!confirm('再次确认：清空所有请求日志？')) return
      this.archiveBusy = true
      try {
        const r = await api.clearLogs()
        toast.success(r.message || '日志已清空')
        await this.loadSummary()
        await this.loadPage(1)
      } catch (e) { toast.error('清空失败: ' + e.message) }
      finally { this.archiveBusy = false }
    },

    // 递归解转义：处理 tool_call.arguments 里单层/多层被 JSON 转义的字符串
    // 例：'{"command":"# kill...\nGet-NetTCPConnection..."}'  →  解开成真正换行的可读文本
    deepUnescape(value) {
      if (value === null || value === undefined) return ''
      if (typeof value === 'object') return JSON.stringify(value, null, 2)
      let result = String(value).trim()
      if (!result) return ''
      for (let i = 0; i < 6; i++) {
        try {
          const parsed = JSON.parse(result)
          if (typeof parsed === 'string') {
            result = parsed           // 解开一层字符串转义，继续往下看是否还有一层
            continue
          }
          return JSON.stringify(parsed, null, 2)  // 解析成对象/数组 → 漂亮缩进
        } catch (e) {
          break
        }
      }
      return result  // 不是 JSON（如原始 shell 命令/计划文本）→ 原样返回，保留真实换行
    },
    formatRichContent(value) {
      return this.deepUnescape(value)
    },
    // 仅用于「返回内容」展示：折叠模型输出里多余的连续空行
    // （保留单段落间距，去掉 3+ 连续换行；不动存储数据，复制按钮仍给原文）
    collapseBlank(text) {
      if (!text) return text
      let s = String(text)
      s = s.replace(/\r\n?/g, '\n')        // 统一换行符
      s = s.replace(/[ \t]+\n/g, '\n')       // 去掉行尾空白
      s = s.replace(/\n{3,}/g, '\n\n')      // 3+ 连续换行 → 2（1 个空行）
      s = s.replace(/^\n+/, '').replace(/\n+$/, '') // 去掉首尾空行
      return s
    },
    // 工具调用参数值统一渲染为文本（对象/数组美化，字符串原样保留换行）
    argText(v) {
      if (v === null) return 'null'
      if (v === undefined) return ''
      if (typeof v === 'object') return JSON.stringify(v, null, 2)
      return String(v)
    },
    async copy(text) {
      if (!text) return
      try {
        if (navigator.clipboard && navigator.clipboard.writeText) {
          await navigator.clipboard.writeText(text)
        } else {
          throw new Error('no clipboard api')
        }
      } catch (e) {
        const ta = document.createElement('textarea')
        ta.value = text
        ta.style.position = 'fixed'
        ta.style.opacity = '0'
        document.body.appendChild(ta)
        ta.select()
        try { document.execCommand('copy') } catch (_) {}
        document.body.removeChild(ta)
      }
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
    // 代理 URL 脱敏：隐藏密码段，避免明文暴露凭据
    maskProxy(url) {
      if (!url) return ''
      try {
        const u = new URL(url)
        if (u.password) u.password = '****'
        return u.toString()
      } catch (e) {
        return url
      }
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
  background: var(--bg-card);
  border: 1px solid var(--border-soft);
  border-radius: 12px;
  padding: 16px;
}
.stat-label {
  font-size: 12px;
  color: var(--text-muted);
  text-transform: uppercase;
  margin-bottom: 4px;
}
.stat-number {
  font-size: 22px;
  font-weight: 700;
  color: var(--primary);
}
.stat-sub {
  font-size: 11px;
  color: var(--text-muted);
  margin-top: 2px;
}
.modal-overlay {
  position: fixed; inset: 0; background: rgba(0,0,0,0.5);
  display: flex; align-items: center; justify-content: center; z-index: 100;
}
.modal-content {
  background: var(--bg-elevated); border-radius: 12px; padding: 24px; max-width: 640px; width: 90%;
  border: 1px solid var(--border-soft);
}
.modal-actions { display: flex; justify-content: flex-end; gap: 8px; margin-top: 16px; }
/* code-block 详情区：保持暗色风格（无论主题），因为里面是 JSON 代码 + 多层嵌套的暗色 inline 颜色 */
.code-block { background: #1e293b; color: #e2e8f0; padding: 16px; border-radius: 8px; font-size: 13px; font-family: monospace; max-height: 500px; overflow-y: auto; white-space: pre-wrap; word-break: break-word; line-height: 1.6; border: 1px solid #334155; }
.code-block strong { color: #93c5fd; }

.detail-modal {
  max-width: min(1180px, 94vw) !important;
  width: 94vw;
  max-height: 88vh;
  overflow: auto;
}
.detail-meta td { padding: 5px 8px; font-size: 13px; vertical-align: top; }
.detail-meta td.k { color: var(--gray-500); width: 92px; white-space: nowrap; }
.detail-meta td.v { word-break: break-all; }
.detail-grid {
  display: grid;
  grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);
  gap: 16px;
  margin-top: 16px;
}
.code-block,
.code-block pre {
  background: #0f172a !important;
  color: #e5e7eb !important;
}
.code-block {
  max-height: 55vh !important;
  overflow: auto !important;
}
@media (max-width: 900px) {
  .detail-grid { grid-template-columns: 1fr; }
}

/* 用量分析（配额追踪并入分析页） */
.usage-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 12px; }
.usage-header h2 { margin: 0; }
.muted { color: var(--text-muted); font-size: 13px; }
.trend-svg { width: 100%; background: var(--bg-card); border-radius: 8px; padding: 8px 0; }
.trend-legend { display: flex; gap: 16px; margin-top: 8px; font-size: 12px; color: var(--text-muted); }
.trend-legend .dot { display: inline-block; width: 10px; height: 10px; border-radius: 2px; margin-right: 4px; vertical-align: middle; }
.trend-legend .dot.token { background: #3b82f6; }
.trend-legend .dot.cost { background: #10b981; }
.bar { height: 8px; background: var(--border-soft); border-radius: 4px; overflow: hidden; display: inline-block; width: 120px; vertical-align: middle; margin-right: 8px; }
.bar-fill { height: 100%; background: linear-gradient(90deg, #3b82f6, #10b981); border-radius: 4px; transition: width 0.3s ease; }
.filter-chip { margin-top: 20px; padding: 10px 14px; background: var(--bg-card); border: 1px solid var(--border-soft); border-radius: 8px; font-size: 14px; display: flex; align-items: center; gap: 10px; }
.usage-section { margin-top: 20px; }
.trend-wrap { position: relative; }
.trend-tip {
  position: absolute;
  transform: translate(-50%, calc(-100% - 14px));
  background: #0f172a; color: #e2e8f0;
  border: 1px solid #334155; border-radius: 8px;
  padding: 8px 10px; font-size: 12px; line-height: 1.7;
  pointer-events: none; white-space: nowrap;
  box-shadow: 0 4px 12px rgba(0,0,0,.35); z-index: 5;
}
.trend-tip-day { font-weight: 700; color: #93c5fd; margin-bottom: 2px; }
.btn.active { background: var(--primary); color: #fff; border-color: var(--primary); }

</style>
