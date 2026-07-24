<template>
  <div>
    <h1 style="margin-bottom: 20px;">请求日志 📋</h1>
    <div class="card" style="margin-bottom: 16px; display: flex; gap: 12px; align-items: center; flex-wrap: wrap;">
      <select v-model="filterStatus" @change="load(1)" style="width: auto;">
        <option value="">全部状态</option>
        <option value="success">成功</option>
        <option value="error">错误</option>
      </select>
      <button class="btn btn-outline" @click="load(1)">刷新</button>
      <span style="color: var(--gray-500); font-size: 12px;">提示：点击任意一行可查看请求/响应详情（自动展开 tool_call 参数）</span>
    </div>
    <div class="card">
      <table>
        <thead>
          <tr>
            <th>时间</th>
            <th>类型</th>
            <th>请求模型</th>
            <th>路由到</th>
            <th>状态</th>
            <th>延迟</th>
            <th>Token</th>
            <th>回退</th>
          </tr>
        </thead>
        <tbody>
          <tr v-if="items.length === 0">
            <td colspan="8" style="text-align: center; padding: 32px; color: var(--gray-500);">暂无日志</td>
          </tr>
          <tr
            v-for="r in items"
            :key="r.id"
            class="log-row"
            @click="openDetail(r.id)"
          >
            <td style="font-size: 12px; white-space: nowrap;">{{ fmtTime(r.created_at) }}</td>
            <td>
              <span v-if="r.media_type === 'image'" class="media-tag media-tag-image">图</span>
              <span v-else-if="r.media_type === 'video'" class="media-tag media-tag-video">视</span>
              <span v-else class="media-tag media-tag-chat">对</span>
            </td>
            <td style="font-family: monospace; font-size: 12px;">{{ r.requested_model }}</td>
            <td style="font-family: monospace; font-size: 12px;">
              <span v-if="r.routed_provider && r.routed_model">{{ r.routed_provider }}/{{ r.routed_model }}</span>
              <span v-else style="color: var(--text-muted);">-</span>
            </td>
            <td>
              <span :class="['badge', r.status === 'success' ? 'badge-success' : 'badge-danger']">{{ r.status === 'success' ? '成功' : '失败' }}</span>
            </td>
            <td>{{ r.latency_ms ? Math.round(r.latency_ms) + 'ms' : '-' }}</td>
            <td style="font-size: 12px;">{{ r.prompt_tokens || 0 }}/{{ r.completion_tokens || 0 }}</td>
            <td>{{ r.fallback_count > 0 ? r.fallback_count : '-' }}</td>
          </tr>
        </tbody>
      </table>
      <div style="display: flex; justify-content: space-between; align-items: center; padding: 12px 0;">
        <span style="color: var(--gray-500); font-size: 13px;">共 {{ total }} 条，第 {{ page }} / {{ totalPages }} 页</span>
        <div style="display: flex; gap: 8px;">
          <button class="btn btn-outline btn-sm" :disabled="page <= 1" @click="load(page - 1)">上一页</button>
          <button class="btn btn-outline btn-sm" :disabled="page >= totalPages" @click="load(page + 1)">下一页</button>
        </div>
      </div>
    </div>

    <!-- 详情抽屉 -->
    <div v-if="detail" class="drawer-mask" @click.self="closeDetail">
      <div class="drawer">
        <div class="drawer-head">
          <div>
            <strong>日志 #{{ detail.id }}</strong>
            <span :class="['badge', detail.status === 'success' ? 'badge-success' : 'badge-danger', 'ml-8']">
              {{ detail.status === 'success' ? '成功' : '失败' }}
            </span>
          </div>
          <button class="btn btn-outline btn-sm" @click="closeDetail">关闭 ✕</button>
        </div>

        <div class="meta-grid">
          <div><span class="meta-k">时间</span><span class="meta-v">{{ fmtTime(detail.created_at) }}</span></div>
          <div><span class="meta-k">请求模型</span><span class="meta-v mono">{{ detail.requested_model || '-' }}</span></div>
          <div><span class="meta-k">路由</span><span class="meta-v mono">{{ (detail.routed_provider && detail.routed_model) ? detail.routed_provider + '/' + detail.routed_model : '-' }}</span></div>
          <div><span class="meta-k">HTTP</span><span class="meta-v mono">{{ detail.http_status ?? '-' }}</span></div>
          <div><span class="meta-k">延迟</span><span class="meta-v mono">{{ detail.latency_ms ? Math.round(detail.latency_ms) + 'ms' : '-' }}</span></div>
          <div><span class="meta-k">Token</span><span class="meta-v mono">{{ detail.prompt_tokens || 0 }}/{{ detail.completion_tokens || 0 }}</span></div>
          <div><span class="meta-k">回退</span><span class="meta-v mono">{{ detail.fallback_count || 0 }}</span></div>
          <div><span class="meta-k">用户IP</span><span class="meta-v mono">{{ detail.user_ip || '-' }}</span></div>
          <div v-if="detail.error_type" style="grid-column: 1 / -1;"><span class="meta-k">错误类型</span><span class="meta-v mono">{{ detail.error_type }}</span></div>
          <div v-if="detail.error_msg" style="grid-column: 1 / -1;"><span class="meta-k">错误信息</span><span class="meta-v err">{{ detail.error_msg }}</span></div>
        </div>

        <div class="body-section">
          <div class="body-head">
            <span>请求体 (Request)</span>
            <button class="btn btn-outline btn-sm" @click="copy(prettyRequest)">复制</button>
          </div>
          <pre class="code-block">{{ prettyRequest }}</pre>
        </div>

        <div class="body-section">
          <div class="body-head">
            <span>响应体 (Response)</span>
            <button class="btn btn-outline btn-sm" @click="copy(prettyResponse)">复制</button>
          </div>
          <pre class="code-block">{{ prettyResponse }}</pre>
        </div>
      </div>
    </div>
  </div>
