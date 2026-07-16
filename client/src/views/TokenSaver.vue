<template>
  <div class="token-saver-page">
    <div class="page-header">
      <h1>Token 节省器</h1>
      <button class="btn btn-outline" @click="load" :disabled="loading">{{ loading ? '刷新中...' : '刷新' }}</button>
    </div>

    <div class="saver-grid">
      <section class="saver-card">
        <div class="card-head">
          <div>
            <h2>压缩工具输出 <a href="https://github.com/rtk-ai/rtk" target="_blank">RTK</a></h2>
            <p>压缩 git / grep / ls / tree / 日志等工具输出，通常可减少 60-90% 输入 token。</p>
          </div>
          <label class="switch-row"><input type="checkbox" v-model="rtk.enabled" /><span>{{ rtk.enabled ? '已启用' : '已关闭' }}</span></label>
        </div>
        <div class="form-row"><label>最小处理长度</label><input type="number" min="0" v-model.number="rtk.min_chars" /><span class="hint-inline">少于该字符数的消息不会压缩</span></div>
        <div class="rule-list">
          <div v-for="rule in rtk.rules" :key="rule.id" class="rule-item"><span class="dot" :class="{ on: rule.default_on }"></span><div><strong>{{ ruleName(rule.id) }}</strong><p>{{ rule.desc }}</p></div></div>
        </div>
      </section>

      <section class="saver-card">
        <div class="card-head"><div><h2>压缩上下文 <a href="https://github.com/chopratejas/headroom" target="_blank">Headroom</a></h2><p>通过 /v1/compress 在路由到模型前压缩 prompt，上游模型不会被调用。</p></div><span class="status-pill ok">已安装</span></div>
        <p class="hint">AIGate 已提供 /v1/compress 压缩端点。为指定 provider 设置每日 token 上限，达到后该 provider 暂时不进 auto 候选池。</p>
        <div v-for="(item, i) in headroomEntries" :key="i" class="headroom-row">
          <select v-model.number="item.provider_id"><option :value="null" disabled>选服务商</option><option v-for="pr in providers" :key="pr.id" :value="pr.id">{{ pr.name }}</option></select>
          <input type="number" v-model.number="item.daily_token_limit" min="0" placeholder="每日 token 限额" />
          <input v-model="item.label" placeholder="标签" class="label-input" />
          <button class="btn btn-outline btn-sm" @click="headroomEntries.splice(i, 1)">删除</button>
        </div>
        <button class="btn btn-outline btn-sm" @click="addHeadroom">+ 添加</button>
      </section>

      <section class="saver-card"><div class="card-head"><div><h2>压缩模型输出 <a href="https://github.com/JuliusBrussee/caveman" target="_blank">Caveman</a></h2><p>注入简短表达风格，倾向更短回答，减少输出 token。</p></div><label class="switch-row"><input type="checkbox" v-model="extra.caveman_enabled" /><span>{{ extra.caveman_enabled ? '已启用' : '已关闭' }}</span></label></div></section>

      <section class="saver-card"><div class="card-head"><div><h2>懒惰高级工程师 <a href="https://github.com/DietrichGebert/ponytail" target="_blank">Ponytail</a></h2><p>让模型偏向最小改动：YAGNI、复用标准库、能删除就不新增。</p></div><label class="switch-row"><input type="checkbox" v-model="extra.ponytail_enabled" /><span>{{ extra.ponytail_enabled ? '已启用' : '已关闭' }}</span></label></div></section>
    </div>

    <section class="saver-card preview-card">
      <div class="card-head"><div><h2>压缩效果预览</h2><p>输入一段 prompt，查看 RTK / Caveman / Ponytail 组合后的压缩效果。</p></div><button class="btn btn-primary" @click="previewCompress" :disabled="previewing">{{ previewing ? '压缩中...' : '试压缩' }}</button></div>
      <textarea v-model="previewText" class="preview-input" spellcheck="false"></textarea>
      <div v-if="previewResult" class="preview-stats"><span>原始 {{ previewResult.original_chars }} 字符</span><span>压缩后 {{ previewResult.compressed_chars }} 字符</span><span>节省 {{ previewResult.chars_saved }} 字符 / {{ Math.round((previewResult.savings_ratio || 0) * 100) }}%</span></div>
      <div v-if="previewResult" class="preview-columns"><div><h3>压缩前</h3><pre>{{ previewText }}</pre></div><div><h3>压缩后</h3><pre>{{ previewOutput }}</pre></div></div>
      <div v-if="previewResult" class="step-list"><span v-for="s in previewResult.steps" :key="s.id" class="status-pill" :class="s.applied ? 'ok' : 'muted'">{{ stepName(s.id) }} ? 节省 {{ s.chars_saved || 0 }} 字符</span></div>
    </section>

    <div class="actions-bar">
      <button class="btn btn-primary" @click="saveAll" :disabled="saving">{{ saving ? '保存中...' : '保存所有设置' }}</button>
      <span v-if="message" class="save-msg">{{ message }}</span>
    </div>
  </div>
