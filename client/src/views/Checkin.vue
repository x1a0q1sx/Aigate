<template>
  <div class="checkin-page">
    <PageHeader
      title="签到监控"
      subtitle="一键领取各平台每日免费积分/额度，并查看账号额度与签到历史。本页不在常规界面提供入口，直接访问 /providers/checkin。"
      icon="shield"
    >
      <template #actions>
        <button class="btn btn-primary" @click="runAll" :disabled="running">
          {{ running ? '签到中…' : '一键签到全部' }}
        </button>
        <button class="btn btn-outline" @click="load" :disabled="loading">刷新</button>
      </template>
    </PageHeader>

    <div v-if="errorText" class="alert alert-error">{{ errorText }}</div>
    <div v-if="runMsg" class="alert" :class="runMsgCls">{{ runMsg }}</div>

    <!-- 总览 -->
    <div class="summary-row">
      <div class="summary-card">
        <span class="summary-num">{{ summary.done_today }}/{{ summary.total_accounts }}</span>
        <span class="summary-label">今日已签到账号</span>
      </div>
      <div class="summary-card">
        <span class="summary-num">{{ summary.credit_today }}</span>
        <span class="summary-label">今日新领积分</span>
      </div>
      <div class="summary-card">
        <span class="summary-num small">{{ fmtTime(summary.last_run_at) || '—' }}</span>
        <span class="summary-label">上次运行{{ summary.last_run_trigger ? '（' + triggerLabel(summary.last_run_trigger) + '）' : '' }}</span>
      </div>
      <div class="summary-card">
        <span class="summary-num small">{{ fmtTime(summary.next_run_at) || '已关闭' }}</span>
        <span class="summary-label">下次自动签到（北京时间）</span>
      </div>
    </div>

    <!-- 按平台分组 -->
    <div v-for="p in providers" :key="p.provider_code" class="card provider-card">
      <div class="provider-head">
        <div>
          <h2>{{ platformLabel(p.provider_code) }}</h2>
          <span class="badge badge-neutral code-tag">{{ p.provider_code }}</span>
          <span v-if="p.supported === false" class="badge badge-neutral">上游无签到接口</span>
          <span v-else-if="p.supported === null" class="badge badge-neutral">待探测</span>
        </div>
        <div class="provider-head-right">
          <span class="muted">{{ p.done_today }}/{{ p.total_accounts }} 已签</span>
          <span v-if="p.credit_today > 0" class="badge badge-success">+{{ p.credit_today }}</span>
          <button
            v-if="p.supported === true"
            class="btn btn-outline btn-sm"
            :disabled="running"
            @click="runOne(p.provider_code)"
          >签到该平台</button>
        </div>
      </div>

      <div v-for="a in p.accounts" :key="a.id" class="acct-row">
        <div class="acct-main">
          <span class="acct-owner">{{ a.owner }}</span>
          <span class="badge" :class="statusCls(a.today_kind)">
            {{ statusLabel(a.today_kind, p.supported) }}
          </span>
          <span v-if="a.today_credit" class="acct-credit">+{{ a.today_credit }} 积分</span>
          <span v-if="a.today_streak_days" class="muted">连续 {{ a.today_streak_days }} 天</span>
          <span v-if="a.today_total_credits" class="muted">累计 {{ a.today_total_credits }}</span>
          <span v-if="a.today_activity" class="muted">· {{ a.today_activity }}</span>
        </div>
        <div v-if="a.today_message || a.today_error" class="acct-msg text-xs">
          {{ a.today_message }}<span v-if="a.today_error" class="acct-err">（{{ a.today_error }}）</span>
        </div>
        <!-- 额度（复用 OAuth 页的口径与样式） -->
        <div v-if="a.usage" class="usage-panel">
          <div v-if="a.usage.message" class="usage-msg text-xs">{{ a.usage.message }}</div>
          <div v-for="(q, name) in quotaRows(a.usage)" :key="name" class="quota-row">
            <span class="quota-name" :title="name">{{ name }}</span>
            <div class="quota-bar"><i :class="{ warn: quotaPct(q) > 85, hot: quotaPct(q) >= 100 }" :style="{ width: quotaPct(q) + '%' }"></i></div>
            <span class="quota-val">{{ quotaText(q) }}</span>
          </div>
        </div>
      </div>
    </div>

    <div v-if="!providers.length && !loading" class="empty-hint">
      还没有可签到的账号。请先到 <router-link to="/providers/oauth">OAuth 连接</router-link> 页连接平台账号。
    </div>

    <!-- 配置 -->
    <div class="card">
      <div class="card-head">
        <div>
          <h2>自动签到</h2>
          <p>按北京时间定时签到（上游活动按 UTC+8 刷新，默认 10:30 留 30 分钟余量）；服务重启错过时间点会自动补签当天。</p>
        </div>
        <span class="status-pill" :class="cfg.enabled ? 'ok' : 'muted'">{{ cfg.enabled ? '已启用' : '未启用' }}</span>
      </div>
      <div class="cfg-grid">
        <label class="checkbox-label"><input type="checkbox" v-model="cfg.enabled" /> 启用自动签到</label>
        <label class="checkbox-label"><input type="checkbox" v-model="cfg.startup_catchup" /> 启动补签</label>
        <label>小时（北京时间）<input v-model.number="cfg.hour" type="number" min="0" max="23" /></label>
        <label>分钟<input v-model.number="cfg.minute" type="number" min="0" max="59" /></label>
      </div>
      <div class="update-actions" style="margin-top: 12px;">
        <button class="btn btn-primary btn-sm" @click="saveCfg" :disabled="cfgSaving">
          {{ cfgSaving ? '保存中…' : '保存' }}
        </button>
      </div>
    </div>

    <!-- 历史 -->
    <div class="card">
      <div class="card-head">
        <h2>签到历史</h2>
        <span class="muted">近 {{ histDays }} 天 · {{ history.length }} 条</span>
      </div>
      <table v-if="history.length" class="hist-table">
        <thead>
          <tr>
            <th>时间</th><th>平台</th><th>账号</th><th>触发</th>
            <th>结果</th><th>积分</th><th>说明</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="h in history" :key="h.id">
            <td class="mono text-xs">{{ fmtTime(h.created_at) }}</td>
            <td>{{ platformLabel(h.provider_code) }}</td>
            <td>{{ h.owner }}</td>
            <td>{{ triggerLabel(h.trigger) }}</td>
            <td><span class="badge" :class="statusCls(h.kind)">{{ statusLabel(h.kind, true) }}</span></td>
            <td>{{ h.credit != null ? '+' + h.credit : '—' }}</td>
            <td class="text-xs muted hist-msg" :title="h.error || h.message">
              {{ h.error || h.message || '—' }}
            </td>
          </tr>
        </tbody>
      </table>
      <div v-else class="muted text-xs">暂无记录。</div>
    </div>
  </div>
