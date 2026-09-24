# AiGate v3 开发发现记录
## 项目现状
- 项目完整度约80%，已有完整框架
- 后端：FastAPI + SQLAlchemy Async + SQLite
- 前端：Vue 3 + Vite，已编译静态文件在 client/dist
- 支持OpenAI兼容接口 + 自定义管理面板
## 现有能力
1. ✅ 服务商CRUD（含预设模板10个）
2. ✅ 密钥加密存储（Fernet）
3. ✅ 模型发现+价格匹配（内置定价表）
4. ✅ Auto路由引擎（RankingService三维度加权）
5. ✅ 健康探测定时任务（APScheduler）
6. ✅ 手动/批量测速
7. ✅ 请求日志+审计日志
8. ✅ Dashboard仪表盘
9. ✅ Playground聊天测试
10. ✅ 管理员CRUD智力静态种子
## 待完善点
1. ✅ Playground前端调用方法名不匹配
2. 收费模型参与Auto选举的UI开关
3. 定时评测可增强记录更多指标供RankingService使用
## 技术决策备忘
- 数据库迁移策略：启动时自动ALTER TABLE
- 多Key轮换：已可通过admin API管理多个key
- 前端构建：npm run build后dist目录挂载到FastAPI
# Fallback 审计发现（2026-09，findings §F1–F8）

## F1 死代码 route_with_fallback
- 位置：server/core/auto_router.py:452-484
- 问题：`while current.success` 进入后立即 return；初始失败则循环不进入；tried 未传给 get_best_candidate
- 影响：无调用者，但 API 语义错误，易被误用
- 处置：删除

## F2 空输出判定先外发后判定（跨候选正文拼接）
- 位置：server/api/v1_router.py:1259-1277（combo 流式）、1756-1777（auto 流式）
- 触发：候选只发 reasoning/role/usage 后结束 → 已 yield 内容判定为"空" → 回退 → 客户端看到第一候选 reasoning + 第二候选正文拼接
- 处置：实质 chunk（content/reasoning/tool_calls）出现前缓冲，出现即 flush 并锁定候选；纯元数据结束才回退

## F3 combo max_fallbacks 失效
- 位置：server/api/v1_router.py:1199（流式取 max 导致恒遍历全部）、1347（非流式无上限）
- 处置：总尝试 = min(候选数, max_fallbacks+1)，与 auto 契约一致

## F4 combo 不支持 free_tier/oauth 候选
- 位置：server/api/v1_router.py:1228-1233、1371-1380 无条件 pick_key_for_model，combo_router.py:93-111 不排除这些 credential 类型
- 影响：合法候选被跳过，回退可能耗尽 503
- 处置：统一凭证解析器 credential_resolver.py，free→free executor、oauth→pick_access_token、atomcode→专用通道、标准→key rotator

## F5 auto cascade 的 free_tier 候选走错 adapter
- 位置：auto_router.py:336-357 返回 keyless 候选；v1 auto cascade 763-803/1701-1747 直接 adapter.chat_completion
- 影响：free_tier 模型进 auto 后先打错误 URL 失败才回退
- 处置：auto cascade 复用 credential_resolver 的 free dispatch

## F6 Playground 流式真实请求不回退
- 位置：server/api/admin_router.py:1758-1796（probe 用 _auto_route_with_runtime_fallback，真实 stream 单发）
- 处置：复用 v1 cascade 生成器

## F7 终态错误被伪装成成功
- 位置：v1 终态 error chunk（1847-1850 / 1325-1328）；responses_router.py:343-365 未检测；anthropic_router.py:205-219 交给 converter 后正常收尾
- 影响：全部候选失败时 Codex/Claude 客户端收到"正常完成"的空响应
- 处置：两包装器检测顶层 error → response.failed / Anthropic error 事件并终止

## F8 Fusion 未完成却伪造成功
- 位置：combo_router.py:359-367 调用不存在的 AutoRouter.get_candidates，异常被吞（418-419），可能返回空 content
- 处置：strategy=fusion 返回 501；前端标注"实验中"

## F9 源库孤儿行（P2-11a 迁移实测发现，2026-09）
- 位置：生产 SQLite rate_limits 56 行 / health_checks 40 行引用已删除的 model/key
- 根因：SQLite 默认不执行外键，模型刷新/删除历史遗留；init_db 原有清理只覆盖 model_api_keys（v8.1）
- 影响：迁移 PG 时被正确拒绝（PG 强制 FK）；对运行中的 SQLite 网关无实际影响（这些行不可达）
- 处置：scripts/migrate_to_pg.py 按目标库父表实况预分类为「孤儿跳过」不计坏行；init_db 追加幂等 DELETE 清理三处孤儿引用
- 迁移工具另踩的方言坑：tz-aware ISO 时间戳 asyncpg 拒绝绑 TIMESTAMP WITHOUT TIME ZONE（解析后剥时区保留字面值）；asyncpg 批量 executemany 的 rowcount 不可靠（改用前后 COUNT 差值统计插入数）

## F10 流式出站退化成「逐字符一行」（CodeBuddy CN）
- 现象（2026-09-22 用户报告）：ZCode 里思考/正文每个字符单独占一行
- 位置：`server/adapters/openai_compat.py` `_consolidate_openai_stream`
- 根因：上游 CodeBuddy CN 逐字下发（每 chunk 1~2 字），且**每个** chunk 的 delta 都带
  `"tool_calls": []`。合并器原判据 `if tc is not None` 把空列表也当「有工具调用」，
  于是每个 chunk 都 flush 一次缓冲 → 合并完全失效，出站退化成逐字符。
  实测生产流：585 分片 / 平均 2.2 字 / 222 个单字符；离线对照
  `tool_calls=[]` → 30 个 1 字符分片，不带该字段 → 2 个（24+6），一次复现。
- 影响：所有走 openai_compat 的逐字上游（不只 CodeBuddy）都会命中；
  客户端把每个字符当独立增量渲染成一行
- 处置：① 判据改 `if tc:`（空列表=没有工具调用，继续缓冲）；
  ② reasoning 也按 `content_chunk_size` 阈值 flush（原先只在正文出现 / 流结束时吐出，
  长思考期间客户端干等，且同样受逐字退化影响）；
  ③ 清掉因此变成死变量的 `reasoning_flushed`
