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

### F23：Qoder 余额「获取不到」的真相——签到积分不在 userQuota 层（2026-09-25）

用户反馈签到页 Qoder 账户余额获取不到。生产三路探针（旧端点全响应体、
新 sash 端点、AIGate 真实代码路径）还原了完整链条：

- **旧链路没坏但看得少**：`/api/v2/quota/usage` 一直 200，但只含 `userQuota`
  （生产账号实测全 0 + `isQuotaExceeded:true`），且**不下发资源包层**；
- **签到积分的落点在 addOnQuota**（资源包）：Jet-Hub 抓包实测账号
  `userQuota.remaining=0` 而 `addOnQuota.remaining=100`——只读 userQuota 的
  实现「签到成功后余额永远是 0」，这正是「漏读某一层」缺陷的又一例；
- **哨兵到期值渲染成废话**：上游 `expiresAt=253402214400000`（9999-12-31，
  「无到期」语义）被原样透传，前端渲染「到期 2879999 天后」，整个额度块
  看起来就是坏的。

修复（commit ad36fab，`_qoder_usage` 重写为三层结构）：
1. 主走 `/sash/api/v2/me/usage`（与签到同协议族：`Bearer` + `Cosy-ClientType: 5`
   + `User-Agent: Qoder`，无需 COSY 签名；2026-09-24 生产实测 200），解析
   `qoderUsage` 下的 套餐额度/资源包/专用资源包 三层（专用包各自 expiresAt 优先）；
2. 旧端点保留为**兜底**（sash 不可用或响应无任何条目时再试）；
3. `_qoder_sane_reset` 吞掉年份 ≥9000 的哨兵到期值（`reset_at: None`）；
4. `displayMode==="enterprise"` 的企业版账号不下发数字，明确提示而非报 0。

部署验证：22 项 qoder 用例 + 全量 453 项全绿；线上输出
`{"套餐额度": {..., "reset_at": null}}`，不再出现「到期 287 万天后」。
当前账号套餐额度 0、无资源包是**真实状态**（免费层额度耗尽 + 活动未领取）；
首次成功领取后「资源包」行会出现对应积分。

### F24：CodeBuddy 在线模型目录实锤——「无在线列表端点」结论被推翻（2026-09-25）

调研 Jet-Hub 时发现其 buddy 系（即 CodeBuddy）模型目录走两个在线端点。
生产只读探针（复用 oauth_tokens 凭据 + CodeBuddy 标准头）实测：

| 端点 | codebuddy_cn | codebuddy_intl |
|---|---|---|
| `GET /console/enterprises/personal/models` | **200** JSON | 500（openresty HTML） |
| `GET /v3/config` | **200** JSON | **200** JSON |

响应形状：`data.agents[].models[]`（每个 agent 配置带一份模型 id 列表，
取并集）；`/v3/config` 另含促销/倍率表（Jet-Hub `parsePromotions` 按
每日时段+时区+validFrom/Until 本地推算，`factor:0`=免费）。

含义：模型刷新不再需要「种子静默兜底」（种子会过期）；倍率表
`STATIC_PRICE_RATIOS` 可升级为在线促销推算。Jet-Hub 的合并规则：
scoped 端点优先、两端口 id 集合**取并集**、`agentReferenced` 例外保留。
遗留：Intl 的 scoped 端点 500，仅用 `/v3/config` 即可（Intl 探针 200）。

### F25：Jet-Hub 剩余渠道接入（TRAE / CodeArts）+ 错误信息不截断（2026-09-26）

用户要求「Jet-Hub 里的都要」，剩余可实现的 2 家全部落地（第 3 家 antigravity
三重否决：网关主机必须跑 IDE / 公共 API 实测 403 / 网关化必然违反防封号约束）。

**TRAE（字节）** —— 自有协议适配器（请求与响应双向转换）：
- 登录 URL **18 参数**，回调地址参数名是 `auth_callback_url`（写错则永远停在
  「授权中」）；回调**直接回传 token**（非 `?code=`）
- `machine_id`/`device_id` 客户端生成且不在响应里 → 必须随凭据持久化，
  且 machine_id **绝不可重生成**（换设备触发风控）
- refresh_token **轮换**（ExchangeToken 每次返回新值，旧值即刻失效）
- OpenAI→SOLO 五条转换规则（tools 参数必须 JSON 字符串化、tool_calls→function_call
  等）；SOLO 自定义 SSE 事件流解析
- ⚠️ **CN 新版请求体加密未实现**（`x-helios`/`x-medusa`/`x-neptune` 五头），
  Jet-Hub 亦未实现 → 受影响模型在显示名标注「暂不可用」，**但不过滤**
  （Jet-Hub 教训：把某一刻的快照当判据会让后人误删可用模型）

**CodeArts Agent（华为）** —— 额度最优（deepseek-v4 每日 1000 万 tokens 免费）：
- portal OAuth：`code_challenge_method=SHA-256` 是**字面量**（不是标准 S256），
  写错 portal 静默回退旧 ticket 流程 —— 用户**登录成功**但拿不到 refresh_token，
  要到几小时后续期时才暴露
- 回调主机名锁死 127.0.0.1 且端口必须 **≥10000**（低端口被 portal 拒绝）
- 推理不是 Bearer，是 **SDK-HMAC-SHA256 签名**（AK/SK/SecurityToken 三元组）→
  access_token 列存 **JSON 凭据**；签名用 node 执行 Jet-Hub 的 sign.ts 逐字节
  对齐验证（3 条金标准 Authorization 串）
- ⚠️ **refresh_token 一次性轮换**（STS5.1806）→ 续期必须串行化。本实现的
  `_refresh_codearts` 只在 `refresh_token()` 的 Single Flight 内被调用，
  且新 token **立即回写两处**（JSON + refresh_token 列，后者是前置判据）
