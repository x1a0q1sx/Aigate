# F40 — OpenCode 全模型 429 定性 + OAuth 多账号治理（2026-10-13）

> 用户报告六项：
> 1. OpenCode 服务商所有 free 模型都 429
> 2. CodeBuddy 国际版经常「断」（研究原因并解决）
> 3. 请求日志详情显示走的哪个账号
> 4. OAuth 多账号按「积分最近到期」选号
> 5. 积分包额度耗尽后不应再参与「最早到期」统计
> 6. 多账号时某账号报错 → 该账号冷却

## 1. 定性：OpenCode 全 free 模型 429（任务 1）

### 1.1 现象与取证

15:16-15:20 CST 批量探测（DB 日志 id=46033~46047），**每个模型返回的错误其实不同**：

| 模型 | 上游返回 | 归类 |
|---|---|---|
| muse-spark-1.3-contributor-free | 429 `FreeUsageLimitError: Rate limit exceeded` | **真限流** |
| space-bunny-free | 同上 | 真限流 |
| step-5-preview-free / ling-3.1-flash-free | 429 `server_error: Endpoint is unavailable` | **上游端点不可用** |
| ling-3.0-flash-fin-free | 400 `server_error: Model is unavailable` | 端点不可用 |
| fledge-alpha-free | 410 `ModelDeprecated` | **模型已下架** |
| exo-free | 410 `ModelDeprecated` | 已下架 |

**关键排除**：同时段 mihomo journal 显示 opencode.ai 全部走 `OPENCODE-REGION[日本…]` 出口
（F39 的 HK 飘移修复仍生效）→ 与 RegionError / 节点问题无关。

### 1.2 「全模型同时 429」的机理

- 免费层鉴权是 `Authorization: Bearer public`（**匿名共享 key**）+ 官方 CLI 会话指纹；
  无账号体系 → 限流只能按「出口 IP + 客户端标识」建桶，且**按上游后端/模型分别结算**。
- 调研（research-scout，2026-10-13）：社区实操建议即为「换 IP」、「静置数分钟」；
  第三方扩展通过伪装 opencode CLI UA 修复 403/身份型 429。
  官方无任何限流维度/数字的公开文档。
- 与仓库内两次历史实证一致（10-09、10-11）：**高频试探上百次 → 所有模型一律 429
  → 静置约 25 分钟自愈**。这是「把共享池打爆」，不是模型或网关故障。

### 1.3 复测（16:20+，距爆约 50 分钟后）

- muse / space-bunny / nemotron-3.5 / longcat **全部恢复成功**（非流式，秒级）。
- 确认 1.1 的「全 429」为批量测试触发的临时限流；step-5/ling 仍不可用（端点侧），
  exo/fledge **已下架**。

### 1.4 网关侧修复：free_tier 失败补模型冷却

**缺口**：free_tier 直连失败路径（流式 `_free_stream` 与异常分支）**从不调用
`mark_failure/mark_cooling`** → auto/combo 每轮都来撞同一个坏模型/限流模型，白白等
sidecar 超时。而普通模型失败都会进冷却。

**修复**：`server/core/free_providers.py::is_free_failure_coolable(err_text)`：
- 含 429 / rate limit / FreeUsageLimitError / Endpoint is unavailable / ModelDeprecated /
  5xx / transport failed / timeout → **冷却 300s**；
- `FreeTierError`（会话/配置类确定性错误）→ **不冷却**（改配置即恢复，冷却会误导）；
- 未知错误 → 保守冷却（宁可 5 分钟后重试）。

接入：`v1_router.py` free_tier 直连的**流式 finally** 与**非流式异常分支**，各调用
`is_free_failure_coolable` 判定后 `mark_failure + mark_cooling(300)`。

## 2. CodeBuddy 国际版「总断」的根因（任务 2）

### 2.1 取证

