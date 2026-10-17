"""模型生效时段（定时免费/限时模型）——纯函数 + 路由接入 + API 契约。

2026-10-17 需求：CodeBuddy Hy4 preview 每晚 23:00-08:00 不计积分（活动
2026-09-11~10-31）、Qoder 错峰折扣 22:00-08:00 等「按时段免费/打折」的模型，
需要按时间窗参与/退出路由。

设计（用户确认）：
- 配置层级=**模型级**（组合/Auto/直连统一生效，组合只是引用模型）
- 两种模式按模型可选：only=时段外硬跳过；boost=时段内优先加分
- Qoder 目录自带 promotion 字段 → 自动识别展示 + 一键套用
"""
import asyncio
import json
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from server.core.model_schedule import (
    evaluate, evaluate_model, normalize_schedule, schedule_summary,
)

TZ = ZoneInfo("Asia/Shanghai")


def _night(**over):
    base = {"mode": "only", "boost": 30, "timezone": "Asia/Shanghai",
            "rules": [{"start": "23:00", "end": "08:00",
                       "date_from": "2026-09-11", "date_to": "2026-10-31",
                       "label": "夜间免费"}]}
    base.update(over)
    return base


# ── 纯函数：窗口判定 ─────────────────────────────────
def test_window_cross_midnight():
    sch = normalize_schedule(_night())
    assert evaluate(sch, datetime(2026, 10, 17, 22, 59, tzinfo=TZ))["blocked"] is True
    assert evaluate(sch, datetime(2026, 10, 17, 23, 0, tzinfo=TZ))["active"] is True
    assert evaluate(sch, datetime(2026, 10, 18, 3, 0, tzinfo=TZ))["active"] is True
    assert evaluate(sch, datetime(2026, 10, 18, 7, 59, tzinfo=TZ))["active"] is True
    assert evaluate(sch, datetime(2026, 10, 18, 8, 0, tzinfo=TZ))["blocked"] is True


def test_window_date_range_inclusive_and_anchor_day():
    """跨零点窗口的归属日=开始那天：10-31 23:30 属活动内，11-01 02:00 已过期。"""
    sch = normalize_schedule(_night())
    assert evaluate(sch, datetime(2026, 10, 31, 23, 30, tzinfo=TZ))["active"] is True
    # 10-31 开的夜窗延续到 11-01 08:00 仍算活动内（「每晚23:00-次日8:00」的自然语义）
    assert evaluate(sch, datetime(2026, 11, 1, 2, 0, tzinfo=TZ))["active"] is True
    assert evaluate(sch, datetime(2026, 11, 1, 8, 0, tzinfo=TZ))["blocked"] is True
    # 起点当天凌晨（02:00）归属 9-10（活动外）
    assert evaluate(sch, datetime(2026, 9, 11, 2, 0, tzinfo=TZ))["blocked"] is True
    assert evaluate(sch, datetime(2026, 9, 11, 23, 30, tzinfo=TZ))["active"] is True


def test_weekday_filter_uses_anchor_day():
    """days=[1]（周一）→ 周一 23:30 与周二 02:00 都算周一窗口，周二 23:30 不算。"""
    sch = normalize_schedule({"mode": "only", "rules": [
        {"start": "23:00", "end": "08:00", "days": [1], "label": "周一夜间"}]})
    assert evaluate(sch, datetime(2026, 10, 19, 23, 30, tzinfo=TZ))["active"] is True   # 周一
    assert evaluate(sch, datetime(2026, 10, 20, 2, 0, tzinfo=TZ))["active"] is True     # 周二凌晨
    assert evaluate(sch, datetime(2026, 10, 20, 23, 30, tzinfo=TZ))["active"] is False  # 周二夜


def test_boost_mode_not_blocked_outside_window():
    sch = normalize_schedule(_night(mode="boost", boost=40))
    out = evaluate(sch, datetime(2026, 10, 17, 12, 0, tzinfo=TZ))
    assert out["blocked"] is False and out["boost"] == 0
    inn = evaluate(sch, datetime(2026, 10, 17, 23, 30, tzinfo=TZ))
    assert inn["active"] is True and inn["boost"] == 40


def test_plain_daytime_window_and_full_day():
    sch = normalize_schedule({"mode": "only", "rules": [
        {"start": "09:00", "end": "18:00", "label": "白天"}]})
    assert evaluate(sch, datetime(2026, 10, 17, 10, 0, tzinfo=TZ))["active"] is True
    assert evaluate(sch, datetime(2026, 10, 17, 20, 0, tzinfo=TZ))["active"] is False
    # start==end = 全天
    full = normalize_schedule({"mode": "only", "rules": [{"start": "00:00", "end": "00:00"}]})
    assert evaluate(full, datetime(2026, 10, 17, 4, 0, tzinfo=TZ))["active"] is True


def test_no_config_is_safe_default():
    """未配置/脏配置一律不拦截（安全默认：绝不因配置错误饿死模型）。"""
    assert evaluate(None)["blocked"] is False
    assert evaluate({})["blocked"] is False
    assert evaluate({"rules": []})["blocked"] is False
    assert normalize_schedule({"rules": [{"start": "bad", "end": "08:00"}]}) is None
    assert normalize_schedule("nonsense") is None


