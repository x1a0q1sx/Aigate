# AIGate — 智能 LLM 聚合网关

一键部署多模型智能路由网关。接入 OpenAI、DeepSeek、通义千问、智谱、Groq 等十余个大模型服务商，统一 OpenAI 兼容接口对外暴露。支持自动选最优模型、级联回退、健康探测、用量分析、日志归档。

---

## 为什么需要 AIGate？

如果你同时使用多个大模型服务商，大概率遇到过这些问题：

- 某个服务商突然限流或挂掉，你的应用跟着崩
- 换模型要改代码、改 Key、改 Base URL，来回折腾
- 免费额度分散在各处，不知道该怎么充分利用

AIGate 把这些问题全部封装了起来。你只需记住一个 API 地址和一个 Key，剩下的路由、切换、回退全都自动完成。

---

## 核心功能

### 🎯 智能路由（Auto 模式）

把请求模型设为 `auto`，AIGate 自动从所有可用模型中选出最优的：

| 维度 | 说明 |
|------|------|
| 速度 | 实时监控每个模型的延迟，慢的自动降权 |
| 智力 | 接入 Arena AI 排行榜 ELO 评分，笨的不选 |
| 稳定性 | 统计成功率，频繁出错的自动排除 |

三维修正 + 人工权重干预，综合打分排序。失败自动回退到第二名、第三名……最多重试 5 次。

### 🌐 多服务商管理

首次启动内置 11 个服务商模板：OpenAI / DeepSeek / Groq / 通义千问 / 智谱AI / Moonshot / SiliconFlow / Together / Fireworks / Anthropic / GitHub Models。

支持自定义添加任何兼容 OpenAI 格式的服务商。每个服务商可绑定多把 API Key。

### 🔑 密钥集成管理

密钥直接在服务商编辑弹窗中管理，不用跳到独立页面。支持：

- 添加 / 删除 API Key
- 查看明文密钥（解密后显示）
- 一键复制到剪贴板
- 密码输入框切换明文/掩码

所有 Key 使用 Fernet 对称加密存储，从不落盘明文。

### ❤️ 健康探测

定时 ping 每个模型，自动标记：

- ✅ 健康 — 响应快，正常服务
- ⚠️ 延迟 — 响应慢但仍可用
- ⏸️ 限流 — 被服务商限速
- ❌ 故障 — 完全不可用

Auto 路由自动避让不健康的模型。探测间隔和延迟阈值可在仪表盘实时调整。

### 📊 请求日志 & 分析面板

每次 API 调用完整记录：请求模型、路由目标、延迟、Token 用量、成功/失败、回退链路。

分析面板提供：
- 总请求数、成功率、Auto/直连比例
- Token 用量趋势
- 平均延迟
- 模型健康分布

点击每条日志可查看完整的请求内容和返回内容，支持 JSON 格式化展示。

### 📦 日志归档（每日自动）

每天凌晨 2 点自动将昨日日志打包为 gzip 压缩文件，存储到 `data/archives/`。分析页底部有归档管理面板：

| 操作 | 说明 |
|------|------|
| 手动归档 | 一键打包当前全部日志 |
| 恢复 | 将某天归档解压并重新导入数据库 |
| 删除 | 永久删除归档文件 |
| 清空 | 清空当前所有日志并 VACUUM 回收空间 |

归档格式为 gzip 压缩的 NDJSON，7000+ 条日志打包后仅几十 KB。

### 🎮 内置 Playground

管理后台自带对话测试面板。选模型、发消息、看返回，无需任何外部工具。用来验证 Key 配置、测试模型效果非常方便。

### 🚦 速率限制

支持按每个 API Key 设置 RPM（每分钟请求数）和 TPM（每分钟 Token 数），防止应用刷爆额度。

### 🛡️ 并发安全

SQLite 数据库自动启用 WAL 模式，写操作不阻塞读，多应用并发调用互不影响。

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

首次启动自动生成加密密钥、AIGate 访问密钥、SQLite 数据库，并创建 11 个内置服务商模板。

打开浏览器访问 `http://localhost:8000/admin` 进入管理面板。

### 构建前端（可选）

已含预构建的 `client/dist/`，无需构建即可使用。如需修改前端：

```bash
cd client
npm install
npm run build
```

---

## 使用方式

### 1. 配置服务商

进入「服务商」页面，点击任意服务商的「编辑」：

- 在「基本信息」Tab 确认 Base URL 正确
- 切换到「密钥管理」Tab，添加你的 API Key

也可点击「+ 添加服务商」接入新的服务商。

