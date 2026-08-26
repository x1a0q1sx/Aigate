<template>
  <div>
    <PageHeader title="服务商管理" icon="server" subtitle="已启用服务商与可添加目录分开管理，支持手动添加或一键添加 API Key 类服务商">
      <template #actions>
        <button class="btn btn-primary" @click="showBackupModal = true">
          <AppIcon name="archive" :size="14" />一键备份
        </button>
        <button class="btn btn-outline" @click="showRestoreModal = true">
          <AppIcon name="upload" :size="14" />一键恢复
        </button>
        <span class="btn-sep"></span>
        <button class="btn btn-outline" @click="showExportModal = true">
          <AppIcon name="download" :size="14" />导出
        </button>
        <button class="btn btn-outline" @click="showImportModal = true">
          <AppIcon name="upload" :size="14" />导入
        </button>
        <button class="btn btn-outline" @click="refreshAllModels" :disabled="busy.refreshModels">
          <AppIcon name="refresh" :size="14" />
          {{ busy.refreshModels ? '刷新中...' : '刷新模型' }}
        </button>
        <button class="btn btn-outline" @click="cleanOrphans" :disabled="busy.cleanOrphans">
          <AppIcon name="broom" :size="14" />
          {{ busy.cleanOrphans ? '清理中...' : '清理孤儿模型' }}
        </button>
        <button class="btn btn-primary" @click="openAddModal">
          <AppIcon name="plus" :size="14" />添加自定义服务商
        </button>
      </template>
    </PageHeader>

    <ModelRefreshModal :visible="showRefreshModal" :result="refreshResult || {}" @close="showRefreshModal = false" />

    <div class="stats-grid">
      <StatCard label="已添加" icon="server" :value="providers.length" />
      <StatCard label="API Key" icon="key" :value="dashboardStats.apiKey" />
      <StatCard label="异常" icon="alert" tone="bad" :value="dashboardStats.error" />
    </div>

    <div class="tabs">
      <button :class="['tab', { active: pageTab === 'enabled' }]" @click="pageTab = 'enabled'">
        <AppIcon name="server" :size="15" />已启用服务商
      </button>
      <button :class="['tab', { active: pageTab === 'catalog' }]" @click="pageTab = 'catalog'">
        <AppIcon name="plus" :size="15" />添加服务商
      </button>
    </div>

    <!-- 已启用服务商 -->
    <section v-if="pageTab === 'enabled'" class="card">
      <div class="provider-toolbar">
        <div class="toolbar-left">
          <div class="segmented">
            <button v-for="f in providerFilters" :key="f.value" :class="{ active: providerFilter === f.value }" @click="providerFilter = f.value">
              {{ f.label }}
            </button>
          </div>
        </div>
        <div class="toolbar-controls">
          <div class="input-group">
            <AppIcon name="search" :size="14" />
            <input v-model.trim="providerSearch" placeholder="搜索服务商 / URL" />
          </div>
          <select v-model="providerSort">
            <option value="pinned">固定优先</option>
            <option value="type">按类型</option>
            <option value="models">模型数</option>
            <option value="failures">失败数</option>
            <option value="name">名称</option>
          </select>
        </div>
      </div>

      <EmptyState v-if="filteredProviders.length === 0" icon="inbox" title="暂无匹配服务商" />
      <div v-else class="table-wrap provider-table-wrap">
        <table class="provider-table">
          <colgroup>
            <col class="col-pin">
            <col class="col-name">
            <col class="col-url">
            <col class="col-credential">
            <col class="col-type">
            <col class="col-count">
            <col class="col-count">
            <col class="col-status-col">
            <col class="col-proxy">
            <col class="col-action-width">
          </colgroup>
          <thead>
            <tr>
              <th class="col-pin" aria-label="固定"></th>
              <th>服务商</th>
              <th class="col-url">Base URL</th>
              <th>凭证</th>
              <th>类型</th>
              <th class="num">模型</th>
              <th class="num">密钥</th>
              <th>状态</th>
              <th>代理</th>
              <th class="col-actions">操作</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="p in filteredProviders" :key="p.id" :class="{ highlight: highlightProviderId === p.id, disabled: isProviderDisabled(p) }">
              <td class="col-pin">
                <button class="icon-btn pin-btn" :class="{ pinned: isPinned(p.id) }" @click="togglePin(p.id)" :aria-label="isPinned(p.id) ? '取消固定' : '固定常用'" title="固定常用">
                <AppIcon name="pin" :size="14" />
              </button>
              </td>
              <td class="cell-name">
                <strong :title="p.description || p.name">{{ p.name }}</strong>
                <span class="badge" :class="credBadgeClass(p.credential_type)">{{ credLabel(p.credential_type) }}</span>
              </td>
              <td class="mono text-xs text-muted col-url"><span class="url-text" :title="p.base_url">{{ p.base_url }}</span></td>
              <td class="text-xs">
                <span v-if="(p.credential_type || 'api_key') === 'api_key'">API Key</span>
                <span v-else-if="(p.credential_type || 'api_key') === 'free_tier'">无需密钥</span>
                <span v-else>OAuth {{ oauthConnectionCount(p) }}</span>
              </td>
              <td><code class="text-xs">{{ p.api_type }}</code></td>
              <td class="num tabular">{{ modelCount(p.id) }}</td>
              <td class="num tabular">{{ (p.credential_type || 'api_key') === 'api_key' ? keyCount(p.id) : '-' }}</td>
              <td class="cell-status">
                <label class="toggle-switch compact" title="启用或禁用该服务商；禁用后请求自动跳过，配置保留">
                  <input type="checkbox" :checked="!isProviderDisabled(p)" @change="toggleProviderEnabled(p, $event.target.checked)" />
                  <span class="toggle-slider"></span>
                </label>
                <StatusBadge :status="providerStatus(p).level" :label="providerStatus(p).text" />
                <span v-for="s in statusLabels(p)" :key="s" class="badge badge-warning badge-sm" :title="s">{{ s }}</span>
              </td>
              <td>
                <label class="toggle-switch compact" title="开启后强制走代理池；关闭后跟随全局代理池开关">
                  <input type="checkbox" :checked="!!p.proxy_enabled" @change="toggleProviderProxy(p, $event.target.checked)" />
                  <span class="toggle-slider"></span>
                </label>
              </td>
              <td class="col-actions">
                <div class="action-stack">
                  <button class="icon-btn" @click="refreshProviderModels(p)" :disabled="busy.refreshModels" title="刷新模型" aria-label="刷新模型"><AppIcon name="refresh" :size="13" /></button>
                  <button class="icon-btn" @click="editProvider(p)" title="编辑" aria-label="编辑"><AppIcon name="edit" :size="13" /></button>
                  <button v-if="(p.credential_type || 'api_key') === 'api_key'" class="icon-btn" @click="editProviderKeys(p)" title="密钥" aria-label="密钥"><AppIcon name="key" :size="13" /></button>
                  <button class="icon-btn" @click="openImportPricing(p)" title="导入定价" aria-label="导入定价"><AppIcon name="dollar" :size="13" /></button>
                  <button v-if="p.api_type === 'atomcode'" class="icon-btn" :class="{ warning: !atomExeStatus.found }" @click="openAtomExeConfig(p)" title="配置可执行文件" aria-label="配置可执行文件"><AppIcon name="scan" :size="13" /></button>
                  <button class="icon-btn danger" @click="deleteProvider(p)" title="删除" aria-label="删除"><AppIcon name="trash" :size="13" /></button>
                </div>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
    </section>

    <!-- 添加服务商 -->
    <section v-else class="card">
      <div class="toolbar">
        <div class="input-group">
          <AppIcon name="search" :size="14" />
          <input v-model.trim="catalogSearch" placeholder="搜索服务商名称 / id / base URL" />
        </div>
        <div class="segmented">
          <button :class="{ active: catalogTab === 'api_key' }" @click="catalogTab = 'api_key'">
            API Key {{ apikeyProviders.length }}
          </button>
        </div>
      </div>

      <div class="catalog-grid">
        <article v-for="c in visibleCatalog" :key="catalogKey(c)" class="catalog-card">
          <div class="catalog-icon" :style="{ background: c.color || '#64748b' }">
            {{ (c.name || '?').charAt(0) }}
          </div>
          <div class="catalog-body">
            <div class="catalog-title">
              <strong>{{ c.name }}</strong>
              <StatusBadge :status="catalogStatus(c).level" :label="catalogStatus(c).text" />
            </div>
            <div class="catalog-id text-xs text-muted">{{ c.id || c.code }}</div>
            <div class="catalog-url mono text-xs text-muted" :title="c.baseUrl || c.api_base_url || c.website">
              {{ c.baseUrl || c.api_base_url || c.website || '-' }}
            </div>
            <div class="catalog-actions">
              <button class="btn btn-primary btn-xs" @click="addFromCatalog(c, 'api_key')" :disabled="catalogStatus(c).state === 'enabled'">
                <AppIcon name="plus" :size="11" />添加
              </button>
              <button v-if="catalogStatus(c).provider" class="btn btn-outline btn-xs" @click="focusProvider(catalogStatus(c).provider)">
                <AppIcon name="eye" :size="11" />查看
              </button>
              <a v-if="c.website" class="btn btn-ghost btn-xs" :href="c.website" target="_blank" rel="noopener">
                <AppIcon name="link" :size="11" />官网
              </a>
            </div>
          </div>
        </article>
      </div>
    </section>

    <!-- 导出模态框 -->
    <AppModal v-model="showExportModal" title="导出服务商配置" icon="download" size="md">
      <div class="form-group">
        <label class="form-label checkbox-label">
          <input type="checkbox" v-model="exportIncludeKeys" />
          <span>导出明文 API Key（用于换机迁移，默认不导出）</span>
        </label>
      </div>
      <div class="form-group">
        <label class="form-label">选择要导出的服务商（留空导出全部）</label>
        <div class="provider-checklist">
          <label v-for="p in providers" :key="p.id" class="checkbox-label">
            <input type="checkbox" :value="p.id" v-model="exportProviderIds" />
            <span>{{ p.name }}</span>
          </label>
        </div>
      </div>
      <template #footer>
        <button class="btn btn-outline" @click="showExportModal = false">取消</button>
        <button class="btn btn-primary" @click="doExport" :disabled="exporting">
          <AppIcon name="download" :size="13" />
          {{ exporting ? '导出中...' : '下载 JSON 文件' }}
        </button>
      </template>
    </AppModal>

    <!-- 导入模态框 -->
    <AppModal v-model="showImportModal" title="导入服务商配置" icon="upload" size="lg">
      <div class="form-group">
        <label class="form-label">粘贴 JSON 或选择文件</label>
        <textarea v-model="importData" rows="10" placeholder='{"kind":"aigate.providers","providers":[...]}'></textarea>
        <div class="form-hint">
          <input type="file" accept="application/json,.json" @change="handleImportFileSelect" id="import-file-input" style="display: none" />
          <button class="btn btn-ghost btn-xs" @click="$el.querySelector('#import-file-input').click()">
            <AppIcon name="file" :size="11" />选择文件
          </button>
        </div>
      </div>

      <div class="form-group">
        <label class="form-label">冲突策略</label>
        <div class="radio-group">
          <label class="radio-label">
            <input type="radio" value="skip" v-model="importConflict" />
            <span>跳过 (skip) — 同名服务商已存在时跳过</span>
          </label>
          <label class="radio-label">
            <input type="radio" value="merge" v-model="importConflict" />
            <span>合并 (merge) — 更新基本信息，补齐模型和密钥（默认）</span>
          </label>
          <label class="radio-label">
            <input type="radio" value="replace" v-model="importConflict" />
            <span>替换 (replace) — 清空原有模型与密钥，按包内容重建</span>
          </label>
        </div>
      </div>

      <div class="form-row">
        <label class="form-label checkbox-label">
          <input type="checkbox" v-model="importKeys" />
          <span>导入密钥</span>
        </label>
        <label class="form-label checkbox-label">
          <input type="checkbox" v-model="importModels" />
          <span>导入模型</span>
        </label>
      </div>

      <div v-if="importResult" class="import-result">
        <div class="alert" :class="importResult.ok ? 'alert-success' : 'alert-error'">
          <AppIcon :name="importResult.ok ? 'checkCircle' : 'xCircle'" :size="16" />
          <div>
            <div class="alert-title">{{ importResult.ok ? '导入成功' : '导入失败' }}</div>
            <div v-if="importResult.ok" class="text-sm">
              创建 {{ importResult.stats.created }}，更新 {{ importResult.stats.updated }}，跳过 {{ importResult.stats.skipped }}，失败 {{ importResult.stats.failed }}
              <br />模型：新增 {{ importResult.stats.models_added }}，更新 {{ importResult.stats.models_updated }}；密钥：新增 {{ importResult.stats.keys_added }}
            </div>
            <div v-else class="text-sm">{{ importResult.detail }}</div>
          </div>
        </div>
        <details v-if="importResult.results && importResult.results.length" class="import-details">
          <summary class="text-sm text-muted">查看明细</summary>
          <div class="import-detail-list">
            <div v-for="r in importResult.results" :key="r.index" class="import-detail-item">
              <span class="badge" :class="r.action === 'failed' ? 'badge-danger' : r.action === 'skipped' ? 'badge-warning' : 'badge-success'">
                {{ r.action }}
              </span>
              <span class="text-sm">{{ r.name }}</span>
              <span v-if="r.error" class="text-xs text-danger">{{ r.error }}</span>
              <span v-else-if="r.reason" class="text-xs text-muted">{{ r.reason }}</span>
              <span v-else-if="r.action === 'created' || r.action === 'updated'" class="text-xs text-muted">
                模型 +{{ r.models_added }} ~{{ r.models_updated }} / 密钥 +{{ r.keys_added }}
              </span>
            </div>
          </div>
        </details>
      </div>

      <template #footer>
        <button class="btn btn-outline" @click="showImportModal = false">关闭</button>
        <button class="btn btn-primary" @click="doImport" :disabled="importing || !importData.trim()">
          <AppIcon name="upload" :size="13" />
          {{ importing ? '导入中...' : '开始导入' }}
        </button>
      </template>
    </AppModal>

    <!-- 编辑/添加服务商 -->
    <AppModal v-model="showModal" :title="isEditing ? '编辑服务商' : '添加服务商'" icon="server" size="lg">
      <div class="modal-tabs">
        <button :class="['modal-tab', { active: activeTab === 'basic' }]" @click="activeTab = 'basic'">基本信息</button>
        <button :class="['modal-tab', { active: activeTab === 'keys' }]" @click="activeTab = 'keys'" :disabled="!editingId">
          密钥管理 <span v-if="modalKeys.length" class="badge badge-neutral badge-sm">{{ modalKeys.length }}</span>
        </button>
      </div>

      <div v-if="activeTab === 'basic'">
        <div class="form-group">
          <label class="form-label">名称 *</label>
          <input v-model="form.name" type="text" placeholder="例如 lyclaude" />
        </div>
        <div class="form-group">
          <label class="form-label">API Base URL *</label>
          <input v-model="form.base_url" type="url" placeholder="https://api.example.com/v1" />
        </div>
        <div class="form-group">
          <label class="checkbox-label">
            <input v-model="form.proxy_enabled" type="checkbox" />
            <span>强制使用代理池（开启后不受全局代理开关影响）</span>
          </label>
        </div>
        <div class="form-group">
          <label class="form-label">API 类型</label>
          <select v-model="form.api_type">
            <option value="openai_compat">OpenAI 兼容</option>
            <option value="codex_responses">Codex / Responses API</option>
            <option value="anthropic">Anthropic Claude (API Key)</option>
            <option value="github">GitHub Models</option>
            <option value="atomcode">AtomCode (AtomGit 签名反代)</option>
          </select>
        </div>
        <div class="form-group">
          <label class="form-label">凭据类型</label>
          <select v-model="form.credential_type">
            <option value="api_key">API Key</option>
          </select>
        </div>
        <div v-if="!isEditing && form.api_type !== 'atomcode'" class="form-group">
          <label class="form-label">API Key（可选，填了保存时一并添加）</label>
          <div class="input-with-btn">
            <input v-model="keyForm.key" :type="showKeyInput ? 'text' : 'password'" placeholder="sk-..." />
            <button class="btn btn-ghost btn-xs" @click="showKeyInput = !showKeyInput">
              <AppIcon :name="showKeyInput ? 'eyeOff' : 'eye'" :size="12" />
            </button>
          </div>
        </div>
        <div v-if="!isEditing && form.api_type !== 'atomcode' && keyForm.key" class="form-group">
          <label class="form-label">密钥标签（可选）</label>
          <input v-model="keyForm.label" placeholder="例如 prod / test" />
        </div>
        <div class="form-group">
          <label class="form-label">描述</label>
          <textarea v-model="form.description" rows="3" placeholder="可选说明"></textarea>
        </div>
      </div>

      <div v-else>
        <p class="text-sm text-muted">管理「{{ form.name }}」的 API Key。</p>
        <div v-if="modalKeys.length" class="key-list">
          <div class="key-item" v-for="k in modalKeys" :key="k.id">
            <div class="key-info">
              <strong>{{ k.label || '未命名' }}</strong>
              <code class="mono text-xs">{{ k.key_prefix }}***</code>
              <span class="badge" :class="k.is_active ? 'badge-success' : 'badge-danger'">{{ k.is_active ? '可用' : '停用' }}</span>
            </div>
            <div class="key-actions">
              <button class="btn btn-outline btn-xs" @click="toggleReveal(k)" :disabled="revealingId === k.id">
                <AppIcon :name="revealedKeys[k.id] ? 'eyeOff' : 'eye'" :size="11" />
                {{ revealedKeys[k.id] ? '隐藏' : '查看' }}
              </button>
              <button class="btn btn-danger btn-xs" @click="deleteKey(k)">
                <AppIcon name="trash" :size="11" />删除
              </button>
            </div>
            <div v-if="revealedKeys[k.id]" class="key-revealed">
              <code class="mono text-xs">{{ revealedKeys[k.id] }}</code>
              <button class="btn btn-outline btn-xs" @click="copyKey(k.id)">
                <AppIcon name="copy" :size="11" />复制
              </button>
            </div>
          </div>
        </div>
        <EmptyState v-else icon="key" title="暂无密钥" small />

        <div class="add-key-form">
          <h4 class="text-base">添加新密钥</h4>
          <div class="form-group">
            <label class="form-label">API Key *</label>
            <div class="input-with-btn">
              <input v-model="keyForm.key" :type="showKeyInput ? 'text' : 'password'" placeholder="sk-..." />
              <button class="btn btn-ghost btn-xs" @click="showKeyInput = !showKeyInput">
                <AppIcon :name="showKeyInput ? 'eyeOff' : 'eye'" :size="12" />
              </button>
            </div>
          </div>
          <div class="form-group">
            <label class="form-label">标签</label>
            <input v-model="keyForm.label" placeholder="例如 prod / test" />
          </div>
          <button class="btn btn-primary btn-sm" @click="addKey" :disabled="keySaving">
            <AppIcon name="plus" :size="12" />
            {{ keySaving ? '添加中...' : '添加密钥' }}
          </button>
        </div>
      </div>

      <template #footer>
        <button v-if="isEditing && activeTab === 'basic'" class="btn btn-outline" @click="openImportPricingFromEdit">
          <AppIcon name="dollar" :size="13" />导入定价
        </button>
        <button v-if="activeTab === 'keys'" class="btn btn-outline" @click="activeTab = 'basic'">
          <AppIcon name="chevronLeft" :size="13" />返回
        </button>
        <span style="flex: 1"></span>
        <button class="btn btn-outline" @click="showModal = false">取消</button>
        <button v-if="activeTab === 'basic'" class="btn btn-primary" @click="saveProvider" :disabled="saving">
          {{ saving ? '保存中...' : '保存' }}
        </button>
      </template>
    </AppModal>

    <!-- 导入定价 -->
    <AppModal v-model="showImportPricingModal" :title="'导入定价 — ' + (importPricingTarget?.name || '')" icon="dollar" size="md">
      <p class="text-sm text-muted">粘贴 newapi / one-api 的 /api/pricing JSON。</p>
      <textarea v-model="importPricingJson" rows="12" placeholder='{"data":[...]}'></textarea>
      <template #footer>
        <button class="btn btn-outline" @click="showImportPricingModal = false">取消</button>
        <button class="btn btn-primary" @click="doImportPricing" :disabled="importingPricing">
          {{ importingPricing ? '导入中...' : '导入' }}
        </button>
      </template>
    </AppModal>

    <!-- AtomCode 鉴权 -->
    <AppModal v-model="showAtomAuthModal" title="启用 AtomCode 直连反代" icon="scan" size="md">
      <p class="text-sm text-muted">
        AIGate 将直接对 AtomGit LLM 网关做反向代理，无需本地运行任何中间件。请粘贴你的 AtomCode 鉴权 JSON（默认读取路径
        <code>~/.atomcode/auth.toml</code>，点击「从默认路径加载」可自动解析），需含 <code>access_token</code> / <code>refresh_token</code> / <code>user.id</code>。
      </p>
      <div class="form-group">
        <label class="form-label">鉴权 JSON *</label>
        <textarea v-model="atomAuthJson" rows="10" placeholder='{"access_token":"...","refresh_token":"...","user":{"id":"..."},"expires_in":7200,"created_at":1700000000}'></textarea>
      </div>
      <p class="text-xs text-muted">必须包含 access_token；user.id 用于请求签名；refresh_token 用于自动续期。</p>
      <p v-if="atomAuthError" class="alert alert-error text-sm">{{ atomAuthError }}</p>
      <template #footer>
        <button class="btn btn-outline" @click="showAtomAuthModal = false">取消</button>
        <button class="btn btn-outline" @click="loadAtomAuth" :disabled="atomAuthLoading || atomAuthBusy">
          {{ atomAuthLoading ? '读取中...' : '从默认路径加载' }}
        </button>
        <button class="btn btn-primary" @click="confirmAtomAuth" :disabled="atomAuthBusy">
          {{ atomAuthBusy ? '启用中...' : '确认启用' }}
        </button>
      </template>
    </AppModal>

    <!-- AtomCode 可执行文件配置 -->
    <AppModal v-model="showAtomExeModal" title="配置 AtomCode 可执行文件" icon="scan" size="md">
      <p class="text-sm text-muted">
        AIGate 的 AtomCode 适配器需要本机运行 <code>atomcode</code> 守护进程来为请求签名。当前未找到可执行文件，请先安装 AtomCode 桌面客户端，再把<strong>安装目录</strong>或
        <strong>exe 路径</strong>填到下面。
      </p>
      <ol class="text-sm text-secondary" style="margin: var(--space-3) 0 var(--space-3) var(--space-5); line-height: 1.8">
        <li>从 <a href="https://atomgit.com" target="_blank" rel="noopener">AtomGit 官网</a> 下载并安装「AtomCode / AtomGit 桌面客户端」。</li>
        <li>记下它的安装目录（例如 <code>AppData\Local\AtomCode</code> 或你自定义的路径）。</li>
        <li>把该目录（或目录里的 <code>atomcode.exe</code>）粘贴到下方，点击保存。AIGate 会自动拉起守护进程并验证。</li>
      </ol>
      <div class="form-group">
        <label class="form-label">安装目录 或 exe 路径 *</label>
        <input v-model="atomExePath" placeholder="例如 AppData\Local\AtomCode 或 atomcode\atomcode.exe" />
      </div>
      <p v-if="atomExeStatus.configured_path && atomExeStatus.configured_path !== atomExePath" class="text-xs text-muted">
        当前已保存路径：<code>{{ atomExeStatus.configured_path }}</code>
      </p>
      <p v-if="atomExeError" class="alert alert-error text-sm">{{ atomExeError }}</p>
      <p v-if="atomExeWarning" class="alert alert-warning text-sm">{{ atomExeWarning }}</p>
      <template #footer>
        <button class="btn btn-outline" @click="showAtomExeModal = false">取消</button>
        <button class="btn btn-primary" @click="setAtomExePath" :disabled="atomExeBusy">
          {{ atomExeBusy ? '验证中...' : '保存并验证' }}
        </button>
      </template>
    </AppModal>

    <!-- OAuth 导入 token -->
    <AppModal v-model="importTokenModal.show" :title="'导入 ' + importTokenModal.provider_code + ' token'" icon="shield" size="md">
      <div class="form-group">
        <label class="form-label">Access Token *</label>
        <textarea v-model="importTokenModal.access_token" rows="3"></textarea>
      </div>
      <div class="form-group">
        <label class="form-label">Refresh Token</label>
        <textarea v-model="importTokenModal.refresh_token" rows="3"></textarea>
      </div>
      <div class="form-row">
        <div class="form-group">
          <label class="form-label">过期秒数</label>
          <input type="number" v-model.number="importTokenModal.expires_in" min="60" />
        </div>
        <div class="form-group">
          <label class="form-label">Owner</label>
          <input v-model="importTokenModal.owner" />
        </div>
      </div>
      <p v-if="importTokenError" class="alert alert-error text-sm">{{ importTokenError }}</p>
      <template #footer>
        <button class="btn btn-outline" @click="importTokenModal.show = false">取消</button>
        <button class="btn btn-primary" @click="submitImportToken" :disabled="importTokenBusy">
          {{ importTokenBusy ? '导入中...' : '确认导入' }}
        </button>
      </template>
    </AppModal>

    <!-- 一键备份 -->
    <AppModal v-model="showBackupModal" title="一键备份" icon="archive" size="md">
      <p class="text-sm text-muted">
        导出全系统配置到一个 JSON 文件：服务商、模型、密钥、OAuth 连接、组合路由、Auto 权重以及各项设置。
        换机 / 重装时用「一键恢复」即可完整还原。
      </p>
      <div class="alert alert-warning" style="margin-top: var(--space-3)">
        <AppIcon name="alert" :size="16" />
        <div>
          <div class="alert-title">备份含明文密钥</div>
          <div class="text-sm">
            文件包含明文 API Key 与 OAuth token，可直接换机恢复。这是敏感文件，请妥善保管，不要上传到公开位置。
          </div>
        </div>
      </div>
      <template #footer>
        <button class="btn btn-outline" @click="showBackupModal = false">取消</button>
        <button class="btn btn-primary" @click="doBackup" :disabled="backingUp">
          <AppIcon name="archive" :size="13" />
          {{ backingUp ? '备份中...' : '下载备份文件' }}
        </button>
      </template>
    </AppModal>

    <!-- 一键恢复 -->
    <AppModal v-model="showRestoreModal" title="一键恢复" icon="upload" size="lg">
      <div class="form-group">
        <label class="form-label">粘贴备份 JSON 或选择文件</label>
        <textarea v-model="restoreData" rows="8" placeholder='{"kind":"aigate.backup",...}'></textarea>
        <div class="form-hint">
          <input type="file" accept="application/json,.json" @change="handleRestoreFileSelect" id="restore-file-input" style="display: none" />
          <button class="btn btn-ghost btn-xs" @click="$el.querySelector('#restore-file-input').click()">
            <AppIcon name="file" :size="11" />选择备份文件
          </button>
        </div>
      </div>

      <div class="form-group">
        <label class="form-label">冲突策略</label>
        <div class="radio-group">
          <label class="radio-label">
            <input type="radio" value="skip" v-model="restoreConflict" />
            <span>跳过 (skip) — 已存在的同名项保持不动</span>
          </label>
          <label class="radio-label">
            <input type="radio" value="merge" v-model="restoreConflict" />
            <span>合并 (merge) — 更新已存在项，补齐缺失项（默认）</span>
          </label>
          <label class="radio-label">
            <input type="radio" value="replace" v-model="restoreConflict" />
            <span>替换 (replace) — 服务商下的模型与密钥清空重建</span>
          </label>
        </div>
      </div>

      <div class="form-group">
        <label class="form-label">恢复内容</label>
        <div class="restore-checks">
          <label class="checkbox-label"><input type="checkbox" v-model="restoreOptions.restore_models" /><span>模型</span></label>
          <label class="checkbox-label"><input type="checkbox" v-model="restoreOptions.restore_keys" /><span>密钥</span></label>
          <label class="checkbox-label"><input type="checkbox" v-model="restoreOptions.restore_oauth" /><span>OAuth 连接</span></label>
          <label class="checkbox-label"><input type="checkbox" v-model="restoreOptions.restore_combos" /><span>组合路由</span></label>
          <label class="checkbox-label"><input type="checkbox" v-model="restoreOptions.restore_weights" /><span>Auto 权重</span></label>
          <label class="checkbox-label"><input type="checkbox" v-model="restoreOptions.restore_config" /><span>系统设置</span></label>
        </div>
      </div>

      <div v-if="restoreResult" class="import-result">
        <div class="alert" :class="restoreResult.ok ? 'alert-success' : 'alert-warning'">
          <AppIcon :name="restoreResult.ok ? 'checkCircle' : 'alert'" :size="16" />
          <div>
            <div class="alert-title">{{ restoreResult.ok ? '恢复完成' : '恢复完成（部分有误）' }}</div>
            <div class="text-sm">
              服务商：新建 {{ restoreResult.stats.providers_created }}，更新 {{ restoreResult.stats.providers_updated }}，跳过 {{ restoreResult.stats.providers_skipped }}
              <br />模型：+{{ restoreResult.stats.models_added }} ~{{ restoreResult.stats.models_updated }}；密钥：+{{ restoreResult.stats.keys_added }}
              <br />OAuth：{{ restoreResult.stats.oauth_restored }}；组合：+{{ restoreResult.stats.combos_created }} ~{{ restoreResult.stats.combos_updated }}
              <br />Auto 权重：{{ restoreResult.stats.weights_restored ? '已恢复' : '未恢复' }}；系统设置：{{ restoreResult.stats.config_restored ? '已恢复' : '未恢复' }}
            </div>
          </div>
        </div>
        <details v-if="restoreResult.errors && restoreResult.errors.length" class="import-details">
          <summary class="text-sm text-danger">{{ restoreResult.errors.length }} 项出错，查看详情</summary>
          <div class="import-detail-list">
            <div v-for="(err, i) in restoreResult.errors" :key="i" class="text-xs text-danger">{{ err }}</div>
          </div>
        </details>
      </div>

      <template #footer>
        <button class="btn btn-outline" @click="showRestoreModal = false">关闭</button>
        <button class="btn btn-primary" @click="doRestore" :disabled="restoring || !restoreData.trim()">
          <AppIcon name="upload" :size="13" />
          {{ restoring ? '恢复中...' : '开始恢复' }}
        </button>
      </template>
    </AppModal>
  </div>
