# AIGate 仪表盘与分析页重设计（信息架构去重）

日期：2026-09-29
状态：待用户评审
范围决策（用户已确认）：Monitor 并入分析页；导航改分组；本次聚焦「仪表盘 + 分析页 + 导航」三块，其余页面仅去重小改；仪表盘放「运行指标 + 失败告警 + 账号权益摘要」。

## 1. 背景与目标

用户原话映射：

| 需求 | 现状差距 |
|---|---|
| 仪表盘一眼看到整个系统运行状况和概览 | 现仪表盘只有 4 个静态计数卡（服务商/密钥/模型/Auto 候选）+ 健康分布 + Auto 最优；**无任何运行指标**（今日请求/成败/Token/成本/进行中），且混入「AIGate 密钥展示 + 快速开始」等接入引导内容 |
| 分析页：每日/每周/每月统计 + 实时详细日志 | 现分析页 7 大块混杂（累计统计、请求日志、今日用量、7 天趋势、按服务商、失败分析、归档管理）；**趋势只有日粒度**（trend 端点 days=1..90），**无周/月分桶、无小时粒度**；实时监控在独立的 Monitor 页 |
| 其它页面重复功能混杂 | 重叠矩阵：`getProviders` 6 页共用、模型列表 5 页、额度 QuotaPanel 4 处、冷却表 3 处（Monitor/Health/Providers 详情）、一键测速 2 处（Health/Models）、刷新模型 2 处（Providers/Models）；导航 16 项平铺无分组；OAuth 连接与签到两页无导航入口 |

## 2. 信息架构（导航分组）

NavBar 从 16 项平铺改为 6 组（数据驱动，`navGroups` 数组渲染）：

```
概览        仪表盘                    /
流量与日志  分析（含实时）             /analytics
            路由决策                  /route-decisions
资源配置    服务商                    /providers
            模型                      /models
            模型别名                  /aliases
            组合路由                  /combos
            网关密钥                  /keys
账号权益    OAuth 连接                /providers/oauth   （原隐藏页，正式入导航）
            签到与成长                /providers/checkin （原隐藏页，正式入导航）
工具        Playground                /playground
            媒体中心                  /media
系统        健康监控                  /health
            Auto 选举                 /auto
            代理池                    /proxies
            省 Token                  /token-saver
            设置                      /settings
```

路由变化：
- 删除 `/monitor` 路由与 `Monitor.vue`；加 `redirect: /monitor → /analytics#realtime`（落到分析页实时区）。
- `/oauth → /providers/oauth` 重定向保留；两个原隐藏页副标题中「本页不在常规界面提供入口」文案删除。
- 未知路径兜底重定向 `/dashboard` 保留。

## 3. 仪表盘（重写 Dashboard.vue）

布局（自上而下）：

```
┌ PageHeader：仪表盘          [刷新]  自动刷新 30s（document.hidden 暂停）
├ 核心运行指标行（7 卡，数据源 /live/summary.today）
│  今日请求 | 成功率 | 失败(红标) | 输入 Token | 输出 Token | 今日成本 | 进行中
├ 系统规模行（4 卡，数据源 /dashboard，保留现口径）
│  服务商 | 密钥(启用·关联) | 启用模型 | Auto 候选
├ 两栏：
│  ├ 健康分布（保留现有健康/延迟/限流/故障四行进度条）
│  └ Auto 当前最优（保留）+ 组件状态摘要（日志队列待写/丢弃、响应缓存命中）
├ 两栏：
│  ├ 最近失败告警（getFailures(24)，Top 8：时间/模型/服务商/错误摘要；底部「查看全部 →」跳 /analytics）
│  └ 账号权益摘要（getCheckinOverview，横向紧凑行：每平台 签到状态 badge + 积分合计 + 成长中心状态；底部「签到监控 →」跳 /providers/checkin）
└ （移除）AIGate 连接密钥卡、快速开始卡 → 挪至设置页
```

数据流：`Promise.all([getLiveSummary, getDashboard, getCurrentModel, getFailures(24), getCheckinOverview])`，每项独立 catch（失败区块显示 EmptyState，不拖垮整页）。30s 定时刷新 + `document.hidden` 暂停。

移除内容去向：
- 「AIGate 连接密钥」卡（含显示/复制/揭示逻辑）→ 设置页新增「接入信息」卡，放在「关于 / 一键更新」区之后。
- 「快速开始」（三步引导 + OpenAI SDK 代码块）→ 同一张「接入信息」卡。

## 4. 分析页（重构 Analytics.vue）

自上而下 7 区（PageHeader 之下）；第 ① 区的全局范围选择器驱动 ①-④ 区联动：

