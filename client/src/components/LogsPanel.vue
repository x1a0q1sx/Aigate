<template>
  <div>
    <!-- 请求日志 -->
    <div class="card">
      <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 12px; flex-wrap: wrap; gap: 8px;">
        <h2>请求日志</h2>
        <div style="display: flex; gap: 8px; flex-wrap: wrap;">
          <select v-model="filterLogType" @change="loadPage(1)" style="width: auto;" title="日志类型：请求调用 / 模型列表刷新">
            <option value="">请求日志</option>
            <option value="refresh">模型刷新</option>
          </select>
          <select v-model="filterStatus" @change="loadPage(1)" style="width: auto;">
            <option value="">全部状态</option>
            <option value="pending">⏳ 待响应</option>
            <option value="success">成功</option>
            <option value="error">失败</option>
          </select>
          <select v-model="providerFilter" @change="loadPage(1)" style="width: auto;">
            <option value="">全部服务商</option>
            <option v-for="p in logProviders" :key="p" :value="p">{{ p }}</option>
          </select>
          <button class="btn btn-outline btn-sm" @click="exportLogs('csv')" title="导出近 7 天请求日志（CSV）">导出 CSV</button>
          <button class="btn btn-outline btn-sm" @click="exportLogs('json')" title="导出近 7 天请求日志（JSON）">导出 JSON</button>
          <button class="btn btn-outline btn-sm" style="color: var(--warning);"
                  title="清零归档后保留的累计统计（实时日志统计保留）" @click="resetSummary">重置统计数据</button>
        </div>
      </div>
      <table>
        <thead>
          <tr>
            <th>时间</th>
            <th>日志类型</th>
            <th>请求模型</th>
            <th>路由到</th>
            <th>状态</th>
            <th title="首字延迟 / 总延迟">延迟（首字/总）</th>
            <th>Token</th>
            <th title="按模型单价估算的本次请求成本">成本</th>
            <th>代理</th>
            <th>操作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-if="items.length === 0">
            <td colspan="10" style="text-align: center; padding: 32px; color: var(--gray-500);">{{ providerFilter === 'refresh' ? '暂无模型刷新日志（点一次「刷新模型列表」即可看到）' : (filterLogType === 'refresh' ? '暂无模型刷新日志（点一次「刷新模型列表」即可看到）' : '暂无请求日志') }}</td>
          </tr>
          <template v-for="r in items" :key="(r.log_type || 'request') + '-' + r.id">
            <!-- 模型刷新日志行：服务商/结果/增删明细 -->
            <tr v-if="r.log_type === 'refresh'">
              <td style="font-size: 12px; white-space: nowrap;">{{ fmtTime(r.created_at) }}</td>
              <td><span class="badge badge-info" style="font-size: 11px;">模型刷新</span></td>
              <td style="font-family: monospace; font-size: 12px;">{{ r.provider_name || '-' }}</td>
              <td style="font-size: 12px;">{{ r.trigger === 'scheduled' ? '定时' : '手动' }}</td>
              <td>
                <span :class="['badge', r.status === 'success' ? 'badge-success' : 'badge-danger']" style="font-size: 11px;" :title="r.error || ''">{{ r.status === 'success' ? '成功' : '失败' }}</span>
              </td>
              <td style="font-family: monospace;">{{ fmtLatency(null, r.duration_ms) }}</td>
              <td style="font-family: monospace; font-size: 12px;" :title="refreshChangesTitle(r)">
                <span v-if="r.added" style="color: var(--success);">+{{ r.added }}</span>
                <span v-if="r.updated" style="color: #2b8aef;"> ~{{ r.updated }}</span>
                <span v-if="r.removed" style="color: var(--danger);"> -{{ r.removed }}</span>
                <span v-if="!r.added && !r.removed && r.status === 'success'" style="color: var(--gray-500);">无变化</span>
              </td>
              <td style="font-family: monospace; font-size: 12px;">
                {{ r.pricing_updated ? '价' + r.pricing_updated : '-' }}
                <!-- 来源：seed=上游无模型列表端点，回退内置种子；pricing=定价接口兜底建模。非真实拉取，标警告色 -->
                <span v-if="r.list_source === 'seed'" class="badge badge-warning" style="font-size: 10px; margin-left: 4px;" :title="r.list_note || '静态种子兜底'">种子</span>
                <span v-else-if="r.list_source === 'pricing'" class="badge badge-warning" style="font-size: 10px; margin-left: 4px;" :title="r.list_note || '定价兜底'">定价</span>
              </td>
              <td></td>
              <td><button class="btn btn-outline btn-sm" @click="showDetail(r)">详情</button></td>
            </tr>
            <!-- 请求日志行（原有） -->
            <tr v-else>
            <td style="font-size: 12px; white-space: nowrap;">{{ fmtTime(r.created_at) }}</td>
            <td><span class="badge badge-neutral" style="font-size: 11px;">请求</span></td>
            <td style="font-family: monospace; font-size: 12px;">{{ r.requested_model || '-' }}</td>
            <td style="font-family: monospace; font-size: 12px;">
              <span v-if="r.routed_provider">{{ r.routed_provider }}/{{ r.routed_model }}</span>
              <span v-else style="color: var(--text-muted);">-</span>
            </td>
            <td>
              <span :class="['badge', r.status === 'pending' ? 'badge-info' : (r.status === 'success' ? 'badge-success' : 'badge-danger')]" style="font-size: 11px;">{{ r.status === 'pending' ? '⏳ 待响应' : (r.status === 'success' ? '成功' : '失败') }}</span>
              <span v-if="r.archived" title="详细内容已归档瘦身，可从归档列表恢复" style="font-size: 11px;">📦</span>
            </td>
            <td style="font-family: monospace;">{{ fmtLatency(r.ttft_ms, r.latency_ms) }}</td>
            <td style="font-family: monospace; font-size: 12px;">{{ r.prompt_tokens || 0 }}/{{ r.completion_tokens || 0 }}<span v-if="r.cache_read_tokens" style="color: #2b8aef;" :title="`缓存读 ${r.cache_read_tokens} / 写 ${r.cache_write_tokens || 0}`"> · 缓存{{ r.cache_read_tokens }}</span></td>
            <td style="font-family: monospace; font-size: 12px;">{{ r.status === 'success' ? ('$' + (r.estimated_cost_usd || 0).toFixed(4)) : '-' }}</td>
            <td>
              <span v-if="r.used_proxy" style="color: #22c55e; font-size: 12px;">🟢 代理</span>
              <span v-else style="color: var(--gray-500); font-size: 12px;">⚪ 直连</span>
            </td>
            <td><button class="btn btn-outline btn-sm" @click="showDetail(r)">详情</button></td>
            </tr>
          </template>
        </tbody>
      </table>
      <div style="display: flex; justify-content: space-between; align-items: center; padding: 12px 0;">
        <span style="color: var(--gray-500); font-size: 13px;">共 {{ total }} 条 · 第 {{ page }} / {{ totalPages }} 页</span>
        <div style="display: flex; gap: 8px; align-items: center;">
          <button class="btn btn-outline btn-sm" :disabled="page <= 1" @click="loadPage(page - 1)">上一页</button>
          <button class="btn btn-outline btn-sm" :disabled="page >= totalPages" @click="loadPage(page + 1)">下一页</button>
          <span style="display: flex; gap: 4px; align-items: center; margin-left: 4px;">
            跳至
            <input v-model.number="pageJump" type="number" min="1" :max="totalPages"
                   @keyup.enter="jumpToPage"
                   style="width: 64px; padding: 4px 6px; font-size: 13px;" />
            页
            <button class="btn btn-outline btn-sm" @click="jumpToPage">跳转</button>
          </span>
        </div>
      </div>
    </div>

    <!-- 日志归档管理（默认折叠） -->
    <details class="archive-fold">
      <summary>日志归档管理 📦</summary>
      <div class="archive-body">
        <div style="display: flex; justify-content: flex-end; align-items: center; margin-bottom: 12px;">
          <div style="display: flex; gap: 8px;">
            <button class="btn btn-outline btn-sm" :disabled="archiveBusy" @click="doArchive">
              {{ archiveBusy ? '归档中...' : '📥 手动归档' }}
            </button>
            <button class="btn btn-sm" style="background: var(--danger); color: #fff;" :disabled="archiveBusy" @click="doClearLogs">
              🗑 清空日志
            </button>
          </div>
        </div>
        <p style="color: var(--gray-500); font-size: 13px; margin-bottom: 12px;">
          每天凌晨 2:00 自动将昨日日志归档为 gzip 压缩文件。也可手动归档，随时解压恢复或永久删除。
        </p>
        <div v-if="lastArchive" style="margin-bottom: 10px; font-size: 12px;" :style="{color: lastArchive.ok === false ? 'var(--danger)' : 'var(--gray-500)'}">
          最近归档：<template v-if="lastArchive.ok === true">✅ {{ fmtTime(lastArchive.at) }} · {{ lastArchive.count }} 条 · 释放 blob {{ lastArchive.blobs_deleted }} 个<template v-if="lastArchive.filename">（{{ lastArchive.filename }}）</template></template>
          <template v-else-if="lastArchive.ok === false">❌ {{ fmtTime(lastArchive.at) }} 失败：{{ lastArchive.error || '未知错误' }}</template>
        </div>
        <table v-if="archives.length > 0">
          <thead>
            <tr>
              <th>归档文件</th>
              <th>日期范围</th>
              <th>记录数</th>
              <th>文件大小</th>
              <th>归档时间</th>
              <th>操作</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="a in archives" :key="a.filename">
              <td style="font-family: monospace; font-size: 12px;">{{ a.filename }}</td>
              <td>{{ a.date_from }} ~ {{ a.date_to }}</td>
              <td>{{ formatNum(a.count) }}</td>
              <td>{{ formatSize(a.size_bytes) }}</td>
              <td style="font-size: 12px;">{{ fmtTime(a.archived_at) }}</td>
              <td>
                <div style="display: flex; gap: 4px;">
                  <button class="btn btn-outline btn-sm" @click="doRestore(a)" :disabled="archiveBusy">恢复</button>
                  <button class="btn btn-outline btn-sm" style="color: var(--danger);" @click="doDeleteArchive(a)" :disabled="archiveBusy">删除</button>
                </div>
              </td>
            </tr>
          </tbody>
        </table>
        <p v-else style="text-align: center; padding: 24px; color: var(--gray-500); font-size: 13px;">暂无归档文件</p>
      </div>
    </details>

    <!-- 详情弹窗 -->
    <div v-if="detailRow" class="modal-overlay" @click.self="detailRow = null">
      <div class="modal-content detail-modal">
        <h3>{{ detailRow.log_type === 'refresh' ? '模型刷新详情' : '请求详情' }}</h3>
        <!-- 模型刷新详情 -->
        <template v-if="detailRow.log_type === 'refresh'">
          <table class="detail-meta" style="width: 100%; margin: 16px 0; table-layout: fixed;">
            <tbody>
            <tr>
              <td class="k">时间</td><td class="v">{{ fmtTime(detailRow.created_at) }}</td>
              <td class="k">服务商</td><td class="v">{{ detailRow.provider_name }}</td>
            </tr>
            <tr>
              <td class="k">触发方式</td><td class="v">{{ detailRow.trigger === 'scheduled' ? '定时' : '手动' }}</td>
              <td class="k">耗时</td><td class="v">{{ fmtLatency(null, detailRow.duration_ms) }}</td>
            </tr>
            <tr>
              <td class="k">结果</td>
              <td class="v">
                <span :class="['badge', detailRow.status === 'success' ? 'badge-success' : 'badge-danger']" style="font-size: 12px;">{{ detailRow.status === 'success' ? '成功' : '失败' }}</span>
              </td>
              <td class="k">模型总数</td><td class="v">{{ detailRow.total_models }}</td>
            </tr>
            <tr>
              <td class="k">新增</td><td class="v" style="color: var(--success);">{{ detailRow.added }}</td>
              <td class="k">删除</td><td class="v" style="color: var(--danger);">{{ detailRow.removed }}</td>
            </tr>
            <tr>
              <td class="k">更新</td><td class="v">{{ detailRow.updated }}</td>
              <td class="k">定价/指标更新</td><td class="v">{{ detailRow.pricing_updated }} / {{ detailRow.metric_updated }}</td>
            </tr>
            <tr v-if="detailRow.pricing_source">
              <td class="k">定价来源</td><td class="v" colspan="3" style="font-family: monospace; font-size: 12px; word-break: break-all;">{{ detailRow.pricing_source }}</td>
            </tr>
            <tr v-if="detailRow.list_source && detailRow.list_source !== 'unknown'">
              <td class="k">列表来源</td>
              <td class="v" colspan="3">
                <span :class="['badge', detailRow.list_source === 'online' ? 'badge-success' : 'badge-warning']" style="font-size: 11px;">
                  {{ detailRow.list_source === 'online' ? '在线拉取' : (detailRow.list_source === 'seed' ? '静态种子兜底' : '定价兜底') }}
                </span>
                <span v-if="detailRow.list_note" style="color: var(--gray-500); font-size: 12px; margin-left: 8px;">{{ detailRow.list_note }}</span>
              </td>
            </tr>
            <tr v-if="detailRow.error">
              <td class="k">错误</td><td class="v" colspan="3" style="color: var(--danger); word-break: break-all;">{{ detailRow.error }}</td>
            </tr>
            </tbody>
          </table>
          <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 16px;">
            <div>
              <h4 style="margin: 0 0 8px; color: var(--success);">➕ 新增模型</h4>
              <div v-if="(detailRow.added_models || []).length" class="code-block" style="max-height: 320px;">
                <div v-for="(m, i) in detailRow.added_models" :key="i" style="font-size: 12px; margin-bottom: 4px;">
                  <strong>{{ m.model_id }}</strong><span v-if="m.display_name && m.display_name !== m.model_id" style="color: #94a3b8;"> · {{ m.display_name }}</span>
                </div>
              </div>
              <p v-else style="color: var(--gray-500); font-size: 13px;">无</p>
            </div>
            <div>
              <h4 style="margin: 0 0 8px; color: var(--danger);">➖ 移除模型</h4>
              <div v-if="(detailRow.removed_models || []).length" class="code-block" style="max-height: 320px;">
                <div v-for="(m, i) in detailRow.removed_models" :key="i" style="font-size: 12px; margin-bottom: 4px;">
                  <strong>{{ m.model_id }}</strong><span v-if="m.display_name && m.display_name !== m.model_id" style="color: #94a3b8;"> · {{ m.display_name }}</span>
                </div>
              </div>
              <p v-else style="color: var(--gray-500); font-size: 13px;">无</p>
            </div>
          </div>
        </template>
        <template v-else>
        <table class="detail-meta" style="width: 100%; margin: 16px 0; table-layout: fixed;">
          <tbody>
          <tr>
            <td class="k">时间</td><td class="v">{{ fmtTime(detailRow.created_at) }}</td>
            <td class="k">状态</td><td class="v">{{ detailRow.status }}</td>
          </tr>
          <tr v-if="detailRow.archived_at">
            <td class="k">归档</td>
            <td class="v" colspan="3" style="color: #f59e0b;">
              📦 详细内容已归档瘦身（{{ fmtTime(detailRow.archived_at) }}），当前仅保留统计数据；可在归档列表恢复后查看
            </td>
          </tr>
          <tr>
            <td class="k">请求模型</td><td class="v">{{ detailRow.requested_model || '-' }}</td>
            <td class="k">路由模型</td><td class="v">{{ detailRow.routed_model || '-' }}</td>
          </tr>
          <tr>
            <td class="k">路由服务商</td><td class="v">{{ detailRow.routed_provider || '-' }}</td>
            <td class="k">延迟</td><td class="v">{{ fmtLatency(detailRow.ttft_ms, detailRow.latency_ms) }}</td>
          </tr>
          <tr>
            <td class="k">Prompt Token</td><td class="v">{{ detailRow.prompt_tokens || 0 }}</td>
            <td class="k">Completion Token</td><td class="v">{{ detailRow.completion_tokens || 0 }}</td>
          </tr>
          <tr v-if="detailRow.cache_read_tokens || detailRow.cache_write_tokens">
            <td class="k">缓存读 Token</td><td class="v" style="color: #2b8aef;">{{ detailRow.cache_read_tokens || 0 }}</td>
            <td class="k">缓存写 Token</td><td class="v" style="color: #2b8aef;">{{ detailRow.cache_write_tokens || 0 }}</td>
          </tr>
          <tr>
            <td class="k">回退次数</td><td class="v">{{ detailRow.fallback_count || 0 }}</td>
            <td class="k">IP</td><td class="v">{{ detailRow.user_ip || '-' }}</td>
          </tr>
          <tr>
            <td class="k">代理</td>
            <td class="v" colspan="3">
              <span v-if="detailRow.used_proxy" style="color: #22c55e; font-weight: 500;">🟢 走代理</span>
              <span v-else style="color: var(--gray-500);">⚪ 直连</span>
              <span v-if="detailRow.proxy_url" style="font-family: monospace; font-size: 12px; margin-left: 6px; color: #94a3b8;">{{ maskProxy(detailRow.proxy_url) }}</span>
            </td>
          </tr>
          <tr v-if="detailRow.error_type">
            <td class="k">错误类型</td><td class="v" colspan="3">{{ detailRow.error_type }}</td>
          </tr>
          <tr v-if="detailRow.error_msg">
            <td class="k">错误信息</td>
            <td class="v" colspan="3">
              <!-- 错误信息可能是多行（概要 + 上游原文），保留换行、等宽字体便于读 JSON；
                   长错误默认折叠到 12 行，避免详情弹窗被一条错误撑爆 -->
              <pre class="err-full" :class="{ collapsed: isLongError(detailRow.error_msg) && !errExpanded }">{{ detailRow.error_msg }}</pre>
              <button v-if="isLongError(detailRow.error_msg)" class="btn btn-outline btn-sm"
                      style="margin-top:4px" @click="errExpanded = !errExpanded">
                {{ errExpanded ? '收起' : '展开全部' }}
              </button>
            </td>
          </tr>
          </tbody>
        </table>
        <!-- 请求内容 + 返回内容 并排展示 -->
        <div class="detail-grid">
          <div>
            <div style="display: flex; justify-content: space-between; align-items: center; margin: 0 0 8px;">
              <h4 style="margin: 0; color: var(--primary);">📤 请求内容</h4>
              <div style="display: flex; gap: 6px; align-items: center;">
                <span v-if="detailRow.request_body_truncated" style="color: #f59e0b; font-size: 11px;">内容已截断</span>
                <button v-if="detailRow.request_body_truncated" class="btn btn-outline btn-sm" @click="showDetail(detailRow, true)">查看完整</button>
                <button class="btn btn-outline btn-sm" @click="copy(prettyRequestBody)">复制</button>
              </div>
            </div>
            <div v-if="reqInfo" class="code-block" style="max-height: 500px;">
              <div v-if="reqInfo.model"><strong>模型:</strong> {{ reqInfo.model }}</div>
              <div v-if="reqInfo.stream != null"><strong>流式:</strong> {{ reqInfo.stream ? '是' : '否' }}</div>
              <div v-if="reqInfo.temperature != null"><strong>温度:</strong> {{ reqInfo.temperature }}</div>
              <div v-if="reqInfo.max_tokens"><strong>Max Tokens:</strong> {{ reqInfo.max_tokens }}</div>
              <hr style="margin: 8px 0; border-color: #334155;" />
              <div v-for="(m, i) in (reqInfo.messages || [])" :key="i" style="margin-bottom: 8px; padding: 6px 8px; background: #1e293b; border-radius: 4px; border-left: 3px solid;" :style="{borderLeftColor: m.role === 'system' ? '#94a3b8' : m.role === 'user' ? '#3b82f6' : m.role === 'assistant' ? '#22c55e' : '#f59e0b'}">
                <span style="font-size: 11px; color: #64748b; text-transform: uppercase; font-weight: bold;">{{ m.role }}</span>
                <div v-if="m.tool_calls" style="margin: 4px 0;">
                  <div v-for="tc in m.tool_calls" :key="tc.id || tc.index" style="margin-bottom: 4px; padding: 4px 8px; background: #0f172a; border-radius: 4px; font-size: 11px;">
                    <span style="color: #f59e0b;">🔧 {{ tc.function?.name || tc.type || 'function' }}</span>
                    <pre style="margin: 4px 0 0; white-space: pre-wrap; word-break: break-word; font-size: 11px; color: #94a3b8; max-height: 260px; overflow-y: auto;">{{ formatRichContent(tc.function?.arguments || JSON.stringify(tc)) }}</pre>
                  </div>
                </div>
                <pre v-else-if="m.content" style="margin: 4px 0 0; white-space: pre-wrap; word-break: break-word; font-size: 12px; color: #e2e8f0; max-height: 300px; overflow-y: auto;">{{ formatRichContent(m.content) }}</pre>
                <pre v-else style="margin: 4px 0 0; white-space: pre-wrap; word-break: break-word; font-size: 12px; color: #94a3b8; max-height: 300px; overflow-y: auto;">(空)</pre>
              </div>
            </div>
            <p v-else style="color: var(--gray-500); font-size: 13px;">无数据</p>
            <details v-if="prettyRequestBody" style="margin-top: 8px;">
              <summary style="cursor: pointer; color: var(--gray-500); font-size: 12px;">原始请求 JSON（美化）</summary>
              <pre class="code-block" style="margin-top: 6px; max-height: 360px;">{{ prettyRequestBody }}</pre>
            </details>
          </div>
          <div>
            <div style="display: flex; justify-content: space-between; align-items: center; margin: 0 0 8px;">
              <h4 style="margin: 0; color: var(--success);">📥 返回内容</h4>
              <div style="display: flex; gap: 6px; align-items: center;">
                <span v-if="detailRow.response_body_truncated" style="color: #f59e0b; font-size: 11px;">内容已截断</span>
                <button v-if="detailRow.response_body_truncated" class="btn btn-outline btn-sm" @click="showDetail(detailRow, true)">查看完整</button>
                <button class="btn btn-outline btn-sm" @click="copy(prettyResponseBody)">复制</button>
              </div>
            </div>
            <div v-if="mediaContent" class="code-block" style="max-height: 600px; overflow-y: auto;">
              <div style="font-size: 13px; color: #94a3b8; margin-bottom: 10px;">
                共 <strong style="color: #e2e8f0;">{{ mediaContent.count }}</strong> 个{{ mediaContent.type === 'image_generation' ? '图片' : '视频' }}
                <span style="margin-left: 8px; font-family: monospace; color: #64748b;">{{ mediaContent.model }}</span>
              </div>
              <div v-if="mediaContent.type === 'image_generation'" style="display: flex; flex-wrap: wrap; gap: 12px;">
                <div v-for="(im, i) in mediaContent.images" :key="i" style="flex: 1 1 220px; max-width: 320px; background: #0f172a; border: 1px solid #334155; border-radius: 8px; overflow: hidden;">
                  <img v-if="im.url" :src="im.url" alt="image" style="width: 100%; max-height: 340px; object-fit: contain; background: #fff; display: block;" loading="lazy" />
                  <img v-else-if="im.data" :src="'data:' + (im.mime || 'image/png') + ';base64,' + im.data" alt="image" style="width: 100%; max-height: 340px; object-fit: contain; background: #fff; display: block;" loading="lazy" />
                  <div v-if="im.revised_prompt" style="padding: 6px 8px; font-size: 11px; color: #94a3b8; white-space: pre-wrap; word-break: break-word;">{{ im.revised_prompt }}</div>
                </div>
              </div>
              <div v-else style="display: flex; flex-wrap: wrap; gap: 12px;">
                <div v-for="(v, i) in mediaContent.videos" :key="i" style="flex: 1 1 280px; max-width: 420px; background: #0f172a; border: 1px solid #334155; border-radius: 8px; overflow: hidden;">
                  <video :src="v.url" controls style="width: 100%; max-height: 380px; display: block; background: #000;"></video>
                  <div v-if="v.duration" style="padding: 6px 8px; font-size: 11px; color: #94a3b8;">时长 {{ v.duration }}s</div>
                </div>
              </div>
            </div>
            <div v-else-if="respInfo || responseToolCalls.length" class="code-block" style="max-height: 500px;">
              <div v-if="respInfo">
                <div v-if="respInfo.model"><strong>模型:</strong> {{ respInfo.model }}</div>
                <div><strong>Token:</strong> 输入 {{ detailRow.prompt_tokens || 0 }} / 输出 {{ detailRow.completion_tokens || 0 }}</div>
                <div v-if="respInfo.finish_reason"><strong>结束原因:</strong> {{ respInfo.finish_reason }}</div>
                <hr style="margin: 8px 0; border-color: #334155;" />
                <div v-if="(respInfo.choices || []).length">
                  <div v-for="(c, i) in respInfo.choices" :key="i" style="margin-bottom: 6px;">
                    <span style="font-size: 11px; color: #64748b;">Choice {{ i + 1 }} <span v-if="c.finish_reason" style="color: #22c55e;">({{ c.finish_reason }})</span></span>
                    <pre style="margin: 4px 0 0; white-space: pre-wrap; word-break: break-word; font-size: 12px; color: #e2e8f0; max-height: 300px; overflow-y: auto;">{{ collapseBlank(c.content) || '(空)' }}</pre>
                  </div>
                </div>
                <div v-else-if="respInfo.content" style="white-space: pre-wrap; word-break: break-word; font-size: 12px; color: #e2e8f0;">
                  {{ respInfo.content }}
                </div>
                <div v-else-if="respInfo.error" style="color: #f87171;">
                  <strong>错误:</strong> {{ respInfo.error }}
                </div>
              </div>
              <div v-if="responseToolCalls.length" style="margin-top: 12px;">
                <div style="font-size: 13px; font-weight: 500; margin-bottom: 8px; color: #94a3b8;">工具调用 · {{ responseToolCalls.length }} 个</div>
                <div v-for="tc in responseToolCalls" :key="tc.id || tc.index" style="margin-bottom: 10px; padding: 10px 12px; background: #0f172a; border: 0.5px solid #334155; border-radius: 6px;">
                  <div style="display: flex; align-items: center; gap: 8px; margin-bottom: 8px;">
                    <span style="font-size: 13px; font-weight: 500; color: #f59e0b;">🔧 {{ tc.name }}</span>
                    <span style="font-size: 11px; color: #64748b; font-family: monospace;">{{ tc.id }}</span>
                  </div>
                  <div v-for="(v, k) in tc.args" :key="k" style="margin-bottom: 4px; font-size: 12px;">
                    <span style="color: #94a3b8;">{{ k }}:</span>
                    <pre style="margin: 2px 0 0; white-space: pre-wrap; word-break: break-word; font-size: 12px; color: #e2e8f0; background: #020617; padding: 6px 8px; border-radius: 4px; max-height: 320px; overflow-y: auto;">{{ argText(v) }}</pre>
                  </div>
                </div>
              </div>
            </div>
            <p v-else style="color: var(--gray-500); font-size: 13px;">无数据</p>
            <details v-if="prettyResponseBody" style="margin-top: 8px;">
              <summary style="cursor: pointer; color: var(--gray-500); font-size: 12px;">原始返回 JSON（美化）</summary>
              <pre class="code-block" style="margin-top: 6px; max-height: 360px;">{{ prettyResponseBody }}</pre>
            </details>
          </div>
        </div>
        </template>
        <div class="modal-actions">
          <button class="btn btn-outline" @click="detailRow = null">关闭</button>
        </div>
      </div>
    </div>
  </div>
