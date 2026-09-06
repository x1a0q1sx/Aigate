# v0.2 任务跟踪
- [x] v0.1 基础（路由 + Auto）
- [x] v0.1 运行时 fallback（patch v1_router.py + mark_cooling 循环）
- [x] v0.2 扩展方案设计文档
- [ ] v0.2 实施（开干中）
## v0.2 实施子项
- [ ] 探查现状（ORM / 迁移方式 / Admin API）
- [ ] 数据库迁移 SQL + 静态智力种子
- [ ] 5 张表 ORM + 1 字段
- [ ] RequestLoggingMiddleware
- [ ] RankingService
- [ ] AutoRouter 改造：用 RankingService
- [ ] Admin API：10 个新接口
- [ ] Admin UI：4 个新 Tab + 干预面板
- [ ] e2e + 启动验证（含 v0.1 fallback）
# 下一阶段优化路线（2026-09 起，当前维护入口）

> 状态：[ ] 待办 / [~] 进行中 / [x] 完成 / [!] 阻塞。根因详情见 docs/findings.md 对应章节。
> 旧 v0.2/v3 章节为历史记录，以本表为准。

## P0 — 稳定性与正确性

### P0-1 Fallback 审计修复（✅ 2026-09 完成，commit 2fc6a31，findings §F1–F8）
- [x] D+E 统一凭证解析器：free_tier/oauth/atomcode/标准密钥一个入口，combo 与 auto cascade 共用
- [x] B 流式空输出语义：实质 chunk（content/reasoning/tool_calls）出现前缓冲不外发，reasoning 算实质并锁定候选；纯元数据结束才无感回退
- [x] C max_fallbacks 契约统一：combo 流式/非流式对齐 auto（总尝试 = min(候选数, max_fallbacks+1)）
- [x] F Playground 流式复用统一 cascade（probe 成功 ≠ 真实请求成功）
- [x] G 终态错误协议：Responses/Anthropic SSE 包装检测 chunk 顶层 error，不再伪装成正常空响应
- [x] H Fusion 策略明确禁用（501 + 前端标注"实验中"），不再伪造空响应
- [x] A 删除死代码 auto_router.route_with_fallback（不可达且无调用者）
- 验收：新增回退测试 ≥6 项全绿；首候选失败→次候选成功；reasoning-only 不再拼接跨候选正文

### P0-2 模型刷新 / Auto 候选 N+1 批量化（✅ 2026-09 完成，commit 10e58be/aa69660）
- [x] get_auto_candidates 逐模型查 ApiKey → 一次预加载映射（model_catalog.py:125-153）
- [x] refresh_models_from_provider 循环内 select/commit → 批量 upsert 单事务（model_catalog.py:386-510）
- 验收：1782 模型下刷新耗时下降 ≥50%，SQL 数量从 O(模型×密钥) 降为 O(1) 组查询

### P0-3 SQLite 写入队列化（✅ 2026-09 完成，commit 280489d；压测 20 并发 0 locked）
- [x] 请求日志改 asyncio.Queue 后台批量 commit（50 条/500ms 阈值），请求路径不再等日志落库
- [x] WAL checkpoint 定时任务；写入失败重试与丢弃计数
- 验收：压测 50 并发下无 database is locked；请求 P95 不受日志写入影响

## P1 — 数据质量与性能

- [x] P1-4 usage 统一归一化（✅ 2026-09 完成，commit 5be9499）：NormalizedUsage 三方言字段驱动提取，Anthropic 缓存口径并入 prompt，v1 五处提取链 + anthropic 适配器统一
- [x] P1-5 上下文估算校准（✅ 2026-09 完成，commit 62b8748）：est_prompt_tokens 落日志、get_estimate_factor 动态系数（TTL 缓存+钳制）、observed_context_limit 超限学习收紧预检窗口、4 处预检全部生效
- [x] P1-6 元数据来源分层（✅ 2026-09 完成，commit 8797b73）：context_source/capability_source 列 + manual 标记（刷新/OpenRouter 回填永不覆盖）+ ModelInfoResponse 暴露
- [ ] P1-7 评分查询聚合化：RankingService 批量 GROUP BY + 30s TTL 内存缓存
- [x] P1-8 日志脱敏 + 归档状态展示（✅ 2026-09 完成，commit 47fe63f）：redact_text 覆盖 Bearer/sk-/wk-/KV 形态（blob 与回退原文两路）、最近归档状态入 /logs/archives 与前端展示；归档行级索引暂缓（无单条恢复消费方）

## P2 — 体验与扩展

- [x] P2-9 协议 fixture 测试（✅ 2026-09 完成，commit b678c74）：三协议面完整 SSE 契约样例（tests/test_protocol_fixes.py），抓到并修复 3 个真实 bug（/v1/messages 流式缺 await 全坏、message_start 被 ping 抢首、非流式块序错误）
- [x] P2-10 代理池完善（✅ 2026-09 完成）：成功/失败累计计数 + 快照字段（ok_count/err_total/last_used_ts）+ request_with_fallback 成功路径标记；get_proxy_pool 读 config 与 per-provider 强制直连/代理已有（init_proxy_pool/proxy_enabled）；目标站探测经评估不加——第三方站点无法判断真实上游可达性，会误杀可用代理（代码注释有据）
- [ ] P2-11 PostgreSQL 支持（DATABASE_URL 切换，SQLite 保持单机默认）
- [ ] P2-12 压测：10/50/100 并发聊天，观察 P95 首字、locked 次数、WAL 增长
