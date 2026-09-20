# 更新日志

格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)。

## [Unreleased]

### Added
- 模型管理「Auto」列升级为「分组」列：行内复选框可同时勾选 Auto 与任意多个组合（combo），一个模型可属多个分组；勾选直接反写 `combo.model_ids`（加入追加到组合末尾、移出只删自己不误伤其他成员），Auto 写 `model.auto_enabled`，保存即生效（新端点 `PUT /admin/api/models/{id}/groups`，模型列表附带 `combos` 归属，组合下拉随列表加载）
- 流口水自动冷却（DroolGuard）：同一模型连续 5 次返回一字不差的相同答案 → 判为复读死循环 → 强制罚时冷却 30 分钟（阈值/时长/最短参与长度可调，设置页开关）。比对的是**答案正文**（流式 chunk 拼接后去空白）而非响应哈希——chunk id 每次都变，哈希抓不到复读；现场依据：2026-09-19 烁公益站/kimi-k3 连续 5 次逐字返回同一条 "The input channel is healthy..."（北京 06:06-06:13）
- 候选竞速（Race）：组合/auto 回退链中当前候选 15 秒没有返回内容 → 不杀它，并行再打下一个候选，谁先出结果用谁的；被超前的候选判失败并自动罚冷却。覆盖 combo 流式/非流式与 auto 级联流式/非流式四条路径（`server/core/race.py` 统一 runner），设置页可开关与调秒数；关闭时完全退化为旧的顺序回退
- Combo 前缀路由（组合即端点）：`http://host:8000/combo:<名称或id>/v1` 直接把 Base URL 锁进某个组合——`/combo:918/v1/chat/completions`、`/v1/messages`、`/v1/responses` 的请求被强制改写为该组合级联路由（思考强度后缀保留），`/combo:918/v1/models`（简写 `/combo:918/models`）只列该组合候选模型；实现为路径改写中间件 + v1 入口注入，三协议零重复逻辑；Combos 页每条组合展示可复制的访问端点
- 登录会话持久化：session 落库 `admin_sessions`（此前纯内存，服务每次重启全员掉线=「一会儿就过期」的根因），重启不丢登录态；活跃会话剩余不足一半时长自动滑动续期；过期时长调整为 2 小时（`auth.session_timeout_hours`）
- OAuth 连接即自动登记服务商（credential_type=oauth，幂等；同名手工服务商只补 oauth 指向）；模型刷新支持 OAuth 服务商：在线 list_models，失败/无端点回退注册表静态种子（claude_code/codex/github_copilot/antigravity/cursor/codebuddy 国服国际服/cline/qoder 已配种子）
- 额度/余额查询 `GET /admin/oauth/connections/{id}/usage`（5 分钟缓存 + 并发合流 + Claude 429 冷却）：claude_code 5h/7d/模型级周窗、codex 会话/周/review/spark+重置券、github_copilot 付费/免费双形态、antigravity 按模型分数+周额度、codebuddy 续包/赠包分列、qoder 用户/组织配额（PAT 先换 job token）、u1s1 永久余额+今日免费（USD/token 双口径）——覆盖范围与 9router usage 一致；隐藏页内「查询余额」进度条面板
- Qoder 推理代理（端口自 9router qoder 全栈）：`QoderAdapter` COSY 签名（RSA+AES-CBC+MD5、17 指纹头）+ WAF 绕过编码（&Encode=1）+ `{statusCodeValue,body}` SSE 信封解包与 finish/usage 合流 + 服务端下发的 per-model model_config（缺键硬错、1h 目录缓存）+ 上下文档位自动升档 + 首帧计费拦截转回退；设备流登录一键连接（本地 PKCE/nonce → 轮询 deviceToken/poll 收 dt- token，签名元数据存连接 scope）；PAT(pt-) 自动换 job token(jt-，api2 路由)
- OAuth 连接隐藏管理页 `/providers/oauth`：设备流一键连接（自动侦测收取 token）、浏览器授权、手动导入 token，连接列表含到期倒计时/强制刷新/断开；不在导航留入口，浏览器授权完成后自动落回本页
- 隐藏 bug 猎捕审计落档 `docs/findings-bughunt-2026-09-18.md`（5 路分区审查+逐条复核：P0×5 / P1×23 / P2·P3 若干，含修复顺序与勾选清单）
- u1s1（有一说一）额度平台接入：服务端复刻官方 CLI 设备登录（start→浏览器批准→poll 收 api_key），免下载客户端；api_key 为长期 Bearer 凭证，OpenAI 兼容直连 api.u1s1.io/v1
- 服务商密钥启用/停用：每把密钥可单独停用（轮转与模型归属选择即时跳过），重新启用时自动清除 401/403 熔断与冷却状态；POST /admin/api/keys/{id}/toggle + Providers 页密钥弹窗按钮
- Cline (api.cline.bot) OAuth 接入：code 即 base64 token 直解、JSON 刷新、回调无 state 收尾、workos: JWT 前缀与 success/data 信封解包
- CodeBuddy 国际服 (www.codebuddy.ai) OAuth 注册（与 CN 同 device_poll 协议，platform=ide）
- `server/core/provider_quirks.py` 上游请求方言档案：CodeBuddy 流式专属（网关侧 SSE 聚合）、CN agent system prompt 中性化、国际服 typed-blocks 形态、reasoning_summary 镜像
- PostgreSQL 支持：`AIGATE_DATABASE_URL` 环境变量切换（默认 SQLite 不变），方言全兼容，服务器 PG16 全链路实测
- 跨库迁移工具 `scripts/migrate_to_pg.py`：SQLite → PostgreSQL 全量数据迁移（幂等、孤儿行预分类、序列对齐）
- 数据库每日定时备份 + 保留策略（SQLite 在线备份 / PG pg_dump），管理页可查看与手动触发
- 启动自检与版本标识（git commit / DB 类型 / 资源盘点），设置页展示
- `/v1/models` 增强组合路由伪模型（`combo:名称`）与思考强度后缀变体（`?include_effort=true`）
- 智力评分每周自动同步（LMArena 数据集）
- 可复用压测脚本 `scripts/load_test.py`

