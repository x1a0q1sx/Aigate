<template>
  <div class="proxy-page">
    <PageHeader title="代理池" icon="globe" subtitle="HTTP 代理轮换池：round-robin / weighted / random + 健康熔断" />

    <!-- 顶部状态 -->
    <div class="status-row">
      <span class="badge" :class="status.enabled ? 'badge-success' : 'badge-danger'">
        {{ status.enabled ? '已启用' : '已禁用' }}
      </span>
      <span class="badge badge-neutral">{{ status.strategy || 'round_robin' }}</span>
      <span class="badge badge-neutral">{{ (status.proxies || []).length }} 个</span>
      <button class="btn btn-sm" :class="status.enabled ? 'btn-danger' : 'btn-primary'" @click="toggleEnabled">
        {{ status.enabled ? '禁用' : '启用' }}
      </button>
    </div>

    <!-- 策略选择 -->
    <h2 class="section-title">轮换策略</h2>
    <div class="strategy-row">
      <button v-for="s in ['round_robin', 'weighted', 'random']" :key="s"
              class="btn btn-sm" :class="status.strategy === s ? 'btn-primary' : 'btn-outline'"
              @click="setStrategy(s)">{{ strategyLabel(s) }}</button>
    </div>

    <!-- 代理列表 -->
    <h2 class="section-title list-head">
      代理列表
      <button class="btn btn-sm btn-danger" v-if="(status.proxies || []).length" @click="clearAll">
        <AppIcon name="trash" :size="13" />
        清空全部
      </button>
    </h2>
    <div class="table-wrap" v-if="(status.proxies || []).length">
      <table>
        <thead><tr>
          <th>#</th><th>名称</th><th>URL（脱敏）</th><th>权重</th><th>失败次数</th><th>冷却到期</th><th>操作</th>
        </tr></thead>
        <tbody>
          <tr v-for="(p, i) in status.proxies" :key="i">
            <td>{{ i + 1 }}</td>
            <td>{{ p.name || '—' }}</td>
            <td><code class="mono">{{ p.url }}</code></td>
            <td>{{ p.weight || 1 }}</td>
            <td :class="p.fail_count > 0 ? 'text-danger' : ''">{{ p.fail_count || 0 }}</td>
            <td>{{ p.cooldown_until ? formatTime(p.cooldown_until) : '—' }}</td>
            <td><button class="btn btn-xs btn-danger" @click="removeProxy(i)" title="删除该代理">
              <AppIcon name="trash" :size="12" />
              删除
            </button></td>
          </tr>
        </tbody>
      </table>
    </div>
    <EmptyState v-else icon="globe" title="暂未配置代理" hint="在下方添加 HTTP 代理以启用轮换池" />

    <!-- 编辑表单 -->
    <h2 class="section-title">添加/编辑代理</h2>
    <div class="card proxy-editor">
      <div class="form-row">
        <input v-model="newProxy.name" placeholder="名称（可空，如 us-1）" class="input" />
        <input v-model="newProxy.url" placeholder="http://user:pass@host:port" class="input" />
        <input v-model.number="newProxy.weight" type="number" min="1" max="10" placeholder="权重" class="input input-small" />
        <button class="btn btn-primary" @click="addProxy">
          <AppIcon name="plus" :size="14" />
          添加
        </button>
      </div>
      <p class="hint">支持 http/https/socks5 代理；weighted 策略下权重越大概率越高</p>
    </div>

    <!-- 密钥轮换状态 -->
    <h2 class="section-title">密钥轮换运行时状态</h2>
    <div class="card" v-if="rotator">
      <p class="hint">每 provider 多个 key 自动 round-robin；3 次连续失败 → 60 秒冷却；401/403 → 永久禁用</p>
      <pre class="json-block mono">{{ JSON.stringify(rotator, null, 2) }}</pre>
    </div>
    <EmptyState v-else icon="key" title="未启用或读取失败" small />
  </div>
</template>

<script>
import api from '../api.js'
import toast from '../toast.js'
import AppIcon from '../components/AppIcon.vue'
import PageHeader from '../components/PageHeader.vue'
import EmptyState from '../components/EmptyState.vue'

