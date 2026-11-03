# -*- coding: utf-8 -*-
"""请求日志筛选必须跟随时间范围（2026-11-03 用户报的 bug）。

现象：分析页顶部选了「今日 / 近7天」，但日志区的状态、服务商、模型、账号筛选
筛出来的永远是**全部历史**——因为 `/admin/api/logs` 压根没有 start/end 参数，
前端也从不传；候选值接口 `/logs/filters`、`/logs/providers` 同样是全表去重。

口径约定（与 analytics/trend 等接口一致）：
- start/end 为 UTC ISO（RangePicker 产出，带 Z 或 ±时区均可），闭区间
- 非法/缺省时不加时间条件（保持旧行为，不把用户挡在门外）
- 窗口必须进 total 缓存键，否则「列表 3 条 / 总数 900」错位（Session 38 同类教训）
- 模型刷新日志（log_type=refresh）同样吃窗口
"""
from datetime import datetime

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from server.api import admin_ops_router, admin_routing
from server.models.base import Base
from server.models.model import Model
from server.models.model_refresh_log import ModelRefreshLog
from server.models.provider import Provider
from server.models.request_log import RequestLog

# 三天前的日志（窗口外）/ 昨天的日志（窗口内早段）/ 今天的日志（窗口内近段）
OLD = datetime(2026, 10, 1, 8, 0)
YDAY = datetime(2026, 10, 28, 9, 0)
TODAY = datetime(2026, 10, 29, 10, 0)

# 前端 RangePicker 实产格式（UTC，带毫秒与 Z）
_W_START = "2026-10-28T00:00:00.000Z"
_W_END = "2026-10-29T23:59:59.000Z"


async def _setup(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/lw.db")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all, tables=[
            Provider.__table__, Model.__table__, RequestLog.__table__,
            ModelRefreshLog.__table__,
        ])
    Session = async_sessionmaker(engine, expire_on_commit=False)
    async with Session() as db:
        db.add_all([
            RequestLog(requested_model="auto", routed_provider="Alpha", routed_model="kimi-k3",
                       routed_account="a@x.com", status="success", created_at=OLD,
                       is_health_check=False),
            RequestLog(requested_model="auto", routed_provider="Beta", routed_model="gpt-5.5",
                       routed_account="b@x.com", status="error", created_at=YDAY,
                       is_health_check=False),
            RequestLog(requested_model="auto", routed_provider="Alpha", routed_model="glm-5.2",
                       routed_account="a@x.com", status="success", created_at=TODAY,
                       is_health_check=False),
            # 窗口外的健康检查行：任何时候都不该出现在日志里
            RequestLog(requested_model="auto", routed_provider="Ghost", routed_model="ghost-m",
                       routed_account="z@x.com", status="error", created_at=TODAY,
                       is_health_check=True),
        ])
        db.add_all([
            ModelRefreshLog(provider_name="Alpha", ok=True, created_at=OLD),
            ModelRefreshLog(provider_name="Beta", ok=False, created_at=TODAY),
        ])
        await db.commit()
    # 进程内全局缓存：逐测试清零，否则跨测试命中上一个窗口的 total
    admin_routing._logs_total_cache.update({"data": None, "ts": 0, "key": None})
    return engine, Session


async def _list(db, **kw):
    kw.setdefault("page", 1)
    kw.setdefault("page_size", 50)
    kw.setdefault("status", None)
    kw.setdefault("provider", None)
    kw.setdefault("model", None)
    kw.setdefault("account", None)
    kw.setdefault("start", None)
    kw.setdefault("end", None)
    kw.setdefault("log_type", None)
    return await admin_routing.list_request_logs(db=db, **kw)


