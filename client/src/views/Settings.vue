<template>
  <div class="settings-page">
    <div class="page-header">
      <h1>设置</h1>
      <button class="btn btn-outline" @click="load" :disabled="loading">{{ loading ? '加载中...' : '刷新' }}</button>
    </div>

    <section class="settings-card">
      <div class="card-head">
        <div>
          <h2>关于 / 一键更新</h2>
          <p>从 GitHub 拉取最新代码并执行隔离迁移、测试、重启健康检查。</p>
        </div>
        <span class="status-pill" :class="updatePillClass">{{ updateStateText }}</span>
      </div>

      <div class="version-row">
        <div class="version-item">
          <span class="version-label">当前版本</span>
          <code class="mono">{{ currentSha || '未知' }}</code>
          <span v-if="currentMessage" class="hint-inline">{{ currentMessage }}</span>
        </div>
        <div class="version-item" v-if="updateInfo.remote">
          <span class="version-label">最新版本</span>
          <code class="mono">{{ updateInfo.remote.sha }}</code>
          <span v-if="updateInfo.behind != null" class="hint-inline">落后 {{ updateInfo.behind }} 个提交</span>
        </div>
      </div>

      <div class="update-actions">
        <button class="btn btn-primary" @click="checkUpdate" :disabled="checking || updateRunning">
          <AppIcon name="refresh" :size="14" />{{ checking ? '检查中...' : '检查更新' }}
        </button>
        <button class="btn btn-success" @click="doUpdate" :disabled="!updateInfo.update_available || applying || updateRunning">
          <AppIcon name="download" :size="14" />{{ applying ? '更新中...' : '立即更新' }}
        </button>
        <button class="btn btn-outline" @click="loadStatus" :disabled="updateRunning && !applying">
          <AppIcon name="eye" :size="14" />刷新状态
        </button>
        <button class="btn btn-outline" @click="createBackup" :disabled="manualBacking || updateRunning">
          <AppIcon name="database" :size="14" />{{ manualBacking ? '备份中...' : '创建恢复点' }}
        </button>
      </div>

      <p v-if="updateInfo.message" class="update-message" :class="{ ok: !updateInfo.update_available && currentSha }">{{ updateInfo.message }}</p>

      <dl v-if="updateStatusData.backup || updateStatusData.phase" class="update-facts">
        <div><dt>阶段</dt><dd>{{ phaseLabel(updateStatusData.phase) }}</dd></div>
        <div v-if="updateStatusData.backup"><dt>恢复点</dt><dd><code>{{ updateStatusData.backup }}</code></dd></div>
        <div v-if="updateStatusData.rollback_performed"><dt>回滚</dt><dd :class="updateState === 'rollback_failed' ? 'text-danger' : 'text-warning'">{{ updateStatusData.rollback_reason || '已恢复上一版本' }}</dd></div>
      </dl>

      <div v-if="updateInfo.commits && updateInfo.commits.length" class="update-detail">
        <h3>本次更新内容（{{ updateInfo.commits.length }} 个提交）</h3>
        <ul class="commit-list">
          <li v-for="c in updateInfo.commits" :key="c" class="commit-item">{{ c }}</li>
        </ul>
        <div v-if="updateInfo.files && updateInfo.files.length" class="file-list">
          <span v-for="f in updateInfo.files" :key="f" class="file-chip">{{ f }}</span>
        </div>
      </div>
    </section>

    <section class="settings-card" v-if="logTail">
      <div class="card-head">
        <div><h2>更新日志</h2><p>最近一次更新 / 正在执行的更新输出。</p></div>
      </div>
      <pre class="log-view">{{ logTail }}</pre>
    </section>

    <section class="settings-card">
      <div class="card-head">
        <div><h2>恢复点</h2><p>最近五份校验通过的事务快照。</p></div>
        <button class="btn btn-outline btn-sm" title="刷新恢复点" aria-label="刷新恢复点" @click="loadBackups"><AppIcon name="refresh" :size="14" /></button>
      </div>
      <div class="backup-list">
        <div v-for="backup in backups" :key="backup.name" class="backup-row">
          <div><strong>{{ backup.name }}</strong><span>{{ fmtTime(backup.refreshed_at || backup.created_at) }}</span></div>
          <code>{{ (backup.commit || 'unknown').slice(0, 8) }}</code>
          <span class="tabular">{{ fmtBytes(backup.database_bytes) }}</span>
          <span class="checksum">{{ backup.database_sha256 ? backup.database_sha256.slice(0, 12) : '无数据库' }}</span>
        </div>
        <div v-if="backups.length === 0" class="backup-empty">暂无恢复点</div>
      </div>
    </section>

    <section class="settings-card">
      <div class="card-head">
        <div>
          <h2>数据安全说明</h2>
          <p>更新过程中以下数据完整保留：</p>
        </div>
      </div>
      <ul class="safe-list">
        <li><strong>服务商配置</strong> — 名称 / 地址 / 鉴权方式 / 启用状态</li>
        <li><strong>模型配置</strong> — 模型清单、价格、优先级、自动选举标记</li>
        <li><strong>密钥</strong> — Fernet 加密存储，备份中亦为密文</li>
        <li><strong>组合路由顺序</strong> — combos 表完整保留，候选顺序不变</li>
        <li><strong>config.yaml</strong> — 加密密钥 / 代理 / 登录密码完全不触碰</li>
        <li><strong>恢复点</strong> — 数据库、config.yaml、前端产物及 SHA-256 清单（保留最近 5 份）</li>
        <li><strong>失败恢复</strong> — 验证或重启健康检查失败时恢复代码、数据库、配置与前端</li>
      </ul>
    </section>
  </div>