</template>

<script>
import api from '../api'
export default {
  name: 'TokenSaver',
  data() { return { loading: false, saving: false, previewing: false, message: '', previewText: ['Please be very careful and thorough.', '', '```json', '{"example":"this is a long json example that can be folded by the token saver", "items": [1,2,3,4,5]}', '```', '', '', 'Repeat    spaces    and    blank lines.'].join('\n'), previewResult: null, rtk: { enabled: true, min_chars: 80, rules: [] }, extra: { caveman_enabled: false, ponytail_enabled: false }, headroomEntries: [], providers: [] } },
  computed: { previewOutput() { const m = this.previewResult?.messages?.[0]; return m && typeof m.content === 'string' ? m.content : '' } },
  mounted() { this.load() },
  methods: {
    ruleName(id) { return ({ fold_json_example: '折叠 JSON 示例', fold_long_policy: '折叠长篇安全策略', drop_please_be: '删除客套提示词', collapse_blank_lines: '折叠多余空行', collapse_spaces: '折叠重复空格' })[id] || id },
    stepName(id) { return ({ rtk: 'RTK', caveman: 'Caveman', ponytail: 'Ponytail' })[id] || id },
    async load() { this.loading = true; this.message = ''; try { const [rtk, extra, headroom, providers] = await Promise.all([api.getTokenSaver(), api.getSaverExtra(), api.getHeadroom(), api.getProviders()]); this.rtk = { enabled: !!rtk.enabled, min_chars: rtk.min_chars ?? 80, rules: rtk.rules || [] }; this.extra = { caveman_enabled: !!extra.caveman_enabled, ponytail_enabled: !!extra.ponytail_enabled }; this.headroomEntries = headroom || []; this.providers = providers || [] } catch (e) { this.message = '加载失败: ' + e.message } finally { this.loading = false } },
    async previewCompress() { this.previewing = true; this.message = ''; try { this.previewResult = await api.previewTokenSaver({ text: this.previewText, rtk_enabled: this.rtk.enabled, caveman_enabled: this.extra.caveman_enabled, ponytail_enabled: this.extra.ponytail_enabled }) } catch (e) { this.message = '预览失败: ' + e.message } finally { this.previewing = false } },
    addHeadroom() { this.headroomEntries.push({ provider_id: null, daily_token_limit: 50000, label: '' }) },
    async saveAll() {
      this.saving = true; this.message = ''
      try {
        const validHeadroom = this.headroomEntries.filter(e => e.provider_id && e.daily_token_limit > 0)
        await Promise.all([
          api.updateTokenSaver(this.rtk.enabled, this.rtk.min_chars),
          api.updateSaverExtra(this.extra),
          api.updateHeadroom(validHeadroom),
        ])
        this.message = '所有设置已保存'
      } catch (e) {
        this.message = '保存失败: ' + e.message
      } finally {
        this.saving = false
      }
    }
  }
}
</script>

