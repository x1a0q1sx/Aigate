# 隐藏 Bug 猎捕审计（2026-09-18）

方法：requesting-code-review skill → 5 个只读审查员并行分区审计（路由核心 / 日志计量 / 认证OAuth / 适配器协议 / 管理端元数据），主 agent 对每条 P0/P1 回读源码复核。行号以 HEAD=469e6b2 为准。

状态勾选：`[ ]` 未修 · `[x]` 已修

---

## P0 — 主链路必坏（5 条，全部复核实锤）

### P0-1 `stream_via` 漏 await，免费/OAuth/atomcode 流式候选 100% 失败且污染健康状态
- `server/core/credential_resolver.py:113`（async def 无 yield，返回协程）；调用点 `server/api/v1_router.py:1578`（combo 流式 free_tier）、`v1_router.py:2124`（auto 级联流式 free/oauth/atomcode）均未 await → `anext()`/`async for` 抛 TypeError → 走失败分支 mark_failure+mark_cooling，把健康连接误判故障并指数冷却，跨请求污染 auto 选举池。
- 修法：两处 `gen = await stream_via(...)`。
- [x]

### P0-2 OAuth 浏览器回调被 AuthMiddleware 拦死（默认 auth.enabled=true 下全断）
- `server/core/auth.py:24-31` 豁免集无 `/admin/oauth/callback`；中间件对未带 session 的 `/admin*` 返回 index.html 200（auth.py:105-108），而回调是 provider 浏览器顶层导航、不可能带 Authorization 头 → code 永远到不了 `exchange_code_for_token`。claude_code/codex/github_copilot/antigravity/cursor/cline 的 authorization_code 流在默认部署形态全部静默失效（device_poll/u1s1/手动导入不受影响）。
- 修法：回调路径加入精确豁免（真实防线是 PKCE/state），且需配套 P2-x state 一次性消费校验。
- [x]

### P0-3 响应缓存命中绕过网关鉴权/预算/日志
- `v1_router.py:1224-1228`：`response_cache.get()` 在 `verify_aigate_api_key`（impl 内 1248 行）之前，中间件又放行 `/v1/`。开启缓存后（config.response_cache 默认 False），未授权客户端重放相同请求体即可取回他人补全，且绕过网关 key RPM/预算/落库。附带：错误体 status 200 也可能被缓存（response_cache.py + 1229 只判 status）。
- 修法：缓存查询挪到鉴权之后；命中补 cache-hit 日志；拒缓存 error 体。
- [x]

### P0-4 Anthropic 适配器流式 finish 双发
- `server/adapters/anthropic_adapter.py:493-502` message_delta 已产出终态 chunk（含 tool_calls、finish="tool_calls"），`520-525` 流结束后 `if _produced:` 又无条件补发 `finish_reason:"stop"`。下游 responses_router.py:443 后到覆盖 → Codex 收到 function_call+stop 的错语义；anthropic_converter.py:447 的 tool_use 终态判定被破坏；OpenAI 直连客户端收到同 choice 两个 finish，违反协议。error 分支（516-518）后同样补发 "stop" 伪装成功。
- 修法：`finish_sent` 标志，仅在未产出终态且未发 error 时补发。
- [x]

### P0-5 `/v1/messages` 入站转换：tool_result 映射成 role:"user" + 凭空追加空消息
- `server/core/anthropic_converter.py:106-110`：tool_result 块 append 的消息 role 取原消息的 "user"，应为 "tool"（tool_call_id 挂在 user 上）；blocks 被 continue 清空后又落进 132-133 的兜底 else，多 append 一条空 user 消息。已运行复现。后果：Claude Code 多轮工具循环转 Anthropic 上游时 tool_use/tool_result 配对断裂 → 上游 400，工具链路不可用。
- 修法：tool_result → role:"tool"；已 append 过 tool 消息时跳过兜底。
- [x]

---

## P1 — 特定条件出错（复核实锤）

