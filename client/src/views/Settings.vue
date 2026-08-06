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
          <p>从 GitHub 拉取最新代码并就地升级。更新前自动备份数据库，config.yaml（密钥 / 代理 / 登录密码）与数据归档全程不受影响。</p>
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
      </div>

      <p v-if="updateInfo.message" class="update-message" :class="{ ok: !updateInfo.update_available && currentSha }">{{ updateInfo.message }}</p>

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
        <li><strong>数据库</strong> — 更新前自动在线备份至 data/backups/（保留最近 5 份）</li>
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
      currentSha: '',
      currentMessage: '',
      updateInfo: {},
      updateState: 'idle',
      logTail: '',
      pollTimer: null,
    }
  },
  computed: {
    updateRunning() {
      return this.updateState === 'running'
    },
    updatePillClass() {
      if (this.updateRunning || this.applying) return 'warn'
      if (this.updateInfo.update_available) return 'warn'
      if (this.currentSha) return 'ok'
      return 'muted'
    },
    updateStateText() {
      if (this.updateRunning || this.applying) return '更新进行中'
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
        await this.loadStatus()
      } finally {
        this.loading = false
      }
    },
    async loadStatus() {
      try {
        const st = await api.updateStatus()
        this.updateState = st.state
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
        this.logTail = st.log_tail || ''
        if (st.state === 'finished' || st.state === 'error') {
          this.applying = false
          this.updateInfo = await api.checkUpdate()
          this.currentSha = this.updateInfo.current?.sha || this.currentSha
        }
      } catch (e) {
        // 服务可能在重启，忽略
      }
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