<style scoped>
.token-saver-page { display: flex; flex-direction: column; gap: 16px; }
.page-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 4px; }
.page-header h1 { margin: 0; }
/* 单行单列：每个配置块上下排列，不再左右并排 */
.saver-grid { display: grid; grid-template-columns: 1fr; gap: 14px; }
.saver-card { background: var(--bg-elevated); border: 1px solid var(--border-soft); border-radius: 8px; padding: 18px; }
.card-head { display: flex; justify-content: space-between; gap: 18px; align-items: flex-start; }
h2 { margin: 0 0 6px; font-size: 18px; }
p { margin: 0; color: var(--text-secondary); line-height: 1.45; }
a { color: #3b82f6; text-decoration: none; }
.form-row { margin: 16px 0; display: flex; align-items: center; gap: 12px; flex-wrap: wrap; }
.form-row input { width: 120px; padding: 8px; }
.hint-inline { color: var(--text-muted); font-size: 13px; }
.switch-row { display: inline-flex; align-items: center; gap: 8px; white-space: nowrap; color: var(--text-secondary); }
.rule-list { display: grid; gap: 8px; margin: 12px 0 16px; }
.rule-item { display: flex; gap: 10px; padding: 10px; background: var(--bg-primary); border: 1px solid var(--border-soft); border-radius: 6px; }
.rule-item strong { font-size: 13px; }
.rule-item p { font-size: 13px; margin-top: 2px; }
.dot { width: 8px; height: 8px; margin-top: 5px; border-radius: 99px; background: var(--text-dim); flex: 0 0 auto; }
.dot.on { background: #22c55e; }
.status-pill { padding: 4px 8px; border-radius: 999px; font-size: 12px; border: 1px solid var(--border-soft); white-space: nowrap; }
.status-pill.ok { color: #22c55e; background: rgba(34,197,94,.12); }
.status-pill.muted { color: var(--text-muted); background: var(--bg-primary); }
.hint { margin-top: 12px; }
.mini-table { margin: 12px 0; display: grid; gap: 6px; }
.mini-row { display: flex; justify-content: space-between; padding: 8px 10px; border: 1px solid var(--border-soft); border-radius: 6px; background: var(--bg-primary); }
.actions-bar { display: flex; align-items: center; gap: 12px; }
.save-msg { color: var(--text-secondary); }
.preview-card { grid-column: 1 / -1; }
.preview-input { width: 100%; min-height: 130px; margin-top: 14px; padding: 10px; border: 1px solid var(--border-soft); border-radius: 6px; background: var(--bg-primary); color: var(--text-primary); font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; resize: vertical; }
.preview-stats { display: flex; gap: 10px; flex-wrap: wrap; margin: 12px 0; color: var(--text-secondary); }
.preview-columns { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }
.preview-columns h3 { margin: 0 0 6px; font-size: 13px; color: var(--text-secondary); }
.preview-columns pre { min-height: 120px; max-height: 320px; overflow: auto; margin: 0; padding: 10px; border-radius: 6px; border: 1px solid var(--border-soft); background: var(--bg-primary); color: var(--text-primary); white-space: pre-wrap; word-break: break-word; }
.step-list { display: flex; gap: 8px; flex-wrap: wrap; margin-top: 12px; }
@media (max-width: 900px) { .preview-columns { grid-template-columns: 1fr; } }

.headroom-row { display: flex; gap: 8px; align-items: center; margin: 8px 0; flex-wrap: wrap; }
.headroom-row select, .headroom-row input { padding: 6px; border: 1px solid var(--border-soft); border-radius: 6px; background: var(--bg-primary); color: var(--text-primary); }
.label-input { min-width: 140px; }
.btn-sm { padding: 4px 10px; font-size: 13px; margin-right: 8px; }
</style>