- 4 个实测坑：`maas_type: benefit` 参与签名、APIG ~60s 空闲断连（deepseek-v4
  必须走 DSML 工具模式，不发 tools 字段）、排队码 TM.00001041 需轮询、
  assistant 历史必须带 `reasoning_content`

**错误信息不截断**（用户反馈）——此前链路三处截断（120/300/500 字符），
用户看到的 error_msg 不含上游原文。现按用途分工：控制流判据用短形态，
落库用全量（`ERROR_MSG_MAX_CHARS=20000` 只作防御），回下游客户端的 attempts
剥掉 `error_detail`；适配器响应体上限 500→4000；前端 `<pre>` 保留换行可折叠。

测试：TRAE 86 项 + CodeArts 116 项 + 接线守卫 10 项 + 错误不截断 9 项，
全量 **705 项全绿**。

## F26 签到页三项排查 + Freebuff 渠道接入（2026-09-26）

### 一、CodeBuddy (International) 签到失败 —— 上游本期未开放，非接入问题

用户反馈「CodeBuddy Intl 签到失败」。生产 `checkin_logs` 显示连续 3 天 `kind=inactive`
（09-24 启动补签、09-25/26 定时），message = 「签到活动未开启（上游 active=false）」。

**本机实测（只读探测，生产凭据）**：

| 端点 | 结果 |
|---|---|
| `POST /v2/billing/meter/checkin-activity-status` | HTTP 200 · `active=false` · `today_checked_in=false` · `daily_credit=100` · **`activity_name="本期：专家能量包"`** · **`start_time=""` `end_time=""`** |
| `POST /v2/billing/meter/checkin-status` | HTTP 200 · 全空占位（`activity_name=""`、`season=0`） |
| `POST /v2/billing/meter/daily-checkin` | **HTTP 400 · code 10001**「签到活动未开启或已过期」 |

**换美国出口 IP 复核**（挂服务器代理 7890 重打）：**结论完全相同**（`active=false`、
活动名与空时间段一致）→ 排除「地区门控」，是**上游本期没给国际版排活动**。
对照 CN 同端点同时刻 `active=true`、`streak_days=8`、`checkin_dates` 8 条、`total_credits=800`
—— 两侧协议同构，差异纯在上游运营侧。

**处置**：`CHECKIN_CAPABILITIES["codebuddy_intl"]` 保持 `True`（端点与活动框架都在，
活动随时可能开，预检会如实反映）；新增 `CHECKIN_NOTES["codebuddy_intl"]` 在签到页
写明「本期未开放，开启后自动签到」，避免用户以为是我们坏了。
`inactive` 仍计入 `ATTEMPTED_KINDS`（当天不重复打上游）。

### 二、u1s1 签到失败 —— 打卡只能在官网仪表盘做，但打卡包是登录自动发放

此前登记为「8 个候选端点全 404、无签到接口」。本轮**从官网前端源码挖到了真实端点**：

- 下载 `https://u1s1.io/app.js`（252KB，v116）→ 找到签到 UI 与调用：
  ```js
  const result = await api("/api/packages/login-checkin/claim", {
    method: "POST",
    body: JSON.stringify({ "cap-token": capToken, "cf-turnstile-response": turnstileToken }),
  });
  ```
- 即 `POST https://u1s1.io/api/packages/login-checkin/claim`（**u1s1.io 网站域**，不是
  api.u1s1.io），且要**两道人机验证**：Capcat（capcat.ai 的 PoW 隐形验证，`cap.solve()`）
  + Cloudflare Turnstile（`/auth/turnstile/config` 实测 `{"enabled":true,"sitekey":"0x4AAA…"}`）；
  另有手机号验证闸门（未绑定手机时按钮跳去绑手机）。

**鉴权实测（生产凭据，6 种组合全 401 `{"error":{"message":"not logged in"}}`）**：
`api_key` Bearer、`device_token`(u1s1d-) Bearer、DPoP 签名（`Authorization: DPoP …` +
逐请求 proof）打 `/api/me`、`/api/packages/login-checkin/claim`、`/api/v1/checkin`
—— 全部只认**网页会话 cookie**。官方 CLI（`u1s1-cli@1.11.6`，解包核对 dist/usage.js）
同样**不做签到**，只提示「每天到仪表盘打卡领免费包 → https://u1s1.io/dashboard」。

**但打卡包是登录自动发放的**（这才是关键）：`GET /v1/me` 的 `packages[]` 里
`kind="login_checkin"`（200 万 tokens/个、约 10 天有效、`note="登录打卡赠送·每日一次·
仅限 u1s1 客户端使用"`），生产账号实测 **6 个包 = 6 次登录**（created_at 与各次登录日期
一一对应）。**AIGate 重新连接该账号即等于打卡**。

**处置**：能力保持 `False`（客户端无法触发领取）+ `CHECKIN_NOTES` 说明真相；
**新增额度透出** —— `_u1s1_usage` 现在解析 `packages[]`，按 `kind` 归组求和
（照官方 CLI `groupPackages` 口径），输出「登录打卡包 ×N」「邀请赠送包」等条目
（unit=tokens、组内取最晚到期），用户终于能在页面上看到打卡包还剩多少。

### 三、Freebuff（Codebuff 免费层）渠道接入

用户要求融入 `github.com/pingmike2/freebuff2api-wokers`。**双源交叉核对**：
社区逆向实现（worker.js / server.js / extract_freebuff.py，AGPL-3.0，含 2026-09 实测）
+ **官方仓库 CodebuffAI/freebuff**（free-agents.ts / freebuff-session.ts /
freebuff-streak.ts / freebuff-countries.ts / use-freebuff-streak-query.ts）。

