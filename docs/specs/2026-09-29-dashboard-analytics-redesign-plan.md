# 仪表盘/分析页/导航重构 — 实现计划

依据：`docs/specs/2026-09-29-dashboard-analytics-redesign-design.md`（用户已批准「开搞」）。
技术栈：FastAPI + SQLAlchemy async（SQLite/PG 方言分支）+ Vue3 options API（无图表库）。

## 全局约束

- 提交钩子会拦截绝对服务器路径；`AGENTS.md`（未跟踪）会被 `git add -A` 带入 → **每次提交前 `git restore --staged AGENTS.md`**。
- 后端 3 端点全部**向后兼容**：不传新参数时行为与现在一致（除 trend 新增 health-check 排除，spec §8 已声明）。
- 前端不引第三方库；LogsPanel 属**机械搬移**（逻辑不动只搬家）；不改动 spec「明确不做」清单里的页面。
- 推送：`git -c http.proxy=socks5h://127.0.0.1:10889 push origin main`（失败重试循环）。

---

## Task 1 — 后端：trend 支持 bucket + start/end（admin_routing.py:710）

- [ ] 新增模块级辅助 `_trend_slots(bucket, start_dt, end_dt)`：生成 day/hour 空桶槽位 key；超过 744 槽返回 None（不补零）。
- [ ] 重写 `analytics_trend`：

```python
@router.get("/analytics/trend")
async def analytics_trend(
    days: int = Query(7, ge=1, le=90),
    bucket: str = Query("day", pattern="^(hour|day|week|month)$"),
    start: Optional[str] = Query(None, description="起始时间（UTC），提供后覆盖 days"),
    end: Optional[str] = Query(None, description="结束时间（含）"),
    db: AsyncSession = Depends(get_db),
):
    """趋势序列：请求数 / Token / 成本。

    bucket=hour|day|week|month（默认 day 向后兼容）；start/end 提供 → 精确窗口，否则回退「最近 N 天」。
    周/月桶由天级分组在 Python 侧归并（跨年周正确 + 两方言一致）：周槽=所在周周一（UTC），月槽="YYYY-MM"。
    空桶补零仅对 day/hour（≤744 槽）执行；week/month 稀疏桶不补。
    """
    from server.db import IS_SQLITE as _is_sqlite
    if _is_sqlite:
        _grp = (func.strftime("%Y-%m-%dT%H:00:00", RequestLog.created_at) if bucket == "hour"
                else func.strftime("%Y-%m-%d", RequestLog.created_at))
    else:
        _grp = (func.to_char(RequestLog.created_at, 'YYYY-MM-DD"T"HH24":00:00"') if bucket == "hour"
                else func.to_char(RequestLog.created_at, "YYYY-MM-DD"))
    now = datetime.utcnow()
    start_dt = _parse_dt_param(start)
    end_dt = _parse_dt_param(end, end_of_day=True)
    if start_dt is None:
        if bucket == "hour":
            start_dt = (now - timedelta(days=days)).replace(minute=0, second=0, microsecond=0)
        else:
            start_dt = (now - timedelta(days=days - 1)).replace(hour=0, minute=0, second=0, microsecond=0)
    if end_dt is None:
        end_dt = now
    rows = (await db.execute(
        select(
            _grp,
            func.count(RequestLog.id),
            func.coalesce(func.sum(RequestLog.prompt_tokens + RequestLog.completion_tokens), 0),
            func.coalesce(func.sum(RequestLog.estimated_cost_usd), 0.0),
        ).where(RequestLog.created_at >= start_dt, RequestLog.created_at <= end_dt,
                RequestLog.is_health_check.is_(False))
        .group_by(_grp).order_by(_grp)
    )).all()
    raw = {str(r[0]): {"requests": int(r[1] or 0), "tokens": int(r[2] or 0),
                       "cost_usd": round(float(r[3] or 0), 4)} for r in rows}
    if bucket in ("week", "month"):
        merged = {}
        for k, v in raw.items():
            try:
                d = datetime.strptime(k[:10], "%Y-%m-%d")
            except ValueError:
                continue
            slot = ((d - timedelta(days=d.weekday())).strftime("%Y-%m-%d") if bucket == "week"
                    else d.strftime("%Y-%m"))
            m = merged.setdefault(slot, {"requests": 0, "tokens": 0, "cost_usd": 0.0})
            for f in ("requests", "tokens", "cost_usd"):
                m[f] += v[f]
        raw = merged
    result = []
    if bucket in ("day", "hour"):
        slots = _trend_slots(bucket, start_dt, end_dt)
        if slots is not None:
            result = [{"day": s, **(raw.get(s) or {"requests": 0, "tokens": 0, "cost_usd": 0.0})}
                      for s in slots]
    if not result:  # week/month 或超 744 槽：只返回观测桶（已排序）
        result = [{"day": k, **raw[k]} for k in sorted(raw)]
    return result


def _trend_slots(bucket: str, start_dt: datetime, end_dt: datetime):
    """生成空桶槽位 key；超过 744 槽 → None（调用方改为只返回观测桶）。"""
    keys = []
    cur = start_dt.replace(minute=0, second=0, microsecond=0)
    step = timedelta(hours=1) if bucket == "hour" else timedelta(days=1)
    while cur <= end_dt:
        keys.append(cur.strftime("%Y-%m-%dT%H:00:00") if bucket == "hour" else cur.strftime("%Y-%m-%d"))
        cur += step
        if len(keys) > 744:
            return None
    return keys
```

