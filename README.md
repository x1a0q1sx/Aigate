# AIGate — 智能 LLM 聚合网关

一键部署多模型智能路由网关。接入 OpenAI、DeepSeek、智谱、Anthropic 等十余个大模型服务商，统一对外暴露 **OpenAI 兼容接口**；额外提供 **Anthropic 格式兼容（`/v1/messages`）**、**组合路由（Combo）**、**级联回退（fallback）**、**健康探测与冷却**、**用量分析**、**日志归档**、**管理面板登录认证**、**多模态（图像/视频）** 等能力。

---

## 为什么需要 AIGate？

如果你同时使用多个大模型服务商，大概率遇到过这些问题：

- 某个服务商突然限流或挂掉，你的应用跟着崩
- 换模型要改代码、改 Key、改 Base URL，来回折腾
- 免费额度分散在各处，不知道该怎么充分利用
- 不同客户端（OpenAI SDK / Claude Code / Codex）要的格式还不一样

AIGate 把这些问题全部封装了起来。你只需记住一个 API 地址和一个 Key，剩下的路由、切换、回退、格式转换全都自动完成。

---

## 核心功能

### 🎯 智能路由（Auto 模式）

把请求模型设为 `auto`，AIGate 自动从所有可用模型中选出最优的：

| 维度 | 说明 |
|------|------|
| 速度 | 实时监控每个模型的延迟，慢的自动降权 |
| 智力 | 接入 Arena AI 排行榜 ELO 评分，笨的不选 |
| 稳定性 | 统计成功率，频繁出错的自动排除 |

三维修正 + 人工权重干预，综合打分排序。失败自动回退到第二名、第三名……最多重试 `max_fallbacks` 次（默认 5 次）。

**失败冷却（指数退避）**：模型连续失败会被冷却，冷却时长按 `30 × 2^(失败次数-1)` 秒递增，上限 3600 秒（1 小时）。某次调用成功后失败计数清零，立即解除冷却。
- 冷却状态已**持久化到数据库**（`models.auto_cooldown_until` / `models.auto_fail_count`），网关重启后继续计时，不会重置。
- 健康页（Health）提供「🧹 一键清除模型冷却」按钮，可整体或针对单个模型立即解除冷却。

**免费模型不再自动开启 auto**：刷新模型列表时，免费模型（`is_free`）不再自动加入 auto 路由候选，避免被免费额度挤占；需要哪个手动在「模型」页开启即可。

### 🧩 组合路由（Combo）

把多个模型组合成一个虚拟名，请求时用 `combo:组合名` 调用，按策略在候选模型间顺序兜底或轮询：

| 策略 | 说明 |
|------|------|
| `fallback` | 顺序兜底：第一个失败 → 自动切下一个，直到成功 |
| `round_robin` | 轮询：每次请求轮到下一个候选，均匀分摊流量 |
| `fusion` | 扇出合并（预留，暂未实现） |

- 候选模型的**顺序即 fallback / 轮询顺序**。
- 编辑组合时支持**拖拽排序**：按住每行左侧的 `⠿` 手柄拖动调整顺序，也可用 ↑/↓ 按钮辅助。
- **失效模型自动剔除**：刷新模型列表后会扫描所有组合，把「上游已删除 / 服务商不存在」的脏候选从组合里永久移除并落库，界面立刻干净（只删「真没了」的候选；手动禁用的模型仅跳过、不删）。

### 🌐 多服务商管理

首次启动（空数据库）仅内置 **3 个开箱即用的渠道**，其余均按需手动添加：

`MiMo`（免费层 / free_tier）· `OpenCode`（免费层 / free_tier）· `AtomCode`（AtomGit 签名反代 / atomcode）

- 支持自定义添加任何兼容 OpenAI 格式的服务商。
- 每个服务商可绑定**多把 API Key**，由密钥轮询器（KeyRotator）自动分配。
- `MiMo` / `OpenCode` 为免密钥免费层，走专属 Free Provider executor；`AtomCode` 走 AtomCode 守护进程签名代理。
- 添加服务商目录默认只提供 **API Key** 类（OpenAI 兼容等）一键添加；Free Tier / OAuth 类接入已从添加界面移除。

