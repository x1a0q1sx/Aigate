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