- 返回结构保持 `[{day, requests, tokens, cost_usd}]`（`day` 即槽位 key，前端兼容）。

## Task 2 — 后端：today 增加延迟字段（admin_routing.py:658）

- [ ] select 追加三列：`func.avg(RequestLog.latency_ms)`、`func.avg(RequestLog.ttft_ms)`、`func.count(RequestLog.ttft_ms)`，解包 `avg_lat, avg_ttft, ttft_cnt`。
- [ ] 返回体追加：

```python
"avg_latency_ms": round(float(avg_lat), 1) if avg_lat is not None else None,
"avg_ttft_ms": round(float(avg_ttft), 1) if avg_ttft is not None else None,
"ttft_samples": int(ttft_cnt or 0),
```

## Task 3 — 后端：by-provider 支持 start/end（admin_routing.py:739）

- [ ] 签名加 `start: Optional[str] = Query(None)` / `end: Optional[str] = Query(None)`；
- [ ] `start_dt = _parse_dt_param(start) or 今日零点`；`end_dt = _parse_dt_param(end, end_of_day=True)`；
- [ ] `aggregate_usage_by_provider(db, since=start_dt, until=end_dt)`（until 参数已存在，执行时先读 provider_usage.py 确认 until 语义为 `<=`）。

## Task 4 — 测试：新建 `tests/test_analytics_range.py`

照 `tests/test_provider_usage_aggregation.py` 的 `_setup/_log` 范式。锚点：**2026-09-28 是周一**。所有断言都显式传 start/end（不用真实 utcnow 的默认窗口）。

- [ ] `test_trend_day_zero_fill_and_health_check_excluded`：start=2026-09-22/end=2026-09-28，7 槽；09-28 有 2 条（健康检查行被排除）+tokens=300、09-30 区外不出现、09-29 补零。
- [ ] `test_trend_hour_bucket`：00:00–23:59 共 24 槽；08:15/08:45 → `2026-09-28T08:00:00`=2，10:05 → 10 桶=1，00 桶=0。
- [ ] `test_trend_week_month_merge`：09-28(一)/09-30(三) 归并到周槽 `2026-09-28`，09-15(二) → 周槽 `2026-09-14`；month 桶 08-01~09-30 → 非空桶仅 `2026-09`。
- [ ] `test_trend_start_end_window`：hour 桶 start=2026-09-28T00:00/end=06:00 → 总请求数=1（23 点那条在窗外）。
- [ ] `test_today_latency_and_range`：latency [1000,3000] → avg_latency_ms=2000.0；ttft [200,600] → 400.0/样本 2；窗口外与健康检查行不计；空窗 → 三字段 None/0。
- [ ] `test_today_end_of_day_inclusive`：end=日期形式 → 23:30 的行计入。
- [ ] `test_by_provider_start_end`：name-only 行 + 窗口外行，start=2026-09-27/end=2026-09-30 → Alpha requests=2/tokens=160。

- [ ] 跑 `python -m pytest tests/test_analytics_range.py -q` 全绿。

## Task 5 — 前端 api.js（client/src/api.js:241-242）

- [ ] 替换两个方法（其余不动）：

```js
getAnalyticsTrend: (params = {}) => {
  const qs = new URLSearchParams()
  if (params.days) qs.append('days', params.days)
  if (params.bucket) qs.append('bucket', params.bucket)
  if (params.start) qs.append('start', params.start)
  if (params.end) qs.append('end', params.end)
  return apiGet(`/admin/api/analytics/trend?${qs.toString()}`)
},
getAnalyticsByProvider: (params = {}) => {
  const qs = new URLSearchParams()
  if (params.start) qs.append('start', params.start)
  if (params.end) qs.append('end', params.end)
  const s = qs.toString()
  return apiGet(`/admin/api/analytics/by-provider${s ? '?' + s : ''}`)
},
```

## Task 6 — 新组件 `client/src/components/RangePicker.vue`