- 证据：`tests/test_stream_consolidation.py`（7 项，含空 tool_calls 等价性、
  真工具调用仍即时透传、reasoning 内容不丢、drop 模式、chunk_size=1 直通、元数据块保留）

## F11 OAuth 额度展示顺序（按到期先后）
- 需求（2026-09-22 用户）：oauth 服务商的额度按到期顺序排序
- 位置：`server/core/oauth_usage.py` 新增 `sort_quotas()`，在 `get_connection_usage` 出口收口
- 背景：同一账号常有多个额度包（CodeBuddy 的续包 + 多个赠包、Qoder 的 user/org、
  u1s1 的永久余额 + 今日免费），各家上游返回顺序无语义，界面按 dict 插入顺序渲染 → 看着乱
- 口径：`reset_at` 升序（最早到期的排最前）；无 reset_at / 不限量 / `9999-12-31` 哨兵
  一律排最后；同到期按名称稳定排序；坏值当无到期处理，排序异常绝不冒穿到额度查询
- 收口点选择：放在 `get_connection_usage` 出口（而非前端或各家 handler），
  缓存命中也带上已排序结果，两个界面（服务商详情浮窗 / OAuth 连接页）自动一致
- 证据：tests/test_oauth_usage.py 新增 5 项（排序、无到期末尾、同到期稳定、naive/Z 时间等价、端到端出口已排序）

### F11 附：赠包编号跟随到期顺序
- 赠包名 `Bonus Pack N` 原按上游数组顺序编号，出口按到期重排后会显示成
  2,3,…,10,1,11…（看着像漏号，实测 33 个赠包时很显眼）
- 处置：`_codebuddy_usage` 内先按 `CycleEndTime` 升序再编号，编号与到期顺序一致

## F12 cline 候选连续失败（2026-09-22 用户报错，非网关 bug）
- 现象：combo 918 cline 候选近 29 次全挂，
  `upstream_stream_error: stream_initialization_failed ... failed to invoke model 'z-ai/…' from OpenRouter`
- 实测取证（生产服务器直连 cline API，带 workos: 前缀 + cline 专属头）：
  1) `qwen/qwen3.8-27b:free` / `z-ai/glm-5.2:free` → OpenRouter **429
     "temporarily rate-limited"**（免费层限流，非配置问题）；
     且 cline 把上游错误**包在 200 SSE 首块里**发回（`{"error":{...}}` + `[DONE]`），
     网关按终态错误识别并记 `_FailAttempt` 回退——行为正确（缺陷 G 的成果）。
  2) 付费模型（z-ai/glm-4.6 等）→ **402 insufficient_credits**：该 Cline 账号
     Credits 余额 $0.004，付费线全部不可用。
- 结论：账号额度问题+上游限流，网关无需改码；候选已进自动罚时冷却。
  处置建议：充值 Cline Credits 或只保留 :free 候选（接受间歇 429）。

## F13 OAuth 多账号改名后"not connected" + 按服务商定时刷新（v4.2）
- 现象（2026-09-22 用户）：codebuddy_cn 加了两个账号（改名成手机号）后
  路由报 `OAuth provider 'codebuddy_cn' not connected`
- 根因：连接记录的 owner 是路由寻址键，凭证拾取写死 `__default`；
  用户把主账号改名（或删除 __default 重连）后 __default 行不存在 → 拾取失败。
  实测生产库：两条 codebuddy_cn token 的 owner 均为手机号，无 __default。
- 处置：
  1) `pick_access_token` 自动模式（owner=__default）缺行/已停用时，兜底取该
     服务商**任一 active 连接**（id 最早）；点名模式下不兜底（用户显式选的号，
     找不到就该报错，不能悄悄换号）。u1s1 设备签名材料同样兜底。
  2) Provider 新增 `oauth_owner`：服务商编辑页可**点名**多账号中的路由账号
     （两个账号可建两个服务商条目分别寻址，进 combo 互为冗余）；
     错误文案带账号名。resolver / model_catalog（OAuth 服务商刷新模型）统一传 owner。
  3) 附带：服务商编辑页新增「定时刷新模型」开关+自定义频率（分钟，≥5）；
     调度器每分钟 tick 扫到点服务商（先拨 next_at 再执行，失败不重复排队；
     重入保护）；已单独设频的服务商从 config.yaml 全局 scheduled 批量中排除，
     避免双刷。next_at/last_at 落库，详情浮窗可见下次时间。
- 证据：tests/test_provider_scheduled_refresh.py（6 项：CRUD 钳制/拨钟、
  全局批量互斥、点名精确、__default 优先、兜底、resolver 透传 owner）

## F14 CodeBuddy/WorkBuddy 模型列表过期：无在线端点 + 种子回退不可见（v4.3）
- 现象（2026-09-23 用户）：「workbuddy 国际版获取到的模型列表不对吧？群友说有 deepseek V4.1 flash」
  生产库 provider 87 确实没有 deepseek-v4.1-flash。
- 根因链：
  1) copilot.tencent.com / www.codebuddy.ai / www.workbuddy.ai 均**无模型列表端点**：
     /v2/models、/v1/models、/v2/plugin/models、/console/enterprises/personal/models 等
     40+ 候选路径实测 404/403/500；`/v2/plugin/login/account?state=` 能通（证明鉴权正确）。
     逆向项目同样佐证：模型列表从桌面端 product.json 读或干脆硬编码。
  2) 刷新逻辑因此静默回退静态种子表，且日志记 `ok=1` 无任何来源标注 →
     种子过期完全不可见。provider 87 的 13 个种子中 7 个已下架
     （deepseek-v4-flash / deepseek-v4-pro / deepseek-v3-2-volc / glm-4.7 / glm-5.0 /
     hy3-preview / minimax-m2.7），且缺 deepseek-v4.1-flash + gpt-6-astra/gpt-5.6-*/
     gpt-5.5/gpt-5.4/gpt-5.3-codex/gemini-3.5-flash。
