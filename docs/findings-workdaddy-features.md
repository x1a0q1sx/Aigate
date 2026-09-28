# WorkDaddy 功能调研：可迁移到 AIGate 的能力（2026-09-28）

**调研对象**：https://github.com/babygoton/WorkDaddy （AGPL-3.0，★ 持续增长）
**性质**：WorkBuddy / WorkBuddy AI **桌面端增强工具** —— 通过 CDP 注入 UI 组件，不改官方安装包
**规模**：56 个脚本 / 约 44,000 行（`inject.js` 17k + `daemon.js` 11k 是大头）
**用户提问**：「还有什么旅行啥的功能，调研一下还有哪些功能可以迁移优化的」

---

## 一、核心结论（先说能落地的）

**WorkDaddy 的绝大多数功能是桌面端专有的（CDP 注入 / DOM 操作 / 本地文件），
但有一块「账号权益」子集是纯 HTTP API，AIGate 可以直接迁移** —— 而且实测对
我们现有的两个 CodeBuddy 账号**全部可用**（同一后端，见 F24：workbuddy.cn ≡ codebuddy.cn）。

**最值得做的三件事**（按价值排序）：

| # | 功能 | 价值 | 实测状态 |
|---|---|---|---|
| **1** | **Buddy 旅行（派猫猫旅行）** | 用户明确问到的；纯 HTTP，可定时自动派发+领奖 | ✅ 端点全通，159 账号**正在旅行中** |
| **2** | **成长任务（19 个，1350 credits 未领）** | 直接换积分；有 14 个可自动接取 | ✅ 实测 **1350 credits + 45 energy** 待领 |
| **3** | **连续活跃 / 补签卡 / 阶梯奖励** | 7/14/28 天阶梯（最高 150 credits + 5 energy） | ✅ 有 `makeup_cards` 补签机制 |

---

## 二、完整功能盘点（分类）

### A. 账号权益类 —— **纯 HTTP，AIGate 可直接迁移** ⭐

WorkDaddy 用 `http.requestAsAccount`（拿各账号 token 直接发请求），
**完全不依赖桌面端**。实测所有端点对我们的 CodeBuddy 账号返回 200：

#### A1. Buddy 旅行（用户问的「旅行」）

```
GET  /v2/activity/growth/buddy/travel/config     # 旅行地点列表（含时长/奖励）
GET  /activity/growth/buddy/travel/status        # 状态：idle / traveling / arrived
POST /v2/activity/growth/buddy/travel/depart     # 派出（body: {location_id}）
POST /activity/growth/buddy/travel/claim         # 到达后领奖
GET  /activity/growth/buddy/info                 # 当前 Buddy
GET  /activity/growth/buddy/list                 # 已拥有 Buddy 列表
POST /activity/growth/buddy/switch               # 选择出战 Buddy（body: {instance_id}）
```

**实测数据（159 账号）**：
```json
// status（我探测时正在旅行）
{"state":"traveling","buddy_id":7895878,"record_id":10459451,
 "location":{"id":1,"code":"coffee","name":"咖啡馆","duration_hours":3}}

// 旅行地点（config）
[{"id":1,"code":"coffee","name":"咖啡馆","duration_hours_min":1,...},
 {"id":2,"code":"mall","name":"商场店铺",...}]

// Buddy（info）
{"instance_id":7895878,"name":"星际喵","personality":"愿景探索者","rarity":"SR"}
```

**状态机**：`idle`（可派发）→ `traveling`（`arrive_at` 到达时间）→ `arrived`（可领奖）
**每日上限**：`daily_limit_reached` 字段
**WorkDaddy 的做法**（`buddy-travel.json`，10 分钟轮询）：
1. 查 `buddy/info` → 无 Buddy 则查 `buddy/list` → 都没有就跳过（需先领养）
2. 有列表但未选 → `buddy/switch` 选第一个（`instance_id` 必须是纯数字串）
3. `travel/status`：`arrived` → `claim`；`idle` 且未达上限 → `travel/config` 取第一个地点 → `depart`
4. **按天去重**（`state.get catSettledDay` 比对本地日期），避免重复请求

#### A2. 成长任务（积分大头）

