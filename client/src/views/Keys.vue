<template>
  <div class="keys-page">
    <div class="page-header">
      <div>
        <h1>网关密钥</h1>
        <p>发给下游客户端（朋友 / 多设备 / 团队）的独立钥匙，支持限速与每日预算。</p>
      </div>
      <div class="header-actions">
        <button class="btn btn-outline" @click="load" :disabled="loading">刷新</button>
        <button class="btn btn-primary" @click="showCreate = true">新建密钥</button>
      </div>
    </div>

    <div class="hint-card">
      <p><strong>用法</strong>：客户端把密钥放进 <code>Authorization: Bearer gk-xxx</code>（或 <code>x-api-key</code> / <code>?key=</code>）。主密钥（config.yaml 里的 aigate_api_key）始终可用且不受预算限制。</p>
    </div>

    <!-- 新建弹层 -->
    <div v-if="showCreate" class="modal-mask" @click.self="showCreate = false">
      <div class="modal">
        <h3>新建网关密钥</h3>
        <div class="form-grid">
          <label>备注<input v-model="form.name" placeholder="给谁用，如：我的笔记本" /></label>
          <label>每分钟请求上限 RPM（空=不限）<input v-model="form.rpm_limit" type="number" min="1" placeholder="60" /></label>
          <label>每日 Token 上限（空=不限）<input v-model="form.daily_token_limit" type="number" min="1" placeholder="1000000" /></label>
          <label>每日费用上限 USD（空=不限）<input v-model="form.daily_cost_limit_usd" type="number" step="0.01" min="0" placeholder="5.00" /></label>
          <label>超限动作
            <select v-model="form.over_limit_action">
              <option value="reject">拒绝请求（429）</option>
              <option value="warn">放行（仅记录）</option>
            </select>
          </label>
          <label>过期时间（空=永不过期）<input v-model="form.expires_at" type="datetime-local" /></label>
        </div>
        <div class="modal-actions">
          <button class="btn btn-outline" @click="showCreate = false">取消</button>
          <button class="btn btn-primary" @click="create" :disabled="creating">{{ creating ? '创建中...' : '创建' }}</button>
        </div>
      </div>
    </div>

    <!-- 明文只显示一次 -->
    <div v-if="plaintext" class="modal-mask">
      <div class="modal">
        <h3>密钥已创建（明文仅此一次，请立即保存）</h3>
        <div class="plaintext-box"><code>{{ plaintext }}</code>
          <button class="btn btn-outline btn-sm" @click="copyPlaintext">复制</button>
        </div>
        <div class="modal-actions">
          <button class="btn btn-primary" @click="closePlaintext">我已保存</button>
        </div>
      </div>
    </div>

    <div class="table-card">
      <table v-if="keys.length">
        <thead>
          <tr><th>备注</th><th>密钥</th><th>状态</th><th>限制</th><th>最近使用</th><th>操作</th></tr>
        </thead>
        <tbody>
          <tr v-for="k in keys" :key="k.id">
            <td class="name-cell">{{ k.name || '(无备注)' }}</td>
            <td><code class="mono">{{ k.key_prefix }}…</code></td>
            <td>
              <span class="pill" :class="k.enabled ? 'ok' : 'muted'">{{ k.enabled ? '启用' : '停用' }}</span>
              <span v-if="expired(k)" class="pill error">已过期</span>
            </td>
            <td class="limit-cell">
              <span v-if="k.rpm_limit">RPM {{ k.rpm_limit }}</span>
              <span v-if="k.daily_token_limit">Token {{ fmtNum(k.daily_token_limit) }}/日</span>
              <span v-if="k.daily_cost_limit_usd">${{ k.daily_cost_limit_usd }}/日</span>
              <span v-if="!k.rpm_limit && !k.daily_token_limit && !k.daily_cost_limit_usd" class="dim">不限</span>
              <span class="dim">超限:{{ k.over_limit_action === 'warn' ? '放行' : '拒绝' }}</span>
            </td>
            <td class="dim">{{ k.last_used_at ? fmtTime(k.last_used_at) : '从未' }}</td>
            <td class="action-cell">
              <button class="btn btn-outline btn-sm" @click="showUsage(k)">用量</button>
              <button class="btn btn-outline btn-sm" @click="toggle(k)">{{ k.enabled ? '停用' : '启用' }}</button>
              <button class="btn btn-danger btn-sm" @click="remove(k)">删除</button>
            </td>
          </tr>
        </tbody>
      </table>
      <div v-else class="empty">还没有网关密钥，点右上角「新建密钥」创建一把。</div>
    </div>

    <!-- 用量弹层 -->
    <div v-if="usage" class="modal-mask" @click.self="usage = null">
      <div class="modal">
        <h3>今日用量 — {{ usage.key.name || ('#' + usage.key.id) }}</h3>
        <div class="usage-grid">
          <div><span class="dim">请求</span><strong>{{ usage.usage.requests }}</strong></div>
          <div><span class="dim">Token</span><strong>{{ fmtNum(usage.usage.tokens) }}<template v-if="usage.usage.token_limit"> / {{ fmtNum(usage.usage.token_limit) }}</template></strong></div>
          <div><span class="dim">费用</span><strong>${{ usage.usage.cost.toFixed(4) }}<template v-if="usage.usage.cost_limit"> / ${{ usage.usage.cost_limit }}</template></strong></div>
        </div>
        <p v-if="usage.usage.over_token || usage.usage.over_cost" class="over-tip">⚠️ 今日预算已超限{{ usage.key.over_limit_action === 'warn' ? '（当前动作：放行）' : '（新请求将被拒绝）' }}</p>
        <div class="modal-actions">
          <button class="btn btn-primary" @click="usage = null">关闭</button>
        </div>
      </div>
    </div>
  </div>