> 实际接入的第三方服务商均为按需手动添加，不计入内置模板。

### 🔑 密钥集成管理与多密钥轮换

- 密钥直接在服务商编辑弹窗中管理，不用跳到独立页面。支持添加 / 删除 / 查看明文 / 一键复制 / 明文掩码切换。
- 所有 Key 使用 **Fernet 对称加密**存储，从不落盘明文。
- **多密钥轮换（KeyRotator）**：per-provider 内存游标 round-robin 分配；遇到 `401/403` 永久禁用该 key；连续失败 ≥3 次进入 60 秒冷却；成功后清空计数。状态保存在内存，重启归零。

### 🔄 多格式兼容（OpenAI + Anthropic）

除标准 OpenAI 接口（`/v1/chat/completions`）外，还提供 **Anthropic 格式翻译层**：

- 端点：`POST /v1/messages`
- 双向转换：Anthropic 请求 ↔ 内部 OpenAI 表示，复用现有 chat 路由核心，不重复路由逻辑。
- 支持：`system` prompt、`temperature` / `top_p` / `max_tokens` / `stop_sequences`、`usage` / `stop_reason` 映射。
- 支持 **tools / tool_use** 跨格式转换（assistant `tool_use` block ↔ OpenAI `tool_calls`，user `tool_result` ↔ OpenAI tool message）。
- 流式：把 OpenAI 的 `data: {...}` SSE 转换为 Anthropic 的 `event: <type>\ndata: {...}` 事件流。
- 鉴权兼容 `x-api-key` 和 `Authorization: Bearer`。
- 已打通 Claude Code 类重度依赖 `tool_use` 的客户端。

### ⚛️ 多模态适配器

- **图像生成**：`image_adapter.py`，对接图像生成服务商。
- **视频生成**：`video_adapter.py`，对接视频生成服务商（含超时与轮询拉取）。
- **AtomCode 上游适配器**：`atomcode_adapter.py` + `atomcode_daemon.py`。由于上游 `llm-api.atomgit.com` 的签名无法在网关内复现，AIGate 以**守护进程（daemon）模式自管本地 atomcode 可执行文件**作为签名代理：按需拉起本地 exe（默认端口 13456），复用已运行实例，请求经 daemon 签名后转发上游；daemon 异常退出后下一次请求自动重新拉起。

### ❤️ 健康探测

定时 ping 每个模型，自动标记：

- ✅ 健康 — 响应快，正常服务
- ⚠️ 延迟 — 响应慢但仍可用
- ⏸️ 限流 — 被服务商限速
- ❌ 故障 — 完全不可用

Auto 路由自动避让不健康的模型。探测间隔和延迟阈值可在仪表盘实时调整。模型失败触发冷却（见 Auto 路由），冷却持久化且可一键清除。

### 📊 请求日志 & 分析面板

每次 API 调用完整记录：请求模型、路由目标、延迟、Token 用量、成功/失败、回退链路、请求诊断（中文化日志：`[请求诊断]` / `[组合流式]` / `[组合路由]` / `[健康检查]`）。

分析面板提供：
- 总请求数、成功率、Auto/直连比例
- Token 用量趋势
- 平均延迟
- 模型健康分布

点击每条日志可查看完整的请求内容和返回内容，支持 JSON 格式化展示。

日志体采用**消息级内容寻址去重 + gzip 压缩**存储（核心 `server/core/request_logger.py`，blob 表 `log_msg_blobs`）：跨请求重复的 system prompt、固定对话历史等只存一份，实测主库占用可由数百 MB 降至约 9MB，去重率约 99%。

### 📦 日志归档（每日自动）

每天凌晨 2 点自动将昨日日志打包为 gzip 压缩文件，存储到 `data/archives/`。分析页底部有归档管理面板：