**协议：三段式门控（不是"拿 token 直接调 chat"）**

```
session(开) → agent-runs(主 + context-pruner 子 run) → chat/completions
```

- `POST /api/v1/freebuff/session`（`x-freebuff-model`）→ `instanceId`；
  一个 session 约 1 小时，**创建时按模型单价扣 Freebucks**，复用不扣 → 必须缓存
- `POST /api/v1/agent-runs {action:START, agentId}` 拿 `runId`（chat 校验其存在）
- `POST /api/v1/chat/completions`，**上游强制流式**（`stream` 恒 true）

**三条「不说就静默降级」的硬约束**（官方源码依据，全部有测试锁死）：

1. **Buffy 前缀**：system 必须以 `You are Buffy, the strategic coding assistant.`
   **字节级开头**（`hasFreebuffRootSystemPromptOpening` 校验）；旧的
   `[System Override…]` 绕过已被官方修补为 403 `free_mode_cli_required`。
2. **外域客户端指纹**（官方 2026-09-17/18 两轮升级）：tools 出现 Claude Code/Codex/
   OpenClaw/opencode 的**精确工具名**、或 system 命中 harness 身份短语 → **静默降级到
   `inclusionai/ling-3.0-tiny:free`**（不报错、只换模型）。对策：工具名统一加
   `mcp__` 前缀（官方认可 `server__tool` 形态，且 `isUnrecognisedToolName` 把含 `__`
   的名字排除在观测名单外）+ 注入官方 `decide` 作为 genuine 签名工具 + harness 短语
   等义替换。响应侧剥前缀还原，客户端无感。
3. **`codebuff_metadata`**：必须带 `run_id` / `client_id` / `cost_mode:"free"` /
   `trace_session_id`（同对话跨轮复用，官方从 previousRun 取）。

**国别分层（官方 `freebuff-countries.ts`，2026-09-12 实测）**：tier1 US；
tier2 CA/GB/AU/NZ/IE/NO/SE/DK/FI/NL/AT/LU/IS；tier3 DE/FR/ES/IT/PT/BE/CH/LI/MT/KR
—— 以上 full access；**其余国家（含 CN/SG/JP）与任何 VPN/代理出口一律 limited access**
（模型目录缩减，仍可用）。生产服务器出口 CN、代理出口 SG/JP → 该渠道在 AIGate 上
默认 limited access，**这是上游策略不是接入 bug**（已在注册表注释与适配器 docstring 写明）。

**Freebucks 计费**：按次扣费的**每日钱包**（不是「每模型每天 N 次」白名单）。
两个 limit 并存，**只信 GET 快照的 `daily.limit`**（429 响应体的 limit 是当日闸值）。
`accessTier`：`full`=未降级（仅撞通用 cap）；`limited`=**新号默认档**（25/天，与 IP 无关）。

**登录**：CLI 授权码轮询（与官方 CLI 同协议，免装客户端）——
`POST /api/auth/cli/code {fingerprintId}` → `{loginUrl, fingerprintHash}` →
浏览器 Google 登录 → 轮询 `GET /api/auth/cli/status` → `user.authToken`。
**本机实测通过**（生产服务器直连可达：`start_cli_login` 返回真实 loginUrl、
未登录时 poll 正确 pending；在线模型目录拉取 59 个模型）。
authToken 长期有效、无标准刷新（`refresh_style=none`）。

**顺带修掉一个既有 bug**：`_do_refresh` 此前**先检查 `refresh_token_enc` 再分发
`refresh_style`** → `refresh_style=none` 的长期凭证（u1s1、freebuff）永远得到
「no refresh_token stored」的假错误。已把 none 分支前移，并加接线守卫测试锁死顺序。

**签到能力**：`False` —— 官方 streak（`freebuff-streak.ts calculateFreebuffStreak`）
由「当天是否用过模型」推导，**只有 GET 没有领取端点**（官方全仓库仅
`use-freebuff-streak-query.ts` 一处 GET）。即「用一次 = 打卡」，上游自动记录。

测试：Freebuff 38 项 + 接线守卫新增 9 项；全量 **755 项全绿**。

---

## F27 服务商页空白 / 模型页报错：headers 非字符串值（2026-09-27）

**用户报告**：「服务商管理页加载不出来内容，模型页报错」。

### 症状与根因（生产日志实证）

```
pydantic_core._pydantic_core.ValidationError: 1 validation error for ProviderResponse
headers.x-video-timeout
  Input should be a valid string [type=string_type, input_value=1800, input_type=int]
```

- `GET /admin/api/providers` **500 × 13**（access log 实测）→ 服务商页拿不到数据 → 空白。
- 模型页 `Promise.all([api.getProviders(), api.getCombos(), ...])` 中
  `getProviders()` 未 catch → **同一个 500 把整个模型页带崩**。
- 全库排查出 3 行脏数据（均为用户手工导入时写入）：

| id | 服务商 | 脏值 |
|---|---|---|
| 89 | aistudio | `x-video-timeout: 1800` (int) |
| 90 | openi | `x-video-timeout: 3000` (int) |
| 93 | 钱咖生视频 | `x-video-timeout: 1500` (int) |

**写入源头**：`POST /providers/import` 直接吃任意 JSON（`data: Any`），
`provider.headers = entry.get("headers") or {}` **不做类型校验**；而
`ProviderResponse.headers: Optional[Dict[str, str]]` 是 Pydantic v2 ——
v2 **不再**像 v1 那样把 int 强转 str，直接抛错（本地实测确认）。

### 第二现场：推理请求也被打断（比页面 500 更严重）

httpx 硬性要求头值为 `str`/`bytes`：