</template>

<script>
import api, { apiPatch } from '../api'
import toast from '../toast'

export default {
  name: 'Keys',
  data() {
    return {
      keys: [],
      loading: false,
      showCreate: false,
      creating: false,
      plaintext: '',
      form: { name: '', rpm_limit: '', daily_token_limit: '', daily_cost_limit_usd: '', over_limit_action: 'reject', expires_at: '' },
      usage: null,
    }
  },
  mounted() { this.load() },
  methods: {
    async load() {
      this.loading = true
      try {
        const r = await api.getGatewayKeys()
        this.keys = r.keys || []
      } catch (e) {
        toast.error('加载失败: ' + e.message)
      } finally { this.loading = false }
    },
    async create() {
      this.creating = true
      try {
        const body = {
          name: this.form.name,
          rpm_limit: this.form.rpm_limit ? Number(this.form.rpm_limit) : null,
          daily_token_limit: this.form.daily_token_limit ? Number(this.form.daily_token_limit) : null,
          daily_cost_limit_usd: this.form.daily_cost_limit_usd ? Number(this.form.daily_cost_limit_usd) : null,
          over_limit_action: this.form.over_limit_action,
          expires_at: this.form.expires_at ? new Date(this.form.expires_at).toISOString() : null,
        }
        const r = await api.createGatewayKey(body)
        this.plaintext = r.plaintext
        this.showCreate = false
        this.form = { name: '', rpm_limit: '', daily_token_limit: '', daily_cost_limit_usd: '', over_limit_action: 'reject', expires_at: '' }
        this.load()
      } catch (e) {
        toast.error('创建失败: ' + e.message)
      } finally { this.creating = false }
    },
    copyPlaintext() {
      navigator.clipboard?.writeText(this.plaintext)
      toast.success('已复制到剪贴板')
    },
    closePlaintext() { this.plaintext = ''; this.load() },
    async toggle(k) {
      try {
        await api.updateGatewayKey(k.id, { enabled: !k.enabled })
        k.enabled = !k.enabled
      } catch (e) { toast.error('操作失败: ' + e.message) }
    },
    async remove(k) {
      if (!confirm(`确定删除密钥「${k.name || '#' + k.id}」？使用该密钥的客户端将立即失效。`)) return
      try {
        await api.deleteGatewayKey(k.id)
        this.load()
      } catch (e) { toast.error('删除失败: ' + e.message) }
    },
    async showUsage(k) {
      try { this.usage = await api.gatewayKeyUsage(k.id) }
      catch (e) { toast.error('查询失败: ' + e.message) }
    },
    expired(k) {
      return k.expires_at && new Date(k.expires_at) < new Date()
    },
    fmtTime(v) { return v ? new Date(v).toLocaleString('zh-CN', { hour12: false }) : '—' },
    fmtNum(n) { return Number(n).toLocaleString('zh-CN') },
  },
}
</script>