```
GET  /v2/activity/growth/tasks      # 完整任务列表（含 reward_credit / reward_energy / progress）
POST /activity/growth/tasks/accept  # 批量接取（body: {task_codes: [...]}）
```

**实测（CN 159 账号，19 个任务）**：

| 任务码 | 状态 | 进度 | credit | energy |
|---|---|---|---|---|
| create_canvas | accepted | 0/1 | **300** | 5 |
| Expert_Philanthropy | not_accepted | -/- | **300** | 0 |
| playbook_prompt | accepted | 0/1 | 100 | 5 |
| Library_read | accepted | 0/1 | 100 | 5 |
| Hp_Appearance | not_accepted | -/- | 100 | 5 |
| Buddy_App | not_accepted | -/- | 100 | 5 |
| wb_wechat_oa_subscribe_task | not_accepted | -/- | 100 | 5 |
| Expert_team_use_3 | accepted | 0/3 | 100 | 5 |
| template_5 | in_progress | 1/5 | 100 | 5 |
| black_cat | accepted | 0/3 | 0 | 0 |
| ~~RichMeow_Chat / Expert_lighthouse / first_buddy / chat_5 / skill_1 / expert_5 / automation_1 / Model_chat_GLM5.2~~ | claimed | — | — | — |
| **未领合计** | | | **1350** | **45** |

**⚠️ 关键约束（WorkDaddy 源码里的白名单）**：
```js
const AUTOMATABLE_TASK_CODES = new Set([
  'create_canvas','template_5','expert_5','Expert_team_use_3','automation_1',
  'playbook_prompt','Expert_lighthouse','Buddy_App','Buddy_App_QQ',
  'Hp_Appearance','chat_5','Model_chat_GLM5.2','black_cat','Library_read']);
const LOCKED_BUDDY_TASK_CODES = new Set(['first_buddy','RichMeow_Chat']);
```
—— 只有这些码**可以在客户端外完成**；其余（如 `Expert_Philanthropy`、`wb_wechat_oa_subscribe_task`）
需要真实点击/关注公众号，**自动化会失败**，只能显示为「手动任务」提示用户。

**含义**：AIGate 能做的是「**自动接取可自动化的任务** + 把需手动的列出来提示用户」，
而不是全部包办。可自动的那部分奖励约 **1000 credits**（create_canvas 300 + 其余 7 项 ×100）。

#### A3. 连续活跃 / 补签 / 阶梯奖励

```
GET  /activity/growth/streak
```

**实测结构**：
```json
{"streak":{"days":0,"month_total_days":3,"next_tier":"7d","next_tier_remaining":6},
 "makeup_cards":{"balance":0,"max":4},
 "redemption_status":{"tier_7d_status":"locked","tier_14d_status":"locked","tier_28d_status":"locked",
   "tiers":[{"tier":"7d","days":7,"credit":0,"energy":2,"cards":1,"chances":1},
            {"tier":"14d","days":14,"credit":50,"energy":3,"cards":1,"chances":1},
            {"tier":"28d","days":28,"credit":150,"energy":5,"cards":1,"chances":1}]},
 "timezone":"Asia/Shanghai"}
```

—— 7/14/28 天阶梯，最高 **150 credits + 5 energy + 1 补签卡 + 1 抽奖机会**。

#### A4. 盲盒（Gacha）与抽奖

```
GET  /activity/growth/buddy/quota        # {affordable, balance, cost_per_open, max_open_count}
POST /v2/activity/growth/buddy/open      # 开盲盒（body: {count: 1}）
GET  /activity/growth/lottery/chances    # {balance} 抽奖次数
POST /v2/activity/growth/lottery/draw    # 抽奖（body: {client_token}）
```

**实测**：`{affordable: 1, balance: 13, cost_per_open: 10, max_open_count: 5}`
—— 能量货币（energy）来自成长任务与阶梯奖励，攒够 10 点可开一次盲盒（得 Buddy 外观/新 Buddy）。
抽奖需要 `client_token`（UUID，防重放）。

#### A5. 每日签到（AIGate 已有，但 WorkDaddy 的更稳健）

WorkDaddy 的 `checkin-result.js` 比我们的实现多了两个防御：