@pytest.mark.asyncio
async def test_list_without_window_keeps_old_behaviour(tmp_path):
    """不传窗口 = 全部历史（3 条非健康检查行），向后兼容"""
    engine, Session = await _setup(tmp_path)
    try:
        async with Session() as db:
            r = await _list(db)
        assert r["total"] == 3
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_list_respects_window(tmp_path):
    """核心回归：窗口内只剩 2 条，10 月 1 日那条不再混进来"""
    engine, Session = await _setup(tmp_path)
    try:
        async with Session() as db:
            r = await _list(db, start=_W_START, end=_W_END)
        assert r["total"] == 2
        assert {i["routed_model"] for i in r["items"]} == {"gpt-5.5", "glm-5.2"}
        assert all(i["created_at"] >= "2026-10-28" for i in r["items"])
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_status_and_provider_filters_combine_with_window(tmp_path):
    """用户投诉的形状：选「失败」+ 近两天 → 窗口内那条 error 才该出现；
    同一状态条件在没有窗口时命中另一条老 error（Ghost 是健康检查行，不算）。"""
    engine, Session = await _setup(tmp_path)
    try:
        async with Session() as db:
            r = await _list(db, status="error", start=_W_START, end=_W_END)
        assert r["total"] == 1 and r["items"][0]["routed_provider"] == "Beta"
        # 窗口收到只剩今天：Beta 那条 error 在窗口外 → 0 条（此前会漏出全部历史）
        async with Session() as db:
            r2 = await _list(db, status="error", start="2026-10-29T00:00:00.000Z", end=_W_END)
        assert r2["total"] == 0 and r2["items"] == []
        # provider 与窗口叠加同理
        async with Session() as db:
            r3 = await _list(db, provider="Alpha", start=_W_START, end=_W_END)
        assert r3["total"] == 1 and r3["items"][0]["routed_model"] == "glm-5.2"
        # model / account 子串筛选也一样吃窗口
        async with Session() as db:
            r4 = await _list(db, model="KIMI", start=_W_START, end=_W_END)
        assert r4["total"] == 0
        async with Session() as db:
            r5 = await _list(db, account="a@x", start=_W_START, end=_W_END)
        assert r5["total"] == 1
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_total_cache_key_includes_window(tmp_path):
    """total 缓存必须区分窗口：否则切范围后列表是 2 条、总数还显示全部历史 3 条"""
    engine, Session = await _setup(tmp_path)
    try:
        async with Session() as db:          # 先算全部历史
            all_r = await _list(db)
        assert all_r["total"] == 3
        async with Session() as db:          # 同条件 + 窗口：不能命中上面缓存
            win_r = await _list(db, start=_W_START, end=_W_END)
        assert win_r["total"] == 2, "total 未跟随窗口 → 缓存键漏了 window"
        async with Session() as db:          # 再切窄一格窗口，同样不能复用
            narrow = await _list(db, start="2026-10-29T00:00:00.000Z", end=_W_END)
        assert narrow["total"] == 1
        # 同窗口重复请求才该命中缓存（TTL 内 total 不变）
        async with Session() as db:
            again = await _list(db, start=_W_START, end=_W_END)
        assert again["total"] == 2
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_filters_candidates_are_scoped_to_window(tmp_path):
    """候选值接口只列窗口内出现过的服务商/模型/账号，Ghost 恒不出现（健康检查行）"""
    engine, Session = await _setup(tmp_path)
    try:
        async with Session() as db:
            full = await admin_routing.list_log_filters(db=db, start=None, end=None)
        assert set(full["providers"]) == {"Alpha", "Beta"}
        assert "kimi-k3" in full["models"]
        async with Session() as db:
            win = await admin_routing.list_log_filters(db=db, start=_W_START, end=_W_END)
        assert set(win["providers"]) == {"Alpha", "Beta"}
        assert set(win["models"]) == {"gpt-5.5", "glm-5.2"}   # 窗口外的 kimi-k3 不该提示
        assert set(win["accounts"]) == {"a@x.com", "b@x.com"}
        async with Session() as db:
            only_today = await admin_routing.list_log_filters(
                db=db, start="2026-10-29T00:00:00.000Z", end=_W_END)
        assert only_today["providers"] == ["Alpha"]
        assert only_today["models"] == ["glm-5.2"]
        assert only_today["accounts"] == ["a@x.com"]
        assert "Ghost" not in str(full)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_providers_endpoint_scoped_to_window(tmp_path):
    """老候选接口同口径（前端兜底路径也在用它）"""
    engine, Session = await _setup(tmp_path)
    try:
        async with Session() as db:
            full = await admin_routing.list_log_providers(db=db, start=None, end=None)
        assert full["providers"] == ["Alpha", "Beta"]
        async with Session() as db:
            win = await admin_routing.list_log_providers(
                db=db, start="2026-10-29T00:00:00.000Z", end=_W_END)
        assert win["providers"] == ["Alpha"]
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_refresh_logs_follow_window(tmp_path):
    """模型刷新日志同样吃窗口：窄窗口只剩今天那条"""
    engine, Session = await _setup(tmp_path)
    try:
        async with Session() as db:
            full = await _list(db, log_type="refresh")
        assert full["total"] == 2
        async with Session() as db:
            win = await _list(db, log_type="refresh", start=_W_START, end=_W_END)
        assert win["total"] == 1 and win["items"][0]["provider_name"] == "Beta"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_bad_window_values_are_ignored_not_fatal(tmp_path):
    """脏时间参数按「未提供」处理（沿用 _parse_dt_param 宽松口径，不 422）"""
    engine, Session = await _setup(tmp_path)
    try:
        async with Session() as db:
            r = await _list(db, start="昨天", end="not-a-date")
        assert r["total"] == 3
        # 只给 start 也算数（单边窗口）
        async with Session() as db:
            r2 = await _list(db, start=_W_START)
        assert r2["total"] == 2
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_export_follows_window(tmp_path):
    """导出与屏幕一致：传窗口就按窗口导，没传才退回 hours 老口径"""
    engine, Session = await _setup(tmp_path)
    try:
        async with Session() as db:
            resp = await admin_ops_router.export_logs(
                format="json", hours=168, status=None, provider=None,
                model=None, account=None, start=_W_START, end=_W_END, db=db)
        rows = await _body_json(resp)
        assert {r["routed_model"] for r in rows} == {"gpt-5.5", "glm-5.2"}
        # 窗口 + 模型子串：此前 api.js 根本不传 model，导出与筛选对不上
        async with Session() as db:
            resp2 = await admin_ops_router.export_logs(
                format="json", hours=168, status="success", provider=None,
                model="glm", account=None, start=_W_START, end=_W_END, db=db)
        assert [r["routed_model"] for r in await _body_json(resp2)] == ["glm-5.2"]
    finally:
        await engine.dispose()