```
TypeError: Header value must be str or bytes, not <class 'int'>
```

生产 `request_logs` 实测 aistudio 出现该报错 —— 即这 3 家的**所有推理请求**
（chat / 模型列表 / 健康检查）此前都在失败，只是用户先看到的是页面问题。

### 同类潜在故障（顺带修掉）

`_merge_oauth_headers` 注入的 `__oauth = True`（**bool**）在
codex_responses / github / anthropic 适配器里**未被过滤**（它们只滤了
`__proxy_force` / `__proxy_url`）→ OAuth 类服务商一旦改用这些适配器，
httpx 会抛同样的 TypeError。生产当前 codex 服务商都是 api_key 类型故未触发，
属"尚未引爆的同类雷"。修复方式：不再让各适配器各维护一份过滤名单（漏一个键
就是一类故障），统一走 `outbound_headers()`，约定 **`__` 前缀 = 网关内部键，
一律不出站**。

### 修复：四层防御（入库 / 出库 / 存量 / 出站）

| 层 | 位置 | 行为 |
|---|---|---|
| ① ORM 读写 | `models/provider.py::HeaderJSON` + `@validates` | 任何写入路径（API/导入/恢复/脚本）存不进非 str；读取历史脏数据自动净化。`@validates` 覆盖"赋值后同会话立即读"的事故时序 |
| ② schema | `schemas/provider.py::_HeaderNormalizeMixin` | 写入侧（Create/Update）**严格**：嵌套值 422 明确报错；读取侧（Response）**宽松**：丢弃脏值，绝不整体 500 |
| ③ 存量 | `db.py::_repair_dirty_provider_headers` | 启动时幂等刷净老库；**不改 `updated_at`**（修复不该伪装成用户编辑）；走 Core update 保证 SQLite/PG 双方言安全 |
| ④ 出站 | `core/header_values.py::outbound_headers` | 6 个适配器统一调用：剥离 `__` 内部键 + 值强制 str |

**前端韧性**（避免"一个接口挂全页崩"重演）：
- 模型页：辅助数据（providers/combos）失败 → 显示可重试横幅，**模型主列表照常加载**；
- 服务商页：此前 **无 try/catch**（mounted 里未捕获 rejection → 整页空白无提示），
  现显式报错 + 重试按钮。

### 测试

新增 `tests/test_header_values.py` **28 项**：归一化函数（标量/None/嵌套/严格模式）、
出站净化（含"未净化的 int 头确实会被 httpx 拒绝"的反证）、ORM 三层
（写后 refresh / 赋值即读 / 历史脏行读取）、schema 两侧、
启动修复（幂等 + `updated_at` 不变 + 源码守卫）、
**端点级复现**（脏行存在时 `list_providers` 仍返回 200）、
6 个适配器的出站头 httpx 可用性。全量 **783 项全绿**。

## F28 CodeBuddy 多账号 token 串号 + Freebuff 档位闸门（2026-09-28）

用户反馈两项：① 「codebuddy 两个账号的额度咋都是一样的，159 那个账号额度不对」；
② 「freebuff 的模型访问报错」。两项根因独立，均已在生产取证。

### 一、CodeBuddy 两账号额度相同 = 一条连接被另一个账号覆盖

**取证**：生产 `oauth_tokens` 里 row1（owner `15944101987`）与 row6
（owner `13500818840`）的 `access_token_enc` / `refresh_token_enc`
**sha1 完全相同**；JWT payload 显示两条的 `sub` 都是 `86b50828-…`、
`preferred_username` 都是 `13500818840` —— 即两条连接**指向同一个账号**，
额度自然一样，而 159 账号的凭据已被静默丢弃。

**成因**：`POST /admin/oauth/authorize/{code}` 的 owner 在**轮询开始前**就已分配
（`_next_owner_for_provider`：首个 `__default`，其后 `account-N`）；device poll 是
后台协程，用户在**另一个账号**上完成登录时，轮询仍把 token 写回原来分配的 owner
→ 覆盖第一个账号的凭据。时间线吻合：row1 最后一次 refresh 14:32，row6 建立于
23:40（+08），而修复「新增账号不再覆盖主账号」的 commit `2f427b2` 提交于 14:58
—— 修的是**分配规则**，没修**轮询落库**这条路径。

**取证手段**：从 `data/backups/aigate-20260922-033000.db`（覆盖发生前一天的备份）
解出原始凭据，确认身份 `15944101987` 且**仍然有效**（billing 接口 200，43 个额度包
合计 7653 积分）。

**修复（写入侧守卫 + 轮询侧自动改存）**：
| 层 | 位置 | 行为 |
|---|---|---|
| 身份解析 | `oauth_client.token_identity()` | 解 JWT payload 取 `preferred_username`/`email`/`sub`（**不校验签名**，只做「是不是同一账号」比对；非 JWT 返回 None） |
| 写入守卫 | `oauth_client._save_token` | 默认**拒绝**把一条连接改成另一个账号（原凭据原封不动）；`allow_identity_change=True` 是显式替换的逃生口 |
| 轮询改存 | `_poll_codebuddy_token` / `_poll_freebuff_login` | 拿到 token 先比身份，不同 → 自动挪到 `account-N`（与 authorize 入口同规则）→ 一键连接语义从「静默覆盖」变成「自动新增账号」 |
| 导入入口 | `oauth_router.import_oauth_token` | 撞守卫返回 **409** 而不是 500 |
| 刷新路径 | 所有 `update_existing=` 调用 | 不受影响（同账号换新 token 正常放行） |

**线上数据修复**：从 09-22 备份恢复 row1 凭据（逐字节，不拼接不推测），
修复后复核：row1 = `15944101987`（43 包 / 7653 积分）、row6 = `13500818840`
（33 包 / 3548 积分）、**各连接 token 已唯一**。

