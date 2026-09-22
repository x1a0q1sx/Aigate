# OpenCode 免费层：判据取证 + CLI sidecar 接入方案

日期：2026-09-22　仓库：aigate-V2　触发：用户报告 combo 中 OpenCode Free 候选持续 403

## 一、判据取证（结论）

OpenCode 免费层**不是「请求头鉴权」**，而是要求请求确出自一个**官方 CLI 进程建立的会话**。
纯 HTTP 转发（无论怎么伪造头/body/运行时）在当前上游一律 403；9router 的头部方案**已失效**。

### 证据链（12 项实验）

| # | 实验 | 结果 |
|---|---|---|
| 1 | 现状：`Authorization: Bearer public` + `x-opencode-client: desktop` | 403 FreeTierError |
| 2 | 复刻官方 CLI 抓包到的**完整请求**（头 + 32KB body 原样） | 403 |
| 3 | 加 9router 全部头（UA `opencode` + session/request/project） | 403 |
| 4 | 用 bun 运行时（与官方 CLI 同运行时/TLS 栈） | 403 |
| 5 | `curl` HTTP/1.1 与 HTTP/2（不同 TLS 栈） | 403 |
| 6 | 只改官方 session 末位字符 | **200** ✅ |
| 7 | 截短官方 session 4 位 | 403 |
| 8 | 保留官方 session 前 8 位 + 补随机 | **200** ✅ |
| 9 | 全新随机 session（任意头/body 组合） | 403 |
| 10 | 官方 CLI 同机同模型实跑（`opencode run -m opencode/mimo-v2.6-flash-free`） | 200 ✅ |
| 11 | 复用官方 session 连续 5 次请求 | 5×200 ✅ |
| 12 | 官方 session + 极简 body（无 tools / 非流式） | 403 |

**判定**：6/8 证明服务端按 session **前缀**匹配已登记会话（非精确值）；7 证明需要足够长的前缀；
9/10 证明该会话只能由官方 CLI 建立；12 说明会话之外仍有次要约束（tools + stream）。

### 9router 的真实做法（读源码 + 抓包对照）

9router **不伪造头**——它让**本机官方 CLI 当客户端**、自己只做透传：

```js
// open-sse/handlers/chatCore.js:155   把下游客户端请求头存起来
if (credentials) credentials.rawHeaders = clientRawRequest?.headers || {};
// open-sse/executors/opencode.js:97   再原样转发（含下游的 x-opencode-session）
"x-opencode-session": lower["x-opencode-session"] || this._currentSessionId || generateSessionId(),
```

其 UI 卡片也是「配置本机 OpenCode CLI 的 baseURL 指向 9router」。所以「9router 能用」的本质是
**蹭了真 CLI 的会话**；单独把它部署到没有 CLI 的服务器上同样会 403。

## 二、实施：官方 CLI sidecar（已落地）

**架构**（复刻 9router 思路）：

```
AIGate 请求 ──> server/core/opencode_sidecar.py ──HTTP──> opencode serve（常驻官方 CLI）
                                                          └──(真 CLI 会话)──> opencode.ai ✅
```

**部署步骤**：
1. 取官方二进制：`npm pack opencode-linux-x64@<version>`（真实二进制在该可选依赖里，
   `opencode-ai` 主包只是下载器），解包后放到 `$HOME/opencode/bin/opencode`（约 177MB）
2. 常驻：`opencode serve --port 4096 --hostname 127.0.0.1`（仅本机可达；
   可用环境变量 `AIGATE_OPENCODE_SIDECAR` / `AIGATE_OPENCODE_BIN` / `AIGATE_OPENCODE_PORT` 覆盖）
3. AIGate：`free_providers.FreeProviderExecutor` 对 `provider_code == "opencode"` 走 sidecar
   （非流式直取；流式「一次取全 + 切块」，combo/auto 的实质锁定、race、drool guard 无需改动）
4. `main.py` lifespan 起守护任务：120s 探活，崩了自动拉起；未装 CLI 则静默跳过
5. 服务商记录须为 `credential_type='free_tier'` 且 `oauth_code='opencode'`（否则走普通 adapter，
   仍是纯 HTTP 403——这是上线时踩到的第一个坑）