- **可用性探测法**（CodeBuddy 类唯一可行的列表来源）：stream 请求 + system 首条
  （`"You are CodeBuddy Code."`，否则 11128 安全策略拦截），max_tokens 给足（太小
  gpt-5.x 报 11133 integer_below_min_value）；按响应码判：200=可用、
  11102 "service info not found"=已下架/不存在、11102 "only available for
  authorized users"=存在但本账号无权限、14003=限流（改期间隔重试探）。
  实测 60+ 候选：intl 可用 28 个，CN 可用 27 个，两版独有模型差异显著
  （gpt/gemini 仅 intl；deepseek-v4-flash/pro/v3-2-volc/kimi-k3-1 仅 CN）。
- 附带结论：**www.workbuddy.ai 与 www.codebuddy.ai 是同一后端**（同 token、同端点、
  同结果，2026-09-23 实测）——「WorkBuddy 国际版」= 网关里的 CodeBuddy (International)，
  不需要新增服务商。
- 处置（commit fe4aa89）：两版种子表按探测结果重建；
  model_refresh_logs 新增 list_source / list_note 两列（online/seed/pricing），
  分析页刷新行以「种子」角标透出，不再伪装在线拉取。
- 证据：生产刷新日志 `('CodeBuddy (International)', 1, 23, 6, 7, 'seed', '静态种子兜底（29个）')`；
  tests/test_model_refresh_log.py 新增 3 项（种子内容回归 + seed 标记落库 + 序列化透出）。

## F15 u1s1 403 真凶：tools[].function.name 黑名单（v4.3）
- 背景：f058bfe 的竞品词消毒上线后，combo:918 的 u1s1 候选仍持续 403（09-22 07:29 之后零成功）。
- 生产 A/B（真实凭证 DPoP+UA，单变量，2026-09-23）：
  - 18 个候选「客户端名词」（含 ZCode/WorkBuddy/CodeBuddy/Qoder/Trae/iFlow 等）**任何位置都不拦**；
    `Claude Code`/`Windsurf`/`Gemini CLI`/`Codex CLI` **仅在 system 位置**拦（同词放 user 放行）
    → f058bfe 结论对一半：词真存在，但拦的是 system 注入，且不是 combo 主因。
  - **唯一必拦项：`tools[].function.name ∈ {apply_patch, update_plan}`**。30 个常见工具名
    （shell/exec_command/Bash/Edit/Write/Read/Task/TodoWrite/…）全放行；描述/参数 schema 随便改都拦，
    改名即放行（applyPatch / apply_patch_2 / plan_update / local_shell 实测 200）。
    **只扫 tools 字段**：正文含 apply_patch、历史 tool_calls 含该名 → 200。
  - UA 仍是硬判据（覆盖成 codex_cli_rs → 403）；`originator`/`x-codex-*`/`OpenAI-Beta` 不拦。
  - `/v1/models` 现 404（attestation 拿不到），chat/completions 带 DPoP+UA 仍 200 → attestation 非必需再证。
- 相关性：09-22 生产 37 条 u1s1 请求，带 apply_patch 的 **19 条 100% 403**，无 tools 的小请求 10 条全成功。
- 修复：`ProviderQuirks.blocked_tool_names`（u1s1 启用）→ transform_payload 改 tools/tool_choice 名，
  响应（message/delta/聚合）还原原名；服务商级 `fingerprint_filter_enabled`（默认开，UI 详情页开关，
  经 `__fg` 标记传递）统一控竞品词消毒 + 工具名改写两件事。

## F16 能力感知路由（多模态/长上下文自动选模型）（v4.3）
- 需求：多模态请求自动走 vision 模型、长上下文请求自动走大窗口模型 → 需要逐模型的
  「上下文窗口 / 输入模态 / 最大输出」元数据与路由闸。
- 现状盘点：`context_length` 链路早已完整（估算→动态系数→observed 收紧→4 处预检跳过），
  长上下文的「装不下就跳过」事实上已成立；缺的是**模态数据与能力闸**。
  生产库 2560 模型：`supports_vision=1` 仅 2 行、`context_length=0`（未知）1653 行、
  capabilities JSON 是死字段——vision 维度当时完全不可用，先修数据才谈路由。
- 落地（数据层→来源→闸→偏好→可见性）：
  1) Model 新列 `input_modalities`(JSON, **NULL=未知**) + `max_output_tokens`；
     `capability_source` 承载可信度 openrouter/provider/manual/inferred。
  2) 来源三层：OpenRouter `architecture.input_modalities`+`top_provider.max_completion_tokens`
     （此前被丢弃，全仓唯一现成数据源）；openai_compat /models 若上游声明则解析（provider 可信）；
     名称启发式 `infer_modalities`（仅正向提示 inferred）。manual 永不覆盖。
  3) **硬闸安全边界**：只有可信来源的闭合模态集合参与拦截（未知一律放行）——
     按「未知即不支持」会误杀 65% 模型。inferred 不算可信。
  4) 请求画像 `analyze_request()`：est_tokens + has_image/has_audio（OpenAI+Anthropic 部件形态都认）。
  5) 闸接点：auto `_filter_candidates`/sticky（含 relaxed 模式不放松能力闸）；
     combo 流式/非流式/fusion/auto-stream 预检点 `_SkipAttempt/_NSkip/_ASkip` 带 reason。
  6) 排序偏好（非硬闸）：多模态请求 +10/模态，长上下文按窗口档位 +2..+14；
     上限 ~24 分，不压过智力/稳定性量级差。combo 顺序不偏好（用户显式配置）。
  7) 前端：模型页「窗口/能力」列（含来源 tooltip）+ 编辑表单窗口/最大输出/模态勾选
     （保存即 capability_source=manual）；/v1/models capabilities 输出扩展。
- 遗留（如实说明）：模态覆盖率取决于 OpenRouter 命中与服务商自声明；
  长尾私有站模型仍会是「未知→放行」，手动编辑兜底；max_output 暂不参与预检 reserve。

## F17 订阅制上游"倍率"（credit multiplier）数据源与落地（v4.4）
- 背景（用户）：「codebuddy、qoder 获取到的都是倍率吧，他们的价格可以改成倍率吗？
  这样比较好分辨哪个是免费的」——订阅制上游没有 USD 单价，价格列恒显 $0，无法分辨贵贱。
