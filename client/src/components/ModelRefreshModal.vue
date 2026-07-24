<template>
  <div v-if="visible" class="modal-overlay" @click.self="$emit('close')">
    <div class="modal-content refresh-modal">
      <div class="rf-head">
        <span class="rf-check">{{ result.error ? '⚠️' : '✅' }}</span>
        <h3>{{ result.error ? '模型刷新失败' : '模型刷新完成' }}</h3>
      </div>

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

      <div class="modal-actions">
        <button class="btn btn-primary" @click="$emit('close')">好的</button>
      </div>
    </div>
  </div>
</template>

<script>
export default {
  name: 'ModelRefreshModal',
  props: {
    visible: { type: Boolean, default: false },
    result: { type: Object, default: () => ({}) },
  },
  emits: ['close'],
  data() {
    return { showDetails: false }
  },
  computed: {
    totalAdded() {
      return (this.result.added_details || []).reduce((s, p) => s + (p.models ? p.models.length : 0), 0)
    },
    totalRemoved() {
      return (this.result.removed_details || []).reduce((s, p) => s + (p.models ? p.models.length : 0), 0)
    },
  },
}
</script>

<style scoped>
.refresh-modal {
  max-width: min(560px, 92vw);
  width: 92vw;
}
.rf-head {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-bottom: 14px;
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
  max-height: 50vh;
  overflow-y: auto;
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