### 2. 刷新模型列表

进入「模型」页面，点击「刷新模型」从已配 Key 的服务商拉取可用模型列表。确认需要参与 Auto 路由的模型已开启 `auto` 开关。

### 3. 调用 API

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

### 4. 在 Playground 中测试

管理面板的 Playground 页面可直接对话测试，验证配置是否正确。

---

## 配置说明

配置文件 `config.yaml`，首次启动自动生成：

```yaml
server:
  host: 0.0.0.0
  port: 8000

security:
  aigate_api_key: ak-xxxx          # AIGate 访问密钥（首次自动生成）
  encryption_key: xxxx              # 加密上游 Key 的密钥（首次自动生成）

auto_router:
  max_fallbacks: 5                  # 失败回退最多尝试次数
  cooling_period_seconds: 30        # 失败后冷却时间
  session_sticky_minutes: 30        # 会话粘性（同一会话优先用同一模型）

health_check:
  interval_minutes: 10              # 健康探测间隔
  healthy_latency_threshold_ms: 2000

log_archive:
  enabled: true                     # 是否启用每日自动归档
  archive_dir: ./data/archives      # 归档存储目录

rate_limit:
  default_rpm: 60                   # 默认每分钟请求上限
  default_tpm: 100000               # 默认每分钟 Token 上限
```

---

## 管理面板

| 页面 | 功能 |
|------|------|
| 仪表盘 | 服务商/密钥/模型数量、Auto 排名第一、健康分布、AIGate 连接密钥 |
| 服务商 | 添加/编辑/删除服务商，导入定价，管理 API Key |
| 模型 | 启用/禁用、手动测速、置顶/降权/排除、刷新模型列表 |
| 健康 | 各模型延迟与健康状态实时展示 |
| Auto | 路由权重调整、模型固定/取消固定、手动冷却 |
| 分析 | 请求量/成功率/Token 统计、日志详情、归档管理 |
| Playground | 内置对话测试面板 |

---

## 发布前清理

日常使用时会产生用户数据（数据库、日志、归档）。发布到 GitHub 前，将项目文件夹复制一份，在副本中运行清理：

```powershell
# Windows PowerShell（在副本目录中执行）
.\release.ps1 v1.0.0
```

脚本自动完成：清理用户数据 → 构建前端 → git 提交 → 打 tag → 推送，最后自删不留痕迹。

---

## 项目结构

```
aigate/
├── config.yaml              # 主配置文件
├── start.py                 # 一键启动
├── requirements.txt         # Python 依赖
├── clean.py                 # 发布前清理脚本
├── release.ps1              # 自动发布脚本（PowerShell）
├── release.sh               # 自动发布脚本（Bash）
│
├── server/                  # Python 后端
│   ├── main.py              # FastAPI 入口
│   ├── config.py            # 配置管理
│   ├── db.py                # 数据库引擎（WAL 模式）
│   ├── api/                 # API 路由
│   │   ├── v1_router.py     # OpenAI 兼容接口
│   │   ├── admin_router.py  # 管理面板 CRUD
│   │   └── admin_routing.py # 排名/分析/日志/归档
│   ├── core/                # 核心逻辑
│   │   ├── auto_router.py   # 智能路由引擎
│   │   ├── ranking_service.py   # 三维评分排序
│   │   ├── health_checker.py    # 健康探测调度
│   │   ├── key_manager.py       # 密钥加解密
│   │   ├── crypto_service.py    # Fernet 加密
│   │   ├── request_logger.py    # 请求日志记录
│   │   └── intelligence_sync.py # Arena 智力分同步
│   ├── models/              # ORM 模型
│   ├── adapters/            # LLM 适配器（OpenAI/Anthropic）
│   └── schemas/             # 请求/响应 Schema
│
├── client/                  # Vue 3 前端
│   ├── dist/                # 预构建的静态文件
│   └── src/
│       ├── views/           # 页面组件
│       ├── components/      # 通用组件
│       └── api.js           # API 调用封装
│
└── data/                    # 运行时数据（不入 git）
    ├── aigate.db            # SQLite 数据库
    └── archives/            # 日志归档目录
```

---

## 安全说明

- `security.aigate_api_key` 和 `security.encryption_key` 首次启动自动生成，请妥善保管
- 所有上游 API Key 使用 Fernet（AES-128-CBC + HMAC）加密存储
- `config.yaml` 和 `data/` 目录已配置 `.gitignore`，不会被提交
- 建议使用 `release.ps1` 发布，自动清理所有用户数据

---

## License

MIT