- 数据源调研（实测，非推测）：
  1) **Qoder 有官方字段**：模型目录 `/algo/api/v2/model/list`（COSY 签名）每条带
     `price_factor`（0.0 / 0.1 / 0.5 / 2.0…）+ `original_price_factor` + `is_free`。
     生产账号实测 15 条：qfmodel 0.0（免费）/ gfmodel 0.1 / dfmodel 0.1 / mmodel 0.2 /
     efficient 0.3 / auto 0.5 / qmodel_38max 0.5 / dmodel 0.5 / kmodel 0.8 / gmodel 0.8 /
     kmodel_latest 1.4 / performance 1.1 / ultimate 2.0。**这是唯一可靠的动态源**。
     注：`price_factor` 会随**促销活动**浮动（同日复测 qmodel_38max 由 0.5 变 0.2，
     对应 promotion「错峰 4 折」active=true；`original_price_factor` 保留原价），
     因此必须走在线目录而非静态表——每次刷新拿到的是当时有效倍率。
  2) **CodeBuddy 无服务端倍率**：40+ 候选端点（含 /v2/config、/v3/config、
     /v2/plugin/model/list、/v2/billing/meter/get-model-resource、/v2/update、
     /v2/plugin/versions、CDN download.codebuddy.cn 等）全部 404/500/无倍率字段；
     chat 响应只带 token usage。倍率只存在于**官方客户端**：
     - WorkBuddy 桌面端 `resources/app.asar.unpacked/cli/product.json` 的 models 数组
       带 `credits: "x0.16 credits"` 形态（endpoint=copilot.tencent.com → CN SaaS 版）；
     - CodeBuddy IDE 客户端日志（CraftInvokableAgent 的 model: 行）带完整模型元数据，
       含 `credits`——**带时间戳**，可解冲突（deepseek-v4-flash 2026-08-20 为 x0.17、
       08-21 起改 x0.08）。
- 落地（v4.4）：
  1) Model 新列 `price_ratio`（NULL=未知 / 0.0=免费）+ `price_ratio_source`
     （qoder | codebuddy | provider | manual）。**与 input_price/output_price 完全分离**
     ——倍率不参与 USD 成本核算（订阅制没有美元口径，混算会污染账单）。
  2) Qoder 适配器 `list_models` 解析 `price_factor`（异常值降级 None；0.0 同时标 is_free）。
  3) CodeBuddy 静态表 `STATIC_PRICE_RATIOS`（oauth_registry）：只收录**有直接证据**的值
     （product.json + 客户端日志），未列出的留 NULL 不猜；按 provider 分别维护
     （国际版种子已剔除 deepseek 系，倍率表也同步剔除——由测试守住）。
  4) 刷新写入：在线值优先；`manual` 永不覆盖（与 USD 价格同规则）；种子兜底路径也带倍率。
  5) 前端：模型页价格列在 `price_ratio` 非空时**改显倍率**（免费绿标 / ≤0.3 蓝标 /
     ≥1.5 黄标 + 来源 tooltip），编辑弹窗新增倍率输入（含清空开关 → 回到未知）。
     `/v1/models` 输出 `price_ratio`。
- 遗留（如实说明）：CodeBuddy 倍率随客户端版本变化，静态表会过期（未列出的模型显"未知"）；
  客户端升级后需重新从 product.json / 日志提取。Qoder 走在线目录不受影响。

## F18 隐藏 bug 审计第二批（P1 × 13 + P2 × 5，v4.4）
- 基线：docs/findings-bughunt-2026-09-18.md（HEAD=469e6b2 审计）。第一批（89e5b2a）清了
  P0 与 10 项 P1；本批修复剩余 13 项 P1 + 5 项 P2。全部先回读源码核实再改，新增 51 项测试。
- P1-4 session sticky 三重失效：① `v1_router` 每请求 `uuid4()` 当 key → 永不可命中
  （`session_sticky_minutes` 形同虚设）；② 缓存只增不清（无界增长）；③ 用 naive
  `datetime.utcnow().timestamp()` 比较（按本地时区折算，UTC+8 下刚写入即"过期"）。
  另 `get_best_candidate` 的 3 处 `return None` 违反调用方 `.success` 契约 → AttributeError。
  修法：新增 `client_ip.derive_conversation_id()`（优先客户端 `x-conversation-id`/
  `x-session-id` 头，否则按「客户端 IP + system + 首条 user」派生稳定哈希）；
  sticky 改用 `time.monotonic()` + 容量上限（4096，超限淘汰最旧到半量）；
  深层嵌套重构为 `_try_sticky_candidate()`，所有失败分支统一 `return None` 由调用方回落选举。
  注意：日志/route_decision trace 仍用**每请求 uuid4**（并发安全），sticky 用独立键。
- P1-5 auto 非流式级联丢内部头：`v1_router` 直接 `candidate.provider.headers`，未合
  `__oauth/__proxy_force/__fg` → 强制代理/指纹过滤开关静默失效。修法：统一走
  `_merge_oauth_headers` 并叠加 `candidate.extra_headers`。
- P1-6 request_overrides 路径不一致：仅「直连」与「combo 非流式」生效，combo 流式与
  auto 级联（流式/非流式）缺 model_alias/body_patch。修法：抽出
  `_apply_request_overrides(request, model, extra_headers)`，5 条出站路径统一调用。
- P1-7 fusion judge 决策被吞：`fusion.py` 写 `attempt="judge"`（str）→ `route_decision`
  `int()` ValueError → 同 try 的 select+finish 一起跳过（明细永久丢失 + `_active` 泄漏）。
  修法：`add_attempt` 对非数字 attempt 降级为字符串（不再抛）。
- P1-10 rate_limiter upsert 死代码：通用 `sqlalchemy.insert` 没有 `.on_conflict_do_nothing`
  → 恒 AttributeError 静默落 except；降级路径裸 `add+commit` 并发首建抛 IntegrityError
  冒穿请求路径。修法：按 `IS_SQLITE` 选方言版 insert；兜底 commit 包 IntegrityError →
  rollback + re-query。
- P1-12 headroom 从未接线：`is_in_headroom_cooling` 全仓零调用（额度保留只是展示）。
  修法：`get_best_candidate` 一次性取 provider 级已用量，循环内跳过触达阈值者；
  统计失败不拦（安全默认，绝不因统计故障饿死候选）。