DB 聚类：`codebuddy_intl` 错误 171 条全部为 **429**，两类业务码：
- `{"code":6004,"msg":"usage exceeds frequency limit, but don't worry, your usage wi…"}`（频控）
- `{"code":14003,"msg":"too many requests"}`（更严的出口/账号限流）

典型风暴：10-12 14:08-14:15，**同一账号连续 12 条 429**（每隔 1 分钟重试一次，全部撞同一账号）。

### 2.2 根因（两个叠加）

1. **自动模式永远只打 id 最早的账号**：`oauth_tokens` 里 `codebuddy_intl` 有 2 个账号
   （id=9 `x1a0q1sx@gmail.com`、id=10 `1355386364@qq.com`），服务商 `oauth_owner=None`。
   旧 `pick_access_token` 的兜底逻辑是 `_get_any_active_token_record`＝**id 最小**；
   → 所有流量固定压 id=9。该账号一旦被上游频控，后续请求全部 429 —— 用户观感即「断」。
2. **账号级零冷却**：某个账号 429 后无任何本地标记，负载也不会切换到 id=10；
   12 条连败实证。

### 2.3 修复（与任务 4/6 共用一套机制）

见 §4 多账号治理：自动模式改走「额度到期排序 + 账号冷却跳过」，失败账号进冷却后
自动换号；id=10 从「从未被使用」变为正常轮转。

## 3. 请求日志显示所用账号（任务 3）

- **DB**：`request_logs.routed_account VARCHAR(100)`（迁移 + ORM）。
- **写入**：全部调用路径（直连非流式中央日志 / 直连流式 / auto 级联非流式成功+失败 /
  auto 流式成功+失败 / combo 非流式成功 / combo 流式成功+失败 / playground）
  从 `RouteResult.account` 或 `ResolvedCredential.account` 取值写入。
- **API**：`GET /admin/api/logs`（列表）与 `GET /admin/api/logs/{id}`（详情）都返回 `routed_account`。
- **前端**：LogsPanel 列表行在服务商/模型后显示 `👤 账号名` 徽标；详情弹窗新增「使用账号」行。
- 账号来源：`pick_access_token_ex()` 返回 `(实际选中的 owner, token)` → 解析器 →
  `RouteResult.account` → 日志；**兼容旧代码**：所有读取用 `getattr(..., "account", None)`。

## 4. 多账号治理：按到期选号 + 报错冷却（任务 4 + 6）

### 4.1 设计

**选号排序**（`OAuthClient._rank_auto_rows`，仅自动模式即 `oauth_owner` 为空时）：
```
(cooling, quota_rank, __default优先, id)
```
其中 `quota_rank`（`oauth_usage.quota_rank_key`，升序优先）：
- `(0, 最早到期时间, -剩余合计)`：**有余额**（任一包 remaining>0 或 unlimited）——
  到期最近的优先级最高（**先用快过期的，避免浪费**）；「最早到期」只统计**仍有余额**
  的包（与任务 5 展示口径一致）；
- `(1, inf, 0)`：无额度数据（查询失败/未预热）；
- `(2, inf, 0)`：明确全部耗尽（排最后，但**不排除**——上游可能续量）。

**冷却跳过**：`cooldown_until > now` 的账号排序垫底；全部冷却时仍回退使用（避免 503）。

**额度数据来源**：`oauth_usage._CACHE`（进程内，300s TTL）——**请求路径只读缓存，
绝不发网络请求**。数据由新增的**预热任务**填充：`oauth_router._usage_prewarm_loop`
每 300s 扫一次「同 oauth_code 有 ≥2 条 active 连接」的 provider，串行
`get_connection_usage(force=True)`。启动延迟 20s。

**失败冷却**（`OAuthClient.note_request_result`）：
- 429 → 冷却 60s 起；401/403 → 300s 起；其余 → 120s 起；均指数退避封顶 1h。
- **只写库（永不抛异常）**；成功仅当该账号「脏」（曾失败/冷却）时才查库清零
  （进程级 `_ACCOUNT_DIRTY` 集合；选号时若发现残留状态也预置脏标记）——成功路径零开销。
