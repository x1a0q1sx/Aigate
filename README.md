<div align="center">

# 🚪 AIGate — 智能 LLM 聚合网关

一站式聚合多家大模型服务商，对外暴露 **统一 OpenAI 兼容接口**，内置智能路由、组合回退、多格式兼容、多模态生成、HTTP 代理池与用量分析。

[![Python](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org)
[![License](https://img.shields.io/badge/license-MIT-green.svg)](#license)
[![Platform](https://img.shields.io/badge/platform-Windows%20%7C%20Linux%20%7C%20macOS-lightgrey.svg)](#)

[🚀 快速开始](#-快速开始) • [💡 核心功能](#-核心功能) • [🔄 工作原理](#-工作原理) • [🛠️ 使用方式](#-使用方式) • [🔀 代理池](#-http-代理池) • [⚙️ 配置说明](#️-配置说明) • [📂 项目结构](#-项目结构)

</div>

---

## 🤔 为什么需要 AIGate？

如果你同时使用多个大模型服务商，大概率踩过这些坑：

| ❌ 没有网关时 | ✅ 用 AIGate 之后 |
|--------------|------------------|
| 某个服务商突然限流 / 挂掉，应用跟着崩 | 自动回退到下一个可用模型，调用不中断 |
| 换模型要改代码、改 Key、改 Base URL | 只记一个地址 + 一个 Key，路由全自动 |
| 免费额度散落各处，不知道怎么用 | Auto 路由优先选免费 / 低延迟模型 |
| 不同客户端要的格式不一样（OpenAI / Claude Code） | 一份网关同时兼容 OpenAI 与 Anthropic 格式 |
| 系统代理开了，网关却没走代理（一脸懵） | 代理走 AIGate 自己的代理池，日志里看得清清楚楚 |

你只需记住 **一个 API 地址、一个 Key**，剩下的路由、切换、回退、格式转换全部自动完成。

---

## 🔄 工作原理

```
        ┌──────────────────────────────────────────────┐
        │    客户端 (OpenAI SDK / Claude Code / Codex / ...)  │
        └───────────────────────┬──────────────────────┘
                                │  base_url = http://<host>:8000/v1
                                ▼
        ┌──────────────────────────────────────────────┐
        │                   AIGate 网关                   │
        │  ┌──────────────────────────────────────────┐  │
        │  │ 路由层: auto 智能选优 / combo 组合 / 直连     │  │
        │  │  · 失败自动回退 (fallback)  · 冷却持久化       │  │
        │  └──────────────────────────────────────────┘  │
        │  ┌──────────────────────────────────────────┐  │
        │  │ 适配器: OpenAI / Anthropic / Codex /        │  │
        │  │  Image / Video / AtomCode / GitHub ...      │  │
        │  └──────────────────────────────────────────┘  │
        │  ┌──────────────────────────────────────────┐  │
        │  │ 密钥轮询 (KeyRotator) · Fernet 加密存储       │  │
        │  └──────────────────────────────────────────┘  │
        └───────────────┬──────────────────────┬─────────┘
                        │                      │
                        ▼                      ▼
        ┌────────────────────────┐   ┌────────────────────────────┐
        │  HTTP 代理池 (可选)      │   │  上游服务商 / 多模态 API        │
        │  SOCKS5 轮询 + 熔断重试    │   │  OpenAI / DeepSeek / 智谱 / ...  │
        └────────────────────────┘   └────────────────────────────┘
```

请求进入网关后：先按 `auto` / `combo:名称` / 具体模型名 解析路由 → 由密钥轮询器选一把 Key → 经对应适配器（含可选的代理池）转发到上游 → 失败按策略回退 → 全程写日志并统计用量。

---

## ⚡ 核心功能

### 🎯 智能路由（Auto 模式）

把请求模型设为 `auto`，AIGate 自动从所有可用模型中选出最优的：

| 维度 | 说明 |
|------|------|
| 速度 | 实时监控每个模型的延迟，慢的自动降权 |
| 智力 | 接入 Arena AI 排行榜 ELO 评分，笨的不选 |
| 稳定性 | 统计成功率，频繁出错的自动排除 |

三维修正 + 人工权重干预，综合打分排序。失败自动回退到第二名、第三名……最多重试 `max_fallbacks` 次（默认 5 次）。

- **失败冷却（指数退避）**：模型连续失败会被冷却，时长按 `30 × 2^(失败次数-1)` 秒递增，上限 3600 秒。调用成功后失败计数清零，立即解除冷却。
- **冷却持久化**：冷却状态写入数据库（`models.auto_cooldown_until` / `auto_fail_count`），网关重启后继续计时。健康页（Health）提供「🧹 一键清除模型冷却」。
- **免费模型不再自动开 auto**：刷新模型列表时，`is_free` 模型不再自动加入 auto 候选，需手动开启。

### 🧩 组合路由（Combo）

把多个模型组合成一个虚拟名，请求时用 `combo:组合名` 调用，按策略在候选模型间顺序兜底或轮询：

| 策略 | 说明 |
|------|------|
| `fallback` | 顺序兜底：第一个失败 → 自动切下一个，直到成功 |
| `round_robin` | 轮询：每次请求轮到下一个候选，均匀分摊流量 |
| `fusion` | 扇出合并（预留，暂未实现） |

- 候选模型的**顺序即 fallback / 轮询顺序**。
- 编辑组合支持**拖拽排序**（每行左侧 `⠿` 手柄）与 ↑/↓ 按钮。
- **失效模型自动剔除**：刷新模型列表后扫描所有组合，把「上游已删除 / 服务商不存在」的脏候选永久移除。

### 🌐 多服务商管理

首次启动（空数据库）仅内置 **3 个开箱即用的渠道**，其余按需手动添加：

`MiMo`（免费层 / free_tier）· `OpenCode`（免费层 / free_tier）· `AtomCode`（AtomGit 签名反代 / atomcode）

- 支持自定义添加任何兼容 OpenAI 格式的服务商。
- 每个服务商可绑定**多把 API Key**，由密钥轮询器（KeyRotator）自动分配。
- `MiMo` / `OpenCode` 走专属 Free Provider executor；`AtomCode` 走守护进程签名代理。

### 🔑 密钥集成管理与多密钥轮换

- 密钥直接在服务商编辑弹窗中管理，支持添加 / 删除 / 查看明文 / 一键复制 / 明文掩码切换。
- 所有 Key 使用 **Fernet 对称加密**存储，从不落盘明文。
- **多密钥轮换（KeyRotator）**：per-provider 内存游标 round-robin 分配；遇 `401/403` 永久禁用该 key；连续失败 ≥3 次进入 60 秒冷却；成功后清空计数。状态保存在内存，重启归零。

### 🔄 多格式兼容（OpenAI + Anthropic）

除标准 OpenAI 接口（`/v1/chat/completions`）外，还提供 **Anthropic 格式翻译层**：

- 端点：`POST /v1/messages`
- 双向转换：Anthropic 请求 ↔ 内部 OpenAI 表示，复用现有 chat 路由核心。
- 支持：`system` prompt、`temperature` / `top_p` / `max_tokens` / `stop_sequences`、`usage` / `stop_reason` 映射。
- 支持 **tools / tool_use** 跨格式转换（assistant `tool_use` block ↔ OpenAI `tool_calls`，user `tool_result` ↔ OpenAI tool message）。
- 流式：OpenAI 的 `data: {...}` SSE 转为 Anthropic 的 `event: <type>\ndata: {...}` 事件流。
- 鉴权兼容 `x-api-key` 和 `Authorization: Bearer`。已打通 Claude Code 类客户端。

### ⚛️ 多模态适配器

- **图像生成**：`image_adapter.py`，对接图像生成服务商（OpenAI Images 协议兼容）。
- **视频生成**：`video_adapter.py`，对接视频生成服务商（同步 / 轮询两套协议，含超时与轮询拉取）。
- **AtomCode 上游适配器**：`atomcode_adapter.py` + `atomcode_daemon.py`。上游 `llm-api.atomgit.com` 的签名无法在网关内复现，AIGate 以**守护进程（daemon）模式自管本地 atomcode 可执行文件**作为签名代理：按需拉起本地 exe（默认端口 13456），复用已运行实例，请求经 daemon 签名后转发上游；daemon 异常退出后下一次请求自动重新拉起。

### 🔀 HTTP 代理池

> ⚠️ **重要**：AIGate **不继承系统 / OS 代理**（你在机器上开的 Clash / V2Ray 系统代理对它无效）。AIGate 只走它自己在 `config.yaml` 里配置的 `proxy_pool`。

- 三种策略：`round_robin` / `weighted` / `random`。
- 所有出网请求（chat / 图像 / 视频）统一经代理池转发；**传输层错误（代理抖动 / 连接中断 / SOCKS 握手失败）自动换代理重试**（最多 3 次）。
- **后台 TCP 端口存活探测**：端口不可达的死代理自动剔除，不再参与轮询。
- **日志如实记录**：每条请求在「分析」页显示 `🟢 代理` / `⚪ 直连`，并展示实际使用的代理地址——媒体生成（图像 / 视频）此前不会记录，现已修复。
- 开启方式：把 `proxy_pool.enabled` 改为 `true` 并填好 `proxies`，或调用 `PUT /admin/api/proxy-pool`（热重载，无需重启）。

```yaml
proxy_pool:
  enabled: true                       # 是否启用代理池（默认 false = 直连）
  strategy: round_robin              # round_robin / weighted / random
  proxies:
    - name: V2
      url: socks5://127.0.0.1:10808
      weight: 1
    - name: Clash
      url: socks5://127.0.0.1:7897
      weight: 1
```

### ❤️ 健康探测

定时 ping 每个模型，自动标记：

- ✅ 健康 — 响应快，正常服务
- ⚠️ 延迟 — 响应慢但仍可用
- ⏸️ 限流 — 被服务商限速
- ❌ 故障 — 完全不可用

Auto 路由自动避让不健康的模型。探测间隔和延迟阈值可在仪表盘实时调整。

### 📊 请求日志 & 分析面板

每次 API 调用完整记录：请求模型、路由目标、延迟、Token 用量、成功/失败、回退链路、是否走代理。

分析面板提供：总请求数、成功率、Auto/直连比例、Token 用量趋势、平均延迟、模型健康分布。点击每条日志可查看完整请求 / 返回内容（JSON 格式化）。

日志体采用**消息级内容寻址去重 + gzip 压缩**存储（`server/core/request_logger.py`，blob 表 `log_msg_blobs`）：跨请求重复的 system prompt、固定对话历史只存一份，实测主库可由数百 MB 降至约 9MB。

### 📦 日志归档（每日自动）

每天凌晨 2 点自动将昨日日志打包为 gzip 压缩文件，存储到 `data/archives/`。分析页底部有归档管理面板：

| 操作 | 说明 |
|------|------|
| 手动归档 | 一键打包当前全部日志 |
| 恢复 | 将某天归档解压并重新导入数据库 |
| 删除 | 永久删除归档文件 |
| 清空 | 清空当前所有日志并 VACUUM 回收空间 |

归档格式为 gzip 压缩的 NDJSON，第 1 行为 `_meta`，后续每行一条 JSON 记录。

### 🎮 内置 Playground

管理后台自带对话测试面板。选模型、发消息、看返回，无需任何外部工具。验证 Key 配置、测试模型效果非常方便。

### 🚦 速率限制

支持按每个 API Key 设置 RPM（每分钟请求数）和 TPM（每分钟 Token 数），防止应用刷爆额度。

### 🔐 管理面板登录认证

管理面板（`/admin/*`）受用户名/密码保护，避免裸奔：

- 基于 **bcrypt 密码哈希 + 内存 session token**（`Authorization: Bearer` 头）。
- 首次启动自动生成默认账号：**用户名 `admin`，密码 `aigate123`**（哈希写入 `config.yaml`）。
- 通过 `PUT /admin/api/auth/password` 修改密码，会清除所有 session 强制重新登录。
- `/v1/*` 业务接口**保持开放**，仅用 `aigate_api_key` 鉴权（与登录无关）。

---

## 🚀 快速开始

### 环境要求

- Python 3.10+
- Node.js 18+（仅构建前端时需要，Releases 已含预构建文件）

### 安装与启动

```bash
# 1. 安装 Python 依赖
pip install -r requirements.txt

# 2. 启动服务
python start.py
```

首次启动自动生成加密密钥、AIGate 访问密钥、SQLite 数据库，并创建 3 个内置渠道（MiMo / OpenCode / AtomCode）及其已知模型。

打开浏览器访问 `http://localhost:8000/admin` 进入管理面板（首次需用 `admin` / `aigate123` 登录）。

### 升级到新版本

**不要重新下载整个项目，也不需要导出导入数据。** 在项目目录直接执行：

```bash
python scripts/update.py          # Windows 也可双击 update.cmd
```

脚本会自动：备份数据库 → `git pull` 增量拉取 → 依赖 / 前端变了才重建 → 重启 PM2。

`data/aigate.db`、`config.yaml`、`data/archives/` 全程不受影响——它们都在 `.gitignore` 里，git 既不会上传也不会覆盖。

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

```bash
pip install pytest pytest-asyncio
python -m pytest tests/ -q
```

---

## 🛠️ 使用方式

### 1. 配置服务商

进入「服务商」页面，点击「编辑」：

- 「基本信息」Tab 确认 Base URL 正确
- 「密钥管理」Tab 添加你的 API Key（支持多把）

也可点击「+ 添加服务商」接入新的服务商。

### 2. 刷新模型列表

进入「模型」页面，点击「刷新模型」从已配 Key 的服务商拉取可用模型列表。确认需参与 Auto 路由的模型已开启 `auto` 开关。

### 3. 调用 API（OpenAI 格式）

完全兼容 OpenAI SDK，只需改 `base_url` 和 `api_key`：

```python
from openai import OpenAI

client = OpenAI(
    base_url="http://localhost:8000/v1",
    api_key="你的AIGate密钥",   # 仪表盘页面顶部可查看和复制
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

## ⚙️ 配置说明

配置文件 `config.yaml`，首次启动自动生成（含密钥，已纳入 `.gitignore`，不会上传）：

<details>
<summary>点击展开完整 config.yaml 示例</summary>

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
  password_hash: "xxxx"             # bcrypt 哈希（首次用默认密码 aigate123 生成）
  session_timeout_hours: 24         # 登录会话有效期

auto_router:
  max_fallbacks: 5                  # 失败回退最多尝试次数
  cooling_period_seconds: 30        # 冷却基准时长（实际按 30×2^(n-1) 指数退避，上限 3600s）
  session_sticky_minutes: 30        # 会话粘性（同一会话优先用同一模型）

proxy_pool:                         # HTTP 代理池（详见「🔀 HTTP 代理池」）
  enabled: false
  strategy: round_robin
  proxies:
    - name: V2
      url: socks5://127.0.0.1:10808
      weight: 1

health_check:
  interval_minutes: 10
  healthy_latency_threshold_ms: 2000

log_archive:
  enabled: true
  archive_dir: ./data/archives

adapters:
  openai_compat:
    reasoning: passthrough          # 思考流处理：passthrough=保留并前置展示；drop=丢弃，仅返回最终回答
    content_chunk_size: 24          # 流式 content 合并块大小(字符)

rate_limit:
  default_rpm: 60
  default_tpm: 100000
```

</details>

---

## 🖥️ 管理面板

| 页面 | 功能 |
|------|------|
| 仪表盘 | 服务商/密钥/模型数量、Auto 排名第一、健康分布、AIGate 连接密钥 |
| 服务商 | 添加/编辑/删除服务商，导入定价，管理 API Key（多密钥轮换） |
| 模型 | 启用/禁用、手动测速、置顶/降权/排除、刷新模型列表 |
| 健康 | 各模型延迟与健康状态实时展示、模型冷却状态、一键清除冷却 |
| 组合 | 创建/编辑/删除组合路由，拖拽排序候选模型，选择 fallback / 轮询策略 |
| Auto | 路由权重调整、模型固定/取消固定、手动冷却 |
| 分析 | 请求量/成功率/Token 统计、日志详情（含代理使用）、归档管理 |
| Playground | 内置对话测试面板 |
| 登录 | 管理面板登录（默认 admin / aigate123） |

---

## 📂 项目结构

<details>
<summary>点击展开目录树</summary>

```
aigate/
├── config.example.yaml      # 配置模板
├── config.yaml              # 主配置文件，含密钥，已在 .gitignore 中，不会上传
├── start.py                 # 一键启动
├── update.cmd               # Windows 双击即可增量更新
├── requirements.txt         # Python 依赖
│
├── .github/workflows/       # CI：推送校验 + 打 tag 自动发版
├── .githooks/pre-commit     # 提交防护：拦截数据库 / 密钥误提交
├── scripts/update.py        # 一键增量更新
│
├── server/                  # Python 后端
│   ├── main.py              # FastAPI 入口 + 内置服务商模板 + 生命周期
│   ├── config.py            # 配置管理
│   ├── db.py                # 数据库引擎（WAL 模式）
│   ├── api/                 # API 路由
│   │   ├── v1_router.py        # OpenAI 兼容接口 + 级联回退 + 流式
│   │   ├── admin_router.py     # 管理面板 CRUD + 模型刷新 + 代理池
│   │   ├── admin_routing.py    # 排名/分析/日志/归档
│   │   ├── auth_router.py      # 登录/登出/改密
│   │   ├── anthropic_router.py # Anthropic 格式 /v1/messages
│   │   ├── media_router.py     # 图像/视频生成入口 + 日志
│   │   └── combos_router.py    # 组合路由 CRUD
│   ├── core/                # 核心逻辑
│   │   ├── auto_router.py      # 智能路由引擎
│   │   ├── ranking_service.py  # 三维评分排序
│   │   ├── health_checker.py   # 健康探测调度 + 冷却持久化
│   │   ├── key_rotator.py      # 多密钥轮询
│   │   ├── key_manager.py      # 密钥加解密
│   │   ├── crypto_service.py   # Fernet 加密
│   │   ├── request_logger.py   # 请求日志记录（消息级去重）
│   │   ├── proxy_pool.py       # HTTP 代理池 + 熔断 + 存活探测
│   │   ├── intelligence_sync.py# Arena 智力分同步
│   │   ├── combo_router.py     # 组合解析 + 失效候选剔除
│   │   └── auth.py             # bcrypt 认证 + 会话中间件
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
│       ├── views/           # 页面组件
│       ├── components/      # 通用组件
│       └── api.js           # API 调用封装
│
├── tests/                   # 单元测试（pytest）
└── data/                    # 运行时数据（不入 git）
    ├── aigate.db            # SQLite 数据库
    └── archives/            # 日志归档目录
```

</details>

---

## 🔒 安全说明

- `security.aigate_api_key` 和 `security.encryption_key` 首次启动自动生成，请妥善保管。
- 所有上游 API Key 使用 Fernet（AES-128-CBC + HMAC）加密存储。
- 管理面板已启用登录认证（bcrypt + session），默认 `admin` / `aigate123`，请尽快修改密码。
- `data/` 与 `config.yaml` 均在 `.gitignore` 中，提交时不会上传；`.githooks/pre-commit` 还会二次拦截。

---

## 📝 近期重要变更

<details>
<summary>点击展开变更记录</summary>

- **媒体（图像/视频）代理使用如实记录**：图像/视频生成请求现在会把「是否走代理 / 实际代理地址」写入日志，分析页的 `🟢 代理 / ⚪ 直连` 标签对媒体请求也准确了（此前媒体日志从不记录，恒显「直连」）。
- **HTTP 代理池**：所有出网请求统一经代理池转发，传输层错误自动换代理重试；后台 TCP 存活探测剔除死代理。**注意 AIGate 不继承系统代理，只走自己的 `proxy_pool` 配置。**
- **组合路由（Combo）**：`combo:名称` 调用，支持 `fallback` / `round_robin`；编辑弹窗候选模型支持拖拽排序，刷新模型后自动剔除失效候选。
- **OpenAI 兼容流式合并**：上游逐字符 `content` 与思考流 `reasoning_content` 自动缓冲合并为大块，解决客户端把思考流渲染成乱码的问题。
- **Codex Responses API 工具调用翻译**：`function_call` 正确翻译为 `tool_calls` 增量流。
- **Auto 路由并发崩溃修复**：修复 WAL 并发下偶发 `MissingGreenlet` 500。
- **请求日志存储优化**：消息级内容寻址去重 + gzip 压缩，主库 739MB → ~9MB。
- **Anthropic 格式兼容**：`POST /v1/messages`，双向转换，已打通 Claude Code 类客户端。
- **刷新模型明细弹窗**：自定义弹窗展示新增/删除模型明细。
- **AtomCode 适配器**：守护进程模式自管本地 atomcode exe 作签名代理。
- **Auto 冷却持久化**：冷却时长指数退避并写入数据库，重启继续计时。
- **多密钥轮换**：KeyRotator 支持 round-robin、401/403 永久禁用、连续失败冷却。

</details>

---

## 📮 社区支持

学技术，了解 AI，上 L 站：[LinuxDO](https://linux.do)

---

## 📄 License

MIT