</template>

<script>
import api from '../api'
import AppIcon from '../components/AppIcon.vue'

export default {
  name: 'Settings',
  components: { AppIcon },
  data() {
    return {
      loading: false,
      checking: false,
      applying: false,
      manualBacking: false,
      currentSha: '',
      currentMessage: '',
      updateInfo: {},
      updateState: 'idle',
      updateStatusData: {},
      backups: [],
      logTail: '',
      pollTimer: null,
    }
  },
  computed: {
    updateRunning() {
      return this.updateState === 'running' || this.updateState === 'rolling_back'
    },
    updatePillClass() {
      if (this.updateRunning || this.applying) return 'warn'
      if (['rolled_back', 'rollback_failed', 'error'].includes(this.updateState)) return 'error'
      if (this.updateInfo.update_available) return 'warn'
      if (this.currentSha) return 'ok'
      return 'muted'
    },
    updateStateText() {
      if (this.applying || this.updateState === 'running') return '更新进行中'
      if (this.updateState === 'rolling_back') return '正在回滚'
      if (this.updateState === 'rolled_back') return '已自动回滚'
      if (this.updateState === 'rollback_failed') return '回滚失败'
      if (this.updateState === 'error') return '更新失败'
      if (this.updateInfo.update_available) return '有可用更新'
      if (this.currentSha) return '已是最新'
      return '未知'
    },
  },
  mounted() {
    this.load()
    this.pollTimer = setInterval(() => this.pollStatus(), 4000)
  },
  beforeUnmount() {
    if (this.pollTimer) clearInterval(this.pollTimer)
  },
  methods: {
    async load() {
      this.loading = true
      try {
        await Promise.all([this.loadStatus(), this.loadBackups()])
      } finally {
        this.loading = false
      }
    },
    async loadStatus() {
      try {
        const st = await api.updateStatus()
        this.updateState = st.state
        this.updateStatusData = st
        this.logTail = st.log_tail || ''
      } catch (e) {
        // 忽略轮询失败
      }
    },
    async checkUpdate() {
      this.checking = true
      try {
        const r = await api.checkUpdate()
        this.updateInfo = r
        this.currentSha = r.current?.sha || ''
        this.currentMessage = r.current?.message || ''
      } catch (e) {
        this.updateInfo = { message: '检查更新失败: ' + (e.response?.data?.detail || e.message) }
      } finally {
        this.checking = false
      }
    },
    async doUpdate() {
      if (!confirm('确定开始更新？更新过程中服务可能短暂重启，期间所有数据会被自动备份并完整保留。')) return
      this.applying = true
      try {
        const r = await api.applyUpdate()
        if (!r.ok) throw new Error(r.message || '更新任务未启动')
        this.updateState = 'running'
        if (r.log_tail !== undefined) this.logTail = r.log_tail
        await this.loadStatus()
      } catch (e) {
        alert('启动更新失败: ' + (e.response?.data?.detail || e.message))
        this.applying = false
      }
    },
    async pollStatus() {
      if (!this.updateRunning) return
      try {
        const st = await api.updateStatus()
        this.updateState = st.state
        this.updateStatusData = st
        this.logTail = st.log_tail || ''
        if (['finished', 'error', 'rolled_back', 'rollback_failed'].includes(st.state)) {
          this.applying = false
          this.updateInfo = await api.checkUpdate()
          this.currentSha = this.updateInfo.current?.sha || this.currentSha
        }
      } catch (e) {
        // 服务可能在重启，忽略
      }
    },
    async loadBackups() {
      try {
        const result = await api.getUpdateBackups()
        this.backups = result.items || []
      } catch (e) {
        this.backups = []
      }
    },
    async createBackup() {
      this.manualBacking = true
      try {
        const result = await api.createUpdateBackup()
        if (!result.ok) throw new Error(result.message || '创建失败')
        this.backups = result.items || []
      } catch (e) {
        alert('创建恢复点失败: ' + e.message)
      } finally {
        this.manualBacking = false
      }
    },
    phaseLabel(value) {
      return ({ preflight: '环境检查', pull: '拉取代码', build: '构建', verify: '隔离验证', health_check: '健康检查', rollback: '回滚', complete: '完成' })[value] || value || '—'
    },
    fmtTime(value) {
      if (!value) return '—'
      return new Date(value).toLocaleString('zh-CN', { hour12: false })
    },
    fmtBytes(value) {
      if (value == null) return '—'
      const mb = Number(value) / 1024 / 1024
      return mb >= 1 ? `${mb.toFixed(1)} MB` : `${Math.max(1, Math.round(Number(value) / 1024))} KB`
    },
  },
}
</script>