- [ ] props: `modelValue`；emits `update:modelValue` + `change`（载荷 `{key,label,bucket,start,end,hours}`，start/end 为 UTC ISO）。
- [ ] 预设：今日(bucket=hour)/近7天(day)/近30天(day)/近90天(week)，end=now；默认选中「近7天」（mounted 时未传值自动 emit）。
- [ ] 自定义：两个 datetime-local；应用时 span≤72h → hour 粒度否则 day；label 显示本地起止。
- [ ] 完整代码照计划执行（tools 已核：图标、日期工具函数内置组件内）。

## Task 7 — 新组件 `client/src/components/TrendChart.vue`

- [ ] 把 Analytics.vue L213-245 SVG 图 + computed(trendScale/trendTokArea/trendTokLine/trendCostLine/trendTipStyle) + methods(trendX/trendYTok/trendYCost/trendShowLabel/trendLabel/onTrendMove) **原样抽出**，`trendData`→prop `rows`。
- [ ] 新 prop `bucket`（'hour'|'day'|'week'|'month'）只影响 X 轴 label：hour→`d.day.slice(11,16)`、day/week→`slice(5)`、month→`slice(0,7)`。
- [ ] `rows.length > 120` 时不渲染数据点圆圈（hour 长窗防 SVG 过重）。
- [ ] 样式 trend-wrap/trend-tip/trend-legend 一并搬入组件 scoped style。

## Task 8 — 新组件 `client/src/components/RealtimePanel.vue`

- [ ] Monitor.vue 主体搬入：section 根元素 `id="realtime"`；头部 pill「实时 3s / 已暂停」+「进行中 N」「冷却 N」pill。
- [ ] 指标格 7 个（今日请求/成功/失败/输入/输出/成本/缓存命中）；冷却表只显示**前 3 条** + `router-link to="/health"`「健康页查看全部 →」；最近请求表照搬；底部组件状态行（日志队列/响应缓存/服务器时间）。
- [ ] 3s 轮询 getLiveSummary + `document.hidden` 暂停（照 Monitor 逻辑）。

## Task 9 — 新组件 `client/src/components/LogsPanel.vue`（机械搬移）

- [ ] 从 Analytics.vue 整体搬出：请求日志卡（筛选/双类型表格/分页/跳页）、归档卡（外层包 `<details class="archive-fold">` + `<summary>日志归档管理 📦</summary>`，默认折叠）、详情弹窗、全部相关 script 与样式（含 safeParseJSON/deepUnescape/collapseBlank/argText/copy/formatJson/formatNum/fmtLatency/formatTokens/maskProxy/formatSize/fmtTime、3s pending 行轮询）。
- [ ] 头部追加导出按钮与重置：`导出 CSV` / `导出 JSON` / `重置统计数据`（confirm×2 照旧）。
- [ ] 对外接口：prop `filterProvider`（watch 同步内部 filterProvider 并 reload）；method `setProviderFilter(name)`（供 ③「看日志」调用）；method `reload()`（= loadPage(this.page)）。

## Task 10 — Analytics.vue 重写为编排层

- [ ] 模板顺序：页头（诊断日志 checkbox + 刷新）→ RangePicker 行 → ① 统计卡 7 张（请求数 sub 成功/成功率/Token sub 输入输出/成本/缓存命中率/平均延迟/平均首字 sub 样本，数据= `getAnalyticsToday({start,end})`）→ ② TrendChart（桶切换按钮 小时/天/周/月，`setBucket` 重载）→ ③ 按服务商表（`getAnalyticsByProvider({start,end})`，「看日志」→ `$refs.logs.setProviderFilter(name)`）→ ④ 失败分析（**范围联动**：`failuresHours = clamp(range.hours, 1, 720)`（端点 `le=24*30`），保留 6/24/72/168 手动下拉覆盖）→ ⑤ `<RealtimePanel />` → ⑥ `<LogsPanel ref="logs" />`。
- [ ] 删除：旧累计统计卡区、旧 usageFilters 双 datetime、旧内联趋势图、旧归档/详情/日志块（已入 LogsPanel）。
- [ ] `onRangeChange(r)`：`range=r; trendBucket=r.bucket` → `Promise.all([loadToday, loadTrend, loadByProvider])` + 失败区间联动 `loadFailures()`。
- [ ] 「刷新」= 范围数据 + failures + `$refs.logs.reload()` + diag。
- [ ] 趋势查询带 `getAnalyticsTrend({ bucket: trendBucket, start, end })`。
- [ ] 保留 import toast/api；删除不再使用的 data/computed/methods。

## Task 11 — Dashboard.vue 重写