```js
// 1. 按 token 的 JWT iss 推导正确域名（而非写死）
const ISSUER_HOST_MAP = new Map([
  ['https://www.workbuddy.ai','https://www.workbuddy.ai'],
  ['https://www.workbuddy.cn','https://www.workbuddy.cn'],
  ['https://www.codebuddy.cn','https://www.codebuddy.cn'],
  ['https://www.codebuddy.ai','https://www.codebuddy.ai']]);
// 2. 多域名兜底：iss 推不出来时依次试 codebuddy.cn / workbuddy.cn
// 3. 严格判据：只有 code=0 且 httpOk，或 code=10001 且**文案明确说已签到**才算成功
const INACTIVE_MESSAGE = /未开启|未开始|未开放|已过期|无.*活动|活动.*(?:结束|关闭|暂停)/i;
const ALREADY_MESSAGE  = /已签到|已领取|已经.*(?:签到|领取)|重复签到|already/i;
const already = normalizedCode === 10001 && !inactive && ALREADY_MESSAGE.test(message);
```

**实测**：四个域名（codebuddy.cn / workbuddy.cn / copilot.tencent.com）**全部 200**，
返回 `{"code":10001,"msg":"今天已签到，请明天再来"}` —— 说明我们的 CN 签到已在工作，
但**多域名兜底**这个防御值得抄（上游换域名时不至于整站失效）。

#### A6. 额度/用量查询

```
GET /profile/plans-usage    # 返回 HTML（是页面不是 API，WorkDaddy 用 CDP 解析）
```
—— 这个**不可迁移**（HTML 页面，WorkDaddy 靠注入脚本读 DOM）。
AIGate 现有的 `get-user-resource` billing API 更直接，保持即可。

---

### B. 桌面端专有 —— **不可迁移**（但可借鉴设计）

| 功能 | 为什么不可迁移 |
|---|---|
| **账号备份/切换**（点切即用） | 操作的是 Electron 的 `Local Storage`/`Cookies`/登录态文件；AIGate 已有 OAuth 多账号（更干净） |
| **主题/壁纸/毛玻璃** | 注入 CSS 到 WorkBuddy 渲染进程 |
| **暂存提示词 / 快捷短语** | DOM 操作输入框（`input-box plugin buttons`） |
| **会话迁移 / 会话分支（Fork）** | 直接读写 WorkBuddy 本机 SQLite + `projects/<slug>/<id>.jsonl` |
| **权限弹窗免打扰** | DOM 监听 + 自动点击 |
| **防止电脑休眠** | 本机 OS 电源管理（`caffeinate`/`SetThreadExecutionState`） |
| **异常中断自动续接** | 监听渲染进程事件后自动发下一条 |
| **模型管理（多同名模型）** | WorkBuddy 客户端配置 |
| **自动化任务引擎** | 整个 `automation.js`（1052 行）是**给 WorkBuddy 用的**：`session.create`/`dom.click`/`account.forEach` 等算子 |
| **Token/积分用量统计页** | 扫描本机 `~/.workbuddy` 的 JSONL 日志文件 |
| **模型限流解封时间** | 从客户端网络响应里抓 429，存本地 SQLite |

---

### C. 值得**借鉴设计**的点（不改架构也能抄）

1. **多域名兜底 + JWT iss 推导**（A5）→ AIGate 的签到可以加，防止上游换域名。
2. **严格成功判据**：`code=0 且 httpOk`，或 `code=10001 且文案明确已签到`。
   我们的实现已接近，但**没有校验「10001 的文案是否真的表示已签到」**
   （万一 10001 也被用于「活动未开启」，会误判为已领 → 不再重试）。
3. **按天幂等（本地日期去重）**：WorkDaddy 用 `state.get/set` 存 `catSettledDay`，
   避免重复请求。AIGate 已有 `checkin_logs` 的当天判重，同理可复用。
4. **任务白名单**（A2）：把「能自动做的」与「必须人工的」分开，
   后者**如实提示**而不是硬做（会失败）。这正是 AIGate 一贯的「如实标注不过滤」哲学。

---

## 三、迁移建议（按优先级）