</template>

<script>
import api from '../api.js'
import toast from '../toast.js'
import AppIcon from '../components/AppIcon.vue'
import PageHeader from '../components/PageHeader.vue'
import StatCard from '../components/StatCard.vue'
import StatusBadge from '../components/StatusBadge.vue'
import AppModal from '../components/AppModal.vue'
import EmptyState from '../components/EmptyState.vue'
import ModelRefreshModal from '../components/ModelRefreshModal.vue'
import { FREE_TIER_PROVIDERS, OAUTH_PROVIDERS as OAUTH_CATALOG, APIKEY_PROVIDERS } from '../data/providersConstants.js'

const PIN_KEY = 'aigate:pinnedProviders'

export default {
  name: 'Providers',
  components: { AppIcon, PageHeader, StatCard, StatusBadge, AppModal, EmptyState, ModelRefreshModal },
  data() {
    return {
      providers: [],
      allKeys: [],
      providerStats: {},
      oauthProvidersState: [],
      oauthConnections: [],
      pageTab: 'enabled',
      providerFilter: 'all',
      providerSearch: '',
      providerSort: 'pinned',
      catalogTab: 'api_key',
      catalogSearch: '',
      providerFilters: [
        { value: 'all', label: '全部' },
        { value: 'api_key', label: 'API Key' },
        { value: 'free_tier', label: 'Free Tier' },
        { value: 'oauth', label: 'OAuth' },
        { value: 'error', label: '异常' },
      ],
      freeTierProviders: FREE_TIER_PROVIDERS,
      apikeyProviders: APIKEY_PROVIDERS,
      pinnedIds: [],
      highlightProviderId: null,

      // 编辑/添加服务商
      showModal: false,
      saving: false,
      isEditing: false,
      editingId: null,
      activeTab: 'basic',
      form: { name: '', base_url: '', api_type: 'openai_compat', credential_type: 'api_key', oauth_code: null, description: '' },
      modalKeys: [],
      keyForm: { key: '', label: '' },
      keySaving: false,
      showKeyInput: false,
      revealedKeys: {},
      revealingId: null,

      // 导入定价
      showImportPricingModal: false,
      importPricingTarget: null,
      importPricingJson: '',
      importingPricing: false,

      // AtomCode 鉴权
      showAtomAuthModal: false,
      atomAuthTarget: null,
      atomAuthJson: '',
      atomAuthBusy: false,
      atomAuthError: '',
      atomAuthLoading: false,
      atomExeStatus: { found: true, exe_path: null, configured_path: null },
      showAtomExeModal: false,
      atomExePath: '',
      atomExeBusy: false,
      atomExeError: '',
      atomExeWarning: '',

      // OAuth 导入 token
      importTokenModal: { show: false, provider_code: '', access_token: '', refresh_token: '', expires_in: 3600, owner: '__default' },
      importTokenBusy: false,
      importTokenError: '',
      pendingCode: null,

      // 导出/导入
      showExportModal: false,
      exportIncludeKeys: false,
      exportProviderIds: [],
      exporting: false,

      showImportModal: false,
      importData: '',
      importConflict: 'merge',
      importKeys: true,
      importModels: true,
      importing: false,
      importResult: null,

      // 一键备份 / 恢复
      showBackupModal: false,
      backingUp: false,
      showRestoreModal: false,
      restoreData: '',
      restoreConflict: 'merge',
      restoring: false,
      restoreResult: null,
      restoreOptions: {
        restore_models: true,
        restore_keys: true,
        restore_oauth: true,
        restore_combos: true,
        restore_weights: true,
        restore_config: true,
      },

      busy: { refreshModels: false, cleanOrphans: false, enableCode: '', togglingProvider: false },
      showRefreshModal: false,
      refreshResult: null,
    }
  },
  computed: {
    oauthProviders() {
      if (this.oauthProvidersState?.length) return this.oauthProvidersState
      return OAUTH_CATALOG.map((p) => ({
        ...p,
        code: p.id,
        api_base_url: p.baseUrl,
        use_pkce: !['codebuddy_cn', 'kimchi', 'qoder'].includes(p.id),
        extra_params: {},
      }))
    },
    dashboardStats() {
      const s = { apiKey: 0, freeTier: 0, oauth: 0, error: 0 }
      for (const p of this.providers) {
        const t = p.credential_type || 'api_key'
        if (t === 'free_tier') s.freeTier++
        else if (t === 'oauth') s.oauth++
        else s.apiKey++
        if (this.providerStatus(p).level === 'error') s.error++
      }
      return s
    },
    filteredProviders() {
      let rows = [...this.providers]
      const q = this.providerSearch.toLowerCase()
      if (q) rows = rows.filter((p) => [p.name, p.base_url, p.oauth_code, p.api_type].some((v) => String(v || '').toLowerCase().includes(q)))
      if (this.providerFilter !== 'all') {
        if (this.providerFilter === 'error') rows = rows.filter((p) => this.providerStatus(p).level === 'error')
        else rows = rows.filter((p) => (p.credential_type || 'api_key') === this.providerFilter)
      }
      const byName = (a, b) => a.name.localeCompare(b.name)
      rows.sort((a, b) => {
        if (this.providerSort === 'pinned') return Number(this.isPinned(b.id)) - Number(this.isPinned(a.id)) || byName(a, b)
        if (this.providerSort === 'type') return (a.credential_type || 'api_key').localeCompare(b.credential_type || 'api_key') || byName(a, b)
        if (this.providerSort === 'models') return this.modelCount(b.id) - this.modelCount(a.id) || byName(a, b)
        if (this.providerSort === 'failures') return this.failCount(b.id) - this.failCount(a.id) || byName(a, b)
        return byName(a, b)
      })
      return rows
    },
    visibleCatalog() {
      const source = this.catalogTab === 'free_tier' ? this.freeTierProviders : this.catalogTab === 'oauth' ? this.oauthProviders : this.apikeyProviders
      const q = this.catalogSearch.toLowerCase()
      return source.filter((c) => !q || [c.name, c.id, c.code, c.alias, c.baseUrl, c.api_base_url, c.website].some((v) => String(v || '').toLowerCase().includes(q)))
    },
  },
  mounted() {
    this.pinnedIds = JSON.parse(localStorage.getItem(PIN_KEY) || '[]')
    this.load()
  },
  methods: {
    async load() {
      const [providers, keys, modelStats, oauthProviders, oauthConnections, atomExe] = await Promise.all([
        api.getProviders(),
        api.getKeys().catch(() => []),
        api.getProviderModelStats().catch(() => []),
        api.getOAuthProviders().catch(() => []),
        api.getOAuthConnections().catch(() => []),
        api.getAtomExeStatus().catch(() => ({ found: true })),
      ])
      this.providers = providers || []
      this.allKeys = keys || []
      this.providerStats = Object.fromEntries(
        (modelStats || []).map((item) => [Number(item.provider_id), {
          model_count: Number(item.model_count) || 0,
          fail_count: Number(item.fail_count) || 0,
        }])
      )
      this.oauthProvidersState = oauthProviders || []
      this.oauthConnections = oauthConnections || []
      this.atomExeStatus = atomExe && atomExe.found !== undefined ? atomExe : { found: true }
    },
    normalizeUrl(v) {
      return String(v || '')
        .replace(/\/+$/, '')
        .toLowerCase()
    },
    catalogKey(c) {
      return c.id || c.code || c.name
    },
    matchProvider(c) {
      const code = c.id || c.code
      const base = this.normalizeUrl(c.baseUrl || c.api_base_url)
      return this.providers.find((p) => (code && p.oauth_code === code) || p.name === c.name || (base && this.normalizeUrl(p.base_url) === base))
    },
    catalogStatus(c) {
      const p = this.matchProvider(c)
      if (!p) return { state: 'not_added', text: '未添加', level: 'idle', provider: null }
      const st = this.providerStatus(p)
      if (st.level === 'error') return { state: 'error', text: '异常', level: 'error', provider: p }
      return { state: 'enabled', text: '已启用', level: 'ok', provider: p }
    },
    keyCount(id) {
      return this.allKeys.filter((k) => k.provider_id === id).length
    },
    getProviderKeys(id) {
      return this.allKeys.filter((k) => k.provider_id === id)
    },
    modelCount(id) {
      return this.providerStats[id]?.model_count || 0
    },
    failCount(id) {
      return this.providerStats[id]?.fail_count || 0
    },
    oauthConnectionCount(p) {
      return this.oauthConnections.filter((c) => c.provider_code === p.oauth_code || c.provider_code === p.name).length
    },
    statusLabels(p) {
      const labels = []
      const type = p.credential_type || 'api_key'
      if (type === 'api_key' && this.keyCount(p.id) === 0 && p.api_type !== 'atomcode') labels.push('缺密钥')
      if (p.api_type === 'atomcode' && !this.atomExeStatus.found) labels.push('需配置 AtomCode')
      if (this.modelCount(p.id) === 0) labels.push('无模型')
      if (type === 'oauth' && this.oauthConnectionCount(p) === 0) labels.push('OAuth 未连接')
      if (!/^https?:\/\//i.test(p.base_url || '') && type !== 'free_tier') labels.push('URL 异常')
      if (p.proxy_enabled) labels.push('代理')
      if (this.failCount(p.id) > 0) labels.push(`失败 ${this.failCount(p.id)}`)
      return labels
    },
    providerStatus(p) {
      // v4.0: 服务商被禁用
      if (this.isProviderDisabled(p)) return { text: '已禁用', level: 'muted' }
      const labels = this.statusLabels(p)
      if (labels.some((x) => ['缺密钥', '无模型', 'OAuth 未连接', 'URL 异常', '需配置 AtomCode'].includes(x))) return { text: labels[0], level: 'error' }
      if (labels.length) return { text: '需关注', level: 'warn' }
      return { text: '可用', level: 'ok' }
    },
    isProviderDisabled(p) {
      return p && p.enabled === false
    },
    async toggleProviderEnabled(p, enabled) {
      if (this.busy.togglingProvider) return
      this.busy.togglingProvider = true
      try {
        await api.updateProvider(p.id, { enabled })
        p.enabled = enabled
        toast.success(enabled ? `服务商「${p.name}」已启用` : `服务商「${p.name}」已禁用，请求将自动跳过`)
      } catch (e) {
        toast.error(e.response?.data?.detail || '切换失败')
      } finally {
        this.busy.togglingProvider = false
      }
    },
    async toggleProviderProxy(p, proxyEnabled) {
      try {
        await api.updateProvider(p.id, { proxy_enabled: proxyEnabled })
        p.proxy_enabled = proxyEnabled
        toast.success(proxyEnabled ? `服务商「${p.name}」已强制走代理池` : `服务商「${p.name}」已恢复跟随全局代理开关`)
      } catch (e) {
        toast.error(e.response?.data?.detail || '代理开关更新失败')
      }
    },
    credLabel(t) {
      return { api_key: 'API Key', free_tier: 'Free', oauth: 'OAuth' }[t || 'api_key'] || 'API Key'
    },
    credBadgeClass(t) {
      const map = { api_key: 'badge-info', free_tier: 'badge-success', oauth: 'badge-neutral' }
      return map[t || 'api_key'] || 'badge-info'
    },
    isPinned(id) {
      return this.pinnedIds.includes(id)
    },
    togglePin(id) {
      this.pinnedIds = this.isPinned(id) ? this.pinnedIds.filter((x) => x !== id) : [...this.pinnedIds, id]
      localStorage.setItem(PIN_KEY, JSON.stringify(this.pinnedIds))
    },
    focusProvider(p) {
      this.pageTab = 'enabled'
      this.providerSearch = p.name
      this.highlightProviderId = p.id
      setTimeout(() => {
        this.highlightProviderId = null
      }, 3000)
    },

    // 编辑/添加服务商
    openAddModal() {
      this.resetForm()
      this.showModal = true
    },
    editProvider(p) {
      this.form = {
        name: p.name,
        base_url: p.base_url,
        api_type: p.api_type,
        credential_type: p.credential_type || 'api_key',
        oauth_code: p.oauth_code || null,
        proxy_enabled: !!p.proxy_enabled,
        description: p.description || '',
      }
      this.isEditing = true
      this.editingId = p.id
      this.activeTab = 'basic'
      this.modalKeys = this.getProviderKeys(p.id)
      this.showModal = true
    },
    editProviderKeys(p) {
      this.editProvider(p)
      this.activeTab = 'keys'
    },
    resetForm() {
      this.form = { name: '', base_url: '', api_type: 'openai_compat', credential_type: 'api_key', oauth_code: null, proxy_enabled: false, description: '' }
      this.isEditing = false
      this.editingId = null
      this.activeTab = 'basic'
      this.modalKeys = []
      this.keyForm = { key: '', label: '' }
      this.revealedKeys = {}
    },
    async saveProvider() {
      if (!this.form.name.trim() || !this.form.base_url.trim()) {
        toast.error('请填写名称和 Base URL')
        return
      }
      this.saving = true
      try {
        const base = {
          name: this.form.name.trim(),
          base_url: this.form.base_url.trim(),
          api_type: this.form.api_type,
          proxy_enabled: !!this.form.proxy_enabled,
          description: this.form.description || '',
        }
        if (this.isEditing) {
          await api.updateProvider(this.editingId, base)
          await this.load()
          toast.success('服务商已更新')
        } else {
          const saved = await api.createProvider({ ...base, credential_type: 'api_key', oauth_code: null })
          const k = (this.keyForm.key || '').trim()
          if (k && this.form.api_type !== 'atomcode') {
            try {
              await api.createKey({ provider_id: saved.id, key: k, label: (this.keyForm.label || '').trim() })
            } catch (e) {
              toast.warning('服务商已创建，但密钥添加失败：' + e.message)
            }
          }
          await this.load()
          this.editingId = saved.id
          this.isEditing = true
          this.keyForm = { key: '', label: '' }
          toast.success('服务商已添加')
        }
        this.showModal = false
      } catch (e) {
        toast.error('保存失败: ' + e.message)
      } finally {
        this.saving = false
      }
    },
    async deleteProvider(p) {
      if (!confirm(`确认删除服务商「${p.name}」？关联密钥和模型也会删除。`)) return
      try {
        await api.deleteProvider(p.id)
        await this.load()
        toast.success('已删除')
      } catch (e) {
        toast.error('删除失败: ' + e.message)
      }
    },

    // 密钥管理
    async addKey() {
      if (!this.keyForm.key.trim()) {
        toast.error('请填写 API Key')
        return
      }
      this.keySaving = true
      try {
        await api.createKey({ provider_id: this.editingId, key: this.keyForm.key.trim(), label: this.keyForm.label.trim() })
        this.keyForm = { key: '', label: '' }
        this.showKeyInput = false
        await this.load()
        this.modalKeys = this.getProviderKeys(this.editingId)
        toast.success('密钥已添加')
      } catch (e) {
        toast.error('添加密钥失败: ' + e.message)
      } finally {
        this.keySaving = false
      }
    },
    async toggleReveal(k) {
      if (this.revealedKeys[k.id]) {
        const x = { ...this.revealedKeys }
        delete x[k.id]
        this.revealedKeys = x
        return
      }
      this.revealingId = k.id
      try {
        const d = await api.revealKey(k.id)
        this.revealedKeys = { ...this.revealedKeys, [k.id]: d.key || d }
      } catch (e) {
        toast.error('查看密钥失败: ' + e.message)
      } finally {
        this.revealingId = null
      }
    },
    async copyKey(id) {
      await navigator.clipboard?.writeText(this.revealedKeys[id] || '')
      toast.success('已复制到剪贴板')
    },
    async deleteKey(k) {
      if (!confirm(`确认删除密钥「${k.label || k.key_prefix + '***'}」？`)) return
      try {
        await api.deleteKey(k.id)
        await this.load()
        this.modalKeys = this.getProviderKeys(this.editingId)
        toast.success('已删除')
      } catch (e) {
        toast.error('删除失败: ' + e.message)
      }
    },

    // 导入定价
    openImportPricing(p) {
      this.importPricingTarget = p
      this.importPricingJson = ''
      this.showImportPricingModal = true
    },
    openImportPricingFromEdit() {
      this.showModal = false
      this.openImportPricing({ id: this.editingId, name: this.form.name })
    },
    async doImportPricing() {
      if (!this.importPricingJson.trim()) {
        toast.error('请粘贴定价 JSON')
        return
      }
      this.importingPricing = true
      try {
        const data = await api.importProviderPricing(this.importPricingTarget.id, this.importPricingJson)
        this.showImportPricingModal = false
        await this.load()
        toast.success(`已更新 ${data.updated} 个，新建 ${data.created || 0} 个`)
      } catch (e) {
        toast.error('导入失败: ' + e.message)
      } finally {
        this.importingPricing = false
      }
    },

    // AtomCode
    async loadAtomAuth() {
      const c = this.atomAuthTarget
      if (!c) return
      this.atomAuthLoading = true
      this.atomAuthError = ''
      try {
        const r = await api.loadAtomAuth()
        this.atomAuthJson = JSON.stringify(r.auth, null, 2)
      } catch (e) {
        this.atomAuthError = '自动读取默认路径失败：' + e.message + '（可手动粘贴 JSON）'
      } finally {
        this.atomAuthLoading = false
      }
    },
    async confirmAtomAuth() {
      const c = this.atomAuthTarget
      if (!c) return
      let parsed
      try {
        parsed = JSON.parse(this.atomAuthJson)
      } catch (e) {
        this.atomAuthError = 'JSON 解析失败：' + e.message
        return
      }
      if (!parsed || !parsed.access_token) {
        this.atomAuthError = '鉴权 JSON 必须包含 access_token 字段'
        return
      }
      this.atomAuthBusy = true
      try {
        const provider = await api.createProvider({
          name: c.name,
          base_url: c.baseUrl,
          api_type: c.api_type || 'atomcode',
          credential_type: 'api_key',
          oauth_code: c.id || c.code,
          description: `AtomCode 直连反代`,
        })
        try {
          await api.createKey({ provider_id: provider.id, key: this.atomAuthJson, label: c.name + ' 鉴权' })
        } catch (e) {
          if (!String(e.message).includes('409')) throw e
        }
        const modelIds = c.models && c.models.length ? c.models : ['deepseek-v4-flash']
        for (const mid of modelIds) {
          try {
            await api.createProviderModel(provider.id, { model_id: mid, display_name: mid, input_price: 0, output_price: 0 })
          } catch (e) {
            if (!String(e.message).includes('409')) throw e
          }
        }
        this.showAtomAuthModal = false
        this.atomAuthJson = ''
        await this.load()
        this.focusProvider(provider)
        toast.success(`${c.name} 已启用（AIGate 直连 AtomGit 网关）。可在 Playground 使用 ${provider.name}/${modelIds[0]}`)
      } catch (e) {
        this.atomAuthError = '启用失败: ' + e.message
      } finally {
        this.atomAuthBusy = false
      }
    },
    openAtomExeConfig(p) {
      this.atomExePath = this.atomExeStatus.configured_path || (p && (p._atomExeDir || '')) || ''
      this.atomExeError = ''
      this.atomExeWarning = ''
      this.atomExeBusy = false
      this.showAtomExeModal = true
    },
    async setAtomExePath() {
      if (!this.atomExePath.trim()) {
        this.atomExeError = '请填写安装目录或 exe 路径'
        return
      }
      this.atomExeBusy = true
      this.atomExeError = ''
      this.atomExeWarning = ''
      try {
        const r = await api.setAtomExePath(this.atomExePath.trim())
        await this.load()
        if (r.warning) {
          this.atomExeWarning = r.warning
          toast.warning('路径已保存，但守护进程拉起失败：' + r.warning)
        } else {
          this.showAtomExeModal = false
          toast.success('AtomCode 可执行文件已配置，守护进程已启动 ✓')
        }
      } catch (e) {
        this.atomExeError = '配置失败: ' + e.message
      } finally {
        this.atomExeBusy = false
      }
    },

    // 从目录添加
    addFromCatalog(c, credentialType) {
      const baseUrl = c.baseUrl || c.api_base_url || c.website || ''
      this.form = {
        name: c.name,
        base_url: baseUrl,
        api_type: c.id === 'codex' || c.alias === 'cx' ? 'codex_responses' : 'openai_compat',
        credential_type: 'api_key',
        oauth_code: null,
        proxy_enabled: false,
        description: '',
      }
      this.isEditing = false
      this.editingId = null
      this.activeTab = 'basic'
      this.showModal = true
    },

    // 刷新模型
    async refreshAllModels() {
      this.busy.refreshModels = true
      try {
        const r = await api.refreshModels()
        await this.load()
        this.refreshResult = r
        this.showRefreshModal = true
      } catch (e) {
        this.refreshResult = { error: e.message }
        this.showRefreshModal = true
      } finally {
        this.busy.refreshModels = false
      }
    },
    async refreshProviderModels(p) {
      this.busy.refreshModels = true
      try {
        const r = await api.refreshModels(p.id)
        await this.load()
        this.refreshResult = r
        this.showRefreshModal = true
      } catch (e) {
        this.refreshResult = { error: e.message }
        this.showRefreshModal = true
      } finally {
        this.busy.refreshModels = false
      }
    },
    async cleanOrphans() {
      if (!confirm('清理没有服务商归属的孤儿模型？')) return
      this.busy.cleanOrphans = true
      try {
        const r = await api.cleanOrphanModels()
        await this.load()
        toast.success(`已清理 ${r.deleted || 0} 个孤儿模型`)
      } catch (e) {
        toast.error('清理失败: ' + e.message)
      } finally {
        this.busy.cleanOrphans = false
      }
    },

    // OAuth 导入 token
    isImportProvider(code) {
      return ['codebuddy_cn', 'kimchi', 'qoder', 'kimi_coding'].includes(code)
    },
    async startAuthorize(code) {
      if (this.isImportProvider(code)) return this.openImportTokenModal(code)
      this.pendingCode = code
      try {
        const r = await api.startOAuthAuthorize(code)
        if (r.login_url) window.open(r.login_url, '_blank', 'noopener,noreferrer,width=900,height=720')
        else if (r.authorize_url) window.open(r.authorize_url, '_blank', 'noopener,noreferrer,width=720,height=720')
        setTimeout(() => this.load(), 5000)
      } catch (e) {
        toast.error('授权失败: ' + e.message)
      } finally {
        this.pendingCode = null
      }
    },
    openImportTokenModal(code) {
      this.importTokenError = ''
      this.importTokenModal = { show: true, provider_code: code, access_token: '', refresh_token: '', expires_in: 3600, owner: '__default' }
    },
    async submitImportToken() {
      if (!this.importTokenModal.access_token.trim()) {
        this.importTokenError = 'access_token 不能为空'
        return
      }
      this.importTokenBusy = true
      try {
        await api.importOAuthToken({
          provider_code: this.importTokenModal.provider_code,
          access_token: this.importTokenModal.access_token.trim(),
          refresh_token: this.importTokenModal.refresh_token.trim(),
          expires_in: this.importTokenModal.expires_in || 3600,
          owner: this.importTokenModal.owner || '__default',
        })
        this.importTokenModal.show = false
        await this.load()
        toast.success('导入成功')
      } catch (e) {
        this.importTokenError = '导入失败: ' + e.message
      } finally {
        this.importTokenBusy = false
      }
    },
    async refreshOAuth(id) {
      try {
        await api.refreshOAuthConnection(id)
        await this.load()
        toast.success('刷新成功')
      } catch (e) {
        toast.error('刷新失败: ' + e.message)
      }
    },
    async removeOAuth(id) {
      if (!confirm('确认删除该 OAuth 连接？')) return
      try {
        await api.deleteOAuthConnection(id)
        await this.load()
        toast.success('已删除')
      } catch (e) {
        toast.error('删除失败: ' + e.message)
      }
    },
    formatTime(t) {
      return t ? String(t).replace('T', ' ').replace('Z', '').slice(0, 19) : '-'
    },

    // 导出
    async doExport() {
      this.exporting = true
      try {
        const params = { include_keys: this.exportIncludeKeys }
        if (this.exportProviderIds.length > 0) {
          params.provider_ids = this.exportProviderIds.join(',')
        }
        const bundle = await api.exportProviders(params)
        const blob = new Blob([JSON.stringify(bundle, null, 2)], { type: 'application/json' })
        const url = URL.createObjectURL(blob)
        const a = document.createElement('a')
        a.href = url
        a.download = `aigate-providers-${new Date().toISOString().slice(0, 10)}.json`
        a.click()
        URL.revokeObjectURL(url)
        toast.success(`已导出 ${bundle.summary.providers} 个服务商`)
        this.showExportModal = false
      } catch (e) {
        toast.error('导出失败: ' + e.message)
      } finally {
        this.exporting = false
      }
    },

    // 导入
    async doImport() {
      if (!this.importData.trim()) {
        toast.error('请粘贴或上传 JSON 数据')
        return
      }
      this.importing = true
      this.importResult = null
      try {
        const result = await api.importProviders({
          data: this.importData,
          conflict: this.importConflict,
          import_keys: this.importKeys,
          import_models: this.importModels,
        })
        this.importResult = result
        if (result.ok) {
          await this.load()
          const s = result.stats
          toast.success(`导入成功：创建 ${s.created}，更新 ${s.updated}，跳过 ${s.skipped}，失败 ${s.failed}`)
        } else {
          toast.error(result.detail || '导入失败')
        }
      } catch (e) {
        toast.error('导入失败: ' + e.message)
      } finally {
        this.importing = false
      }
    },
    handleImportFileSelect(event) {
      const file = event.target.files?.[0]
      if (!file) return
      const reader = new FileReader()
      reader.onload = (e) => {
        this.importData = e.target?.result || ''
      }
      reader.readAsText(file)
    },

    // 一键备份
    async doBackup() {
      this.backingUp = true
      try {
        const bundle = await api.fullBackup()
        const blob = new Blob([JSON.stringify(bundle, null, 2)], { type: 'application/json' })
        const url = URL.createObjectURL(blob)
        const a = document.createElement('a')
        a.href = url
        a.download = `aigate-backup-${new Date().toISOString().slice(0, 10)}.json`
        a.click()
        URL.revokeObjectURL(url)
        const s = bundle.summary || {}
        toast.success(`备份完成：${s.providers || 0} 服务商 / ${s.models || 0} 模型 / ${s.keys || 0} 密钥 / ${s.combos || 0} 组合`)
        this.showBackupModal = false
      } catch (e) {
        toast.error('备份失败: ' + e.message)
      } finally {
        this.backingUp = false
      }
    },

    // 一键恢复
    async doRestore() {
      if (!this.restoreData.trim()) {
        toast.error('请粘贴或上传备份 JSON')
        return
      }
      if (!confirm('确认恢复？将按所选策略写入当前系统配置，建议先做一次备份。')) return
      this.restoring = true
      this.restoreResult = null
      try {
        const result = await api.fullRestore({
          data: this.restoreData,
          conflict: this.restoreConflict,
          ...this.restoreOptions,
        })
        this.restoreResult = result
        await this.load()
        if (result.ok) {
          toast.success('一键恢复完成')
        } else {
          toast.warning(`恢复完成，但有 ${result.errors.length} 项出错`)
        }
      } catch (e) {
        toast.error('恢复失败: ' + e.message)
      } finally {
        this.restoring = false
      }
    },
    handleRestoreFileSelect(event) {
      const file = event.target.files?.[0]
      if (!file) return
      const reader = new FileReader()
      reader.onload = (e) => {
        this.restoreData = e.target?.result || ''
      }
      reader.readAsText(file)
    },
  },
}
</script>

<style scoped>
/* 工具栏 */
.toolbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--space-3);
  margin-bottom: var(--space-4);
  flex-wrap: wrap;
}
.toolbar-left,
.toolbar-right {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  flex-wrap: wrap;
}
.segmented {
  display: flex;
  gap: 2px;
  background: var(--surface-2);
  padding: 2px;
  border-radius: var(--radius-md);
}
.segmented button {
  padding: 4px var(--space-3);
  border: 0;
  background: transparent;
  color: var(--text-muted);
  border-radius: var(--radius-sm);
  cursor: pointer;
  font-size: var(--text-sm);
  transition: all 0.15s;
}
.segmented button.active {
  background: var(--surface-4);
  color: var(--text-primary);
  font-weight: 500;
}
.input-group {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  background: var(--surface-2);
  padding: 0 var(--space-3);
  border-radius: var(--radius-md);
  border: 1px solid var(--border-base);
}
.input-group input {
  border: 0;
  background: transparent;
  padding: 6px 0;
  min-width: 180px;
}