async def _body_json(resp):
    import json
    parts = []
    async for c in resp.body_iterator:   # Starlette 把同步迭代器包成了 async gen
        parts.append(c if isinstance(c, str) else c.decode("utf-8"))
    return json.loads("".join(parts))


@pytest.mark.asyncio
async def test_frontend_wires_range_into_logs_panel():
    """源码守卫：range 必须从分析页下传，且列表/候选值/导出三处都带上窗口参数"""
    import pathlib
    root = pathlib.Path(__file__).resolve().parents[1]
    analytics = (root / "client/src/views/Analytics.vue").read_text(encoding="utf-8")
    panel = (root / "client/src/components/LogsPanel.vue").read_text(encoding="utf-8")
    apijs = (root / "client/src/api.js").read_text(encoding="utf-8")

    assert '<LogsPanel ref="logs" :range="range"' in analytics
    assert "range: { type: Object, default: null }" in panel
    assert "logWindow()" in panel
    # 三处取数都带窗口
    assert panel.count("const w = this.logWindow()") >= 3
    assert "if (w.start) params.start = w.start" in panel
    assert "api.getLogFilters(w)" in panel
    assert "start: w.start, end: w.end" in panel
    # api.js 三个方法都放行 start/end
    for frag in ("if (params.start) qs.append('start', params.start)",):
        assert apijs.count(frag) >= 3, "列表/候选值/导出应都传窗口"
    assert "if (params.model) qs.append('model', params.model)" in apijs


@pytest.mark.asyncio
async def test_backend_endpoints_declare_window_params():
    """源码守卫：别让下一个改动又把 start/end 悄悄删掉（本 bug 的成因）"""
    import pathlib
    src = pathlib.Path(admin_routing.__file__).resolve().read_text(encoding="utf-8")
    assert "start: Optional[str] = Query(None" in src
    assert src.count("_log_window_conds(") >= 3          # /logs、/logs/filters、/logs/providers
    assert "_logs_total_key(status, provider, model, account, _window_key)" in src
    ops = pathlib.Path(admin_ops_router.__file__).resolve().read_text(encoding="utf-8")
    assert "start: Optional[str] = Query(None" in ops