- P1-14 OAuth single-flight 假成功 + InvalidStateError：合并方丢弃 leader 结果无条件
  `return True`；且 `wait_for` 超时会 **cancel 共享 Future** → leader `set_result` 抛
  InvalidStateError 穿透到请求路径（500）。修法：合并方 `shield` 等待并回传真实结果；
  set 前判 `fut.done()`。
- P1-15 刷新失败一律永久下线：任意 ≥400 都 `is_active=False` → 上游一次 429/502/超时
  就把连接判死刑（调度器只扫 active → 须人工重登）。修法：新增
  `_refresh_credential_dead(status, text)`——401 恒失效、400/403/404 需带
  invalid_grant 类标记、429/5xx 一律临时；三处刷新分支（通用/codebuddy×2/cline×2）全部接入。
- P1-17 手改价格标记被刷新抹掉：`manual_priced` 算出来后，`pricing_source = source_url`
  在 remote 分支内**无守卫** → 标记丢失，下轮刷新直接覆盖用户价格；另 success_rate 等
  指标远程缺字段时以 None 抹掉旧值。修法：source 写入移入 `not manual_priced`；
  四项指标加 `is not None` 守卫。
- P1-18 智力分手工校准永不生效：面板 upsert 不传 `source`（默认 "arena"），而同步侧
  免覆盖要求 `source=="manual"` → 手调分数每周被覆盖。修法：两个分支都写 `source="manual"`。
- P1-20 delete_provider 清理是死代码：先 DELETE 再 SELECT model ids → 查询恒空，
  HC 内存清理成死代码；HealthCheck/RateLimitState/ModelApiKey 留孤儿行。
  `delete_key` 不清 KeyRotator 进程内状态（SQLite rowid 复用 → 新 key 继承旧冷却）。
  修法：SELECT 前移；补三张关联表清理；新增 `KeyRotator.forget_key()`（清熔断+游标）
  并在 `delete_key` 调用。
- P1-21 明文密钥无二次鉴权：`/providers/export?include_keys=true`（明文上游密钥+refresh
  token）、`/keys/{id}/reveal`、`/aigate-key?reveal=true` 仅靠会话 Cookie——被盗会话
  一个 fetch 拖走全部明文密钥。修法：三处挂 `_require_admin_reauth`；前端配套
  `prompt()` 询问密码（Dashboard 明文密钥改为按需揭示，掩码走新 `masked` 字段）。
- P1-22 Gemini 流式名不副实：`gemini_to_chat` 硬编码 `stream=False` →
  streamGenerateContent 实际等全文完成；`chunk_to_gemini` 丢弃 tool_calls、吞网关
  终态 error；SSE 生成器无 finally aclose。修法：stream 参数由 action 传入；
  chunk 转 functionCall 分片 + 错误事件透出；生成器加 finally aclose。
- P2 数项：`_raw_response` 返回前 pop（此前随错误体泄漏上游原始报文）；
  RPD/TPD 日窗口重置 + 纳入 check_limit（此前只增不减、从不检查，日限永不生效）；
  codex `function_call_arguments.delta` 按 item_id 建槽/别名（此前按 call_id 查恒落空，
  逐片参数静默丢弃）；force_stream 注入 `stream_options.include_usage`（否则聚合 usage 恒 0）；
  内置价表改最长前缀匹配（此前 `gpt-4o-mini-*` 命中 `gpt-4o` 档，高估 30 倍）。

## F19 模态覆盖率：现状量化与下一步（v4.4 收尾）
- 生产基线（v4.3 交付时）：总 2590 模型，模态已知 1178（45%），未知 1412（55%）。
- **先把"影响面"量化清楚**（避免过度投入）：模态硬闸只在 **Auto 选举**与 **combo 预检**生效；
  combo 目标由用户显式配置（不参与自动选路），直连请求根本不走闸。生产 Auto 参与仅 40 个
  模型（其中未知 25 个）——所以"55% 未知"听起来吓人，**真实暴露面是 25 个 Auto 候选**。
- 未知模型的 provider 分布（Top）：openrouter官方 165 / 哈吉米付费 164 / Cline 158 /
  huggingface 102 / 星见雅 90 / imagic 75 / NVIDIA NIM 71 …（多为公益站与聚合站）。
- **已落地的快速见效**（本轮完成，无需改代码）：`openrouter官方` 那 165 个是**本地行未刷新**
  （v4.3 才加的解析代码，这些行建于代码之前）。执行一次 provider 刷新：
  added=26 / updated=432 / removed=13，该 provider 未知 **165 → 0**；
  全库覆盖率 **45% → 52%**（已知 1178 → 1358），`capability_source='provider'` 0 → 180。
- **下一步（按性价比排序，均未实施）**：
  1) **补第二数据源 models.dev**（实测可行，收益最大）：`https://models.dev/api.json`
     （4.9MB，3666 个去重模型，带 `modalities.input`；含 image 1998 / 纯 text 1578）。
     对当前 1410 个未知模型实测覆盖 **758 个（54%）**，其中**可补多模态 239 个**
     （正是视觉请求路由最需要的）、可补纯文本 519 个、未覆盖 621 个。
     接入方式与 OpenRouter 同构（`intelligence_sync` 加一个 fetcher + 回填分支，
     `capability_source='models.dev'`，manual 仍不覆盖）。注意它按 provider 组织，
     同一 model_id 多 provider 出现 → 取模态并集（宽松侧）。
  2) **刷新全部聚合站/公益站 provider**：凡是上游 /models 自带
     `architecture.input_modalities` 的（NewAPI 系转售 OpenRouter 的站），刷新即自动补齐
     （openai_compat 适配器已支持解析）。可在模型页逐个刷新，或后续加"批量刷新全部服务商"。
  3) **名称启发式扩容**：当前 `infer_modalities` 只覆盖有限 tag；生产有 248 个未知模型的
     名称带视觉系特征（vl / vision / claude-opus|sonnet / gpt-4o / gemini / llava / pixtral…）。
     **注意安全边界**：inferred 只做正向提示、不参与硬闸（这是刻意设计——inferred 能证明
     "支持 X"，永远不能证明"不支持 Y"）。扩容可提升排序偏好命中率，但不该放宽闸门。
  4) 剩余 ~620 个（私有站自研模型名）无公开数据源可查 → 只能手动标注或维持"未知→放行"。
