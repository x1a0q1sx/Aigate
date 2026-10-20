<template>
  <div v-if="visible" class="modal-overlay" @click.self="$emit('close')">
    <div class="modal-content refresh-modal">
      <div class="rf-head">
        <span class="rf-check">{{ result.error ? '⚠️' : '✅' }}</span>
        <h3>{{ result.error ? '模型刷新失败' : '模型刷新完成' }}</h3>
      </div>

      <!-- 整体滚动区：汇总/失败列表/明细一起滚动，不再只让明细单独滚 -->
      <div class="rf-body">
        <!-- 错误 -->
        <div v-if="result.error" class="rf-error">{{ result.error }}</div>

        <!-- 汇总 -->
        <div class="rf-summary" v-if="!result.error">
          <span class="rf-chip rf-added">新增 {{ result.added }}</span>
          <span class="rf-chip rf-updated">更新 {{ result.updated }}</span>
          <span class="rf-chip rf-removed" v-if="result.removed > 0">删除 {{ result.removed }}</span>
          <span class="rf-chip">共 {{ result.total }} 个</span>
          <span class="rf-chip" v-if="result.pricing_updated">价格 {{ result.pricing_updated }}</span>
          <span class="rf-chip" v-if="result.metric_updated">成功率 {{ result.metric_updated }}</span>
          <span class="rf-chip rf-failed" v-if="result.failed_details && result.failed_details.length">
            失败/超时 {{ result.failed_details.length }}
          </span>
          <span class="rf-chip" v-if="result.duration_ms">耗时 {{ (result.duration_ms / 1000).toFixed(1) }}s</span>
        </div>

        <!-- 失败/超时的服务商（并发刷新的关键反馈 + 就地禁用） -->
        <div class="rf-section" v-if="result.failed_details && result.failed_details.length">
          <div class="rf-section-title failed">
            ⏱️ 失败/超时（{{ result.failed_details.length }} 个，已跳过不等待）
            <button v-if="failedUndisabled.length > 1" class="btn btn-outline btn-xs rf-disable-all"
                    :disabled="disablingAll" @click="disableAllFailed">
              {{ disablingAll ? '禁用中...' : `全部禁用（${failedUndisabled.length}）` }}
            </button>
          </div>
          <div class="rf-provider" v-for="p in result.failed_details" :key="'f'+p.provider_id">
            <div class="rf-provider-row">
              <div class="rf-provider-name">{{ p.provider_name }}</div>
              <span v-if="isDisabled(p.provider_id)" class="rf-disabled-tag">已禁用</span>
              <button v-else class="btn btn-danger btn-xs"
                      :disabled="disablingId === p.provider_id || disablingAll"
                      @click="disableProvider(p)">
                {{ disablingId === p.provider_id ? '禁用中...' : '禁用' }}
              </button>
            </div>
            <div class="rf-fail-err">{{ p.error }}</div>
          </div>
        </div>

        <!-- 可点击展开的下拉按钮 -->
        <button class="rf-toggle" v-if="!result.error" @click="showDetails = !showDetails">
          <span>{{ showDetails ? '收起详情' : '查看新增/删除明细' }}</span>
          <span class="rf-caret" :class="{ open: showDetails }">▾</span>
        </button>

        <!-- 展开区 -->
        <div v-if="showDetails && !result.error" class="rf-details">
          <!-- 新增 -->
          <div class="rf-section" v-if="result.added_details && result.added_details.length">
            <div class="rf-section-title added">🟢 新增模型（{{ totalAdded }}）</div>
            <div class="rf-provider" v-for="p in result.added_details" :key="'a'+p.provider_id">
              <div class="rf-provider-name">{{ p.provider_name }}</div>
              <ul class="rf-model-list">
                <li v-for="(m, i) in p.models" :key="i">
                  <span class="rf-m-name">{{ m.display_name }}</span>
                  <span class="rf-m-id" v-if="m.display_name !== m.model_id">{{ m.model_id }}</span>
                </li>
              </ul>
            </div>
          </div>

          <!-- 删除 -->
          <div class="rf-section" v-if="result.removed_details && result.removed_details.length">
            <div class="rf-section-title removed">🔴 删除模型（{{ totalRemoved }}）</div>
            <div class="rf-provider" v-for="p in result.removed_details" :key="'r'+p.provider_id">
              <div class="rf-provider-name">{{ p.provider_name }}</div>
              <ul class="rf-model-list">
                <li v-for="(m, i) in p.models" :key="i">
                  <span class="rf-m-name">{{ m.display_name }}</span>
                  <span class="rf-m-id" v-if="m.display_name !== m.model_id">{{ m.model_id }}</span>
                </li>
              </ul>
            </div>
          </div>

          <!-- 空 -->
          <div class="rf-empty" v-if="!(result.added_details && result.added_details.length) && !(result.removed_details && result.removed_details.length)">
            本次刷新无新增/删除的模型
          </div>
        </div>
      </div>

      <div class="modal-actions">
        <button class="btn btn-primary" @click="$emit('close')">好的</button>
      </div>
    </div>
  </div>
</template>

<script>
import api from '../api.js'
import toast from '../toast.js'