**语义差异与解决（重要）**：CLI 默认 agent 是「编码助手」人格——实测原样转发会回
`Hi! I'm ready to help with your workspace at ...`，与 chat completions 的直出语义不符。
解决：用 CLI 的 agent 配置能力定义**网关专用 agent**（`~/.config/opencode/opencode.json`，
建 session 时用 `agent: "aigate"` 指定）：

```json
{
  "$schema": "https://opencode.ai/config.json",
  "agent": {
    "aigate": {
      "description": "AIGate gateway chat-only agent",
      "mode": "primary",
      "prompt": "You are a direct assistant behind an API gateway. Answer the user's request directly and completely in the same language they used. Do not greet. Do not describe yourself. Do not mention tools, workspaces, or files. Do not ask clarifying questions unless the request is truly impossible to answer."
    }
  }
}
```

实测效果（同一问题「用一句话说明什么是 HTTP 404」）：
- 默认 agent：`Hi! I'm ready to help with your workspace at ...`
- `aigate` agent：`HTTP 404 是"未找到"状态码，表示服务器无法在指定地址上找到请求的资源。` ✅

两个坑：① **禁用全部 tools 会让回复变空**（tools 全 false → 空回复），只保留提示词约束即可；
② agent 名可用环境变量 `AIGATE_OPENCODE_AGENT` 覆盖，未配置时自动回退 CLI 默认 agent
（不致命，只是带人格）。

**实现坑（CLI API 特性）**：assistant 消息是**逐步填充**的——先出现 `time.created` 与空的
reasoning part，之后才补 `content[].text` 并写入 `time.completed`。只判「消息存在」会拿到
空回复，必须等 `time.completed`（或 `finish`）出现才算就绪。

## 三、接口要点（`opencode serve` 的 HTTP API，见 `/doc` OpenAPI）

- `POST /api/session` body `{title, model:{id, providerID}, agent?}` → `{data:{id}}`
- `POST /api/session/{sid}/prompt` body `{prompt:{text, files?}}`
- `GET /api/session/{sid}/message` → `{data:[...]}`
  - user 项：`{"type":"user","text":...,"time":{"created"}}`
  - assistant 项：`{"type":"assistant","agent":...,"model":{...},"content":[{type:"reasoning"|"text",text}],"finish":"stop","tokens":{"input","output"},"time":{"created","completed"}}`
- `DELETE /api/session/{sid}` 清理
- **必须用 `/api` 前缀**；无前缀的 `/session/{id}/prompt` 会落到 Web UI（返回 HTML）

## 四、取证方法（可复用）

服务器 8000 未对本地网络开放，且 CLI 日志不打印请求头。最终有效的组合：

1. **官方二进制**：`npm pack opencode-linux-x64@<version>`（见上）
2. **透明抓包**：`mitmdump -s <addon> --listen-port 18888`，addon 里 `request(flow)` 记录
   `flow.request.headers` / `body`；CLI 侧设 `NODE_EXTRA_CA_CERTS=<mitm ca>` +
   `HTTPS_PROXY=http://127.0.0.1:18888`
3. **A/B 判据定位**：从官方完整请求出发，逐项剥离头/body 字段，观察 403/200 翻转
4. 备注：mitmdump **必须前台 exec**，用 `nohup ... &` 在 SSH 会话里反复启动失败；
   可配 `ssh -f` 或独立终端

抓到的官方请求形态（供参考）：
```
POST https://opencode.ai/zen/v1/chat/completions
Authorization: Bearer public
User-Agent: opencode/1.18.32 ai-sdk/provider-utils/4.0.23 runtime/bun/1.3.14
x-opencode-client: cli            ← 注意是 cli，不是 desktop
x-opencode-project: global
x-opencode-request: msg_<24 hex>
x-opencode-session: ses_<28 chars>
# body: model + max_tokens:32000 + 9585 字 system + 11 个 tools + tool_choice:auto
#       + stream:true + stream_options:{include_usage:true}
```

## 五、使用须知

- sidecar 依赖官方 CLI 常驻进程（约 177MB 磁盘 + 常驻内存）；请自行判断是否符合该服务的使用条款
- 该免费候选的回复质量受 CLI agent 行为影响（已用自定义 agent 缓解），
  定位是 combo/auto 里的**免费候选**，不保证与付费 API 服务商完全一致的指令跟随
- 上游若再次收紧（例如要求 CLI 交互式确认），sidecar 可能失效——届时看本文件重新取证