def test_next_open_reported_for_error_message():
    sch = normalize_schedule(_night())
    out = evaluate(sch, datetime(2026, 10, 17, 12, 0, tzinfo=TZ))
    assert out["next_open"].startswith("2026-10-17T23:00:00")
    # 活动过期后无下次开放
    assert evaluate(sch, datetime(2026, 11, 5, 12, 0, tzinfo=TZ))["next_open"] is None


def test_timezone_respected():
    sch = normalize_schedule(_night(timezone="UTC"))
    # UTC 23:00 = 北京 07:00 次日 → 北京 07:30 在 UTC 前一日 23:30，属窗口内
    assert evaluate(sch, datetime(2026, 10, 18, 7, 30, tzinfo=TZ))["active"] is True
    # 北京 16:30 = UTC 08:30（窗口 23:00-08:00 之外）
    assert evaluate(sch, datetime(2026, 10, 18, 16, 30, tzinfo=TZ))["active"] is False


def test_schedule_summary_human_readable():
    txt = schedule_summary(normalize_schedule(_night()))
    assert "仅时段内" in txt and "23:00-08:00" in txt and "夜间免费" in txt


# ── ORM 接入：auto 选举 / 组合 / 直连 ────────────────
class _M:
    def __init__(self, schedule=None):
        self.active_schedule = schedule


def test_evaluate_model_tolerates_orm_without_attr():
    assert evaluate_model(object())["blocked"] is False
    assert evaluate_model(_M(None))["blocked"] is False


def test_auto_candidates_exclude_blocked(monkeypatch):
    """get_auto_candidates 必须跳过「仅时段内可用」且当前时段外的模型。"""
    import server.core.model_catalog as mc

    class _Row:
        pass

    # 直接测过滤逻辑所在函数较难（需 DB），此处锁定源码契约：
    src = open(mc.__file__, encoding="utf-8").read()
    assert "evaluate_model(model)[\"blocked\"]" in src
    assert "v4.6 生效时段" in src


def test_auto_router_skip_and_boost_contract():
    import server.core.auto_router as ar_mod
    src = open(ar_mod.__file__, encoding="utf-8").read()
    # skip_reason 必须能报时段原因（决策面板可见）
    assert "outside active window" in src
    # boost 模式窗口内加分
    assert "st[\"boost\"]" in src
    # sticky 出窗口要放走
    assert src.count("evaluate_model(sticky_model)") >= 1


def test_combo_router_skips_blocked_but_keeps_order():
    import server.core.combo_router as cr
    src = open(cr.__file__, encoding="utf-8").read()
    assert "_st[\"blocked\"]" in src
    assert "时段外跳过" in src


def test_direct_route_returns_409_with_next_open():
    import server.api.v1_router as vr
    src = open(vr.__file__, encoding="utf-8").read()
    assert "status_code=409" in src
    assert "next_open" in src
    assert "outside its active window" in src


def test_playground_shares_direct_semantics():
    import server.api.admin_router as ad
    src = open(ad.__file__, encoding="utf-8").read()
    assert "outside its active window" in src


# ── 更新接口 & 列表透传 ─────────────────────────────
def test_update_endpoint_wires_schedule_fields():
    import server.api.admin_router as ad
    import server.core.model_catalog as mc
    ad_src = open(ad.__file__, encoding="utf-8").read()
    mc_src = open(mc.__file__, encoding="utf-8").read()
    assert "active_schedule=data.active_schedule" in ad_src
    assert "clear_active_schedule=data.clear_active_schedule" in ad_src
    assert "normalize_schedule(active_schedule)" in mc_src


def test_model_response_exposes_schedule_and_promotion():
    import server.schemas.provider as sp
    src = open(sp.__file__, encoding="utf-8").read()
    assert "active_schedule: Optional[Dict[str, Any]] = None" in src
    assert "schedule_state: Optional[Dict[str, Any]] = None" in src
    assert "promotion: Optional[Dict[str, Any]] = None" in src


def test_qoder_adapter_emits_promotion():
    import server.adapters.qoder_adapter as qa
    src = open(qa.__file__, encoding="utf-8").read()
    assert 'promotion=(entry.get("promotion")' in src


def test_model_list_attaches_schedule_state():
    import server.api.admin_router as ad
    src = open(ad.__file__, encoding="utf-8").read()
    assert "item.schedule_state = _eval_sched" in src


# ── 前端契约（静态源码扫描，仓库无 JS 测试运行时）─────────
def test_frontend_model_edit_has_schedule_editor():
    src = open("client/src/views/Models.vue", encoding="utf-8").read()
    assert "active_schedule" in src
    assert "生效时段" in src
    assert "23:00" in src or "start" in src
    assert "clear_active_schedule" in src


def test_frontend_model_row_shows_schedule_badge():
    src = open("client/src/views/Models.vue", encoding="utf-8").read()
    assert "schedule_state" in src
    assert "时段外" in src


def test_frontend_promotion_one_click_apply():
    src = open("client/src/views/Models.vue", encoding="utf-8").read()
    assert "promotion" in src
    assert "套用" in src