- [ ] 行1 运行指标 7 卡（Monitor 风格 stat-card）：今日请求/成功率/失败(红)/输入/输出/今日成本/进行中，数据 `live.today`；页头「刷新」+「自动 30s」pill。
- [ ] 行2 规模 4 卡（保留现 StatCard：服务商/已配置密钥 keySub/启用模型/Auto 候选）。
- [ ] 行3 两栏：健康分布（原样保留）| 右列 = Auto 当前最优（原样）+ 组件状态卡（日志队列 待写/丢弃、响应缓存 条数·命中，数据 live.log_queue/live.cache）。
- [ ] 行4 两栏：最近失败告警（`getFailures(24)` 总数 badge + latest_error_sample；`getLogs({page:1,page_size:8,status:'error'})` 行级 Top8：时间/请求模型/路由/错误摘要（`error_type || error_msg 截断`，执行时按 getLogs 实际返回字段核对）；「查看全部 →」to /analytics）| 账号权益摘要（`getCheckinOverview(false)`：`summary.done_today/total_accounts` + `credit_today`；每平台行 provider_code + `done_today/total_accounts` badge + `credit_today`；「签到监控 →」to /providers/checkin）。
- [ ] `load()` = Promise.all 6 项（getLiveSummary/getDashboard/getCurrentModel/getFailures(24)/getLogs error 8/getCheckinOverview(false)），每项独立 catch；30s 定时 + document.hidden 暂停。
- [ ] 删除：AIGate 连接密钥卡、快速开始卡及其 script（usageCode/toggleKey/copyKey/maskedKey 等）。

## Task 12 — Settings.vue 接入信息卡

- [ ] 「关于 / 一键更新」section 之后插入新 `<section class="settings-card">`：Base URL 行（`window.location.origin + '/v1'` + 复制）、API Key 行（maskedDisplay + 显示/隐藏（管理员密码揭示，逻辑照 Dashboard.toggleKey）+ 复制）、说明行、OpenAI SDK 代码块（usageCode 照搬）+ 复制。
- [ ] script 迁入：aigateKey/keyMeta/showKey、maskedKey/maskedDisplay/keyConfigured computed、toggleKey、copyText（Settings 无 copy 方法则新增）、load 时 `api.getAIGateKey(false)`。

## Task 13 — 导航 + 路由 + 隐藏页文案

- [ ] NavBar.vue：`routes` → `navGroups` 6 组 17 项（spec §2 原文分组；OAuth 连接=link 图标、签到与成长=gift）；模板 `<template v-for="g in navGroups">` + `.nav-group-label`（collapsed 时隐藏 label）；CSS 加 `.nav-group-label`。
- [ ] router.js：删 Monitor import + `/monitor` 路由；加 `{ path: '/monitor', redirect: { path: '/analytics', hash: '#realtime' } }`。
- [ ] OAuthConnections.vue:5 / Checkin.vue:5 副标题删「本页不在常规界面提供入口…」句。
- [ ] `git rm client/src/views/Monitor.vue`；grep 确认 client 内无其它 Monitor 引用。

## Task 14 — 构建 + 全量测试

- [ ] `cd client && npm run build` 通过。
- [ ] `python -m pytest tests -q` 全绿（基线 899 + 新增 7）。

## Task 15 — 提交推送 + 部署 + 冒烟

- [ ] 提交（`git restore --staged AGENTS.md` 后 add/commit）；推送 retry 循环。
- [ ] 服务器部署（SSH 脚本走 `/d/tmp/*.sh` + `ssh ... "bash -s"`）：pull → npm run build → `pm2 restart aigate`（**不套 timeout**）→ 端口 8000 持有 PID == pm2 PID。
- [ ] 冒烟（spec §7）：仪表盘 7 卡有数/失败告警/权益摘要/30s 刷新；分析页四档切换、粒度切换、实时区 3s、日志筛选/详情/导出/归档、`/monitor` 重定向；设置页接入信息卡；侧栏 6 组 17 项、隐藏页入口可达。
- [ ] 更新记忆文件。

## 自检结论

- spec §2-§7 全覆盖；偏差 2 处已在对应 Task 注明：①周/月桶用「天级分组 + Python 归并」替代 spec 的 SQL `%Y-W%W`（跨年周正确 + 方言一致，spec §5 本意即周一起始/月起始语义不变）；②失败告警 Top8 行级数据来自 `getLogs(status=error)`（getFailures 只返回分组聚合，无行级字段），汇总 badge 仍来自 getFailures(24)。
- 无 TODO/占位符；接口名均与现状核对（api.js 现行方法、`aggregate_usage_by_provider(since,until)`、failures `hours ge=1 le=720`、checkin overview `summary/providers` 结构、AppIcon 图标名 link/gift/chart 存在）。