### 二、Freebuff 模型报错 = 上游静默换模型，而 chat 仍带请求的模型名

**用户看到的报错**：
```
HTTP 409 {"error":"session_model_mismatch",
          "message":"Limited free access is only available with GLM 5.3 Flash or
                     DeepSeek V4.1 Flash or MiMo 2.6 Flash or Solar Mini 4 or Solar Pro 4."}
```

**真因（生产实测）**：上游在档位受限时**静默替换 session 模型** —— POST session
请求 `stealth/space-bunny-alpha`，上游返回 200 且 `status=active`，但 `model` 字段
给的是 `deepseek/deepseek-v4-flash`。而 chat 请求体里仍带**请求的**模型名，上游按
「会话模型 ≠ 请求模型」拒掉，报的正是上面的 409（消息里那串"仅限这些模型"）。
`worker.js` 也记录了同一现象（其 `normalizeSession` 用 `data?.model || requestedModel`）。

**档位真相**：免费层按出口国别分层（US=full，其余=limited）。limited 档的放行集合
就是 `GET /session` 快照里 `rateLimitsByModel` 的键（实测 CN 出口 5 个：
`deepseek/deepseek-v4-flash`、`mimo/mimo-v2.5`、`upstage/solar-mini4`、
`upstage/solar-pro4`、`z-ai/glm-5.3-flash`），与上游文案一一对应。

**修复**：
| 问题 | 修复 |
|---|---|
| chat 用请求模型（真凶） | 改用 session **实际授予**的模型（run 链同步）；替换时记 warning，如实透出 |
| 409 一律当「session 脏」 | 分开：档位拒绝 → 直接终止 + **DELETE 释放单会话锁**（不再清缓存重建 —— POST session 是**扣费**的，实测白扣 10 Freebucks）；真 stale（superseded 等）→ 仍重建重试一次 |
| 409 错误码被盖成 `session_model_mismatch` | 如实回显上游真实码（实测有 `session_superseded` / `model_locked`），否则用户会去改模型而不是重试 |
| 单会话锁冲突无恢复 | 照 worker.js 补「GET 拿持锁 instanceId → DELETE → 再 POST」**只重试一次**（`current_session_instance()` 只在释放锁时调用 —— 该 GET 本身会占用 session，为查额度随手调会顶掉正在进行的 chat） |
| 报错不可执行 | 转成「指出哪个模型 + 给出实测放行清单 + 给换出口这条路」，保留上游原文 |
| 模型页看不出能用谁 | 放行清单在**已有的**额度查询/健康探测里顺手缓存（**零额外请求**），模型页把清单外模型标「当前档位不可用」但**不过滤**（出口换区/升档后即可用，失败由上游如实报错）；额度面板新增 note 行直接列 id |

**验证**：档位拒绝时余额不变（5 → 5，确认不再白扣）；放行模型 `upstage/solar-mini4`
可正常建 session。⚠️ 探测过程用尽该账号当日 Freebucks（daily 25/25，Pacific 日界重置）。

### 测试

新增 **25 项**（808 全绿）：
- freebuff 13 项：档位/脏 session 语义区分、不白重建、释放锁、真 stale 仍重试、
  **上游替换模型时 chat 用授予模型**（+反向对照）、409 回显真实码、
  单会话锁释放后重试、标注但不过滤、未知不标注、健康探测顺带记清单且绝不 POST
- oauth 12 项：JWT 身份解析、跨账号拒绝覆盖、同账号续期放行、非 JWT 不参与、
  逃生口、owner 分配（含空洞复用）、轮询自动改存（正反两向）、导入 409、源码守卫

## F29 分析页缓存命中率腰斩 = 上游占位键骗过方言判定，prompt 被重复叠加缓存读（2026-09-28）

**用户反馈**：「看下分析页下的请求日志，缓存都不到一半，这对吗？」

**结论：不对。真实命中率是 ~98.8%，面板显示的 49.7% 是统计口径 bug —— 而且不是
缓存策略问题，是 prompt_tokens 被多算了一倍。**

### 取证（生产原始报文，逐行可复算）

从响应体 blob 里解出 CodeBuddy 的原始 usage（`id=33120`，Intl）：

```json
{"prompt_tokens": 75014, "completion_tokens": 3803, "total_tokens": 78817,
 "prompt_tokens_details": {"cached_tokens": 74112},
 "prompt_cache_hit_tokens": 74112, "prompt_cache_miss_tokens": 902,
 "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0,
 "prompt_cache_write_tokens": 0, "cached_tokens": 0}
```

三处自洽证据：
1. **`prompt_cache_hit_tokens + prompt_cache_miss_tokens = 74112 + 902 = 75014 = prompt_tokens`**
   → 上游口径明确是「prompt 已含缓存」（OpenAI 语义），命中率 = 74112/75014 = **98.8%**。
2. `prompt_tokens_details.cached_tokens = 74112`，与 `prompt_cache_hit_tokens` 一致。
3. 但 AIGate 日志存的是 `prompt_tokens=149126 = 75014 + 74112` —— **缓存读被加了两遍**。

### 根因：方言判定按「键是否存在」，被恒为 0 的占位键骗了

`server/core/usage_normalize.py` 的旧判定：

```python
if "cache_read_input_tokens" in usage or "cache_creation_input_tokens" in usage or "cache_creation" in usage:
    nu.source = "anthropic"
    nu.prompt_tokens += nu.cache_read_tokens + nu.cache_write_tokens   # ← Anthropic 口径修正
```