- **冷却资格闸**（`credential_resolver.is_account_worthy_error`）：429/401/403/5xx 或含
  rate limit/quota/usage limit 文案 → 可冷却；空输出、race 取消、上下文超限 → 不冷却
  （不是账号问题，避免白白减少可用账号）。
- **点名不受影响**：`provider.oauth_owner` 显式指定账号时照用（用户明确指定）。

### 4.2 接入点（全路径）

| 路径 | 失败钩子 | 成功钩子 | 日志 |
|---|---|---|---|
| 直连非流式 | 循环内每次异常（状态码分级） | 循环内成功 | `enqueue_log(routed_account=…)` |
| 直连流式 | 流结束按 `_stream_err` | 流正常走完 | `write_log(routed_account=_direct_account)` |
| auto 级联非流式 | `_record_fail` | 胜出候选 | 失败 `write_log` + 中央日志 |
| auto 流式 | `_record_fail` | 胜出候选 | `_write_stream_log(routed_account=…)` |
| combo 非流式 | `_ns_on_fail` | 胜出候选 | `_write_stream_log` |
| combo 流式 | `_on_fail` | 胜出候选 | `_write_stream_log` |
| playground | 异常分支 | 成功 | `_write_log` |
| fusion | `default_call` | `default_call` | — |

### 4.3 前端

- OAuth 连接页：账号行新增「冷却中」徽标（`cooldown_until > now`）+ 橙色文案
  「冷却 X 分钟后恢复（连续失败 N 次）」。
- `/admin/oauth/connections` 返回 `cooldown_until` / `fail_count`。

## 5. 积分包展示口径（任务 5）

`QuotaPanel.bonusAgg.firstExpiry`：**只统计仍有余额（remaining > 0）的包**的到期时间。
已耗尽的包还挂在列表里，但其到期时间已无意义（不会再消耗）——旧逻辑把耗尽包的
到期时间也算进「最早到期」，显示用户早已不受影响的日期（如「10-14 到期」而该包已空）。

## 6. 测试与验证

- 新增 `tests/test_oauth_multi_account.py`（23 项）：排序键（到期优先/耗尽包不参与/
  三档位/unlimited）、冷却跳过/全冷却回退/点名不受冷却、note_request_result 退避与清零、
  冷却资格闸、状态码提取、free 失败判据、DB 列与迁移、全路径静态锁、四条前端静态锁。
- 改写 `tests/test_provider_scheduled_refresh.py::test_resolver_passes_oauth_owner`
  （解析器改走 `pick_access_token_ex`，新增 account 透传断言）。
- 全量 1005 → **1028 全绿**；前端 build 通过（bundle `index-Igfu_2y0.js`）。
- E2E 冒烟（临时 DB）：额度缓存选「明天到期」账号 → 冷却后自动切到另一账号，通过。

## 7. 教训

1. **上游 429 要分类**：`FreeUsageLimitError`（共享池限流，静置自愈）≠
   `Endpoint is unavailable`（端点侧故障）≠ `ModelDeprecated`（下架）。网关必须
   对这三类**都**做冷却，否则 auto 反复撞墙；对 `FreeTierError`（配置类）则相反，
   冷却会掩盖真正的配置问题。
2. **多账号的本质是「不把鸡蛋放一个篮子」**：匿名/账号型限流只打单个账号时，
   自动模式固定 id 最早 = 单点故障。选号必须看「健康（冷却）+ 收益（先用快过期）」
   两个维度。
3. **额度查询绝不能进请求路径**：上游额度接口本身限流（Claude 429 有 180s 冷却先例）
   → 用后台预热 + 进程内缓存，请求路径只读。
4. 「成功清零」要零开销：脏标记（失败过才写库）是小巧但关键的优化，否则每个成功
   请求都多一次 DB 写。