</template>

<script>
import api from '../api.js'
import toast from '../toast.js'

function safeParseJSON(str) {
  if (!str) return null
  try { return JSON.parse(str) } catch (e) { return null }
}

// 请求日志 + 归档 + 详情弹窗：自 Analytics.vue 机械搬移（逻辑不动只搬家）。
// 对外接口：prop filterProvider（由父级「看日志」联动）；方法 reload()。
export default {
  name: 'LogsPanel',
  props: {
    filterProvider: { type: String, default: '' },
  },
  data() {
    return {
      items: [],
      page: 1,
      pageJump: null,
      total: 0,
      totalPages: 1,
      filterStatus: '',
      filterLogType: '',
      providerFilter: '',
      detailRow: null,
      errExpanded: false,   // 错误信息的展开/收起（长错误默认折叠）
      detailLoading: false,
      archives: [],
      archiveBusy: false,
      lastArchive: null,
      logProviders: [],
    }
  },
  watch: {
    filterProvider(v) {
      this.providerFilter = v
      this.loadPage(1)
    },
  },
  mounted() {
    this.loadPage(1)
    this.loadLogProviders()
    this.loadArchives()
    // 有「待响应」行时自动轮询当前页（3s），全部落定后自动停
    this._pollTimer = setInterval(async () => {
      if (document.hidden) return
      if (!(this.items || []).some((i) => i.status === 'pending')) return
      try { await this.loadPage(this.page) } catch (e) { /* 静默 */ }
    }, 3000)
  },
  beforeUnmount() {
    if (this._pollTimer) clearInterval(this._pollTimer)
  },
  computed: {
    reqInfo() {
      return safeParseJSON(this.detailRow?.request_body)
    },
    respInfo() {
      const raw = safeParseJSON(this.detailRow?.response_body)
      if (!raw) {
        // [stream] 或无数据
        const s = this.detailRow?.response_body
        if (typeof s === 'string' && s !== '[stream]') {
          // 成功的媒体生成（图片/视频）会把结果摘要以纯文本存入 response_body，
          // 不应当作 error；只有真正失败的请求才标红为错误。
          if (this.detailRow?.status === 'error') return { error: s }
          return { content: s }
        }
        return null
      }
      // 媒体生成（图片/视频）：结构化结果，直接交给 mediaContent 渲染，不要当 error
      if (raw.type === 'image_generation' || raw.type === 'video_generation') {
        return raw
      }
      // 流式：chunk 数组 → 合并
      if (Array.isArray(raw)) {
        const merged = { choices: [], usage: null, model: '' }
        const contentMap = {}
        const tcOnly = {}
        for (const ck of raw) {
          if (ck.model) merged.model = ck.model
          if (ck.usage && Object.keys(ck.usage).length) merged.usage = ck.usage
          for (const ch of (ck.choices || [])) {
            const idx = ch.index ?? 0
            if (!contentMap[idx]) contentMap[idx] = ''
            const delta = ch.delta || ch.message || {}
            // 工具调用已在下方「工具调用」卡片中解析展示，主内容区不再塞原始 JSON
            const text = delta.content || delta.reasoning_content || delta.reasoning || ch.text || ''
            if (text) contentMap[idx] += text
            else if (delta.tool_calls) tcOnly[idx] = true
          }
        }
        for (const [idx, text] of Object.entries(contentMap)) {
          merged.choices.push({
            index: Number(idx),
            content: text || (tcOnly[idx] ? '(工具调用见下方卡片)' : ''),
          })
        }
        merged.choices.sort((a, b) => a.index - b.index)
        return merged
      }
      // 非流式：标准 {choices: [...], usage: {...}}
      if (raw.choices) {
        return {
          model: raw.model || '',
          usage: raw.usage,
          choices: raw.choices.map(c => {
            // 工具调用已在下方「工具调用」卡片中解析展示，主内容区不再塞原始 JSON
            const tc = c.message?.tool_calls || c.delta?.tool_calls
            const content = (c.message?.content || c.message?.reasoning_content || c.delta?.content || c.delta?.reasoning_content || c.text
              || (tc && '(工具调用见下方卡片)')
              || JSON.stringify(c.message || c.delta || c))
            return { index: c.index, finish_reason: c.finish_reason, content }
          })
        }
      }
      return { error: typeof raw === 'string' ? raw : JSON.stringify(raw) }
    },
    // 媒体生成（图片/视频）结构化结果：从 respInfo 中提取，供图廊/播放器渲染
    mediaContent() {
      const r = this.respInfo
      if (r && (r.type === 'image_generation' || r.type === 'video_generation')) return r
      return null
    },
    // 解析 response_body 中的 tool_calls，兼容三种存储形态：
    //   A. 顶层干净数组 [{index,id,type:"function",function:{name,arguments}}]
    //   B. 流式 SSE delta 累积数组 [{id,choices:[{index,delta:{tool_calls:[...]}}]}]
    //   C. 非流式对象 {choices:[{message:{tool_calls:[...]}}]}
    // 对形态 B 的增量 arguments 按 index 合并
    responseToolCalls() {
      const parseArgs = (a) => {
        if (typeof a !== 'string') return a
        try { return JSON.parse(a) } catch (e) { return a }
      }
      const normalizeTC = (tc, i) => {
        const fn = tc.function || {}
        return {
          index: tc.index ?? i ?? 0,
          id: tc.id || '',
          name: fn.name || tc.name || tc.type || 'function',
          args: parseArgs(fn.arguments ?? tc.arguments),
        }
      }
      const raw = safeParseJSON(this.detailRow?.response_body)
      if (!raw) return []
      // 形态 C：非流式对象
      if (!Array.isArray(raw)) {
        const out = []
        for (const ch of raw.choices || []) {
          const tcs = (ch.message || {}).tool_calls
          if (Array.isArray(tcs)) for (const tc of tcs) out.push(normalizeTC(tc))
        }
        return out
      }
      if (!raw.length) return []
      // 形态 B：流式 delta 数组（每个元素带 choices）
      if (Array.isArray(raw[0]?.choices)) {
        const acc = {}
        for (const el of raw) {
          const d = ((el.choices || [])[0] || {}).delta
          const tcs = d && d.tool_calls
          if (!Array.isArray(tcs)) continue
          for (const tc of tcs) {
            const i = tc.index ?? 0
            if (!acc[i]) acc[i] = { id: tc.id, type: tc.type, name: '', args: '' }
            if (tc.id) acc[i].id = tc.id
            if (tc.type) acc[i].type = tc.type
            if (tc.function && tc.function.name) acc[i].name = tc.function.name
            if (tc.function && typeof tc.function.arguments === 'string') acc[i].args += tc.function.arguments
          }
        }
        return Object.keys(acc).sort((a, b) => a - b).map(i => ({
          index: Number(i),
          id: acc[i].id || '',
          name: acc[i].name || acc[i].type || 'function',
          args: parseArgs(acc[i].args),
        }))
      }
      // 形态 A：干净的 tool_calls 数组
      if (raw.every(x => x && (x.function || x.type === 'function'))) {
        return raw.map((x, i) => normalizeTC(x, i))
      }
      return []
    },
    prettyRequestBody() {
      const raw = this.detailRow?.request_body
      if (!raw) return ''
      // 超大原文（>120KB）跳过递归美化：deepUnescape + stringify 在浏览器里会卡数秒
      if (raw.length > 120 * 1024) return raw
      const parsed = safeParseJSON(raw)
      return parsed ? this.deepUnescape(parsed) : raw
    },
    prettyResponseBody() {
      const raw = this.detailRow?.response_body
      if (!raw) return ''
      if (raw.length > 120 * 1024) return raw
      const parsed = safeParseJSON(raw)
      return parsed ? this.deepUnescape(parsed) : raw
    }
  },
  methods: {
    reload() {
      return this.loadPage(this.page)
    },
    setProviderFilter(name) {
      this.providerFilter = name
      this.loadPage(1)
    },
    async loadPage(p) {
      // 换页才同步跳转输入框；5s 自动刷新原位重载不动用户正在输入的内容
      if (p !== this.page) this.pageJump = p
      this.page = p
      const params = { page: p, page_size: 10 }
      if (this.filterLogType) params.log_type = this.filterLogType
      if (this.filterStatus) params.status = this.filterStatus
      if (this.providerFilter) params.provider = this.providerFilter
      try {
        const data = await api.getLogs(params)
        this.items = data.items || []
        this.total = data.total || 0
        this.totalPages = data.total_pages || 1
      } catch (e) { toast.error('加载日志失败: ' + e.message) }
    },
    jumpToPage() {
      let n = parseInt(this.pageJump, 10)
      if (!Number.isFinite(n) || n < 1) n = 1
      if (n > this.totalPages) n = this.totalPages
      if (n === this.page) { this.pageJump = n; return }
      this.loadPage(n)
    },
    async exportLogs(format) {
      try {
        await api.exportLogs({ format, hours: 168, status: this.filterStatus || '', provider: this.providerFilter || '' })
        toast.success('导出已开始下载')
      } catch (e) { toast.error('导出失败: ' + e.message) }
    },
    async resetSummary() {
      if (!confirm('确定重置统计数据吗？\n\n将清零归档后保留的累计统计（总请求数、成功率、Token、平均延迟等）。\n当前实时日志的统计不受影响。')) return
      if (!confirm('再次确认：重置统计数据？')) return
      try {
        await api.resetAnalyticsSummary()
        toast.success('统计数据已重置')
      } catch (e) { toast.error('重置失败: ' + e.message) }
    },
    loadLogProviders() {
      api.getLogProviders().then(d => { this.logProviders = (d && d.providers) || [] }).catch(() => {})
    },
    // 模型刷新行的悬停摘要：新增/移除的模型名清单
    refreshChangesTitle(r) {
      const names = (arr) => (arr || []).map((m) => m.model_id).join('、')
      const parts = []
      if ((r.added_models || []).length) parts.push('新增: ' + names(r.added_models))
      if ((r.removed_models || []).length) parts.push('移除: ' + names(r.removed_models))
      if (r.error) parts.push('错误: ' + r.error)
      return parts.join('\n') || (r.status === 'success' ? '无增删' : '')
    },
    // 错误信息超过该长度即折叠（保留「展开全部」按钮）
    isLongError(t) { return !!t && String(t).length > 600 },
    showDetail(r, full = false) {
      this.errExpanded = false   // 每次打开详情重置展开态
      // 模型刷新行：列表接口已带全量字段（增删清单/错误），直接展示
      if (r.log_type === 'refresh') { this.detailRow = { ...r }; return }
      this.detailLoading = true
      if (!full) this.detailRow = { ...r }  // 先显示已有字段
      api.getLogDetail(r.id, full).then(data => {
        // 合并而非替换，保留 loading 等状态字段
        this.detailRow = { ...(this.detailRow || {}), ...data }
      }).catch(e => {
        console.error('加载详情失败', e)
      }).finally(() => {
        this.detailLoading = false
      })
    },
    async loadArchives() {
      try {
        const data = await api.listArchives()
        this.archives = data.archives || []
        this.lastArchive = data.last_archive || null
      } catch (e) { console.error('加载归档列表失败', e) }
    },
    async doArchive() {
      if (!confirm('确定归档所有当前日志吗？归档后这些记录将从数据库移出，保存为压缩文件。')) return
      this.archiveBusy = true
      try {
        const r = await api.triggerArchive()
        toast.success(r.archived_count > 0 ? `成功归档 ${r.archived_count} 条记录 → ${r.filename}` : r.message || '暂无需要归档的日志')
        await this.loadArchives()
        await this.loadPage(this.page)
      } catch (e) { toast.error('归档失败: ' + e.message) }
      finally { this.archiveBusy = false }
    },
    async doRestore(a) {
      if (!confirm(`确定恢复归档 "${a.filename}" 吗？\n\n${this.formatNum(a.count)} 条记录将被重新导入数据库，归档文件将被删除。`)) return
      this.archiveBusy = true
      try {
        const r = await api.restoreArchive(a.filename)
        toast.success(r.message || '恢复完成')
        await this.loadArchives()
        await this.loadPage(this.page)
      } catch (e) { toast.error('恢复失败: ' + e.message) }
      finally { this.archiveBusy = false }
    },
    async doDeleteArchive(a) {
      if (!confirm(`⚠️ 永久删除归档 "${a.filename}"？\n\n此操作不可撤销！${this.formatNum(a.count)} 条日志将被永久删除。`)) return
      this.archiveBusy = true
      try {
        await api.deleteArchive(a.filename)
        await this.loadArchives()
        toast.success('归档文件已删除')
      } catch (e) { toast.error('删除失败: ' + e.message) }
      finally { this.archiveBusy = false }
    },
    async doClearLogs() {
      if (!confirm('⚠️ 确定清空所有当前请求日志？\n\n此操作不可撤销！建议先手动归档保留历史记录。')) return
      if (!confirm('再次确认：清空所有请求日志？')) return
      this.archiveBusy = true
      try {
        const r = await api.clearLogs()
        toast.success(r.message || '日志已清空')
        await this.loadPage(1)
      } catch (e) { toast.error('清空失败: ' + e.message) }
      finally { this.archiveBusy = false }
    },

    // 递归解转义：处理 tool_call.arguments 里单层/多层被 JSON 转义的字符串
    // 例：'{"command":"# kill...\nGet-NetTCPConnection..."}'  →  解开成真正换行的可读文本
    deepUnescape(value) {
      if (value === null || value === undefined) return ''
      if (typeof value === 'object') return JSON.stringify(value, null, 2)
      let result = String(value).trim()
      if (!result) return ''
      for (let i = 0; i < 6; i++) {
        try {
          const parsed = JSON.parse(result)
          if (typeof parsed === 'string') {
            result = parsed           // 解开一层字符串转义，继续往下看是否还有一层
            continue
          }
          return JSON.stringify(parsed, null, 2)  // 解析成对象/数组 → 漂亮缩进
        } catch (e) {
          break
        }
      }
      return result  // 不是 JSON（如原始 shell 命令/计划文本）→ 原样返回，保留真实换行
    },
    formatRichContent(value) {
      return this.deepUnescape(value)
    },
    // 仅用于「返回内容」展示：折叠模型输出里多余的连续空行
    // （保留单段落间距，去掉 3+ 连续换行；不动存储数据，复制按钮仍给原文）
    collapseBlank(text) {
      if (!text) return text
      let s = String(text)
      s = s.replace(/\r\n?/g, '\n')        // 统一换行符
      s = s.replace(/[ \t]+\n/g, '\n')       // 去掉行尾空白
      s = s.replace(/\n{3,}/g, '\n\n')      // 3+ 连续换行 → 2（1 个空行）
      s = s.replace(/^\n+/, '').replace(/\n+$/, '') // 去掉首尾空行
      return s
    },
    // 工具调用参数值统一渲染为文本（对象/数组美化，字符串原样保留换行）
    argText(v) {
      if (v === null) return 'null'
      if (v === undefined) return ''
      if (typeof v === 'object') return JSON.stringify(v, null, 2)
      return String(v)
    },
    async copy(text) {
      if (!text) return
      try {
        if (navigator.clipboard && navigator.clipboard.writeText) {
          await navigator.clipboard.writeText(text)
        } else {
          throw new Error('no clipboard api')
        }
      } catch (e) {
        const ta = document.createElement('textarea')
        ta.value = text
        ta.style.position = 'fixed'
        ta.style.opacity = '0'
        document.body.appendChild(ta)
        ta.select()
        try { document.execCommand('copy') } catch (_) {}
        document.body.removeChild(ta)
      }
    },
    formatJson(s) {
      try { return JSON.stringify(JSON.parse(s), null, 2) } catch (e) { return s }
    },
    formatNum(n) { return (n || 0).toLocaleString() },
    // 延迟双值展示：首字延迟 / 总延迟（如 0.8s/3.2s）
    fmtLatency(ttft, latency) {
      const fmt = (v) => (v ? (v / 1000).toFixed(1) + 's' : '-')
      const lat = fmt(latency)
      if (ttft == null) return lat
      return `${fmt(ttft)}/${lat}`
    },
    formatTokens(n) {
      if (!n || n < 1000) return String(n || 0)
      if (n < 1e6) return (n / 1000).toFixed(1) + 'K'
      return (n / 1e6).toFixed(1) + 'M'
    },
    // 代理 URL 脱敏：隐藏密码段，避免明文暴露凭据
    maskProxy(url) {
      if (!url) return ''
      try {
        const u = new URL(url)
        if (u.password) u.password = '****'
        return u.toString()
      } catch (e) {
        return url
      }
    },
    formatSize(bytes) {
      if (!bytes || bytes < 1024) return (bytes || 0) + ' B'
      if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + ' KB'
      return (bytes / 1024 / 1024).toFixed(1) + ' MB'
    },
    fmtTime(ts) {
      if (!ts) return '-'
      // 判断是否已有时区信息：Z 结尾 或 +/-HH:MM 结尾
      const hasTZ = /[zZ]$|[+-]\d{2}:\d{2}$/.test(ts)
      const d = new Date(hasTZ ? ts : ts + 'Z')
      if (Number.isNaN(d.getTime())) return ts
      return d.toLocaleString('zh-CN', { timeZone: 'Asia/Shanghai', hour12: false })
    }
  }
}
</script>

