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

### P0-3 SQLite 写入队列化
- [ ] 请求日志改 asyncio.Queue 后台批量 commit（50 条/500ms 阈值），请求路径不再等日志落库
- [ ] WAL checkpoint 定时任务；写入失败重试与丢弃计数
- 验收：压测 50 并发下无 database is locked；请求 P95 不受日志写入影响

## P1 — 数据质量与性能

- [ ] P1-4 usage 统一归一化：NormalizedUsage(prompt/completion/cache_read/cache_write/reasoning/usage_source)，三个协议面共用
- [ ] P1-5 上下文估算校准：按模型历史 usage 估算系数 + 上游"内容超限"错误学习（记录 provider 实际可用窗口）
- [ ] P1-6 元数据来源分层：price/context/capability 各自 source 字段（manual > provider > public > default）
- [ ] P1-7 评分查询聚合化：RankingService 批量 GROUP BY + 30s TTL 内存缓存
- [ ] P1-8 日志脱敏（Authorization/x-api-key/api_key 写入前打码）+ 归档索引 + 归档状态展示

## P2 — 体验与扩展

- [ ] P2-9 协议 fixture 测试：Codex Responses / Anthropic Messages / OpenAI Chat 的 SSE 事件序列固定样例回归
- [ ] P2-10 代理池：get_proxy_pool 读 config、目标站探测（HF/OpenRouter/GitHub）、成功率展示、per-provider 强制直连/代理
- [ ] P2-11 PostgreSQL 支持（DATABASE_URL 切换，SQLite 保持单机默认）
- [ ] P2-12 压测：10/50/100 并发聊天，观察 P95 首字、locked 次数、WAL 增长