</template>

<script>
import api from '../api'
import toast from '../toast'
import PageHeader from '../components/PageHeader.vue'

// 平台显示名（后端 provider_code → 人话）
const PLATFORM_LABELS = {
  codebuddy_cn: 'CodeBuddy CN (腾讯)',
  codebuddy_intl: 'CodeBuddy (International)',
  qoder: 'Qoder',
  u1s1: 'u1s1 (有一说一)',
}

export default {
  name: 'Checkin',
  components: { PageHeader },
  data() {
    return {
      loading: false, running: false, errorText: '',
      providers: [], summary: {}, history: [], histDays: 7,
      cfg: { enabled: true, hour: 10, minute: 30, startup_catchup: true },
      cfgSaving: false,
      runMsg: '', runMsgCls: 'alert-info',
    }
  },
  mounted() { this.load() },
  methods: {
    async load() {
      this.loading = true
      this.errorText = ''
      try {
        const d = await api.getCheckinOverview(true)
        this.providers = d.providers || []
        this.summary = d.summary || {}
        this.history = d.history || []
        if (d.config) this.cfg = { ...this.cfg, ...d.config }
      } catch (e) {
        this.errorText = '加载失败：' + e.message
      } finally {
        this.loading = false
      }
    },
    async runAll() { await this._run(null) },
    async runOne(code) { await this._run(code) },
    async _run(code) {
      this.running = true
      this.runMsg = ''
      try {
        const d = await api.runCheckin(code)
        const results = d.results || []
        const claimed = results.filter(r => r.kind === 'claimed')
        const already = results.filter(r => r.kind === 'already_claimed')
        const failed = results.filter(r => r.kind === 'failed')
        const credit = claimed.reduce((s, r) => s + (r.credit || 0), 0)
        if (!d.ran) {
          this.runMsg = d.message || '没有需要签到的账号'
          this.runMsgCls = 'alert-info'
        } else {
          let msg = `签到完成：新领 ${claimed.length} 个（+${credit} 积分）· 已领 ${already.length} 个`
          if (failed.length) msg += ` · 失败 ${failed.length} 个`
          this.runMsg = msg
          this.runMsgCls = failed.length ? 'alert-error' : 'alert-success'
          if (failed.length) {
            toast.error(`签到失败 ${failed.length} 个：` + failed.map(f =>
              `${this.platformLabel(f.provider_code)}/${f.owner}`).join('、'))
          } else {
            toast.success(msg)
          }
        }
        await this.load()
      } catch (e) {
        this.runMsg = '签到失败：' + e.message
        this.runMsgCls = 'alert-error'
      } finally {
        this.running = false
      }
    },
    async saveCfg() {
      this.cfgSaving = true
      try {
        this.cfg = { ...this.cfg, ...(await api.updateCheckinConfig(this.cfg)) }
        toast.success('签到配置已保存')
        await this.load()
      } catch (e) {
        toast.error('保存失败：' + e.message)
      } finally { this.cfgSaving = false }
    },
    platformLabel(code) { return PLATFORM_LABELS[code] || code },
    triggerLabel(t) {
      return { manual: '手动', scheduled: '定时', startup_catchup: '启动补签' }[t] || t || '—'
    },
    statusLabel(kind, supported) {
      if (supported === false) return '不支持'
      if (supported === null) return '待探测'
      return {
        claimed: '已签到', already_claimed: '今日已领', inactive: '活动未开启',
        unsupported: '不支持', failed: '失败',
      }[kind] || '未签到'
    },
    statusCls(kind) {
      if (kind === 'claimed') return 'badge-success'
      if (kind === 'already_claimed') return 'badge-neutral'
      if (kind === 'failed') return 'badge-error'
      if (kind === 'inactive' || kind === 'unsupported') return 'badge-neutral'
      return 'badge-neutral'
    },
    fmtTime(iso) {
      if (!iso) return ''
      // 后端给的是 UTC（带 Z）；按本地时区展示
      const d = new Date(iso)
      if (!Number.isFinite(d.getTime())) return iso
      return d.toLocaleString('zh-CN', { hour12: false })
    },
    // ── 额度展示（与 OAuthConnections.vue 口径一致）──
    quotaRows(entry) { return (entry && entry.quotas) || {} },
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
.summary-row {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
  gap: 12px;
  margin: 14px 0;
}
.summary-card {
  border: 1px solid var(--border-color, #1c2839);
  border-radius: 10px;
  background: var(--bg-card, var(--bg-surface, #0f1623));
  padding: 12px 14px;
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.summary-num { font-size: 22px; font-weight: 700; }
.summary-num.small { font-size: 13px; font-weight: 600; }
.summary-label { color: var(--text-muted, #7e8ea9); font-size: 12px; }
.provider-card { margin-top: 14px; }
.provider-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  flex-wrap: wrap;
  margin-bottom: 10px;
}
.provider-head-right { display: flex; align-items: center; gap: 8px; }
.code-tag { margin-left: 6px; }
.acct-row {
  border-top: 1px dashed var(--border-color, #1c2839);
  padding: 8px 0;
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.acct-main { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.acct-owner { font-weight: 600; }
.acct-credit { color: #34d399; font-weight: 600; }
.acct-msg { color: var(--text-muted, #7e8ea9); }
.acct-err { color: #f87171; }
.usage-panel { display: flex; flex-direction: column; gap: 6px; }
.quota-row {
  display: grid;
  grid-template-columns: minmax(80px, 1.4fr) 1fr minmax(120px, auto);
  align-items: center;
  gap: 8px;
  font-size: 12px;
}
.quota-name {
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  color: var(--text-muted, #a9b7cd);
}
.quota-bar {
  height: 6px; border-radius: 3px;
  background: rgba(126, 142, 169, 0.18);
  overflow: hidden;
}
.quota-bar i {
  display: block; height: 100%;
  background: var(--primary, #5b8dff);
  border-radius: 3px;
  transition: width 0.25s;
}
.quota-bar i.warn { background: #fbbf24; }
.quota-bar i.hot { background: #f87171; }
.quota-val { text-align: right; white-space: nowrap; }
.usage-msg { color: var(--text-muted, #7e8ea9); }
.cfg-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
  gap: 10px;
  align-items: center;
}
.cfg-grid label { display: flex; flex-direction: column; gap: 4px; font-size: 13px; }
.cfg-grid input[type="number"] {
  padding: 6px 8px; border: 1px solid var(--border-color, #1c2839);
  border-radius: 6px; background: var(--bg-input, var(--bg-card, #0f1623));
  color: inherit;
}
.checkbox-label { flex-direction: row !important; align-items: center; gap: 6px !important; }
.hist-table { width: 100%; font-size: 13px; }
.hist-table th { text-align: left; padding: 6px 8px; }
.hist-table td { padding: 6px 8px; border-top: 1px solid var(--border-color, #1c2839); }
.hist-msg { max-width: 320px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.empty-hint { color: var(--text-muted, #7e8ea9); padding: 24px 0; }
.muted { color: var(--text-muted, #7e8ea9); }
</style>
