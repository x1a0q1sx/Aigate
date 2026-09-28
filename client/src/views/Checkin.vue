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

      <div v-if="p.note" class="provider-note text-xs">{{ p.note }}</div>

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
        <!-- 额度（复用统一 QuotaPanel：积分总量 + 赠包折叠，三页口径一致） -->
        <QuotaPanel v-if="a.usage" :entry="a.usage" />
      </div>
    </div>

    <div v-if="!providers.length && !loading" class="empty-hint">
      还没有可签到的账号。请先到 <router-link to="/providers/oauth">OAuth 连接</router-link> 页连接平台账号。
    </div>

    <!-- 成长中心（CodeBuddy Buddy 旅行 / 成长任务） -->
    <div class="card growth-card">
      <div class="card-head">
        <div>
          <h2>成长中心 <span class="badge badge-neutral">CodeBuddy</span></h2>
          <p>
            派 Buddy 旅行领礼物、接取成长任务换积分、自动开盲盒/抽奖。只自动做
            「客户端外能完成」的事（白名单任务接取），其余如实列出提示手动完成。
            盲盒/抽奖是消耗性操作，默认不自动执行，点按钮 = 手动授权这一轮。
          </p>
        </div>
        <div class="growth-head-actions">
          <button class="btn btn-outline btn-sm" @click="runGrowth(null, 'travel')" :disabled="growthRunning">
            处理旅行
          </button>
          <button class="btn btn-outline btn-sm" @click="runGrowth(null, 'task')" :disabled="growthRunning">
            接取任务
          </button>
          <button class="btn btn-outline btn-sm" @click="runGrowth(null, 'gacha')" :disabled="growthRunning">
            开盲盒
          </button>
          <button class="btn btn-outline btn-sm" @click="runGrowth(null, 'lottery')" :disabled="growthRunning">
            抽奖
          </button>
          <button class="btn btn-primary btn-sm" @click="runGrowth(null, null)" :disabled="growthRunning">
            {{ growthRunning ? '处理中…' : '一键处理全部' }}
          </button>
        </div>
      </div>

      <div v-if="growthMsg" class="alert" :class="growthMsgCls">{{ growthMsg }}</div>

      <div v-for="a in growth.accounts" :key="a.provider_code + '|' + a.owner" class="growth-acct">
        <div class="growth-acct-head">
          <span class="acct-owner">{{ a.owner }}</span>
          <span class="badge badge-neutral code-tag">{{ platformLabel(a.provider_code) }}</span>
          <span v-if="!a.ok" class="badge badge-neutral">不可用</span>
          <span v-else-if="!a.tasks || !a.tasks.length" class="badge badge-neutral">本版无此活动</span>
        </div>

        <div v-if="!a.ok" class="text-xs muted">{{ a.message || '成长中心不可用' }}</div>

        <template v-else>
          <!-- 旅行状态 -->
          <div class="growth-line">
            <span class="growth-k">旅行</span>
            <span class="badge" :class="travelCls(a.travel)">{{ travelLabel(a.travel) }}</span>
            <span v-if="a.travel && a.travel.location" class="muted">
              · {{ a.travel.location }}
            </span>
            <span v-if="a.travel && a.travel.arrive_at" class="muted">
              · {{ arriveText(a.travel.arrive_at) }}
            </span>
            <span v-if="a.travel && a.travel.daily_limit_reached" class="muted">· 今日次数已用完</span>
          </div>

          <!-- Buddy -->
          <div class="growth-line" v-if="a.buddy && a.buddy.name">
            <span class="growth-k">Buddy</span>
            <span class="acct-owner">{{ a.buddy.name }}</span>
            <span v-if="a.buddy.rarity" class="badge badge-info">{{ a.buddy.rarity }}</span>
            <span v-if="a.buddy.count" class="muted">· 已拥有 {{ a.buddy.count }} 只</span>
          </div>

          <!-- 任务进度 -->
          <div class="growth-line" v-if="a.task_summary">
            <span class="growth-k">任务</span>
            <span>{{ a.task_summary.completed }}/{{ a.task_summary.total }} 完成</span>
            <span v-if="a.task_summary.claimable_credits > 0" class="badge badge-success">
              待领 {{ a.task_summary.claimable_credits }} 积分
            </span>
          </div>

          <!-- 连续活跃 -->
          <div class="growth-line" v-if="a.streak">
            <span class="growth-k">活跃</span>
            <span>连续 {{ a.streak.days }} 天</span>
            <span v-if="a.streak.next_tier" class="muted">
              · 距 {{ a.streak.next_tier }} 档还差 {{ a.streak.next_tier_remaining }} 天
            </span>
            <span v-if="a.streak.makeup_cards" class="muted">· 补签卡 {{ a.streak.makeup_cards }}</span>
          </div>

          <!-- 盲盒 / 抽奖 -->
          <div class="growth-line" v-if="a.gacha">
            <span class="growth-k">盲盒</span>
            <span v-if="a.gacha.affordable > 0" class="badge badge-success">可开 {{ a.gacha.affordable }} 个</span>
            <span v-else class="muted">能量 {{ a.gacha.balance }} / 每开 {{ a.gacha.cost_per_open }}（暂不足）</span>
          </div>
          <div class="growth-line" v-if="a.lottery">
            <span class="growth-k">抽奖</span>
            <span v-if="a.lottery.chances > 0" class="badge badge-success">待抽 {{ a.lottery.chances }} 次</span>
            <span v-else class="muted">暂无抽奖次数</span>
          </div>

          <!-- 手动任务提示 -->
          <div v-if="a.manual_tasks && a.manual_tasks.length" class="text-xs muted growth-manual">
            需手动完成：{{ a.manual_tasks.join('、') }}
          </div>

          <!-- 任务明细（可折叠） -->
          <details v-if="a.tasks && a.tasks.length" class="growth-details">
            <summary class="text-xs muted">查看 {{ a.tasks.length }} 个任务明细</summary>
            <table class="hist-table">
              <thead>
                <tr><th>任务</th><th>状态</th><th>进度</th><th>奖励</th><th>可自动</th></tr>
              </thead>
              <tbody>
                <tr v-for="t in a.tasks" :key="t.code">
                  <td :title="t.guide">{{ t.title }}</td>
                  <td><span class="badge" :class="taskCls(t.state)">{{ taskLabel(t.state) }}</span></td>
                  <td class="mono text-xs">{{ t.current }}/{{ t.target }}</td>
                  <td class="text-xs">
                    <span v-if="t.credit">{{ t.credit }} 积分</span>
                    <span v-if="t.energy" class="muted"> +{{ t.energy }} 能量</span>
                    <span v-if="!t.credit && !t.energy">—</span>
                  </td>
                  <td class="text-xs">{{ t.automatable ? '✓' : '需手动' }}</td>
                </tr>
              </tbody>
            </table>
          </details>
        </template>
      </div>

      <div v-if="!growth.accounts.length" class="muted text-xs">
        没有 CodeBuddy 账号（成长中心仅支持 CodeBuddy CN / International）。
      </div>

      <!-- 成长中心配置 -->
      <div class="cfg-grid" style="margin-top: 12px;">
        <label class="checkbox-label"><input type="checkbox" v-model="growthCfg.enabled" /> 启用自动处理</label>
        <label class="checkbox-label"><input type="checkbox" v-model="growthCfg.travel" /> 自动旅行</label>
        <label class="checkbox-label"><input type="checkbox" v-model="growthCfg.tasks" /> 自动接取任务</label>
        <label class="checkbox-label"><input type="checkbox" v-model="growthCfg.gacha" /> 自动开盲盒（消耗能量）</label>
        <label class="checkbox-label"><input type="checkbox" v-model="growthCfg.lottery" /> 自动抽奖（消耗次数）</label>
        <label class="checkbox-label"><input type="checkbox" v-model="growthCfg.startup_catchup" /> 启动补跑</label>
        <label>小时（北京时间）<input v-model.number="growthCfg.hour" type="number" min="0" max="23" /></label>
        <label>分钟<input v-model.number="growthCfg.minute" type="number" min="0" max="59" /></label>
      </div>
      <div class="update-actions" style="margin-top: 12px;">
        <span class="muted text-xs" v-if="growth.next_run_at">
          下次自动处理：{{ fmtTime(growth.next_run_at) }}（北京时间）
        </span>
        <button class="btn btn-primary btn-sm" @click="saveGrowthCfg" :disabled="growthCfgSaving">
          {{ growthCfgSaving ? '保存中…' : '保存成长中心配置' }}
        </button>
      </div>
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
import QuotaPanel from '../components/QuotaPanel.vue'