| 操作 | 说明 |
|------|------|
| 手动归档 | 一键打包当前全部日志 |
| 恢复 | 将某天归档解压并重新导入数据库 |
| 删除 | 永久删除归档文件 |
| 清空 | 清空当前所有日志并 VACUUM 回收空间 |

归档格式为 gzip 压缩的 NDJSON，第 1 行为 `_meta`，后续每行一条 JSON 记录；7000+ 条日志打包后仅几十 KB。

### 🎮 内置 Playground

管理后台自带对话测试面板。选模型、发消息、看返回，无需任何外部工具。用来验证 Key 配置、测试模型效果非常方便。

### 🚦 速率限制

支持按每个 API Key 设置 RPM（每分钟请求数）和 TPM（每分钟 Token 数），防止应用刷爆额度。

### 🛡️ 并发安全

SQLite 数据库自动启用 WAL 模式，写操作不阻塞读，多应用并发调用互不影响。

### 🔐 管理面板登录认证

管理面板（`/admin/*`）受用户名/密码保护，避免裸奔：

- 基于 **bcrypt 密码哈希 + 内存 session token**（`Authorization: Bearer` 头）。
- 首次启动自动生成默认账号：**用户名 `admin`，密码 `aigate123`**（哈希写入 `config.yaml`）。
- 通过 `PUT /admin/api/auth/password` 修改密码，会清除所有 session 强制重新登录。
- `/v1/*` 业务接口**保持开放**，仅用 `aigate_api_key` 鉴权（与登录无关）。
- 前端 SPA 带路由守卫，401 自动跳登录页。

---

## 快速开始

### 环境要求

- Python 3.10+
- Node.js 18+（仅构建前端时需要，已含预构建文件）

### 安装与启动

```bash
# 1. 安装 Python 依赖
pip install -r requirements.txt

# 2. 启动服务
python start.py
```

首次启动自动生成加密密钥、AIGate 访问密钥、SQLite 数据库，并创建 3 个内置渠道（MiMo / OpenCode / AtomCode）及其已知模型（mimo-auto、OpenCode 常用模型、AtomCode 常用模型）。

打开浏览器访问 `http://localhost:8000/admin` 进入管理面板（首次需用 `admin` / `aigate123` 登录）。

### 升级到新版本

**不要重新下载整个项目，也不需要导出导入数据。** 在项目目录直接执行：

```bash
python scripts/update.py          # Windows 也可双击 update.cmd
```

脚本会自动：备份数据库 → `git pull` 增量拉取（只下载变化的文件）→ 依赖变了才装依赖、前端变了才重建 → 重启 PM2。

`data/aigate.db`（服务商 / 模型 / 密钥 / 日志）、`config.yaml`（加密密钥 / 代理 / 密码）、`data/archives/`（日志归档）全程不受影响——它们都在 `.gitignore` 里，git 既不会上传也不会覆盖。

常用参数：

| 参数 | 说明 |
| --- | --- |
| `--check` | 只预览有哪些更新，不实际执行 |
| `--stash` | 有本地未提交改动时自动暂存 |
| `--no-restart` | 更新后不自动重启服务 |
| `--rebuild` | 强制重建前端 |

数据库备份保存在 `data/backups/`，自动保留最近 5 份。

### 构建前端（可选）

Releases 压缩包已内置构建好的 `client/dist/`，开箱即用。从源码 clone 的用户需要构建一次：

```bash
cd client
npm install
npm run build
```

### 运行测试（可选）

`tests/` 目录包含核心逻辑的单元测试（适配器、SSE 格式化、定价解析、冷却、Auto 路由粘性排除等）。运行需要：

```bash
pip install pytest pytest-asyncio
python -m pytest tests/ -q
```

---

## 使用方式

### 1. 配置服务商

进入「服务商」页面，点击任意服务商的「编辑」：

- 在「基本信息」Tab 确认 Base URL 正确
- 切换到「密钥管理」Tab，添加你的 API Key（支持多把）