### 路由/选路
- [x] **P1-1 `sa_select` 未导入 NameError**：`v1_router.py:1893`（provider 全部 key 停用/删光后的兜底分支）→ 500 裸异常而非 503。改 `select`。
- [x] **P1-2 combo 清理误删"临时禁用"的模型**：`server/core/combo_router.py:99-110,187-196` 查询带 `Model.enabled==True`，禁用模型查不到 → 判 stale → `combo.model_ids` 改写落库（124-126），与 146 行"跳过不删"注释矛盾；provider 禁用有豁免、模型禁用没有。另 81-85 行旧格式裸 model_id 直接删，注释承诺的回查未实现。
- [x] **P1-3 401/403 硬熔断永不生效**：`v1_router.py:2585` `getattr(e,"status_code",None)` 在 httpx.HTTPStatusError 上恒 None（状态码在 `.response.status_code`，实测复现）；且 `key_rotator.py:157-176` `_fallback_key` 不查 `_hard_disabled`，拉黑的 key 照样兜底返回。
- [ ] **P1-4 session sticky 整体失效 + 泄漏 + 时区错**：`auto_router.py:452-465` 以 conversation_id 为键，而 v1_router 每请求新造 uuid4（1244）→ 永不可命中，`session_sticky_minutes` 配置形同虚设；`_sticky_cache` 只增不清（427 写、仅命中时清）→ 慢性内存增长；458 行 naive utcnow.timestamp() 按本地时区折算，偏差 -8h。连锁：修好命中后 `get_best_candidate` 221/234/236 的 `return None` 违反调用方 `.success` 契约（v1_router 718/860/2071）→ AttributeError。
- [ ] **P1-5 auto 非流式级联丢 `__proxy_force`/extra_headers**：`v1_router.py:893` 直接用 `candidate.provider.headers`，不 `_merge_oauth_headers`（对照 2113/1739/2311 都有）→ 代理池关闭时"强制走代理"的 provider 裸连出站。
- [ ] **P1-6 request_overrides 流式路径不生效**：combo 流式（1573-75）与 auto 级联（890-91 全无、2114-16 只有 headers）缺 model_alias/body_patch，直连（2329-42）与 combo 非流式（1742-55）齐全 → 依赖 alias 的模型按路径不同行为不一致。
- [ ] **P1-7 fusion "judge" 决策记录被吞**：`fusion.py:207,219` attempt 写字符串 "judge" → `route_decision.py:203` `int()` ValueError → `v1_router.py:1040` 抛出后被 1065 except 吞掉，同 try 的 select+finish 一并跳过，明细永久丢失且不可见。

### 日志/计量
- [x] **P1-8 log_queue 关闭不排空，注释撒谎**：`log_queue.py:185` worker `while not stopped` 退出时不 drain，`254` 注释声称会；`stop_log_queue` 的 `while not q.empty()`（256-258）永不成立 → 每次重启固定白等 8s + 队列里未落库的日志丢失 → pending 行 30 分钟后被 sweep 成 error（虚增错误率、缺 usage）。
- [x] **P1-9 网关每日 token 预算双倍计缓存 token**：`gateway_keys.py:115` `pt+ct+crt+cwt`，但归一化契约 prompt_tokens 已含缓存（usage_normalize.py:20-22，`_segmented_cost` 的 `max(0,pt-crt-cwt)` 反证）→ 设 daily_token_limit 的 key 预算最快 2 倍速耗尽、误伤 429。修法 `pt+ct`。
- [ ] **P1-10 rate_limiter "并发安全插入"是死代码**：`rate_limiter.py:72-84` 用通用 `sqlalchemy.insert` 调 `.on_conflict_do_nothing`（仅方言版有）→ 恒 AttributeError 落 except；同 (model,key) 并发首建时 96-106 的裸 add+commit 抛 IntegrityError 冒穿到请求路径（500）且 session 处于失败态。
- [x] **P1-11 Anthropic 流式 usage 丢 input/cache**：`anthropic_adapter.py:427-432` message_start 的 usage（官方 input_tokens/cache_* 所在）被忽略，delta 只有 output → prompt_tokens=0 走 chars//4 粗估，缓存费恒 0、成本虚高。（与 P0-4 同文件同函数，一并修。）
- [ ] **P1-12 headroom 路由保护从未接线**：`headroom_manager.py:5` 声称 auto_router 会跳过，全仓 `is_in_headroom_cooling` 无调用方 → 额度保留只是展示。
- [x] **P1-13 rank_all 缓存永不命中且无界增长**：`auto_router.py:121-123` 冷却占位填 `datetime.utcnow()`（含微秒）→ `ranking_service.py:360-364` 把值 str 进 cache key，任何模型在冷却时每请求 key 不同、命中率归零；`_rank_cache` 无淘汰（345,489）→ 内存随请求线性涨。健康冷却排除（428）也因此恒 False。