```
┌ PageHeader：分析   [范围：今日|7天|30天|90天|自定义起止]  [刷新]
├ ① 统计卡行（跟随范围）：
│   请求数 | 成功率 | 输入/输出 Token | 成本 | 缓存命中率 | 平均延迟 | 平均首字
├ ② 用量趋势图（跟随范围 + 粒度可切换 day/week/month；纯 CSS 柱状，沿用现有实现，不引图表库）
│   默认粒度：今日→hour、7/30 天→day、90 天→week
├ ③ 按服务商用量表（跟随范围）
├ ④ 失败分析（跟随范围换算 hours 传给 getFailures）
├ ⑤ 实时区（id=realtime，3s 轮询 /live/summary，document.hidden 暂停，「实时/已暂停」pill）
│   进行中/冷却计数 pill + 冷却中模型前 3 条（+「健康页查看全部 →」）+ 最近请求表（时间/模型/路由/状态/延迟/错误）
│   + 组件状态行（日志队列、响应缓存、服务器时间）
├ ⑥ 请求日志（现有功能原样保留：类型/状态/服务商筛选、分页、详情弹层、刷新日志混排行、导出 JSON、重置统计）
└ ⑦ 日志归档管理（<details> 折叠，现有功能保留）
```

组件拆分（避免新版 Analytics 再长成 1500+ 行）：

| 新组件 | 职责 |
|---|---|
| `RangePicker.vue` | 范围选择（预设 + 自定义起止），emit `{start,end,label,hours}` |
| `TrendChart.vue` | CSS 柱状图（现有 Analytics 内联实现抽出），props: rows/bucket |
| `RealtimePanel.vue` | 原 Monitor 的面板主体（冷却简表+最近请求+组件状态），自管 3s 轮询与暂停 |
| `LogsPanel.vue` | 现请求日志+归档区整体搬出（逻辑不动，只搬家） |

Analytics.vue 瘦身为编排层（范围状态 + 4 个子组件 + 区块顺序）。

## 5. 后端改动（3 个端点，全部向后兼容）

1. `GET /analytics/trend`（admin_routing.py:710）
   - 新增可选参数 `bucket=hour|day|week|month`（默认 `day`）、`start`/`end`（与 `days` 二选一，`start` 优先）。
   - 分桶表达式按现有 SQLite/PG 方言分支：SQLite `strftime('%Y-%m-%dT%H:00:00'|'%Y-%m-%d'|'%Y-W%W'|'%Y-%m')`；PG 对应 `to_char`。周桶 `%W` 以周一为始（UTC 语义，与库内 naive UTC 一致，前端标注 UTC）。
   - 空桶补零逻辑仅对 `day`/`hour` 保留（week/month 桶稀疏，不补）。
2. `GET /analytics/summary/today`（admin_routing.py:658）
   - 返回体新增 `avg_latency_ms`、`avg_ttft_ms`、`ttft_samples`（对窗口内 `latency_ms IS NOT NULL` / `ttft_ms IS NOT NULL` 求均值）。
3. `GET /analytics/by-provider`（admin_routing.py:739）
   - 新增可选 `start`/`end`，传给既有 `aggregate_usage_by_provider(db, since=…)`；缺省保持「今日」。

## 6. 范围内去重小改

| 重复点 | 处置 |
|---|---|
| Monitor 冷却表 vs Health 冷却三表 | 分析页实时区只显示**计数 + 前 3 条** + 链接健康页；全量三表与清除按钮仍归 Health |
| Monitor「最近请求」vs 分析页请求日志 | 实时区是**轻量尾随**（无筛选无详情，3s 轮询）；重查询只在日志区。两者职责不同，保留各自 |
| Dashboard 移除后密钥/快速开始 | 设置页「接入信息」卡唯一入口 |
| 隐藏页无入口 | 入导航（见 §2），页面副标题文案同步更新 |

明确**不做**（YAGNI / 范围控制）：不拆 Providers 巨无霸；不合并 Health/RouteDecisions；不引入 ECharts 等图表库；不做可配置仪表盘；不改 Checkin/OAuth 页内部结构。

## 7. 测试与验收

后端（pytest，新文件 `tests/test_analytics_range.py`）：
- trend：插入已知 `request_logs`（跨 2 周），断言 `bucket=day/week/month/hour` 各自分桶数值与 key 格式；`start/end` 过滤生效；空桶补零仅 day/hour。
- today：窗口内 avg_latency_ms / avg_ttft_ms 数值正确；无样本时为 None。
- by-provider：`start/end` 过滤后聚合正确（含只写名称的历史行）。

前端：
- `npm run build` 通过。
- 手动冒烟清单：仪表盘 7 卡有数、失败告警跳转、权益摘要显示两平台、30s 自动刷新；分析页切换今日/7天/30天/90天四档趋势粒度联动、实时区 3s 轮询且切后台暂停、日志筛选/详情/导出/归档不回归、`/monitor` 重定向生效；设置页出现接入信息卡；侧栏 6 组 17 项、隐藏页可从导航进入。

## 8. 风险与边界

- 日志表为 SQLite 大表，trend 新增 hour/week/month 分桶聚合走 `created_at` 索引 + 窗口过滤，风险低；`is_health_check=False` 条件与现有查询保持一致。
- trend 返回结构变更（新增 bucket 语义）向后兼容：默认 `bucket=day` + `days` 参数行为与现在完全一致，旧调用方不受影响。
- Monitor.vue 删除属破坏性变更：仅内部管理界面，加 redirect 兜底。
- 「账号权益摘要」依赖签到页的 overview 端点（已有，只读），仪表盘只读展示、不带任何写操作按钮。