<style scoped>
.modal-overlay {
  position: fixed; inset: 0; background: rgba(0,0,0,0.5);
  display: flex; align-items: center; justify-content: center; z-index: 100;
}
.modal-content {
  background: var(--bg-elevated); border-radius: 12px; padding: 24px; max-width: 640px; width: 90%;
  border: 1px solid var(--border-soft);
}
.modal-actions { display: flex; justify-content: flex-end; gap: 8px; margin-top: 16px; }
/* code-block 详情区：保持暗色风格（无论主题），因为里面是 JSON 代码 + 多层嵌套的暗色 inline 颜色 */
.code-block { background: #1e293b; color: #e2e8f0; padding: 16px; border-radius: 8px; font-size: 13px; font-family: monospace; max-height: 500px; overflow-y: auto; white-space: pre-wrap; word-break: break-word; line-height: 1.6; border: 1px solid #334155; }
.code-block strong { color: #93c5fd; }

.detail-modal {
  max-width: min(1180px, 94vw) !important;
  width: 94vw;
  max-height: 88vh;
  overflow: auto;
}
.detail-meta td { padding: 5px 8px; font-size: 13px; vertical-align: top; }
.detail-meta td.k { color: var(--gray-500); width: 92px; white-space: nowrap; }
.detail-meta td.v { word-break: break-all; }
/* 错误信息：保留换行 + 等宽（读上游 JSON 错误体用），可滚动 */
.err-full {
  margin: 0; padding: 6px 8px; font-family: ui-monospace, "Cascadia Code", Consolas, monospace;
  font-size: 12px; line-height: 1.5; white-space: pre-wrap; word-break: break-all;
  background: rgba(148, 163, 184, 0.10); border-radius: 6px;
  max-height: 340px; overflow: auto;
}
.err-full.collapsed { max-height: 190px; }
.detail-grid {
  display: grid;
  grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);
  gap: 16px;
  margin-top: 16px;
}
.code-block,
.code-block pre {
  background: #0f172a !important;
  color: #e5e7eb !important;
}
.code-block {
  max-height: 55vh !important;
  overflow: auto !important;
}
@media (max-width: 900px) {
  .detail-grid { grid-template-columns: 1fr; }
}

/* 归档折叠区 */
.archive-fold { margin-top: 20px; border: 1px solid var(--border-soft); border-radius: 12px; background: var(--bg-card); }
.archive-fold summary {
  cursor: pointer; padding: 14px 18px; font-weight: 600; font-size: 15px;
  color: var(--text-primary); user-select: none;
}
.archive-fold[open] summary { border-bottom: 1px solid var(--border-soft); }
.archive-body { padding: 16px 18px; }
</style>