也可点击「+ 添加服务商」接入新的服务商。

### 2. 刷新模型列表

进入「模型」页面，点击「刷新模型」从已配 Key 的服务商拉取可用模型列表。确认需要参与 Auto 路由的模型已开启 `auto` 开关。

### 3. 调用 API（OpenAI 格式）

完全兼容 OpenAI SDK，只需改 `base_url` 和 `api_key`：

```python
from openai import OpenAI

client = OpenAI(
    base_url="http://localhost:8000/v1",
    api_key="你的AIGate密钥"   # 仪表盘页面顶部可查看和复制
)

response = client.chat.completions.create(
    model="auto",              # 自动选最优模型
    messages=[{"role": "user", "content": "你好"}]
)
print(response.choices[0].message.content)
```

也可直接指定模型：`model="gpt-4o"`、`model="deepseek-chat"` 等，AIGate 会精确路由到对应服务商。

### 4. 调用组合路由

```python
response = client.chat.completions.create(
    model="combo:我的组合名",   # 走组合路由（fallback / 轮询）
    messages=[{"role": "user", "content": "你好"}]
)
```

### 5. 调用 Anthropic 格式接口

```bash
curl http://localhost:8000/v1/messages \
  -H "x-api-key: 你的AIGate密钥" \
  -H "anthropic-version: 2023-06-01" \
  -H "content-type: application/json" \
  -d '{
    "model": "auto",
    "max_tokens": 1024,
    "messages": [{"role": "user", "content": "你好"}]
  }'
```

### 6. 在 Playground 中测试

管理面板的 Playground 页面可直接对话测试，验证配置是否正确。

---

## 配置说明

配置文件 `config.yaml`，首次启动自动生成：

```yaml
server:
  host: 0.0.0.0
  port: 8000

security:
  aigate_api_key: ak-xxxx          # AIGate 访问密钥（首次自动生成，业务接口 /v1/* 鉴权用）
  encryption_key: xxxx              # 加密上游 Key 的密钥（首次自动生成）

auth:
  enabled: true                     # 是否开启管理面板登录认证
  username: admin                   # 登录用户名
  password_hash: "xxxx"             # bcrypt 哈希（首次启动用默认密码 aigate123 生成）
  session_timeout_hours: 24         # 登录会话有效期

auto_router:
  max_fallbacks: 5                  # 失败回退最多尝试次数
  cooling_period_seconds: 30        # 冷却基准时长（实际按 30×2^(n-1) 指数退避，上限 3600s）
  session_sticky_minutes: 30        # 会话粘性（同一会话优先用同一模型）

health_check:
  interval_minutes: 10              # 健康探测间隔
  healthy_latency_threshold_ms: 2000

log_archive:
  enabled: true                     # 是否启用每日自动归档
  archive_dir: ./data/archives      # 归档存储目录

adapters:
  openai_compat:
    reasoning: passthrough          # 思考流(reasoning_content)处理：passthrough=保留并前置展示；drop=丢弃，仅返回最终回答
    content_chunk_size: 24          # 流式 content 合并块大小(字符)，消除上游逐字符碎片，提升客户端渲染体验

rate_limit:
  default_rpm: 60                   # 默认每分钟请求上限
  default_tpm: 100000               # 默认每分钟 Token 上限
```

---

## 管理面板

| 页面 | 功能 |
|------|------|
| 仪表盘 | 服务商/密钥/模型数量、Auto 排名第一、健康分布、AIGate 连接密钥 |
| 服务商 | 添加/编辑/删除服务商，导入定价，管理 API Key（多密钥轮换） |
| 模型 | 启用/禁用、手动测速、置顶/降权/排除、刷新模型列表 |
| 健康 | 各模型延迟与健康状态实时展示、模型冷却状态、一键清除冷却 |
| 组合 | 创建/编辑/删除组合路由，拖拽排序候选模型，选择 fallback / 轮询策略 |
| Auto | 路由权重调整、模型固定/取消固定、手动冷却 |
| 分析 | 请求量/成功率/Token 统计、日志详情、归档管理 |
| Playground | 内置对话测试面板 |
| 登录 | 管理面板登录（默认 admin / aigate123） |