</template>

<script>
import api from '../api.js'

export default {
  name: 'LogsView',
  data() {
    return {
      items: [],
      page: 1,
      total: 0,
      totalPages: 1,
      filterStatus: '',
      detail: null,
      loadingDetail: false
    }
  },
  mounted() {
    this.load(1)
  },
  methods: {
    async load(p) {
      this.page = p
      const params = { page: p, page_size: 10 }
      if (this.filterStatus) params.status = this.filterStatus
      try {
        const data = await api.getLogs(params)
        this.items = data.items || []
        this.total = data.total || 0
        this.totalPages = data.total_pages || 1
      } catch (e) {
        alert('加载日志失败: ' + e.message)
      }
    },
    fmtTime(ts) {
      if (!ts) return '-'
      const d = new Date(/[zZ]|[+-]\d/.test(ts) ? ts : ts + 'Z')
      if (Number.isNaN(d.getTime())) return ts
      return d.toLocaleString('zh-CN', { timeZone: 'Asia/Shanghai', hour12: false })
    },
    async openDetail(id) {
      this.loadingDetail = true
      try {
        this.detail = await api.getLogDetail(id)
      } catch (e) {
        alert('加载日志详情失败: ' + e.message)
      } finally {
        this.loadingDetail = false
      }
    },
    closeDetail() {
      this.detail = null
    },
    copy(text) {
      if (!text) return
      navigator.clipboard?.writeText(text).then(
        () => {},
        () => {}
      )
    },
    // 把可能被转义的 JSON 字符串逐层解开，并美化输出
    // 处理 tool_call.arguments 这类 "{\"command\":\"...\"}" 嵌套字符串
    deepUnescape(node, depth = 0) {
      if (depth > 25) return node
      if (typeof node === 'string') {
        const t = node.trim()
        if ((t.startsWith('{') && t.endsWith('}')) || (t.startsWith('[') && t.endsWith(']'))) {
          try {
            return this.deepUnescape(JSON.parse(node), depth + 1)
          } catch (_) {
            return node
          }
        }
        return node
      }
      if (Array.isArray(node)) return node.map((x) => this.deepUnescape(x, depth + 1))
      if (node && typeof node === 'object') {
        const out = {}
        for (const k of Object.keys(node)) out[k] = this.deepUnescape(node[k], depth + 1)
        return out
      }
      return node
    },
    pretty(str) {
      if (str === null || str === undefined || str === '') return '（空）'
      let obj
      try {
        obj = JSON.parse(str)
      } catch (_) {
        // 非 JSON：原样返回（可能是 [stream] 或纯文本）
        return str
      }
      try {
        return JSON.stringify(this.deepUnescape(obj), null, 2)
      } catch (_) {
        return JSON.stringify(obj, null, 2)
      }
    }
  },
  computed: {
    prettyRequest() {
      return this.detail ? this.pretty(this.detail.request_body) : ''
    },
    prettyResponse() {
      return this.detail ? this.pretty(this.detail.response_body) : ''
    }
  }
}
</script>

<style scoped>
.log-row {
  cursor: pointer;
}
.log-row:hover {
  background: var(--hover-bg, #f5f7fa);
}
.ml-8 {
  margin-left: 8px;
}
.mono {
  font-family: monospace;
}
.err {
  color: #e5484d;
  white-space: pre-wrap;
  word-break: break-word;
}
.drawer-mask {
  position: fixed;
  inset: 0;
  background: rgba(0, 0, 0, 0.45);
  display: flex;
  justify-content: flex-end;
  z-index: 1000;
}
.drawer {
  width: min(960px, 96vw);
  height: 100%;
  background: var(--bg, #fff);
  box-shadow: -4px 0 24px rgba(0, 0, 0, 0.25);
  overflow-y: auto;
  padding: 20px 24px 40px;
  box-sizing: border-box;
}
.drawer-head {
  display: flex;
  justify-content: space-between;
  align-items: center;
  position: sticky;
  top: 0;
  background: var(--bg, #fff);
  padding-bottom: 12px;
  border-bottom: 1px solid var(--border, #e5e7eb);
  margin-bottom: 16px;
  font-size: 16px;
}
.meta-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 8px 24px;
  margin-bottom: 18px;
}
.meta-grid > div {
  display: flex;
  gap: 8px;
  font-size: 13px;
  align-items: baseline;
}
.meta-k {
  color: var(--gray-500, #6b7280);
  min-width: 64px;
  flex-shrink: 0;
}
.meta-v {
  word-break: break-all;
}
.body-section {
  margin-bottom: 18px;
}
.body-head {
  display: flex;
  justify-content: space-between;
  align-items: center;
  font-weight: 600;
  font-size: 14px;
  margin-bottom: 8px;
}
.code-block {
  background: var(--code-bg, #0f172a);
  color: #e2e8f0;
  border-radius: 8px;
  padding: 14px 16px;
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-size: 12.5px;
  line-height: 1.55;
  white-space: pre-wrap;
  word-break: break-word;
  max-height: 46vh;
  overflow: auto;
  margin: 0;
}

/* 媒体类型标签 */
.media-tag {
  display: inline-block; width: 20px; height: 20px; line-height: 20px;
  text-align: center; border-radius: 4px; font-size: 11px; font-weight: 600;
}
.media-tag-chat { background: var(--bg-elevated); color: var(--text-muted); border: 1px solid var(--border-base); }
.media-tag-image { background: rgba(79, 70, 229, 0.15); color: #4f46e5; }
.media-tag-video { background: rgba(234, 88, 12, 0.15); color: #ea580c; }
</style>