<style scoped>
.keys-page { display: flex; flex-direction: column; gap: var(--space-4); }
.page-header { display: flex; justify-content: space-between; align-items: flex-start; gap: var(--space-3); }
.page-header h1 { margin: 0 0 4px; font-size: var(--text-2xl, 1.5rem); }
.page-header p { margin: 0; color: var(--text-muted); font-size: var(--text-sm); }
.header-actions { display: flex; gap: var(--space-2); }
.hint-card { background: var(--surface-2); border: 1px solid var(--border-base); border-radius: var(--radius-md); padding: var(--space-3) var(--space-4); font-size: var(--text-sm); color: var(--text-muted); }
.hint-card code { background: var(--surface-1); padding: 1px 6px; border-radius: 4px; }
.table-card { background: var(--surface-2); border: 1px solid var(--border-base); border-radius: var(--radius-lg); overflow-x: auto; }
table { width: 100%; border-collapse: collapse; font-size: var(--text-sm); }
th, td { padding: 10px 14px; text-align: left; border-bottom: 1px solid var(--border-base); white-space: nowrap; }
th { color: var(--text-muted); font-weight: 500; font-size: var(--text-xs); }
.name-cell { max-width: 200px; overflow: hidden; text-overflow: ellipsis; }
.limit-cell { display: flex; flex-direction: column; gap: 2px; white-space: normal; }
.limit-cell .dim { font-size: var(--text-xs); }
.action-cell { display: flex; gap: 6px; }
.pill { padding: 2px 8px; border-radius: 999px; font-size: var(--text-xs); }
.pill.ok { background: rgba(34, 197, 94, .15); color: #22c55e; }
.pill.error { background: rgba(239, 68, 68, .15); color: #ef4444; margin-left: 4px; }
.pill.muted { background: rgba(148, 163, 184, .15); color: #94a3b8; }
.empty { padding: var(--space-6); text-align: center; color: var(--text-dim); }
.dim { color: var(--text-dim); }
.mono { font-family: var(--font-mono, monospace); }
.modal-mask { position: fixed; inset: 0; background: rgba(0, 0, 0, .45); display: flex; align-items: center; justify-content: center; z-index: 100; }
.modal { background: var(--surface-2); border: 1px solid var(--border-base); border-radius: var(--radius-lg); padding: var(--space-5); width: min(560px, 92vw); max-height: 85vh; overflow: auto; }
.modal h3 { margin: 0 0 var(--space-4); }
.form-grid { display: flex; flex-direction: column; gap: var(--space-3); }
.form-grid label { display: flex; flex-direction: column; gap: 4px; font-size: var(--text-sm); color: var(--text-muted); }
.form-grid input, .form-grid select { background: var(--surface-1); border: 1px solid var(--border-base); border-radius: 6px; padding: 8px 10px; color: var(--text-primary); font-size: var(--text-sm); }
.modal-actions { display: flex; justify-content: flex-end; gap: var(--space-2); margin-top: var(--space-4); }
.plaintext-box { display: flex; gap: var(--space-2); align-items: center; background: var(--surface-1); border: 1px dashed var(--border-base); border-radius: 6px; padding: var(--space-3); word-break: break-all; }
.usage-grid { display: grid; grid-template-columns: repeat(3, 1fr); gap: var(--space-3); margin: var(--space-3) 0; }
.usage-grid div { display: flex; flex-direction: column; gap: 4px; background: var(--surface-1); border-radius: 8px; padding: var(--space-3); }
.over-tip { color: #f59e0b; font-size: var(--text-sm); }
.btn { cursor: pointer; border: 1px solid transparent; border-radius: 6px; padding: 7px 14px; font-size: var(--text-sm); }
.btn-primary { background: var(--primary, #2f5fe0); color: #fff; }
.btn-outline { background: transparent; border-color: var(--border-base); color: var(--text-primary); }
.btn-danger { background: rgba(239, 68, 68, .12); color: #ef4444; }
.btn-sm { padding: 4px 10px; font-size: var(--text-xs); }
.btn:disabled { opacity: .5; cursor: not-allowed; }
</style>
