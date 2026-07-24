# AiGate v3 开发任务计划
## 项目目标
构建自定义服务商 + Auto智能路由的LLM API网关，免费模型自动参选，收费模型可选手动加入选举。
## 当前进度
| 阶段 | 状态 | 负责人 | 产出 |
|------|------|--------|------|
| 📋 需求定义 | ✅ 完成 | 林若愚 | requirements-v3.md |
| 🏗️ 架构设计 | ✅ 完成 | 欧阳睿 | architecture-v3.md |
| 🐛 BUG修复 | ✅ 完成 | 程子轩 | 修复 Playground 报错 |
| ⚡ 免费自动参选 | ⏳ 待开始 | 程子轩 | - |
| 🎨 收费模型Auto开关UI | ⏳ 待开始 | 苏语薇 | - |
| ⚡ 定时评测增强 | ⏳ 待开始 | 程子轩 | - |
| 🔬 构建+测试 | ⏳ 待开始 | 周明远 | - |
## 开发任务明细
### 任务2：完善免费模型自动参选逻辑
- 位置：model_catalog.py → refresh_models_from_provider()
- 逻辑：模型发现时，价格=0 → is_free=True, auto_enabled=True；价格>0 → auto_enabled=False
- 已有代码已基本正确，需加强前端展示售卖/免费状态
### 任务3：Models页面收费模型Auto开关
- 位置：Models.vue
- 新增：每行收费模型显示「参与Auto选举」开关
- 调用API：PUT /admin/api/models/{id} 设置 auto_enabled
### 任务4：增强定时评测
- 位置：health_checker.py → 在check_all_enabled中调用RankingService
- 现有健康检查已足够，但需确保每次探测记录足够数据给RankingService用
- 或者新增一个轻量评测定时任务，发送小prompt并记录完整延迟
## 已知BUG
1. ✅ Playground - api.playground is not a function（已修复：添加playground别名）
## 错误记录
| 错误 | 方案 | 结果 |
|------|------|------|
| - | - | - |