export default {
  name: 'ModelRefreshModal',
  props: {
    visible: { type: Boolean, default: false },
    result: { type: Object, default: () => ({}) },
  },
  emits: ['close', 'provider-disabled'],
  data() {
    return { showDetails: false, disablingId: null, disablingAll: false, disabledIds: [] }
  },
  computed: {
    totalAdded() {
      return (this.result.added_details || []).reduce((s, p) => s + (p.models ? p.models.length : 0), 0)
    },
    totalRemoved() {
      return (this.result.removed_details || []).reduce((s, p) => s + (p.models ? p.models.length : 0), 0)
    },
    failedUndisabled() {
      return (this.result.failed_details || []).filter((p) => !this.isDisabled(p.provider_id))
    },
  },
  watch: {
    // 每次打开新结果时重置「本次已禁用」标记
    visible(v) {
      if (v) this.disabledIds = []
    },
  },
  methods: {
    isDisabled(providerId) {
      // 后端回包带 enabled 状态（刷新时已禁用的服务商同样会失败，无需再禁用）
      if (this.disabledIds.includes(providerId)) return true
      const p = (this.result.failed_details || []).find((x) => x.provider_id === providerId)
      return !!(p && p.enabled === false)
    },
    async disableProvider(p) {
      this.disablingId = p.provider_id
      try {
        await api.updateProvider(p.provider_id, { enabled: false })
        if (!this.disabledIds.includes(p.provider_id)) this.disabledIds.push(p.provider_id)
        toast.success(`服务商「${p.provider_name}」已禁用，请求将自动跳过`)
        this.$emit('provider-disabled', p.provider_id)
      } catch (e) {
        toast.error((e.response?.data?.detail) || '禁用失败：' + e.message)
      } finally {
        this.disablingId = null
      }
    },
    async disableAllFailed() {
      this.disablingAll = true
      const targets = this.failedUndisabled
      let ok = 0
      const failed = []
      for (const p of targets) {
        try {
          await api.updateProvider(p.provider_id, { enabled: false })
          if (!this.disabledIds.includes(p.provider_id)) this.disabledIds.push(p.provider_id)
          ok += 1
          this.$emit('provider-disabled', p.provider_id)
        } catch (e) {
          failed.push(p.provider_name)
        }
      }
      this.disablingAll = false
      if (ok) toast.success(`已禁用 ${ok} 个失败/超时的服务商`)
      if (failed.length) toast.error('部分禁用失败：' + failed.join('、'))
    },
  },
}
</script>

<style scoped>
.refresh-modal {
  max-width: min(560px, 92vw);
  width: 92vw;
}
/* 汇总 + 失败列表 + 明细统一滚动（原来只有 .rf-details 单独滚，内容一多就只剩一条缝） */
.rf-body {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  padding-right: 4px;
}
.rf-head {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-bottom: 14px;
  flex-shrink: 0;
}
.rf-head h3 {
  margin: 0;
  font-size: 18px;
}
.rf-check { font-size: 22px; }
.rf-error {
  background: #fef2f2;
  border: 1px solid #fecaca;
  color: #dc2626;
  padding: 10px 12px;
  border-radius: 8px;
  font-size: 13px;
  margin-bottom: 14px;
  word-break: break-word;
}
.rf-summary {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  margin-bottom: 14px;
}
.rf-chip {
  padding: 4px 10px;
  border-radius: 999px;
  font-size: 13px;
  background: var(--gray-100);
  color: var(--gray-700);
  border: 1px solid var(--border-soft);
}
.rf-chip.rf-added { color: #16a34a; border-color: #bbf7d0; background: #f0fdf4; }
.rf-chip.rf-updated { color: #2563eb; border-color: #bfdbfe; background: #eff6ff; }
.rf-chip.rf-removed { color: #dc2626; border-color: #fecaca; background: #fef2f2; }
.rf-chip.rf-failed { color: #d97706; border-color: #fde68a; background: #fffbeb; }
.rf-section-title.failed { color: #d97706; display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
.rf-disable-all { margin-left: auto; }
.rf-provider-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  margin-bottom: 4px;
}
.rf-provider-row .rf-provider-name { margin-bottom: 0; }
.rf-disabled-tag {
  font-size: 12px;
  color: var(--gray-500);
  border: 1px solid var(--border-soft);
  border-radius: 999px;
  padding: 1px 8px;
}
.rf-fail-err {
  font-size: 12px;
  color: #b45309;
  word-break: break-word;
}

.rf-toggle {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  background: transparent;
  border: 1px solid var(--border-soft);
  color: var(--primary);
  padding: 6px 12px;
  border-radius: 8px;
  cursor: pointer;
  font-size: 13px;
  transition: background 0.15s;
}
.rf-toggle:hover { background: var(--gray-100); }
.rf-caret { transition: transform 0.2s; }
.rf-caret.open { transform: rotate(180deg); }

.rf-details {
  margin-top: 14px;
  border-top: 1px solid var(--border-soft);
  padding-top: 12px;
}
.rf-section { margin-bottom: 14px; }
.rf-section-title {
  font-size: 13px;
  font-weight: 600;
  margin-bottom: 6px;
}
.rf-section-title.added { color: #16a34a; }
.rf-section-title.removed { color: #dc2626; }
.rf-provider {
  background: var(--gray-50);
  border: 1px solid var(--border-soft);
  border-radius: 8px;
  padding: 8px 10px;
  margin-bottom: 8px;
}
.rf-provider-name {
  font-size: 13px;
  font-weight: 600;
  color: var(--gray-700);
  margin-bottom: 4px;
}
.rf-model-list {
  margin: 0;
  padding-left: 18px;
}
.rf-model-list li {
  font-size: 13px;
  color: var(--gray-700);
  line-height: 1.7;
}
.rf-m-name { font-weight: 500; }
.rf-m-id {
  font-family: monospace;
  font-size: 11px;
  color: var(--gray-500);
  margin-left: 6px;
}
.rf-empty {
  font-size: 13px;
  color: var(--gray-500);
  text-align: center;
  padding: 8px 0;
}
</style>