/* Tabs */
.tabs {
  display: flex;
  gap: var(--space-2);
  margin-bottom: var(--space-4);
}
.tab {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  padding: var(--space-2) var(--space-4);
  border: 1px solid var(--border-base);
  background: var(--surface-2);
  color: var(--text-secondary);
  border-radius: var(--radius-md);
  cursor: pointer;
  transition: all 0.15s;
}
.tab.active {
  background: var(--primary);
  border-color: var(--primary);
  color: #fff;
}

.provider-toolbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--space-3);
  min-width: 0;
}
.toolbar-controls {
  display: flex;
  align-items: center;
  justify-content: flex-end;
  flex: 1 1 360px;
  min-width: 280px;
  max-width: 460px;
  gap: var(--space-2);
}
.toolbar-controls .input-group {
  flex: 1 1 auto;
  min-width: 0;
}
.toolbar-controls .input-group input {
  width: 100%;
  min-width: 0;
}
.toolbar-controls select {
  flex: 0 0 126px;
  width: 126px;
  min-height: 32px;
  padding-left: var(--space-2);
}

/* 服务商列表 */
.provider-table-wrap {
  overflow-x: clip;
}
.provider-table {
  table-layout: fixed;
  width: 100%;
  min-width: 0;
  border-collapse: collapse;
  font-size: 12px;
}
.provider-table th,
.provider-table td {
  overflow: hidden;
  padding: 6px 8px;
  border-bottom: 1px solid var(--border-base);
  vertical-align: middle;
  white-space: nowrap;
}
.provider-table thead th {
  color: var(--text-muted);
  font-size: 11px;
  font-weight: 600;
  text-align: left;
  background: var(--surface-1);
  border-bottom-color: var(--border-medium);
}
.provider-table tbody tr:hover {
  background: var(--surface-2);
}
.provider-table tbody tr.highlight {
  box-shadow: inset 3px 0 0 var(--primary);
  background: rgba(91, 141, 255, 0.07);
}
.provider-table tbody tr.disabled {
  opacity: 0.58;
  filter: grayscale(0.5);
}
.provider-table th.col-pin,
.provider-table td.col-pin {
  width: 36px;
  padding-right: 0;
}
.provider-table col.col-name { width: 15%; }
.provider-table col.col-url { width: 20%; }
.provider-table col.col-credential { width: 8%; }
.provider-table col.col-type { width: 9%; }
.provider-table col.col-count { width: 5%; }
.provider-table col.col-status-col { width: 18%; }
.provider-table col.col-proxy { width: 6%; }
.provider-table col.col-action-width { width: 180px; }
.provider-table th.num,
.provider-table td.num {
  text-align: right;
  width: 48px;
}
.provider-table .col-url {
  width: 20%;
}
.provider-table .url-text {
  display: block;
  overflow: hidden;
  text-overflow: ellipsis;
}
.cell-name {
  display: flex;
  align-items: center;
  gap: 6px;
  min-width: 0;
}
.cell-name strong {
  flex: 1 1 auto;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: 13px;
}
.cell-name .badge {
  flex: 0 0 auto;
}
.cell-status {
  display: flex;
  align-items: center;
  gap: 6px;
  min-width: 0;
}
.col-actions {
  text-align: right;
  vertical-align: middle;
}
.action-stack {
  display: inline-flex;
  flex-direction: row;
  align-items: center;
  justify-content: flex-end;
  gap: 4px;
}
.icon-btn {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 25px;
  height: 25px;
  padding: 0;
  border: 1px solid var(--border-base);
  border-radius: 6px;
  background: var(--surface-2);
  color: var(--text-secondary);
  cursor: pointer;
}
.icon-btn:hover {
  border-color: var(--primary);
  color: var(--primary);
}
.icon-btn.warning {
  border-color: var(--warning);
  color: var(--warning);
}
.icon-btn.danger:hover {
  border-color: var(--danger);
  color: var(--danger);
}
@media (max-width: 900px) {
  .provider-toolbar {
    flex-wrap: wrap;
  }
  .toolbar-controls {
    width: 100%;
    max-width: none;
  }
}
.pin-btn {
  border: 0;
  background: transparent;
  color: var(--text-dim);
  cursor: pointer;
  padding: 2px;
  transition: color 0.15s;
  flex-shrink: 0;
}
.pin-btn.pinned {
  color: var(--warning);
}
/* 启用/禁用开关 */
.toggle-switch {
  position: relative;
  display: inline-flex;
  align-items: center;
  width: 36px;
  height: 20px;
  flex-shrink: 0;
  cursor: pointer;
}
.toggle-switch input {
  opacity: 0;
  width: 0;
  height: 0;
  position: absolute;
}
.toggle-switch .toggle-slider {
  position: absolute;
  inset: 0;
  background: var(--border-strong, #6b7280);
  border-radius: 20px;
  transition: background 0.2s;
}
.toggle-switch .toggle-slider::before {
  content: '';
  position: absolute;
  width: 14px;
  height: 14px;
  left: 3px;
  top: 3px;
  background: #fff;
  border-radius: 50%;
  transition: transform 0.2s;
}
.toggle-switch input:checked + .toggle-slider {
  background: var(--primary, #5b8dff);
}
.toggle-switch input:checked + .toggle-slider::before {
  transform: translateX(16px);
}
.toggle-switch.compact {
  width: 30px;
  height: 17px;
  flex-shrink: 0;
}
.toggle-switch.compact input:checked + .toggle-slider::before {
  transform: translateX(13px);
}
.provider-table .toggle-switch {
  width: 34px !important;
  height: 19px !important;
}
.provider-table .toggle-switch input[type='checkbox'] {
  width: 100% !important;
  height: 100% !important;
  min-height: 0 !important;
  margin: 0 !important;
  padding: 0 !important;
}
.provider-table .toggle-switch .toggle-slider::before {
  width: 13px !important;
  height: 13px !important;
  left: 3px !important;
  top: 3px !important;
  margin: 0 !important;
}
.provider-table .toggle-switch input[type='checkbox']:checked + .toggle-slider::before {
  transform: translateX(15px) !important;
}
.toggle-switch input:disabled + .toggle-slider {
  opacity: 0.5;
  cursor: not-allowed;
}
/* 目录网格 */
.catalog-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(320px, 1fr));
  gap: var(--space-3);
}
.catalog-card {
  display: flex;
  gap: var(--space-3);
  border: 1px solid var(--border-base);
  background: var(--surface-2);
  border-radius: var(--radius-lg);
  padding: var(--space-3);
  transition: all 0.15s;
}
.catalog-card:hover {
  border-color: var(--border-medium);
}
.catalog-icon {
  width: 40px;
  height: 40px;
  min-width: 40px;
  border-radius: var(--radius-md);
  color: #fff;
  display: flex;
  align-items: center;
  justify-content: center;
  font-weight: 700;
  font-size: var(--text-lg);
}
.catalog-body {
  min-width: 0;
  flex: 1;
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.catalog-title {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: var(--space-2);
}
.catalog-title strong {
  font-size: var(--text-base);
  color: var(--text-primary);
}
.catalog-id,
.catalog-url {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.catalog-actions {
  display: flex;
  gap: var(--space-2);
  margin-top: 4px;
}

/* 模态框内容 */
.modal-tabs {
  display: flex;
  border-bottom: 1px solid var(--border-base);
  margin-bottom: var(--space-4);
  gap: var(--space-1);
}
.modal-tab {
  padding: var(--space-2) var(--space-4);
  border: 0;
  background: transparent;
  color: var(--text-muted);
  border-bottom: 2px solid transparent;
  cursor: pointer;
  transition: all 0.15s;
  display: flex;
  align-items: center;
  gap: var(--space-2);
}
.modal-tab.active {
  color: var(--primary);
  border-bottom-color: var(--primary);
}
.modal-tab:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

/* 密钥列表 */
.key-list {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
  margin-bottom: var(--space-4);
}
.key-item {
  border: 1px solid var(--border-base);
  border-radius: var(--radius-md);
  padding: var(--space-3);
  background: var(--surface-1);
}
.key-info {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  flex-wrap: wrap;
  margin-bottom: var(--space-2);
}
.key-actions {
  display: flex;
  gap: var(--space-2);
}
.key-revealed {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  margin-top: var(--space-2);
  padding: var(--space-2);
  background: var(--surface-3);
  border-radius: var(--radius-sm);
}
.key-revealed code {
  flex: 1;
  word-break: break-all;
}
.add-key-form {
  border: 1px dashed var(--border-medium);
  border-radius: var(--radius-md);
  padding: var(--space-4);
  background: var(--surface-1);
}
.add-key-form h4 {
  margin: 0 0 var(--space-3);
  color: var(--text-primary);
}

/* 导入结果 */
.import-result {
  margin-top: var(--space-4);
  padding-top: var(--space-4);
  border-top: 1px solid var(--border-base);
}
.import-details {
  margin-top: var(--space-3);
}
.import-details summary {
  cursor: pointer;
  padding: var(--space-2);
  border-radius: var(--radius-sm);
  transition: background 0.15s;
}
.import-details summary:hover {
  background: var(--surface-2);
}
.import-detail-list {
  margin-top: var(--space-2);
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
}
.import-detail-item {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  padding: var(--space-2);
  background: var(--surface-1);
  border-radius: var(--radius-sm);
  flex-wrap: wrap;
}

/* 服务商选择列表 */
.provider-checklist {
  max-height: 240px;
  overflow-y: auto;
  border: 1px solid var(--border-base);
  border-radius: var(--radius-md);
  padding: var(--space-2);
  background: var(--surface-1);
  display: flex;
  flex-direction: column;
  gap: var(--space-1);
}

/* 备份/恢复 */
.btn-sep {
  width: 1px;
  align-self: stretch;
  margin: 0 var(--space-1);
  background: var(--border-base);
}
.restore-checks {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(120px, 1fr));
  gap: var(--space-1);
  border: 1px solid var(--border-base);
  border-radius: var(--radius-md);
  padding: var(--space-2);
  background: var(--surface-1);
}

/* 表单 */
.input-with-btn {
  position: relative;
  display: flex;
  align-items: center;
}
.input-with-btn input {
  flex: 1;
  padding-right: 40px;
}
.input-with-btn button {
  position: absolute;
  right: 6px;
}
.radio-group {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
}
.radio-label,
.checkbox-label {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  cursor: pointer;
  padding: var(--space-2);
  border-radius: var(--radius-sm);
  transition: background 0.15s;
}
.radio-label:hover,
.checkbox-label:hover {
  background: var(--surface-2);
}
.radio-label input,
.checkbox-label input {
  width: auto;
  margin: 0;
  cursor: pointer;
}
.form-row {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: var(--space-3);
}

.btn-warning {
  background: var(--warning);
  border-color: var(--warning);
  color: var(--surface-1);
}
.btn-warning:hover {
  filter: brightness(1.1);
}

@media (max-width: 900px) {
  .catalog-grid {
    grid-template-columns: 1fr;
  }
  .form-row {
    grid-template-columns: 1fr;
  }
}
</style>