Anthropic 的 `input_tokens` **不含**缓存，所以合并是对的；但 CodeBuddy 的报文
**同时**带 `prompt_tokens`（已含缓存）和 `cache_read_input_tokens: 0` /
`cache_creation_input_tokens: 0` 这两个**恒为 0 的占位键**（上游 SDK 模板残留）。
旧判定只看键名 → 误入 Anthropic 分支 → 把已含在 prompt 里的缓存读又加一遍。

**影响面（全量核对）**：
| 项 | 值 |
|---|---|
| 受影响服务商 | 仅 CodeBuddy 两家（Intl / CN）——其余 Qoder/基元律动/烁公益站/sensenova/微信等全是标准 OpenAI 形状，未受影响 |
| 受影响行数 | **6303 行**（2026-09-22 09:27 起，此前该上游不返回这些键） |
| 精确性 | 归档 3792 行 + 备份库 5970 行逐行核对 `stored == raw + cache_read`，**反例 0** |
| 报表值 → 真值 | prompt 合计 2,490,615,470 → 1,252,674,990；命中率 49.6% → ~98.6% |

### 修复（两处，都是「按语义判，不按键名判」）

1. **方言判定改用主计数字段**：Anthropic API **从不返回 `prompt_tokens`**，反之带
   `prompt_tokens` 的报文一律是 OpenAI 口径（prompt 已含缓存）。判据改为
   「有 prompt_tokens → openai_chat / openai_responses（看有无 `*_details`）；
   只有 input_tokens 且带 Anthropic 缓存键 → anthropic 并合并」。
   另补「显式 Responses 明细结构优先」分支，避免带占位键的 Responses 报文被误判。
2. **补齐 DeepSeek/CodeBuddy 风格缓存字段**：新增 `prompt_cache_hit_tokens`
   （缓存读）与 `prompt_cache_write_tokens`（缓存写）识别 —— 旧逻辑完全不认这两个
   字段，即使不误判方言，命中数也会丢（CodeBuddy 报文里 `cached_tokens` 在外层
   是 0，真值只在 `prompt_cache_hit_tokens`）。

### 历史数据修复

`scripts/repair_prompt_double_count.py`（默认干跑，`--apply` 落库）：三处来源
（live blob → 归档 jsonl.gz → 备份库）合并取原始 usage，**三条判据同时成立才修**
（取到 raw_prompt + 报文带 Anthropic 占位键特征 + `stored == raw + cache_read`），
UPDATE 带原值守卫（幂等、可重复执行），落库前写回滚凭据（id/旧值/新值）。
干跑结果：**6303/6303 全部可证，跳过 0，异常差值 0**。

### 测试

新增 **6 项**（全量 **814 项全绿**）：占位键不叠加（复现生产报文）、真 Anthropic
仍合并、残缺 Anthropic 仍合并、DeepSeek 拆分字段识别、缓存写字段、Responses
带占位键不误判。

## F30 Freebuff glm-5.3-flash 报 500 = 网关异常处理缺口 + 「在档位≠有额度」（2026-09-28）

**用户反馈**：选 `Freebuff (Codebuff 免费层)/z-ai/glm-5.3-flash` 报
`HTTP 500: Internal Server Error`。

**两个独立问题叠在一起**：

### 一、网关异常处理缺口（把可读原因吞成 500）

`playground_chat` 的适配器调用点**只捕获 httpx 三类异常**：

```python
except (httpx.HTTPStatusError, httpx.TimeoutException, httpx.ConnectError) as e:
```

而 freebuff 适配器抛的是**业务异常**（`SessionError`）——不在捕获列表里 →
穿透出 endpoint → FastAPI 兜底成 **500 Internal Server Error**。真实原因
（`Freebucks 额度已用尽`）只留在服务端日志里，前端什么都看不到。

对照 `/v1`：同一位置是 `except Exception` 宽捕获 → 503 + 可读文案。
**两条路径口径不一致**，playground 是漏网的那条。

**修复**：非流式与流式两条路径都补宽捕获（对齐 `/v1`）——
非流式 → 503 + `upstream_call_failed: <类型>: <原因>`；
流式 → SSE `error` 事件（**不再让异常穿透生成器**，否则前端只看到流中断）。

### 二、「在档位里」≠「现在有额度」

`rateLimitsByModel` 的**键**是「本档位包含的模型」，但**不保证有额度**。
生产实测该账号：

| 模型 | pool | limit |
|---|---|---|
| deepseek/deepseek-v4-flash / mimo-v2.5 / solar-mini4 / solar-pro4 | `limited` | **6** |
| **z-ai/glm-5.3-flash** | `glm`（Reward 奖励池） | **0** |

旧标注只判「在不在键里」→ glm 被当成可用，用户点了才撞墙。而实际调用时
上游回的是 **429**（不是档位拒绝）：

```json
{"status":"rate_limited","pool":"freebucks","poolLabel":"Freebucks","limit":25,
 "recentCount":25,"resetAt":"2026-09-28T07:00:00.000Z","retryAfterMs":1493705,
 "freebucksShortfall":{"price":5,"balance":0}}
```

同时该账号当日 Freebucks 确实用尽（`daily: 25/25, remaining 0`；wallet 0）。

**修复**：新增 `quota_backed_model_ids()`（按 `limit > 0` 过滤）与
`known_quota_backed()` 缓存（与 `known_entitlement` 成对，仍**只标注不过滤**）；
模型页对「档位内但无额度」的模型标「**当前无额度**」（与「档位不可用」区分）；
额度面板 note 补一句「其中 … 当前额度为 0（奖励池），要等发放或升档」。

### 三、顺带修掉残缺错误文案

上游 429 响应体**没有 `message` 字段**（实测），而旧代码是
`f"Freebucks 额度已用尽：{data.get('message') or ''}"` → 文案变成
**「Freebucks 额度已用尽：」后面空着**（日志里可见）。新增
`quota_exhausted_message()` 从结构化字段拼：「（本次需 5、余额 0；约 24 分钟后重置）」。