---

---

## 项目结构

```
aigate/
├── config.example.yaml      # 配置模板（首次使用复制为 config.yaml）
├── config.yaml              # 主配置文件，含密钥，已在 .gitignore 中，不会上传
├── start.py                 # 一键启动
├── update.cmd               # Windows 双击即可增量更新
├── requirements.txt         # Python 依赖
├── mobile_agent.py          # （实验性）手机屏幕 Agent 演示，与网关核心无关
│
├── .github/workflows/       # CI：推送校验 + 打 tag 自动发版
├── .githooks/pre-commit     # 提交防护：拦截数据库 / 密钥误提交
├── scripts/update.py        # 一键增量更新
│
├── server/                  # Python 后端
│   ├── main.py              # FastAPI 入口 + 内置服务商模板 + 生命周期
│   ├── config.py            # 配置管理（含 AuthConfig / AdaptersConfig）
│   ├── db.py                # 数据库引擎（WAL 模式）
│   ├── api/                 # API 路由
│   │   ├── v1_router.py     # OpenAI 兼容接口 + 级联回退 + 流式
│   │   ├── admin_router.py  # 管理面板 CRUD + 模型刷新（含组合失效清理）
│   │   ├── admin_routing.py # 排名/分析/日志/归档
│   │   ├── auth_router.py   # 登录/登出/改密
│   │   ├── anthropic_router.py  # Anthropic 格式 /v1/messages
│   │   └── combos_router.py # 组合路由 CRUD
│   ├── core/                # 核心逻辑
│   │   ├── auto_router.py   # 智能路由引擎（含粘性、冷却）
│   │   ├── ranking_service.py   # 三维评分排序
│   │   ├── health_checker.py    # 健康探测调度 + 冷却持久化
│   │   ├── key_rotator.py       # 多密钥轮询
│   │   ├── key_manager.py       # 密钥加解密
│   │   ├── crypto_service.py    # Fernet 加密
│   │   ├── request_logger.py    # 请求日志记录
│   │   ├── intelligence_sync.py # Arena 智力分同步
│   │   ├── combo_router.py      # 组合解析 + 失效候选剔除
│   │   └── auth.py              # bcrypt 认证 + 会话中间件
│   ├── models/              # ORM 模型
│   ├── adapters/            # LLM 适配器
│   │   ├── base_adapter.py      # 适配器基类
│   │   ├── openai_compat.py     # OpenAI 兼容
│   │   ├── anthropic_adapter.py # Anthropic 模型/转换
│   │   ├── atomcode_adapter.py + atomcode_daemon.py  # AtomCode 守护进程签名代理
│   │   ├── codex_responses.py   # Codex Responses API
│   │   ├── github_adapter.py    # GitHub Models
│   │   ├── image_adapter.py     # 图像生成
│   │   ├── video_adapter.py     # 视频生成
│   │   └── xyusec_pricing.py    # 定价解析
│   └── schemas/             # 请求/响应 Schema
│
├── client/                  # Vue 3 前端
│   ├── dist/                # 预构建的静态文件
│   └── src/
│       ├── views/           # 页面组件（含 Combos.vue 拖拽排序）
│       ├── components/      # 通用组件（含 NavBar 退出登录）
│       └── api.js           # API 调用封装（带 Bearer token + 401 跳登录）
│
├── tests/                   # 单元测试（pytest）
│   └── test_runtime_fixes.py
│
└── data/                    # 运行时数据（不入 git）
    ├── aigate.db            # SQLite 数据库
    └── archives/            # 日志归档目录
```

---

## 安全说明

- `security.aigate_api_key` 和 `security.encryption_key` 首次启动自动生成，请妥善保管
- 所有上游 API Key 使用 Fernet（AES-128-CBC + HMAC）加密存储
- 管理面板已启用登录认证（bcrypt + session），默认 `admin` / `aigate123`，请尽快修改密码