<style scoped>
.settings-page {
  display: flex;
  flex-direction: column;
  gap: var(--space-4);
}
.page-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: var(--space-2);
}
.page-header h1 {
  font-size: var(--text-2xl, 1.5rem);
  margin: 0;
}
.settings-card {
  background: var(--surface-2);
  border: 1px solid var(--border-base);
  border-radius: var(--radius-lg);
  padding: var(--space-5);
}
.card-head {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  gap: var(--space-3);
  margin-bottom: var(--space-3);
}
.card-head h2 {
  margin: 0 0 4px;
  font-size: var(--text-lg, 1.1rem);
}
.card-head p {
  margin: 0;
  color: var(--text-muted);
  font-size: var(--text-sm, 0.875rem);
}
.status-pill {
  padding: 3px 10px;
  border-radius: 999px;
  font-size: var(--text-xs, 0.75rem);
  white-space: nowrap;
}
.status-pill.ok { background: rgba(34, 197, 94, 0.15); color: #22c55e; }
.status-pill.warn { background: rgba(245, 158, 11, 0.15); color: #f59e0b; }
.status-pill.error { background: rgba(239, 68, 68, 0.15); color: #ef4444; }
.status-pill.muted { background: rgba(148, 163, 184, 0.15); color: #94a3b8; }
.version-row {
  display: flex;
  flex-wrap: wrap;
  gap: var(--space-6);
  margin-bottom: var(--space-4);
}
.version-item {
  display: flex;
  align-items: center;
  gap: var(--space-2);
}
.version-label {
  color: var(--text-muted);
  font-size: var(--text-sm, 0.875rem);
}
.mono {
  font-family: var(--font-mono, monospace);
  background: var(--surface-1);
  padding: 2px 8px;
  border-radius: 6px;
  font-size: var(--text-sm, 0.875rem);
}
.hint-inline {
  color: var(--text-dim);
  font-size: var(--text-xs, 0.75rem);
  max-width: 320px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.update-actions {
  display: flex;
  gap: var(--space-3);
  flex-wrap: wrap;
}
.update-message {
  margin-top: var(--space-3);
  color: var(--warning);
  font-size: var(--text-sm, 0.875rem);
}
.update-message.ok { color: #22c55e; }
.update-facts { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: var(--space-2); margin: var(--space-3) 0 0; padding-top: var(--space-3); border-top: 1px solid var(--border-base); }
.update-facts div { min-width: 0; }
.update-facts dt { font-size: var(--text-xs); color: var(--text-dim); }
.update-facts dd { margin: 4px 0 0; font-size: var(--text-sm); overflow-wrap: anywhere; }
.text-danger { color: #ef4444; }
.text-warning { color: #f59e0b; }
.update-detail {
  margin-top: var(--space-4);
  border-top: 1px solid var(--border-base);
  padding-top: var(--space-3);
}
.update-detail h3 {
  font-size: var(--text-base, 1rem);
  margin: 0 0 var(--space-2);
}
.commit-list {
  list-style: none;
  margin: 0;
  padding: 0;
}
.commit-item {
  font-family: var(--font-mono, monospace);
  font-size: var(--text-xs, 0.75rem);
  color: var(--text-primary);
  padding: 4px 0;
  border-bottom: 1px dashed var(--border-base);
}
.file-list {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin-top: var(--space-2);
}
.file-chip {
  background: var(--surface-1);
  border: 1px solid var(--border-base);
  border-radius: 6px;
  padding: 2px 8px;
  font-size: var(--text-xs, 0.72rem);
  color: var(--text-muted);
}
.log-view {
  background: var(--surface-1);
  border: 1px solid var(--border-base);
  border-radius: var(--radius-md);
  padding: var(--space-3);
  font-family: var(--font-mono, monospace);
  font-size: var(--text-xs, 0.75rem);
  white-space: pre-wrap;
  word-break: break-all;
  max-height: 360px;
  overflow: auto;
  color: var(--text-primary);
}
.backup-list { border-top: 1px solid var(--border-base); }
.backup-row { display: grid; grid-template-columns: minmax(220px, 1fr) 90px 90px 110px; gap: var(--space-3); align-items: center; min-height: 48px; border-bottom: 1px solid var(--border-base); font-size: var(--text-sm); }
.backup-row > div { min-width: 0; display: flex; flex-direction: column; }
.backup-row strong { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.backup-row div span, .checksum { color: var(--text-dim); font-size: var(--text-xs); }
.backup-row code, .checksum { font-family: var(--font-mono); }
.backup-empty { padding: var(--space-5); text-align: center; color: var(--text-dim); font-size: var(--text-sm); }
@media (max-width: 700px) { .backup-row { grid-template-columns: 1fr auto; padding: var(--space-2) 0; } .backup-row .checksum { display: none; } }
.safe-list {
  margin: 0;
  padding-left: var(--space-4);
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
  color: var(--text-primary);
  font-size: var(--text-sm, 0.875rem);
}
.safe-list strong {
  color: var(--text-primary);
}
</style>
