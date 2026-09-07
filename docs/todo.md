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
- [x] P1-7 评分查询聚合化（✅ 2026-09 完成，commit f4a6507）：RankingService 批量 GROUP BY（avg_by_model + statuses_by_model 两条组查询）+ 10s TTL 内存缓存（key 含模型集合+冷却集）+ 聚合排除健康检查行
- [x] P1-8 日志脱敏 + 归档状态展示（✅ 2026-09 完成，commit 47fe63f）：redact_text 覆盖 Bearer/sk-/wk-/KV 形态（blob 与回退原文两路）、最近归档状态入 /logs/archives 与前端展示；归档行级索引暂缓（无单条恢复消费方）

## P2 — 体验与扩展

- [x] P2-9 协议 fixture 测试（✅ 2026-09 完成，commit b678c74）：三协议面完整 SSE 契约样例（tests/test_protocol_fixes.py），抓到并修复 3 个真实 bug（/v1/messages 流式缺 await 全坏、message_start 被 ping 抢首、非流式块序错误）
- [x] P2-10 代理池完善（✅ 2026-09 完成）：成功/失败累计计数 + 快照字段（ok_count/err_total/last_used_ts）+ request_with_fallback 成功路径标记；get_proxy_pool 读 config 与 per-provider 强制直连/代理已有（init_proxy_pool/proxy_enabled）；目标站探测经评估不加——第三方站点无法判断真实上游可达性，会误杀可用代理（代码注释有据）
- [x] P2-11 PostgreSQL 支持（✅ 2026-09 完成，服务器装 PG16 实测验证）：AIGATE_DATABASE_URL 环境变量切换（默认 SQLite 不变）；方言化全部完成——blob upsert 分支、趋势 strftime/to_char、孤儿 GC 改 Python 收集、种子查后插、VACUUM/checkpoint 仅 SQLite、布尔列比较修正；实测 PG 全链路 chat 200 + 日志落 PG
- [x] P2-11a 跨库迁移工具（✅ 2026-09 完成，scripts/migrate_to_pg.py，服务器实测 15693/15693 日志全量迁移）：只读打开源库、ORM create_all 补齐目标表、反射目标列类型驱动三类方言坑转换（JSON 字符串反序列化 / 0→1 布尔 / ISO 字符串→datetime 含剥时区）、FK 依赖序写入、逐批 ON CONFLICT DO NOTHING 幂等可重跑、批量失败降级逐行并报坏行详情、源库孤儿行预分类跳过（PG 迁移实测发现源库遗留 96 条孤儿：rate_limits 56 / health_checks 40，init_db 已加幂等清理）、收尾重置 id 序列 + VACUUM ANALYZE
  - 切换 PG 步骤：低峰停写 → 跑迁移工具（先 --dry-run）→ .env 设 AIGATE_DATABASE_URL → 重启验证；详见脚本 docstring
- [x] P2-12 压测矩阵（✅ 2026-09 完成，scripts/load_test.py 可复用）：10 并发实测 20/20 日志落库完整、0 locked、p50=11s（瓶颈在上游公益站）；50/100 并发脚本参数已支持，建议配快速上游执行

# 体验与流程优化提案 v2（✅ 2026-09 全部完成，commits cbe0236 / 512363b / 95fdf24 / 76a60c9）

> 实施说明：22 项全部落地并在服务器完成端到端冒烟（scripts/smoke_v2.py，15 项检查）。
> 其中 C2 一键升级、B6 暗色模式经核实为既有能力（update_router / App.vue 主题系统），本轮未重做仅核验。
> 冒烟抓到并修复 2 个真实 bug：downstream_key_id 未达 enqueue_log/RequestLogger 落库路径（统一为 apply_downstream_key 注入）；备份列表 Path.glob 生成器拼接 TypeError。

## A. 客户端接入与协议面
- [x] A1 Gemini 原生协议入口（generateContent/streamGenerateContent，复用 converter 层思路）——覆盖 Gemini CLI/LobeChat 类客户端
- [x] A2 /v1/models 增强：combo 别名与思考强度后缀模型进清单，客户端下拉可直接选
- [x] A3 /v1/embeddings、/v1/images 透传路由（轻接口统一计费统计）
- [x] A4 可选响应缓存（请求指纹短 TTL，复用 blob 哈希体系；省公益站额度）

## B. Admin UI 体验
- [x] B1 实时监控仪表盘（SSE live tail 请求流 + 冷却中模型 + 今日成本/TTFT 卡片，复用 log_queue.stats）
- [x] B2 请求失败分析看板（error_type 分组 + 单请求 fallback 链路可视化，routing_decisions 数据已有）
- [x] B3 模型管理批量操作 + 多条件筛选 + 虚拟滚动（1955 模型逐个点太慢）
- [x] B4 一键诊断（模型/组合页内发真实小请求出连通性/TTFT/吞吐报告）
- [x] B5 日志与报表导出 CSV/JSON（按当前筛选条件）
- [x] B6 暗色模式 + 移动端响应式（✅ 核实为既有能力：App.vue 双主题 + NavBar 切换，本轮核验未重做）

## C. 部署与运维流程（开源友好）
- [x] C1 Dockerfile + docker-compose（可选 PG service，一条命令部署）
- [x] C2 管理页一键升级（检查更新→备份 DB→pull→装依赖→构建→重启→失败回滚）
- [x] C3 定时备份策略（每日自动 + 保留 N 份 + 一键恢复；PG 时 pg_dump）
- [x] C4 启动自检摘要 + 页脚版本号/检查更新（对比 GitHub latest）
- [x] C5 社区模板（CONTRIBUTING/issue 模板/CHANGELOG；ci.yml 已有）

## D. 成本、配额与安全
- [x] D1 多下游 API Key 管理（多 key/备注/过期/per-key 限速，日志 api_key_id 已在）
- [x] D2 预算与配额（per-key 每日 token/费用上限，超限动作可配：拒绝/降级/告警）
- [x] D3 通知渠道（Webhook/Telegram/钉钉：连续失败冷却、全候选失败、预算超限推送）
- [x] D4 价格健康度提醒（缺价/异常波动标黄，快捷手改入口，manual 保护已有）

## E. 路由智能与数据沉淀
- [x] E1 模型别名/映射（请求名→实际模型，公益站命名五花八门）
- [x] E2 round_robin 升级权重路由（灰度新站/按额度分配流量）
- [x] E3 智力评分定时自动同步（每周任务 + 同步状态展示，失败不删分已有）