### P0 —— Buddy 旅行（用户明确问到）

**新增 `_travel_*` 到 `server/core/checkin.py` 或新建 `server/core/codebuddy_growth.py`**：

```
旅行状态查询（只读）→ 面板展示「旅行中 · 咖啡馆 · 2 小时后到达」
自动派发（每日一次）→ idle 且未达上限 → 选第一个地点 depart
自动领奖 → arrived → claim
```

- 复用现有 `checkin_logs` 表（新增 `kind: traveled/claimed_travel`）或新表
- 复用现有调度器（`_schedule_maintenance`，与签到同机制）
- **只读优先**：先在额度面板展示旅行状态（零风险），再考虑自动派发

### P1 —— 成长任务（积分大头，1350 credits 待领）

- `GET /v2/activity/growth/tasks` 展示进度 + 待领奖励
- `POST /activity/growth/tasks/accept` **只对白名单任务码**自动接取
- 非白名单任务**列出提示**用户手动完成（对齐「如实标注」原则）

### P2 —— 连续活跃 / 盲盒 / 抽奖（展示为主）

- `streak` 展示「连续 N 天 · 距 7 天档还差 M 天」
- `buddy/quota` + `lottery/chances` 展示可用次数
- 自动开盲盒/抽奖**建议先不做**（消耗性操作，收益不确定，且有 `client_token` 防重放设计）

### P3 —— 签到防御增强（低成本高收益）

- 抄 `ISSUER_HOST_MAP` 多域名兜底
- 强化 `10001` 判据：必须文案明确表示已签到

---

## 四、风险与边界

| 风险 | 说明 | 处置 |
|---|---|---|
| **上游活动随时变** | 这是运营活动 API（`/activity/growth/*`），可能下线或改协议 | 全部走「拿不到就如实显示不可用」，绝不硬编码假设 |
| **`instance_id` 类型** | 实测是数字，WorkDaddy 强制 `Number()` 且校验 `String(x) === String(id)` | 照抄该校验（防字符串/数字混用） |
| **积分是否真到账** | `accept` 只是接取任务，奖励要完成才发 | 面板如实显示 `claimed/completed/pending` 三态 |
| **封号风险** | 目前只做只读查询 + 白名单内的常规操作 | **严格串行、无重试**（对齐现有 checkin 的克制做法）；不做高频轮询 |
| **Intl 账号无此活动** | 实测 Intl 返回空（`buddy: null`、`buddies: []`、`travel.config: {}`） | 按 provider 分别登记能力，Intl 显示「本版无此活动」 |
| **`/profile/plans-usage` 是 HTML** | 不是 API | 不迁移（AIGate 已有 billing API） |

---

## 五、实测证据（本次调研的生产数据）

```
账号: codebuddy_cn / 15944101987
  /activity/growth/tasks        → 200, 19 个任务, 未领 1350 credits + 45 energy
  /activity/growth/streak       → 200, 7/14/28 天阶梯
  /activity/growth/buddy/info   → 200, 星际喵 SR (instance_id 7895878)
  /activity/growth/buddy/list   → 200, 已拥有 Buddy
  /activity/growth/buddy/quota  → 200, {affordable:1, balance:13, cost_per_open:10}
  /activity/growth/lottery/chances → 200, {balance:0}
  /v2/activity/growth/buddy/travel/config  → 200, 地点列表
  /activity/growth/buddy/travel/status     → 200, state=traveling（咖啡馆 3 小时）

账号: codebuddy_intl / x1a0q1sx@gmail.com
  同类端点全部 200 但数据为空（buddy:null / buddies:[] / travel.config:{}）
  → 本版无此活动，非接入问题

签到端点（4 域名实测）:
  codebuddy.cn / workbuddy.cn / copilot.tencent.com
  → 全部 200 {"code":10001,"msg":"今天已签到，请明天再来"}
```

**取证方法**：复用生产 `oauth_tokens` 凭据 + CodeBuddy 标准头（`X-Product: SaaS`、
`X-Product-Code: codebuddy`、CLI UA）+ WorkDaddy 的 web 头
（`origin`/`referer: /profile/growth-center`、`x-client-platform: web`）。