### OAuth/认证
- [ ] **P1-14 刷新 single-flight 假成功 + InvalidStateError 炸穿**：`oauth_client.py:291-296` 合并方丢弃 leader 结果无条件返回成功；waiter 的 `wait_for(15s)` 超时**会 cancel 共享 Future**（asyncio 语义）→ leader 成功路径 `set_result` 抛 InvalidStateError（302）→ except 内 `set_exception` 再抛（305，cancelled future）→ 穿透 refresh_token 到 `v1_router.py:1873`/`credential_resolver.py:64`（均无 try）→ 用户请求 500。修法：leader 持 Task+结果值，等待方读结果；set 前判 cancelled。
- [ ] **P1-15 刷新失败一律永久下线**：`oauth_client.py:343-349`（及 codebuddy 377-390、cline 509-522）对任意 ≥400（含 429/502/超时后的网络异常）置 `is_active=False`，调度器只扫 active（oauth_router.py:212）→ 上游一次抖动=连接判死刑，须人工重登。修法：仅 invalid_grant/401 语义才 deactivate，其余记 last_error+退避。
- [x] **P1-16 更新检查冻结事件循环**：`update_router.py:137` async 处理函数里同步 `subprocess.run(git fetch)`（81-84，无 timeout）+ `_probe_proxy` 同步 TCP 探测 → 远端不可达时挂 1-3 分钟，期间全部 /v1 流量停摆。

### 管理端/元数据
- [ ] **P1-17 手改价格保护标记被刷新抹掉**：`model_catalog.py:406` 算出 manual_priced 后，425 行在 `remote_metadata` 分支内**无条件** `pricing_source = source_url` → 本轮价格没覆盖但标记没了，下轮刷新直接覆盖用户价格。同类：404 行 display_name 无守卫、421 行 success_rate 远程缺字段时以 None 抹掉。
- [ ] **P1-18 智力分手工校准保护永不生效**：`admin_routing.py:735` 创建 IntelligenceStatic 不传 `source`（模型默认 "arena"），`intelligence_sync.py:275` 免覆盖判定要求 `source=="manual"` → 手调分数周一同步必被覆盖。
- [x] **P1-19 Playground 日志路由归属恒 NULL**：`admin_router.py:1665` `_write_log(..., _route_result=route_result)` 默认参数在 def 时冻结为 None，后续对 `route_result` 的赋值不生效（1761/1796/1813）→ routed_provider/model 恒 None、fallback_count 恒 0，ranking 统计丢 playground 归属。
- [ ] **P1-20 delete_provider 清理是死代码 + 关联表孤儿 + rowid 复用继承脏冷却**：`admin_router.py:953-961` 先 DELETE 再 SELECT model ids → 必空，hc 内存缓存一次不清；不清 HealthCheck/RateLimitState/ModelApiKey；SQLite rowid 复用 → 新模型 id 撞旧冷却条目，上线即被 auto 跳过。delete_key（key_manager.py:54-62 / admin_router 1000-1005）同缺 rotator 内存清理与 ModelApiKey 解绑。
- [ ] **P1-21 明文密钥导出/揭示绕过二次鉴权**：`admin_router.py:631-696` export（691-95 明文 key）与 975-980 reveal 仅需会话，未挂 `_require_admin_reauth`（155-173 的威胁模型正是"会话被盗"）→ 被盗会话一个 fetch 拖走全部上游明文密钥（含 refresh token）。

### 适配器/协议
- [ ] **P1-22 Gemini streamGenerateContent 实际不走流式**：`gemini_converter.py:91` 硬编码 stream=False，`gemini_router.py:137-148` 无 stream 分支 → "流"要等全文完成；且 `chunk_to_gemini`（151-171）丢 tool_calls、吞网关终态 error chunk；router 56-98 源流无 finally aclose（anthropic/responses 都修过，此文件漏）。
- [x] **P1-23 openai_compat 流式只认 `data: `（带空格）**：`openai_compat.py:318-324` 与 `github_adapter.py:119-125`，对照聚合路径 223 行用 `startswith("data:")`；无空格上游 → 整条流静默丢弃，直连路径以 `[DONE]` 空成功收尾。

