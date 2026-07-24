# AiGate 设计规格说明书 v2.0
## 1. 概述
本次迭代在现有 AiGate 架构上增加三个核心能力：
- **人工干预 Auto 选举**：priority_boost + auto_excluded
- **手动延迟测试**：单模型/批量 ping API
- **延迟可视化**：前端柱状图 + 颜色编码
## 2. 架构总览
```
┌──────────────────────────────────────────────────────┐
│                    Frontend (Vue3)                    │
│  ┌──────────┐ ┌──────────┐ ┌────────────────────┐   │
│  │ Models   │ │ Health   │ │ Dashboard          │   │
│  │ (干预按钮)│ │ (柱状图) │ │ (Top5最快)         │   │
│  └──────────┘ └──────────┘ └────────────────────┘   │
└──────────────────────┬───────────────────────────────┘
                       │ HTTP REST
┌──────────────────────┴───────────────────────────────┐
│                  Backend (FastAPI)                    │
│  ┌──────────────────────────────────────────────┐    │
│  │           admin_router.py                     │    │
│  │  + POST /models/{id}/ping                     │    │
│  │  + POST /models/ping-all                      │    │
│  │  + GET  /models/latency-stats                 │    │
│  │  + PUT  /models/{id} (新增字段)               │    │
│  └──────────────────────────────────────────────┘    │
│  ┌──────────────────────────────────────────────┐    │
│  │         health_checker.py                     │    │
│  │  + ping_single_model()  手动测速              │    │
│  │  + ping_all_models()    批量测速              │    │
│  └──────────────────────────────────────────────┘    │
│  ┌──────────────────────────────────────────────┐    │
│  │         auto_router.py                        │    │
│  │  _sort_candidates() 增加 priority_boost 排序  │    │
│  │  _filter_candidates() 增加 auto_excluded 过滤 │    │
│  └──────────────────────────────────────────────┘    │
└──────────────────────────────────────────────────────┘
```
## 3. 模块设计
### 3.1 数据模型变更（Model 表）
```python
# 新增字段
priority_boost: int = 0      # 优先级加成，默认0，范围 [-100, 100]
auto_excluded: bool = False  # 是否排除出 auto 选举
```
### 3.2 Auto 路由排序算法（更新后）
```
排序键 = (
    auto_excluded,           # True 排最后（被过滤掉）
    -priority_boost,         # 降序，值越大越靠前
    status_score,            # healthy=0, degraded=1, rate_limited=2, unhealthy=3
    free_score,              # free=0, paid=1
    latency_ms               # 升序
)
```
### 3.3 手动测速 API 设计
#### POST /admin/api/models/{model_id}/ping
```json
// Request: 无 body
// Response:
{
  "model_id": 1,
  "model_full_id": "GitHub Models/gpt-4o",
  "status": "healthy",
  "latency_ms": 342.5,
  "error_message": null,
  "checked_at": "2025-01-15T10:30:00Z"
}
```
#### POST /admin/api/models/ping-all
```json
// Request: 无 body
// Response:
{
  "total": 10,
  "healthy": 8,
  "degraded": 1,
  "unhealthy": 1,
  "results": [
    {"model_id": 1, "status": "healthy", "latency_ms": 342.5},
    ...
  ]
}
```
#### GET /admin/api/models/latency-stats
```json
{
  "fastest": [
    {"model_id": 1, "model_full_id": "...", "latency_ms": 120, "status": "healthy"}
  ],
  "slowest": [...],
  "average_latency_ms": 450.2
}
```
## 4. 前端设计
### 4.1 模型列表页（Models.vue）改动
- 每行增加「🔍 测速」按钮
- 每行增加「⬆️ 置顶」「⬇️ 降权」「🚫 排除」按钮
- 延迟列颜色编码：绿(<500) / 黄(500-2000) / 红(>2000)
- 顶部增加「🚀 一键测速」按钮
### 4.2 健康监控页（Health.vue）改动
- 增加 CSS 柱状图展示所有模型延迟
- 每个柱子可点击触发单模型测速
### 4.3 Dashboard 改动
- 增加「⚡ 最快模型 Top5」卡片
- Auto 排名表格增加 priority_boost 和 excluded 列
## 5. 关键流程
### 5.1 手动测速流程
```
用户点击「测速」→ 前端 POST /models/{id}/ping
→ 后端获取模型+Provider+Key
→ 创建适配器，调用 health_check()
→ 保存 HealthCheck 记录到 DB
→ 更新内存缓存
→ 返回延迟结果给前端
→ 前端更新显示
```
### 5.2 批量测速流程
```
用户点击「一键测速」→ 前端 POST /models/ping-all
→ 后端获取所有 enabled 模型
→ 串行遍历，每个间隔 0.5s
→ 逐个 ping 并收集结果
→ 返回汇总结果
→ 前端更新所有模型的延迟显示
```
## 6. 技术决策
| 决策 | 选择 | 理由 |
|------|------|------|
| 延迟图表 | 纯 CSS 柱状图 | 不引入额外依赖，保持轻量 |
| 批量测速 | 串行 + 0.5s 间隔 | 避免触发服务商限流 |
| priority_boost | 整数范围 [-100, 100] | 足够灵活，不过度设计 |
| 数据库迁移 | 应用启动时自动 ALTER | 简单可靠，无需迁移工具 |
## 7. 风险与权衡
| 风险 | 缓解 |
|------|------|
| 批量测速耗时长 | 前端显示进度，后端串行执行 |
| 数据库字段不存在 | 启动时检测并自动添加 |
| 前端柱状图兼容性 | 使用 CSS flexbox，兼容所有现代浏览器 |