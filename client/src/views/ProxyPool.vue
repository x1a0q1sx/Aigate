<template>
  <div class="proxy-page">
    <h1 class="page-title text-2xl font-semibold">🌐 代理池</h1>
    <p class="text-sub">HTTP 代理轮换池：round-robin / weighted / random + 健康熔断</p>

    <!-- 顶部状态 -->
    <div class="status-row">
      <div class="badge" :class="status.enabled ? 'badge-on' : 'badge-off'">
        {{ status.enabled ? '已启用' : '已禁用' }}
      </div>
      <div class="badge badge-neut">{{ status.strategy || 'round_robin' }}</div>
      <div class="badge badge-neut">{{ (status.proxies || []).length }} 个</div>
      <button class="btn-toggle" @click="toggleEnabled">{{ status.enabled ? '禁用' : '启用' }}</button>
    </div>

    <!-- 策略选择 -->
    <h2 class="section-title">轮换策略</h2>
    <div class="strategy-row">
      <button v-for="s in ['round_robin', 'weighted', 'random']" :key="s"
              class="strat-btn" :class="{active: status.strategy === s}"
              @click="setStrategy(s)">{{ strategyLabel(s) }}</button>
    </div>

    <!-- 代理列表 -->
    <h2 class="section-title list-head">
      代理列表
      <button class="btn-clear" v-if="(status.proxies || []).length" @click="clearAll">清空全部</button>
    </h2>
    <table class="table" v-if="(status.proxies || []).length">
      <thead><tr>
        <th>#</th><th>名称</th><th>URL（脱敏）</th><th>权重</th><th>失败次数</th><th>冷却到期</th><th>操作</th>
      </tr></thead>
      <tbody>
        <tr v-for="(p, i) in status.proxies" :key="i">
          <td>{{ i + 1 }}</td>
          <td>{{ p.name || '—' }}</td>
          <td><code>{{ p.url }}</code></td>
          <td>{{ p.weight || 1 }}</td>
          <td :class="p.fail_count > 0 ? 'warn-cell' : ''">{{ p.fail_count || 0 }}</td>
          <td>{{ p.cooldown_until ? formatTime(p.cooldown_until) : '—' }}</td>
          <td><button class="btn-del" @click="removeProxy(i)" title="删除该代理">✕ 删除</button></td>
        </tr>
      </tbody>
    </table>
    <p v-else class="muted">暂未配置代理</p>

    <!-- 编辑表单 -->
    <h2 class="section-title">添加/编辑代理</h2>
    <div class="proxy-editor">
      <div class="form-row">
        <input v-model="newProxy.name" placeholder="名称（可空，如 us-1）" class="input" />
        <input v-model="newProxy.url" placeholder="http://user:pass@host:port" class="input" />
        <input v-model.number="newProxy.weight" type="number" min="1" max="10" placeholder="权重" class="input small" />
        <button class="btn-add" @click="addProxy">添加</button>
      </div>
      <p class="hint">支持 http/https/socks5 代理；weighted 策略下权重越大概率越高</p>
    </div>

    <!-- 密钥轮换状态 -->
    <h2 class="section-title">密钥轮换运行时状态</h2>
    <div class="rotator-table" v-if="rotator">
      <p class="hint">每 provider 多个 key 自动 round-robin；3 次连续失败 → 60 秒冷却；401/403 → 永久禁用</p>
      <pre class="json-block">{{ JSON.stringify(rotator, null, 2) }}</pre>
    </div>
    <p v-else class="muted">未启用或读取失败</p>
  </div>
</template>