- 结论：**不建议为覆盖率做激进改造**。当前"未知→放行"是安全侧默认（漏拦 ≠ 误杀），
  真实损失是"含图请求可能仍选到不支持图片的模型"——而这仅影响 Auto 的 25 个候选，
  且候选排序已对视觉请求加偏好分。优先做第 1 项（models.dev）即可把可补的多模态
  239 个补上，性价比最高。

## F20 模型刷新并发化 + 单服务商硬超时（2026-09）
- 用户反馈：「模型刷新速度太慢了，能不能改成多线程的呢，然后再设定一个阈值超过多少秒就判定失败，
  不一直等待。设置中能进行配置超时时间。」
- 瓶颈实测（生产 `model_refresh_logs`）：58 个服务商**串行**逐个刷新，
  单站平均 **17.2s**、最大 **141.4s**（b.ai官方）→ 全量约十几分钟，
  且端点在整个过程中不返回（前端只能干等）。
- 改造（commit 6b9cf66）：
  1) `refresh_providers_concurrent(providers, trigger, concurrency, provider_timeout,
     catalog, key_manager, session_factory)` —— 抽成模块级函数（便于测试），
     端点与定时 tick 共用。
  2) **每个服务商独立 DB session**：刷新内部有 commit/rollback，共享 session 会撞 SQLite
     写锁并错乱 ORM 状态。session 工厂从调用方 session 派生（`_session_factory_for`），
     生产=全局 `AsyncSessionLocal`，测试=临时库（否则并发写到错误的库）。
  3) **硬超时** `asyncio.wait_for`（默认 45s，可配 5~1800）：到点判失败、不再等待。
  4) **异常隔离**：任一超时/抛错不影响其余；超时也落 `model_refresh_logs`
     （此前这类失败在分析页完全不可见）。
  5) 单服务商刷新退化为串行（用户就刷这一个）。
  6) 定时 tick（main.py 的 per-provider 每分钟扫描）同步改为并发，
     避免单个卡死拖垮整轮（并保留「先拨钟再执行」的防重入语义）。
- 配置（`config.model_refresh` + 设置页）：
  `concurrency`(默认 12, 1~32) / `provider_timeout_seconds`(默认 45, 5~1800) /
  `timeout_seconds`(单次请求, 默认 20, 3~600)。后端钳制区间防配错。
  端点 `GET/PUT /admin/api/model-refresh`；刷新结果新增 `failed_details` + `duration_ms`。
- **生产实测**（58 服务商全量，单站超时 45s）：
  | 并发 | 耗时 | 成功 | 失败/超时 |
  |---|---|---|---|
  | 1（改造前串行） | ~16 min | — | — |
  | 6 | 101.5s | 52 | 6 |
  | **12（新默认）** | **60.2s** | 52 | 6 |
  | 20 | 52.4s | 52 | 6 |
  边际递减明显（12→20 只快 8s，却把上游限流风险抬高），故默认取 12。
- 超时的 6 个站（七倍公益/agentrouter/河涛公益/huggingface官方/zzzcoding公益/b.ai官方）
  是**上游真的慢**（45s 内没回），现在被明确标为失败而非拖死整轮——
  与串行时代的差别是：它们不再影响其余 52 个站的结果返回时间。
- 测试：tests/test_model_refresh_concurrency.py 17 项（并发峰值、并发上限、
  超时判失败不等待、异常隔离、provider 已删除、单服务商串行、
  session 工厂派生生产/临时库两路、配置钳制、端点委托防回退、schema 字段）。

## F21 分析页「按服务商用量」显示不全：routed_provider_id 大面积缺失（2026-09-24）

**现象**（用户报）：分析页「按服务商用量」只列出 4 家、且明显占比不对；
实际当天有 6 家在服务。Providers 页详情弹窗的「今日用量」同样为空。

**生产取证**（当日 832 行日志）：

| 分组 | 行数 | 说明 |
|---|---|---|
| `routed_provider_id IS NULL` + 有 ttft | 786 | **直连流式写入路径** |
| `routed_provider_id IS NULL` + 无 ttft | 50 | pending 在途 + 部分非流式失败 |
| `routed_provider_id` 有值 | 35 | 仅 combo/auto 少数路径 |

名称分布：CodeBuddy CN 596 行 / Qoder 139 行 / CodeBuddy Intl 51 行 —— 全部
`routed_provider_id = NULL`。也就是说**当天 95% 的请求在用量面板上凭空消失**。

**根因**：`routed_provider_id` 是后加的列（方案A 把配额统计从 quota_usage 并到
request_logs 时引入），而 `_write_stream_log` 与若干写入路径当时只写了服务商
**名称**；聚合查询统一写 `WHERE routed_provider_id IS NOT NULL`，两相叠加 →
「名单之外的都得靠 id，但大家都只写了名字」。

**连带缺陷**：`headroom_manager.get_provider_breakdown` 用同一份过滤条件 →
**每日 token 限额统计不到真实用量，保留额度形同虚设**（这是功能性缺陷，
不只是显示问题）：配置 4000 token 限额的站实际跑了 5000 也不会被跳过。

**修复三层**（缺任一层都会复发）：

1. **聚合层收敛**：新增 `server/core/provider_usage.aggregate_usage_by_provider`
   ——「id 优先、名称兜底」聚合：加载 providers 名称→id 映射，把只有名称的历史行
   归到正确服务商并**与 id 行合并为同一行**；名称匹配不到（已删除/改名）保留
   原始名称的独立桶，不静默丢数据；排除 `is_health_check` 与 `status='pending'`
   （后者在途且完成时原位更新，提前计入会虚增）。
   `analytics/by-provider` 与 `get_provider_breakdown` 都改接它。
2. **写入层补 id**：`_write_stream_log` 新增 `routed_provider_id` 形参（传了就直接
   用，不再按名查询——重名/改名场景会错配）；直连流式在路由完成时快照
   `_rt_prov_id`；combo 流式各站点、free_tier 流式、非流式 combo 胜出、
   媒体生成（4 处）、透传（2 处）、Playground、健康检查全部补齐。
   **兜底**：`log_queue._write_batch` 与 `write_log` 同步路径在落库前按名解析
   `routed_provider_id` —— 将来新增路径再漏写也不会丢数据。
