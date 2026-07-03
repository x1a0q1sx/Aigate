<template>
  <div>
    <h1 style="margin-bottom: 20px;">请求日志 📋</h1>
    <div class="card" style="margin-bottom: 16px; display: flex; gap: 12px; align-items: center;">
      <select v-model="filterStatus" @change="load(1)" style="width: auto;">
        <option value="">全部状态</option>
        <option value="success">成功</option>
        <option value="error">错误</option>
      </select>
      <button class="btn btn-outline" @click="load(1)">刷新</button>
    </div>
    <div class="card">
      <table>
        <thead>
          <tr>
            <th>时间</th>
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
            <td colspan="7" style="text-align: center; padding: 32px; color: var(--gray-500);">暂无日志</td>
          </tr>
          <tr v-for="r in items" :key="r.id">
            <td style="font-size: 12px; white-space: nowrap;">{{ fmtTime(r.created_at) }}</td>
            <td style="font-family: monospace; font-size: 12px;">{{ r.requested_model }}</td>
            <td style="font-family: monospace; font-size: 12px;">
              <span v-if="r.routed_provider && r.routed_model">{{ r.routed_provider }}/{{ r.routed_model }}</span>
              <span v-else style="color: #999;">-</span>
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
      filterStatus: ''
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
    }
  }
}
</script>