### 测试

新增 **10 项**（全量 **824 项全绿**）：
- `tests/test_playground_errors.py` 5 项：非流式业务异常 → 503 可读文案、
  流式业务异常 → SSE error + [DONE]、httpx 两条原路径不回归、源码守卫
  （**已验证：撤掉修复后其中 3 项立刻失败**）
- `tests/test_freebuff.py` +5 项：limit=0 与档位内可区分、模型页分开标注、
  429 文案用结构化字段、create_session 的 429 文案可执行、额度面板 note 分述

## F31 CodeBuddy 倍率大面积缺失 = 在线源一直存在但没接入（2026-09-28）

**用户反馈**：「codebuddy 有很多模型的价格还是没显示倍率，啥情况？」

**根因：上游一直有在线倍率源，但代码只读一张 6 条的手工表。**

生产实测（`GET {base}/v3/config`，CodeBuddy 标准头）：

| 版本 | `data.models[]` | 带 `credits` |
|---|---|---|
| CN (`copilot.tencent.com`) | 31 条 | **31 条全带** |
| Intl (`www.codebuddy.ai`) | 22 条 | 21 条 |

`models[].credits` 形态：`"x0.17 credits"` / `"x0.00"` / `"x2.00 credits"`。
而 AIGate 的 `STATIC_PRICE_RATIOS` 只有 CN 6 条 / Intl 3 条（来自 2026-08 的
客户端产物 `product.json` + IDE 日志），于是库里 **CN 27 个模型只有 6 个有倍率、
Intl 29 个只有 3 个**。

**另一处实测坑（决定成败）**：Intl 的返回集**由 UA 决定**：

| User-Agent | models[] | 带 credits |
|---|---|---|
| `CLI/2.108.1 CodeBuddy/2.108.1` | **22** | **21** |
| `IDE/2.108.1 CodeBuddy/2.108.1`（原注册表配置） | 13 | 7 |
| `CodeBuddy/2.63.2` | 0 | 0 |

→ 必须固定用 CLI UA（两个域名都验过）。

**修复**：新增 `fetch_codebuddy_ratios()`（`server/core/model_catalog.py`）——
刷新时读 `/v3/config` 的 `models[].credits`，**在线值优先覆盖**静态表；任何失败
（非 200 / 网络异常 / 解析不出）返回空表，**保持既有值不动**（绝不因一次抖动清空倍率）。
静态表降级为「在线拿不到时的兜底」并按在线值校准（`hy4-preview` 0.00 → **0.29**、
`deepseek-v4-flash` 0.08 → **0.17**、`pro` 0.16 → **0.51** —— 旧客户端值已过期）。

**⚠️ 一个必须守住的设计约束**：在线结果**只用于叠加倍率，绝不用它替换模型列表**。
实测若把 `/v3/config` 的 `models[]` 当权威清单，CN/Intl 各会**误删 8 个模型**：

```
CN   : auto, balanced-model, deep-model, fast-model, hy3-preview,
       hy3-preview-agent, kimi-k3, minimax-m3-pay
Intl : auto, glm-5.1, glm-5v-turbo, gpt-5.3-codex, hy4-preview-f,
       kimi-k2.5, kimi-k2.7, minimax-m3
```

（这些档位别名不在 `models[]` 里但**确实可用**）—— 对齐 Jet-Hub 教训
「把某一刻的快照当判据会让后人误删可用模型」。测试里加了源码守卫锁死这一点。

**预期效果**（生产 dry-run）：

| 版本 | 在线倍率 | 可覆盖 | 新填 | 修正过期值 |
|---|---|---|---|---|
| CN | 29 条 | 18 | **13** | 3 |
| Intl | 21 条 | 20 | **18** | 1 |

前端来源标签从「客户端实测」改为「在线倍率」（tooltip 写明 `/v3/config`
的 `models[].credits`）。

### 测试

新增 **13 项**（全量 **837 项全绿**）：credits 形态解析（含 `x0.00`/`0.50x`/
不可解析必须 None 而非 0）、非 200 / 网络异常返回空表、base_url 带路径要裁剪到
host、**必须用 CLI UA**、**叠加而非替换**（含源码守卫，已反向验证：注入
「用在线列表替换清单」的错误实现后立刻失败）、只读（不得 POST）。

## F32 WorkDaddy 功能调研：可迁移到 AIGate 的能力（2026-09-28）

用户提供 https://github.com/babygoton/WorkDaddy 问「还有什么旅行啥的功能，
调研一下还有哪些功能可以迁移优化的」。完整报告见
**`docs/findings-workdaddy-features.md`**，此处只记结论。

**WorkDaddy 是什么**：WorkBuddy 桌面端增强工具（CDP 注入，不改官方包），
AGPL-3.0，56 个脚本 / 约 44k 行。功能横跨账号备份、主题、会话迁移/分支、
自动化引擎、防休眠、免打扰弹窗等。

**关键判断**：绝大多数功能是**桌面端专有**（CDP 注入 / DOM / 本机文件），
但其中「账号权益」子集是**纯 HTTP API**，AIGate 可直接迁移 —— 实测对现有
两个 CodeBuddy 账号**全部可用**（同后端，见 F24：workbuddy.cn ≡ codebuddy.cn）。

**可迁移的三块**（按价值）：

| 功能 | 端点 | 实测 |
|---|---|---|
| **Buddy 旅行**（用户问的） | `buddy/travel/{config,status,depart,claim}` + `buddy/{info,list,switch}` | ✅ 159 账号**正在旅行**（星际喵 SR → 咖啡馆 3h） |
| **成长任务** | `GET /v2/activity/growth/tasks` + `POST .../accept` | ✅ **19 个任务，1350 credits + 45 energy 未领** |
| **连续活跃/阶梯** | `GET /activity/growth/streak` | ✅ 7/14/28 天阶梯（最高 150 credits + 5 energy + 补签卡） |

