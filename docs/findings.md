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