3. **历史数据回填 + 索引**：`init_db` 回填
   `UPDATE request_logs SET routed_provider_id = (SELECT p.id FROM providers p WHERE p.name = request_logs.routed_provider) WHERE routed_provider_id IS NULL AND routed_provider IS NOT NULL`
   （幂等，启动跑一次；providers.name 有 unique 约束故无歧义）；
   新增 `idx_request_logs_prov_time(routed_provider_id, created_at)`。

**前端**：Providers 详情弹窗由「只按 id 命中」改为「id 优先、名称兜底」；
Analytics 卡片标题加「N 家」计数，名称匹配不到 id 的行加「（已删除）」标注，
避免用户再看到「明明有流量却不显示」或反之的困惑。

**验证**：tests/test_provider_usage_aggregation.py 21 项（名称行计入、id/名称合并
单行、已删除桶、health/pending 排除、时间窗、成本累加、NULL token 容错、排序、
headroom 限额生效、端点返回全集、log_queue 兜底两路、迁移 SQL 幂等 + 注册断言、
各写入路径断言、前端断言）；并用生产今日真实分布（786 名称行 + 35 id 行、6 家）
离线复现：修复前面板只见 2 家，修复后 6 家齐全且计数与原始日志一致。

**教训（跨模块）**：给统计列加「必填项」性质的过滤条件时，必须同时保证
（a）所有写入路径都写该列、（b）历史数据有回填、（c）聚合端有兜底容忍。
本次三层缺一即复发——审计时曾把「加列」当作纯增量，没检查写入方覆盖度。

## F22 一键签到 + 额度监控（协议实证与能力边界，2026-09-24）

**需求**：用户看到 Jet-Hub（DeepSeek Harness 插件，做 11 个 AI 编码平台的凭据托管 +
多账号池）的「一键领取」按钮，想在 AIGate 的 OAuth 页旁边也搞一个签到监控页面。

⚠️ **用户给的链接是错的**：`github.com/zhengwuj/Jet-Hub` 是 404（该用户不存在），
真实仓库是 **`github.com/zhengwuji/Jet-Hub`**（少一个字母）。

**Jet-Hub 的三个关键事实**（源码 tarball 全量核对，非 README 宣传）：

| README 宣传 | 源码实情 |
|---|---|
| 「每日签到积分自动领取」 | **没有任何签到定时器**。唯一的 setInterval 是 30 分钟的**凭据续期**（`src/index.ts:533`），与签到无关 |
| — | 签到状态**不持久化**，每次实时查上游（无日志、无历史） |
| — | **无跨平台总览**（无仪表盘、无合计） |

即：用户要的「监控页面」正是 Jet-Hub 缺的部分。同类里更完整的是
`masknull/dsh-qoder-connect`（自定义签到时刻 + 开机防漏补签 + 签到日志）与
`techysy/CreditDaddy`（守护进程 + 首页仪表盘 + 每 2 小时扫描）——本实现按后者形态做。

**与 AIGate 重合的平台只有 3 个**（Jet-Hub 11 个，其余 AIGate 无对应 provider）：

| Jet-Hub | AIGate | 签到协议 | 实证强度 |
|---|---|---|---|
| `buddy` | `codebuddy_cn` | `POST /v2/billing/meter/checkin-activity-status` + `/daily-checkin` | 强 |
| `buddy-intl` | `codebuddy_intl` | 同上，host=`www.codebuddy.ai` | 中（仓库无独立实测） |
| `qoder` | `qoder` | `GET /sash/api/v1/me/campaigns` → `POST …/{id}/claim` | 强（keylog 抓包解出） |
| `antigravity` | `antigravity` | 明确不支持 | — |
| `workbuddy` 系 | 无 | 上游无接口 | — |

**本机生产实测（2026-09-24，只读探测）**：

| 平台 | 结果 | 结论 |
|---|---|---|
| CodeBuddy CN | HTTP 200 · `active=True` `today_checked_in=True` `streak_days=6` `daily_credit=100` `total_credits=600` `activity=高校新生攻略` | ✅ 协议有效 |
| CodeBuddy Intl | HTTP 200 · `active=False`（本期活动未开启） | ✅ **端点存在**（此前仓库无独立证据） |
| Qoder | HTTP 200 · `showCampaign=False campaigns=[]` | ✅ 协议有效（今日已领语义） |
| u1s1 | 8 个候选端点（`/v1/me/checkin`、`/v1/checkin`、`/v1/me/free_claim`…）**全部 404** | ❌ **无签到接口** |

**u1s1 的真相**：它确实有「每日签到」——但形态完全不同：`/v1/me` 的
`packages[]` 里存在 `kind="login_checkin"`「登录打卡赠送·每日一次·仅限 u1s1 客户端使用」
（每次 2,000,000 tokens、约 10 天有效）。**该包由上游在登录时自动发放**，
不需要也不能由客户端触发（`free_claim` 字段实测为 `null`）。故能力矩阵登记为
`False` 并在页面标注「上游无签到接口」——**绝不猜一个端点写上去**。

**协议中反直觉之处（实现的关键，全部有测试锁死）**：

1. **CodeBuddy 幂等判据是 body `code`（10001/1001）而非 HTTP 状态** —— 重复领取
   返回的是 **HTTP 400** + `code:10001`。只看状态码会把「已领」判成失败。
2. **CodeBuddy 401/403 可能返回 HTML 而非 JSON** —— 必须先取 text 再 parse，
   否则 `.json()` 抛异常会丢掉「凭据失效」这个关键判据。
3. **CodeBuddy 必须用 `checkin-activity-status`，不能用 `checkin-status`** ——
   后者返回占位数据（`active:false`、`checkin_dates:null`），会误判成「活动未开启」。
4. **`X-Domain` 必须跟产品配置走**（cn=`copilot.tencent.com` / intl=`www.codebuddy.ai`），
   不能跟凭据里可能过期的 domain —— 否则 baseURL 与身份头自相矛盾。
5. **Qoder 幂等判据是 body `replayed`** —— 重复领取**同样返回 HTTP 200**，
   但 `replayed=true` 且**不含 `benefit`**、`claimedAt` 是旧时间。只看状态码会把
   「今天已领」误报成「领取成功 +100 积分」。