### Changed
- 12 项稳定性/性能优化路线完成：统一凭证解析器、流式空输出语义、usage 三方言归一化、上下文估算校准、元数据来源分层、评分聚合缓存、日志脱敏、代理池计数等
- 消息级 blob 去重与归档瘦身（DB 98MB → 9.6MB）
- 模型管理/Playground/日志详情性能优化（N+1 消除、懒加载、截断）

### Fixed
- 隐藏 Bug 猎捕审计第一批修复（docs/findings-bughunt-2026-09-18.md 勾选同步）：**P0-1** `stream_via` 漏 await（combo 流式 free_tier 与 auto 级联两处的 free/oauth/atomcode 候选 100% 失败并污染健康冷却池）；**P0-3** 响应缓存命中绕过网关鉴权/预算/日志（查询挪到 verify 之后，命中补 cache-hit 日志，error 体拒缓存）；**P0-4** Anthropic 适配器流式 finish 双发 + error 被 stop 伪装成功（finish_sent/error_sent 终态收敛），同函数带 **P1-11** message_start 的 input/cache usage 被丢弃（prompt_tokens=0 走粗估、缓存费恒 0）一并修复；**P0-5** /v1/messages 入站 tool_result 映射 role:"user" + 凭空追加空 user 消息（Claude Code 工具链配对断裂 → 上游 400）；**P1-1** `sa_select` NameError 兜底分支裸 500；**P1-2** combo 清理误删"临时禁用"模型（禁用只跳过不删）；**P1-3** 401/403 硬熔断恒失效（httpx 状态码取错属性）+ `_fallback_key` 不查熔断集合；**P1-8** log_queue 关闭不排空（每次重启白等 8s + 丢在途日志 → 被 sweep 成假 error）；**P1-9** 网关每日 token 预算双倍计缓存 token（最快 2 倍速耗尽误伤 429）；**P1-13** rank 缓存 key 含微秒时间戳恒 0 命中 + dict 无界增长（量化到秒 + 封顶淘汰）；**P1-16** 更新检查 git fetch/代理探测同步阻塞冻结事件循环（远端不可达=全站停摆）；**P1-19** Playground 日志路由归属恒 NULL（闭包默认参数冻结）；**P1-23** openai_compat/github/free_providers 流式只认 `data: ` 带空格形态（无空格上游整条流静默丢弃）
- OAuth 服务商模型刷新 404（「Cline 免费模型获取不到」根因）：注册表 api_base_url 填到推理端点级别（`…/api/v1/chat/completions`）时，models 列表 URL 又拼了一层 `/v1/models` 成 `…/chat/completions/v1/models`；现剥除 chat/completions、messages、responses、embeddings 等端点后缀再挂 `/models`（已是版本根则直接拼），Cline 实时目录 445 模型（含 `:free` 后缀免费模型）正常入库；`/models` 裸数组响应形态同步兼容（此前只认 `{"data":[...]}`），CodeBuddy 的同类刷新 404 一并治愈
- 改密码后强制全员重新登录现会同时清除落库会话（此前只清内存，DB 行在重启回填后仍「可复活」）
- OAuth 浏览器回调 `/admin/oauth/callback` 被 auth 中间件拦成 SPA 页面（回调是浏览器顶层导航，不可能带 Authorization 头），authorization_code 流在默认配置下全断（审计 P0-2）→ 精确豁免该路径（其防线本就是 state/PKCE 而非登录态）；回调结果 redirect 到 /providers/oauth 的 success/error 落地提示，用户拒登的 error 回跳与换票异常不再裸 422/500
- CodeBuddy CN device_poll 协议纠正：state 请求从 GET 改为 POST ?platform=，解析字段对齐真实协议的 authUrl（旧实现按 loginUrl 解析，实际拿不到登录链接）
- 日志「一成功一失败」双行：systemd `aigate.service` 与 pm2 双守护冲突（每 5s 崩溃重拉，累计 48934 次），每次启动清扫把在途 pending 行误翻为 interrupted；启动收尾现只处理早于本进程启动（留 5s 余量）的遗留行
- Anthropic 流式缺 await 导致 /v1/messages 全坏、message_start 被 ping 抢首、非流式块序错误
- 终态错误被包装成"正常完成"的空响应（Responses/Anthropic SSE）
- 请求日志 token 全 0、PG 布尔比较、种子 NOT NULL 等方言问题