export default {
  name: 'ProxyPool',
  components: { AppIcon, PageHeader, EmptyState },
  data() {
    return {
      status: { enabled: false, strategy: 'round_robin', proxies: [] },
      newProxy: { name: '', url: '', weight: 1 },
      rotator: null,
    }
  },
  async mounted() { await this.refresh() },
  methods: {
    async refresh() {
      try {
        const [s, r] = await Promise.all([api.getProxyPool(), api.getKeyRotatorStatus()])
        this.status = s
        this.rotator = r
      } catch (e) { console.error('proxy fetch failed', e) }
    },
    strategyLabel(s) {
      return { round_robin: 'Round Robin', weighted: 'Weighted', random: 'Random' }[s] || s
    },
    async toggleEnabled() {
      try {
        await api.updateProxyPool({ enabled: !this.status.enabled })
        await this.refresh()
      } catch (e) { toast.error('失败：' + e.message) }
    },
    async setStrategy(s) {
      try {
        await api.updateProxyPool({ strategy: s })
        await this.refresh()
      } catch (e) { toast.error('失败：' + e.message) }
    },
    async addProxy() {
      if (!this.newProxy.url) { toast.error('URL 不能为空'); return }
      const proxies = [...(this.status.proxies || []), { ...this.newProxy }]
      try {
        const normalized = proxies.map(p => ({
          url: p.url, weight: p.weight || 1, name: p.name || p.url
        }))
        await api.updateProxyPool({ proxies: normalized })
        this.newProxy = { name: '', url: '', weight: 1 }
        await this.refresh()
      } catch (e) { toast.error('失败：' + e.message) }
    },
    async removeProxy(i) {
      const raw = this.status.raw_proxies
      if (!raw) {
        // 后端未返回原始列表（旧版本），禁止基于脱敏 URL 回写，避免密码丢失
        toast.error('后端版本不支持删除，请重启网关后重试')
        return
      }
      if (i < 0 || i >= raw.length) return
      const next = raw.slice()
      next.splice(i, 1)
      const normalized = next.map(p => ({
        url: p.url, weight: p.weight || 1, name: p.name || p.url
      }))
      try {
        await api.updateProxyPool({ proxies: normalized })
        await this.refresh()
      } catch (e) { toast.error('删除失败：' + e.message) }
    },
    async clearAll() {
      if (!window.confirm('确认清空全部代理？此操作不可撤销。')) return
      try {
        await api.updateProxyPool({ proxies: [] })
        await this.refresh()
      } catch (e) { toast.error('清空失败：' + e.message) }
    },
    formatTime(t) {
      if (!t) return '—'
      return t.replace('T', ' ').replace('Z', '').slice(0, 19)
    },
  },
}
</script>

<style scoped>
.proxy-page {
  padding: var(--space-5);
  color: var(--text-primary);
}

/* 顶部状态栏 */
.status-row {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  margin-bottom: var(--space-6);
}

/* 区段标题 */
.section-title {
  margin: var(--space-6) 0 var(--space-3);
  font-size: var(--text-lg);
  font-weight: 600;
}
.list-head {
  display: flex;
  align-items: center;
  gap: var(--space-3);
}
.list-head .btn {
  margin-left: auto;
}

/* 策略按钮组 */
.strategy-row {
  display: flex;
  gap: var(--space-2);
  margin-bottom: var(--space-5);
}

/* 代理编辑表单 */
.proxy-editor {
  max-width: 720px;
}
.form-row {
  display: flex;
  gap: var(--space-2);
  align-items: center;
}
.input-small {
  max-width: 100px;
}
.hint {
  color: var(--text-muted);
  font-size: var(--text-sm);
  margin-top: var(--space-3);
}

/* JSON 展示 */
.json-block {
  white-space: pre-wrap;
  font-size: var(--text-sm);
  background: var(--bg-sunken);
  color: var(--text-primary);
  padding: var(--space-3);
  border-radius: var(--radius-sm);
  max-height: 360px;
  overflow: auto;
  margin: 0;
}

/* 表格操作列 */
td .btn {
  white-space: nowrap;
}

/* 响应式 */
@media (max-width: 768px) {
  .form-row {
    flex-wrap: wrap;
  }
  .form-row .input {
    min-width: 140px;
  }
}
</style>