<script>
import api from '../api.js'
export default {
  name: 'ProxyPool',
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
      } catch (e) { this.$emit('toast', '失败：' + e.message) }
    },
    async setStrategy(s) {
      try {
        await api.updateProxyPool({ strategy: s })
        await this.refresh()
      } catch (e) { this.$emit('toast', '失败：' + e.message) }
    },
    async addProxy() {
      if (!this.newProxy.url) { this.$emit('toast', 'URL 不能为空'); return }
      const proxies = [...(this.status.proxies || []), { ...this.newProxy }]
      try {
        const normalized = proxies.map(p => ({
          url: p.url, weight: p.weight || 1, name: p.name || p.url
        }))
        await api.updateProxyPool({ proxies: normalized })
        this.newProxy = { name: '', url: '', weight: 1 }
        await this.refresh()
      } catch (e) { this.$emit('toast', '失败：' + e.message) }
    },
    async removeProxy(i) {
      const raw = this.status.raw_proxies
      if (!raw) {
        // 后端未返回原始列表（旧版本），禁止基于脱敏 URL 回写，避免密码丢失
        this.$emit('toast', '后端版本不支持删除，请重启网关后重试')
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
      } catch (e) { this.$emit('toast', '删除失败：' + e.message) }
    },
    async clearAll() {
      if (!window.confirm('确认清空全部代理？此操作不可撤销。')) return
      try {
        await api.updateProxyPool({ proxies: [] })
        await this.refresh()
      } catch (e) { this.$emit('toast', '清空失败：' + e.message) }
    },
    formatTime(t) {
      if (!t) return '—'
      return t.replace('T', ' ').replace('Z', '').slice(0, 19)
    },
  },
}
</script>

<style scoped>
.proxy-page { padding: 20px; color: var(--text-primary); }
.page-title { margin: 0 0 6px 0; }
.text-sub { color: var(--text-muted); margin: 0 0 20px 0; font-size: 14px; }
.status-row { display: flex; align-items: center; gap: 10px; margin-bottom: 24px; }
.badge { padding: 4px 12px; border-radius: 12px; font-size: 12px; font-weight: 500; }
.badge-on { background: var(--green, #10b981); color: white; }
.badge-off { background: var(--alert-error-bg, #fee2e2); color: var(--alert-error-text, #b91c1c); }
.badge-neut { background: var(--bg-elevated); color: var(--text-secondary); border: 1px solid var(--border-base); }
.btn-toggle { padding: 6px 16px; background: var(--accent-primary, #4f46e5); color: white; border: none; border-radius: 4px; cursor: pointer; }
.section-title { margin: 24px 0 12px; font-size: 16px; font-weight: 600; }
.strategy-row { display: flex; gap: 8px; margin-bottom: 18px; }
.strat-btn { padding: 8px 16px; background: var(--bg-elevated); color: var(--text-secondary); border: 1px solid var(--border-base); border-radius: 6px; cursor: pointer; }
.strat-btn.active { background: var(--accent-primary, #4f46e5); color: white; border-color: transparent; }
.table { width: 100%; border-collapse: collapse; margin-bottom: 16px; }
.table th, .table td { padding: 10px 14px; border-bottom: 1px solid var(--border-base); text-align: left; font-size: 13px; }
.warn-cell { color: var(--alert-error-text, #b91c1c); }
.muted { color: var(--text-muted); font-size: 13px; }
.proxy-editor { background: var(--bg-elevated); padding: 16px; border-radius: 8px; border: 1px solid var(--border-base); max-width: 720px; }
.form-row { display: flex; gap: 8px; align-items: center; }
.input { padding: 8px; border-radius: 4px; border: 1px solid var(--border-base); background: var(--bg-input, transparent); color: var(--text-primary); flex: 1; }
.input.small { max-width: 100px; }
.btn-add { padding: 8px 14px; background: var(--green, #10b981); color: white; border: none; border-radius: 4px; cursor: pointer; }
.list-head { display: flex; align-items: center; gap: 12px; }
.btn-del { padding: 4px 10px; background: var(--alert-error-bg, #fee2e2); color: var(--alert-error-text, #b91c1c); border: 1px solid var(--alert-error-text, #b91c1c); border-radius: 4px; cursor: pointer; font-size: 12px; }
.btn-del:hover { filter: brightness(0.95); }
.btn-clear { padding: 5px 12px; background: transparent; color: var(--alert-error-text, #b91c1c); border: 1px solid var(--alert-error-text, #b91c1c); border-radius: 4px; cursor: pointer; font-size: 13px; margin-left: auto; }
.btn-clear:hover { background: var(--alert-error-bg, #fee2e2); }
.hint { color: var(--text-muted); font-size: 12px; margin-top: 10px; }
.rotator-table { background: var(--bg-elevated); padding: 14px; border-radius: 8px; border: 1px solid var(--border-base); }
.json-block { white-space: pre-wrap; font-family: 'Menlo', monospace; font-size: 12px; background: var(--bg-code, #1e293b); color: #f8fafc; padding: 12px; border-radius: 6px; max-height: 360px; overflow: auto; }
</style>