// 平台显示名（后端 provider_code → 人话）
const PLATFORM_LABELS = {
  codebuddy_cn: 'CodeBuddy CN (腾讯)',
  codebuddy_intl: 'CodeBuddy (International)',
  qoder: 'Qoder',
  u1s1: 'u1s1 (有一说一)',
  lobsterai: 'LobsterAI (有道)',
  codearts: 'CodeArts Agent (华为)',
  trae: 'TRAE (字节)',
  freebuff: 'Freebuff (Codebuff)',
}

export default {
  name: 'Checkin',
  components: { PageHeader, QuotaPanel },
  data() {
    return {
      loading: false, running: false, errorText: '',
      providers: [], summary: {}, history: [], histDays: 7,
      cfg: { enabled: true, hour: 10, minute: 30, startup_catchup: true },
      cfgSaving: false,
      runMsg: '', runMsgCls: 'alert-info',
      // 成长中心（CodeBuddy Buddy 旅行 / 成长任务）
      growth: { accounts: [], next_run_at: null },
      growthCfg: { enabled: true, travel: true, tasks: true, gacha: false, lottery: false, hour: 11, minute: 0, startup_catchup: true },
      growthCfgSaving: false, growthRunning: false,
      growthMsg: '', growthMsgCls: 'alert-info',
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
      // 成长中心独立加载：失败不影响签到页主体
      try {
        const g = await api.getGrowth(true)
        this.growth = { accounts: g.accounts || [], next_run_at: g.next_run_at || null }
        if (g.config) this.growthCfg = { ...this.growthCfg, ...g.config }
      } catch (e) {
        this.growth = { accounts: [], next_run_at: null, error: e.message }
      }
    },
    // ── 成长中心 ──
    travelLabel(tr) {
      const s = (tr && tr.state) || 'unknown'
      return { idle: '空闲（可派发）', traveling: '旅行中', arrived: '已到达（可领奖）',
               locked: '本版无此活动', unknown: '未知' }[s] || s
    },
    travelCls(tr) {
      const s = (tr && tr.state) || 'unknown'
      if (s === 'arrived') return 'badge-success'
      if (s === 'traveling') return 'badge-info'
      return 'badge-neutral'
    },
    arriveText(ts) {
      if (!ts) return ''
      const left = Math.round((ts * 1000 - Date.now()) / 60000)
      if (left > 0) return `${left} 分钟后到达`
      return '已到达'
    },
    taskLabel(state) {
      return { claimed: '已领取', completed: '已完成待领', in_progress: '进行中',
               not_accepted: '未接取' }[state] || state
    },
    taskCls(state) {
      if (state === 'claimed') return 'badge-success'
      if (state === 'completed') return 'badge-warning'
      if (state === 'in_progress') return 'badge-info'
      return 'badge-neutral'
    },
    async runGrowth(code, action) {
      this.growthRunning = true
      this.growthMsg = ''
      try {
        const d = await api.runGrowth(code, action)
        const failed = (d.results || []).filter(r => r.kind === 'failed')
        const acted = (d.results || []).filter(
          r => ['traveled', 'claimed', 'accepted'].includes(r.kind))
        const credit = d.credit || 0
        const energy = d.energy || 0
        if (!d.ran) {
          this.growthMsg = d.message || '没有可处理的账号'
          this.growthMsgCls = 'alert-info'
        } else if (failed.length) {
          this.growthMsg = `执行 ${acted.length} 项、失败 ${failed.length} 项：`
            + failed.slice(0, 3).map(f => `${f.owner}·${f.action} ${f.message}`).join('；')
          this.growthMsgCls = 'alert-error'
        } else {
          this.growthMsg = acted.length
            ? `执行 ${acted.length} 项（+${credit} 积分 / +${energy} 能量）`
            : '无变化（旅行进行中、无待接任务或盲盒/抽奖无可消耗）'
          this.growthMsgCls = 'alert-success'
        }
        await this.load()
      } catch (e) {
        this.growthMsg = '处理失败：' + e.message
        this.growthMsgCls = 'alert-error'
      } finally {
        this.growthRunning = false
      }
    },
    async saveGrowthCfg() {
      this.growthCfgSaving = true
      try {
        const d = await api.updateGrowthConfig(this.growthCfg)
        this.growthCfg = { ...this.growthCfg, ...d }
        toast.success('成长中心配置已保存')
      } catch (e) {
        toast.error('保存失败：' + e.message)
      } finally {
        this.growthCfgSaving = false
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
.provider-note {
  color: var(--text-muted, #7e8ea9);
  background: rgba(126, 142, 169, 0.08);
  border-left: 2px solid var(--border-color, #1c2839);
  padding: 4px 8px;
  border-radius: 0 4px 4px 0;
  margin-bottom: 8px;
  line-height: 1.6;
}
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
/* 成长中心（Buddy 旅行 / 成长任务）：与签到卡片同构，独立区块 */
.growth-card { margin-top: 20px; }
.growth-head-actions { display: flex; gap: 8px; flex-wrap: wrap; }
.growth-acct {
  padding: 12px 0;
  border-top: 1px solid var(--border-color, #1c2839);
}
.growth-acct-head { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; margin-bottom: 6px; }
.growth-line {
  display: flex; align-items: center; gap: 8px; flex-wrap: wrap;
  font-size: 13px; margin-top: 4px;
}
.growth-k { color: var(--text-muted, #7e8ea9); min-width: 48px; }
.growth-manual { margin-top: 6px; line-height: 1.5; }
.growth-details { margin-top: 8px; }
.growth-details summary { cursor: pointer; }
</style>
