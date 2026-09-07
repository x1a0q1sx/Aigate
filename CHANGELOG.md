# 更新日志

格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)。

## [Unreleased]

### Added
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
- Anthropic 流式缺 await 导致 /v1/messages 全坏、message_start 被 ping 抢首、非流式块序错误
- 终态错误被包装成"正常完成"的空响应（Responses/Anthropic SSE）
- 请求日志 token 全 0、PG 布尔比较、种子 NOT NULL 等方言问题