**⚠️ 一个必须照抄的约束**：WorkDaddy 源码里有 `AUTOMATABLE_TASK_CODES` 白名单 ——
只有 14 个任务码**能在客户端外完成**，其余（如「关注公众号」「体验公益专家」）
需要真实点击，自动化必失败。所以迁移时的正确做法是「**自动接取白名单内的 +
把其余列出来提示用户手动做**」，而不是全部包办 —— 正是 AIGate 一贯的
「如实标注不过滤」哲学。

**顺手可抄的两个防御**（低成本高收益，P3）：
1. `checkin-result.js` 的 **JWT iss → 域名映射 + 多域名兜底**（上游换域名时
   不至于整站失效；实测 4 个域名全部 200）
2. **严格成功判据**：`code=0 且 httpOk`，或 `code=10001 且文案明确表示已签到`
   —— 现有实现没校验 10001 的文案，若上游用同一码表示「活动未开启」会误判为已领

**不可迁移**（桌面专有）：账号备份切换、主题壁纸、暂存提示词/快捷短语、
会话迁移与分支、免打扰弹窗、防休眠、异常续接、自动化引擎、
Token 用量统计（扫本机 JSONL）、模型限流解封时间（抓客户端网络响应）。

**建议优先级**：P0 Buddy 旅行（用户问到，纯 HTTP，状态机简单）→
P1 成长任务（积分大头）→ P2 连续活跃/盲盒/抽奖（先只读展示）→
P3 签到防御增强。

**Intl 版本无此活动**（实测端点全 200 但数据为空：`buddy:null`、`buddies:[]`、
`travel.config:{}`）→ 按 provider 分别登记能力，如实显示「本版无此活动」。

## F33 CodeBuddy 成长中心接入（Buddy 旅行 + 成长任务，2026-09-28）

用户要求把 WorkDaddy 的功能「开搞」。调研结论见
`docs/findings-workdaddy-features.md`（F32）—— 这块是**纯 HTTP API**，与桌面端无关。

### 落地内容

| 层 | 文件 | 说明 |
|---|---|---|
| 协议 | `core/codebuddy_growth.py` | 只读快照 + 旅行（depart/claim/run）+ 任务接取 |
| 数据 | `models/growth_log.py` | 操作日志（traveled/claimed/accepted/already/inactive/failed） |
| 配置 | `config.GrowthConfig` | enabled / travel / tasks / hour=11 / minute=0 / startup_catchup |
| 调度 | `main.py` | 每日 **11:00 北京时间**（与签到 10:30 错开）+ 启动补跑 + 即时重排 |
| API | `api/checkin_router.py` | `GET /checkin/growth`、`POST .../run`、`GET/PUT .../config` |
| 前端 | `views/Checkin.vue` | 签到页新增「成长中心」区块（旅行/Buddy/任务/活跃/任务明细） |

### 三条设计约束（照抄上游，不发明）

1. **只自动做「客户端外能完成」的事**：`AUTOMATABLE_TASK_CODES` 与 WorkDaddy
   源码**逐字一致**（14 个码）。白名单外的（关注公众号、体验公益专家等）需要
   真实点击，自动化必失败 → **如实列出提示手动做**，不硬做。
2. **严格串行 + 按天幂等**：同账号请求串行（上游对高频敏感）；每个动作当天
   只做一次，`failed` 不算（必须允许重试）。
3. **只读优先、失败不编造**：`fetch_snapshot` 只发 GET；拿不到就如实标不可用。

### 生产实测（部署即生效）

启动补跑自动执行，`growth_logs` 落库：

```
[09:26:21] codebuddy_cn/15944101987 travel/already   旅行进行中（等待到达）
[09:26:22] codebuddy_cn/15944101987 task/accepted    已接取 3 个任务
[09:26:23] codebuddy_cn/13500818840 travel/traveled  已派出旅行
[09:26:24] codebuddy_cn/13500818840 task/accepted    已接取 3 个任务
[09:26:24] codebuddy_intl/__default travel/inactive  本版无旅行活动（上游返回空配置）
[09:26:25] codebuddy_intl/__default task/already     没有待接取的可自动化任务
```

- 159 账号旅行本就在进行中 → **正确跳过**（不重复派发）；135 账号 idle → **派发成功**
- 两个 CN 账号各接取 3 个任务；Intl 如实报「本版无活动」
- **幂等复验**：再跑一次 6 项全部 `already 今天已处理过，跳过`，**0 次上游写请求**

### 一个排查教训

部署时我用 `git checkout -- <文件>` 清理手拷的未跟踪副本，结果**把 pull 覆盖了**
（自检仍显示旧版本 `v1e86c3d`）。正确做法是 `rm -f` 未跟踪文件再 pull ——
`git checkout --` 对**已跟踪**文件是「丢弃改动」，在这里是误用。

### 测试

新增 **38 项**（全量 **875 项全绿**）：白名单（逐字核对 WorkDaddy）、`instance_id`
纯数字校验、任务码注入防护、旅行状态机（arrived→claim / idle→depart /
traveling→不动 / 无 Buddy→跳过 / 达上限→跳过）、快照只读性、批量串行（
`maxInFlight==1`）、单账号失败不中断、动作开关、按天幂等（含 `failed` 允许重试）、
配置默认值与调度接线守卫、端点注册。
**已反向验证**：注入「忽略白名单」与「traveling 重复派发」两个错误实现后，
对应测试立刻失败。