6. **Qoder 的 claim body 必须是空串 `""` 而非 `{}`**（抓包实测 `content-length: 0`）。
7. **Qoder 只领 `actionType=CLAIM_BENEFIT` + `claimStatus=CLAIMABLE`** ——
   实测还有 `VIEW_DETAILS` 型活动（如「Pro 首月翻倍」），对它发 claim 是错的。
8. **Qoder 状态端点「列表为空」≠「没有活动」** —— 服务端在「今天已领」时返回
   `{showCampaign:false, campaigns:[]}`。Jet-Hub 曾据此误判「Qoder 无签到端点」。
9. **Qoder 签到不需要 COSY/WASM 签名** —— 那只有推理端点和模型列表要；
   签到只要 4 个头（`Accept` / `Bearer` / `Cosy-ClientType: 5` / `User-Agent: Qoder`）。
10. **严格串行**：Jet-Hub 三处注释强调「并发易触发风控」，并有单测锁死
    `maxInFlight==1`。本实现同样串行、单账号失败不中断整批、不做重试退避。

**实现**（改动面收敛，不碰现有路由/额度逻辑）：

- 新表 `checkin_logs`（`server/models/checkin_log.py`）：kind 五态
  `claimed`/`already_claimed`/`inactive`/`unsupported`/`failed`，
  **「今日已领」不是失败**——这个区分在数据层就成立，否则 UI 无法正确显示。
  「今日状态」从日志派生（查当天行），不做双写。
- 新模块 `server/core/checkin.py`：三个适配器 + 能力矩阵 + 批量执行 + 幂等辅助。
- 调度：挂进既有 `_schedule_maintenance()` 的 `AsyncIOScheduler`（**本项目公认落点，
  自带 shutdown**），`timezone="Asia/Shanghai"` 显式时区（上游按 UTC+8 刷新，
  不能依赖服务器 TZ），默认 **10:30 北京时间**（Qoder 活动 10:00 刷新 + 30 分钟余量）；
  `_busy` 重入保护 + `coalesce/max_instances=1`；
  **启动补签**（照 `_backfill_oauth_providers` 的既有形态）——服务重启错过时间点后
  当天自动补签；幂等靠 `collect_targets` 查当天日志（已完成的账号不发上游请求）。
- 路由 `server/api/checkin_router.py`（prefix **必须** `/admin/api` ——
  `core/auth.py` 的 `_is_admin_api_path` 只认 `/admin/api/` 与 `/admin/oauth/`，
  用别的前缀未登录时返回 200+index.html 而非 401 JSON，前端 `JSON.parse` 直接炸）。
- 前端隐藏页 `/providers/checkin`（照 `/providers/oauth` 的写法）+ OAuth 页头部
  「签到监控 →」入口；额度进度条复用 OAuth 页的 `quotaPct/quotaText` 口径，
  两页展示一致。
- 通知：`notify_event("checkin", …)` + `NotifyConfig.notify_checkin` 开关
  （getattr 兜底，老 config.yaml 无此字段时不炸）。

**测试**：`tests/test_checkin.py` 40 项，全部锁在上面 10 条反直觉协议上；
全量 448 项通过（408 基线 + 40 新增）。

**边界与风险**：协议是逆向得来的私有 API，随时可能变——签到全程在推理请求路径
**之外**，任何失败只落 `checkin_logs`，绝不触碰路由；上游改协议只影响签到页。

### F22 补充：两个生产问题（当天遇到并修复）

**1) 幂等遗漏 inactive → 活动未开启的站每次重启都重打上游**（commit fdbe590）

上线后观察 `checkin_logs`，发现 codebuddy_intl 一次刷出 **25 条重复行**，
而 codebuddy_cn / qoder 各只有 1 条 —— 这个对比正好反证了缺口：

- codebuddy_cn / qoder 的结果是 `already_claimed` → 在 `DONE_KINDS` 里 → 跳过 ✅
- codebuddy_intl 的结果是 `inactive`（活动未开启）→ **不在** `DONE_KINDS` 里 →
  每次进程重启的启动补签都再打一遍上游 ❌

修复：`ATTEMPTED_KINDS = DONE_KINDS + ("inactive",)` 作为幂等判据。
**依据**：自动触发只在**计划时刻之后**发生（定时 10:30 / 启动补签仅在已过点时才跑），
那一刻上游活动早已刷新完毕 —— 此时仍报「未开启」就是当天真的没有活动，反复重试无意义。
`failed` 仍不在其列：失败必须允许下次重试。

验证：修复后重启，启动补签输出「无需签到（跳过 5 个账号）」，行数**保持 4 条不变**。

**2) 孤儿进程抢端口 → pm2 崩溃循环 4000+ 次，新代码根本没加载**

部署后 pm2 显示 online、health=200，看起来一切正常，但：
- `pm2 list` 的 restart 计数在疯涨，日志里刷 `[Errno 98] address already in use`
- **新功能完全不生效**（签到页 404、启动日志里没有签到排程）

根因：`sudo ss -ltnp | grep :8000` 显示持端口的是 PID 228832 —— 一个
**PPID=1 的手工 `start.py`**（17:13 启动，早于本次部署），跑着**旧代码**；
而 pm2 进程每次启动都因端口被占而立即退出 → 被 pm2 反复拉起 → 崩溃循环。
systemd 的 `aigate.service` 是 disabled/inactive，**不是**它（排除了这个常见嫌疑）。

处置：`pm2 stop` → `kill` 孤儿（优雅退出未果，需 `-9`）→ `pm2 restart`。

**核对方法（以后部署必做）**：
```bash
PORT_PID=$(sudo ss -ltnp | grep ':8000' | grep -oP 'pid=\K[0-9]+' | head -1)
PM2_PID=$(cat ~/.pm2/pids/aigate-3.pid)
[ "$PORT_PID" = "$PM2_PID" ] && echo OK || echo "pm2 没在管线上服务"
ps -o pid,ppid,lstart,cmd -p "$PORT_PID"   # PPID 应为 pm2 而非 1
```
**教训**：`pm2 restart` 返回成功**不等于**新代码已生效 —— 它可能正在安静地崩溃循环，
而服务照旧由别的进程提供。部署验证必须落到「持端口进程 = pm2 管理的 pid」这一条。