---

## 近期重要变更

- **组合路由（Combo）**：新增 `combo:名称` 调用方式，支持 `fallback` / `round_robin` 策略；编辑弹窗候选模型支持**拖拽排序**（🧷 手柄）与 ↑/↓ 按钮；刷新模型后自动剔除已失效上游模型的脏候选。
- **OpenAI 兼容适配器流式合并（openai_compat）**：上游（如 grok-4.5）返回的逐字符 `content` 与思考流 `reasoning_content` 现自动缓冲合并为大块并前置 flush，彻底解决客户端把思考流渲染成"子弹列表 / 串行空格"乱码的问题。新增 `adapters.openai_compat` 配置：`reasoning: passthrough|drop`、`content_chunk_size`（详见配置说明）。
- **Codex Responses API 工具调用翻译（codex_responses）**：`api_type=codex_responses`（OpenAI Responses API 风格上游）现在会把上游 `function_call` 正确翻译为 OpenAI 的 `tool_calls` 增量流，并在有工具调用时输出 `finish_reason: tool_calls`；非流式同样聚合 `tool_calls`。修复了"模型返回工具调用 JSON 但客户端不执行、直接停止"的问题。
- **Auto 路由并发崩溃修复（auto_router）**：修复 WAL 并发下 `rate_limiter` 触发 `rollback` 导致候选 ORM 对象过期、随后访问 `candidate.id` 抛 `MissingGreenlet` 的偶发 500；现于候选遍历循环顶部 greenlet-safe 刷新过期对象。
- **请求日志存储优化（request_logger）**：日志体改为**消息级内容寻址去重 + gzip 压缩**（`log_msg_blobs` 表），跨请求重复上下文只存一份，实测主库 739MB → ~9MB；含回填脚本与 WAL 回收（`wal_checkpoint(TRUNCATE)`）。
- **Anthropic 格式兼容**：新增 `POST /v1/messages`，双向转换 OpenAI ↔ Anthropic，支持 tools/tool_use、system、temperature/top_p/max_tokens/stop_sequences，流式转为 Anthropic SSE 事件；已打通 Claude Code 类客户端。
- **刷新模型明细弹窗**：刷新模型（全部 / 单个服务商）后，提示从原生 `alert` 升级为自定义弹窗——展示新增/更新/删除/总计汇总，并提供**可点击展开的下拉按钮**，按服务商分组列出本次新增与删除的具体模型（名称 + model_id）；删除明细仅在 `model_refresh.remove_missing_models=true` 时出现。后端 `refresh_models` 现返回 `added_details` / `removed_details`，`ModelsRefreshResponse` 新增 `removed` / `added_details` / `removed_details` 字段。
- **AtomCode 适配器**：以守护进程模式自管本地 atomcode exe 作签名代理，打通上游 `llm-api.atomgit.com`。
- **Auto 冷却持久化**：冷却时长改为 `30 × 2^(n-1)` 秒指数退避（上限 3600s），写入数据库 `models.auto_cooldown_until / auto_fail_count`，重启继续计时；健康页支持一键清除冷却。
- **免费模型不再自动开 auto**：刷新模型时 `is_free` 模型不再自动加入 auto 候选。
- **管理面板登录认证**：新增 bcrypt + session 登录（默认 admin/aigate123），`/admin/*` 受保护，`/v1/*` 仍用 `aigate_api_key` 开放。
- **流式级联回退修复**：修复 `aclose()` / `MissingGreenlet` 异常，回退链路稳定穿过多候选；上游不返回 usage 时按字符数粗估 token 展示。
- **多密钥轮换**：KeyRotator 支持 per-provider round-robin、401/403 永久禁用、连续失败 3 次进 60s 冷却。
- **诊断日志中文化**：请求/组合/健康相关日志改为中文标签，便于排查。

---

## 社区支持

学技术，了解 AI，上 L 站：[LinuxDO](https://linux.do)

---

## License

MIT