## P2（择要，完整清单见审查记录）
- [ ] auto 级联 OAuth 头缺失同 P1-5；fusion 实为串行（fusion.py:133-151，文档承诺并行）；`_raw_response` 泄漏进客户端错误体（v1_router 2602-03/2698-2703）；rate_limiter 进程内缓存跨 session 复用 ORM（2,36-37,108）；caveman/ponytail 主链路 dict-only 静默无效（caveman_saver.py:72-74 vs v1 pydantic 对象）；ponytail 无角色白名单会删 user 消息近重复段落（ponytail_saver.py:56-64）；log_queue 整批失败重试改纯 insert 丢 update 语义 → 重复行（163-176）；RPD/TPD 永不重置永不生效（rate_limiter 56-59,119-123）；OAuth 非 PKCE provider state 无 CSRF 校验、owner 可控（oauth_client 183-195）；_pending_sessions 以 provider 单键并发互踩（112-113,141-142）；device/u1s1 create_task 不持引用可被 GC（451-452,587-589）；authorize URL 不 urlencode（168）；换票 httpx 异常裸抛 500、拒登回跳 422（oauth_router 158 必填 code）；CORS `*`+AuthMiddleware 外层拦 OPTIONS、auth 关闭时任意网页可跨源读 /providers/export（main.py 219-227）；PG 备份把掩码连接串喂给 pg_dump 必失败（backup_service.py:68 应 render_as_string(hide_password=False)）；InvalidToken 取密链全裸奔（crypto_service 18-20 及 8 处调用）；codex/github/passthrough 未滤 `__oauth` 头 → httpx 非 str 头值 TypeError；codex function_call_arguments.delta 按 call_id 索引收 item_id 事件 → 逐片参数全丢（codex_responses 592-620）；force_stream 聚合不注入 stream_options → usage 恒 0；_collect_stream 无 index 的 tool_calls 拼毒参；n>1/多 choice 流损坏；anthropic 适配器 stop/max_tokens/refusal/thinking 映射缺口（P2-7 全项）；responses 协议 status/输出索引/usage details 语义缺口；探活取 key 与真实路由集合不一致（health_checker 242-254）；`is_cooling` 每调用新开 sqlite3 连接阻塞事件循环（health_checker 104-106 无 negative cache）；内置价表子串匹配错定价（model_catalog 61-63 应最长前缀）；provider 改名静默打断 combos/别名（名字即外键语义）；archive 删除/恢复 filename 未校验（Windows 反斜杠穿越；生产 Linux 影响降级）；batch 接口 int() 非数字 500；notify/test 把 4xx 报成功；速度/稳定性统计跨 provider 同名混算（ranking_service 241-247,268-272）。

## P3（观察项，暂不排期）
- 流式响应客户端正常断开时，每次请求产生 ~2 条 `asyncio.exceptions.CancelledError: Cancelled via cancel scope ... BaseHTTPMiddleware call_next` traceback（AuthMiddleware/ComboPrefixMiddleware 均为 BaseHTTPMiddleware，SSE teardown 时下游 task 取消 + async session 归还被取消，伴随 aiosqlite non-checked-in connection GC 警告）。日志噪音而非功能损坏（成功日志已有 shield 保护，实测落库正常）；若治理可评估纯 ASGI 中间件替换 BaseHTTPMiddleware。
- 主密钥比较未用 compare_digest；login 计时侧信道+无限流；_state_verifier_map 无 TTL；(provider,owner) 非唯一索引可产生僵尸行；expires_in 字符串浮点 ValueError；u1s1 refresh=none 每 60s 假"refreshed"日志；redirect_override 信任 x-forwarded-host 且写全局单例；默认密码不强制首登改；update.py 恢复点明文复制 encryption_key；combo 创建查重非原子；死代码（v1_router 2022 `_combo_targets_map`、704/793 无调用者、text_done_marker）；LIKE 通配未转义；负 offset；ping-all 串行 N×(0.5+10)s；CSV 公式注入；request_logger blob ref_count 泄漏边缘；stop 不 cancel checkpoint_task；context_guard 只计 image_url。

---

## 复核说明
- 五份分区报告中所有 P0 与绝大多数 P1 已由主 agent 回读源码逐条确认（anthropic 双发、tool_result role、stream_via 漏 await、sa_select、回调拦截、缓存绕权、预算双计、on_conflict 死代码、single-flight、combo 误删、manual_priced、closure 快照、path 拼接、死清理均直接对行核实）。
- 两份报告独立撞车于"响应缓存绕鉴权"与"Anthropic message_start usage 丢失"，可信度最高。
- 修复顺序建议：P0-1（一行×2）→ P1-1（一词）→ P0-4/P1-11（同函数一起改）→ P0-5 → P0-2+P1-14/15（OAuth 一批）→ P0-3 → 其余 P1。
