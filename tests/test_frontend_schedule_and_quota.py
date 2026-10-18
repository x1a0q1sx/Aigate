# -*- coding: utf-8 -*-
"""前端静态回归（2026-10-18 Session 30）：

1. 「组合内定时启用模型」入口可见性 —— 用户找不到该功能在哪：
   功能本身是**模型级**（模型页编辑弹窗的「生效时段」），组合页需要
   (a) 说明入口位置；(b) 在候选池/已选栏标出「时段外」（否则用户不知道为什么被跳过）。
2. 限额本地拒绝文案 —— 模型页徽标 tooltip 明确「网关直接本地拒绝，不再发上游」。
3. Cline 402 的提示接线（后端已单测，这里只锁前端不误删徽标）。

按仓库既有约定做源码静态扫描（无 JS 测试运行时）。
"""
from pathlib import Path

_CLIENT = Path("client/src")


def _read(rel: str) -> str:
    return (_CLIENT / rel).read_text(encoding="utf-8")


# ── 1. 组合页：时段入口说明 + 时段外徽标 ─────────────────────

def test_combos_page_points_to_model_level_schedule_editor():
    src = _read("views/Combos.vue")
    # 说明入口：模型页 → 编辑模型 → 生效时段
    assert 'to="/models"' in src
    assert "生效时段" in src
    assert "定时启用模型" in src
    # 统一生效范围说明（组合 / Auto / 直连）
    assert "组合 / Auto / 直连" in src


def test_combos_pool_and_selection_show_schedule_blocked_badge():
    src = _read("views/Combos.vue")
    # 左侧候选池：时段外徽标
    assert "m.schedule_blocked" in src
    assert "时段外" in src
    # 右侧已选栏：同样标记（selScheduleBlocked）
    assert "selScheduleBlocked(m)" in src
    assert "schedule_blocked" in src


def test_combos_badge_explains_skip_semantics():
    """徽标 tooltip 要说明「请求会被跳过、到点自动恢复」，避免误判为故障。"""
    src = _read("views/Combos.vue")
    assert "请求会跳过" in src or "会被跳过" in src
    assert "自动恢复" in src


# ── 2. 模型页：限额徽标文案（本地拒绝语义）────────────────────

def test_models_quota_tooltip_says_local_reject():
    src = _read("views/Models.vue")
    assert "quotaLimitTitle(m)" in src
    assert "直接本地拒绝" in src
    assert "不再发上游" in src


# ── 3. 组合页 light 端点消费 schedule_blocked ─────────────────

def test_combos_uses_light_endpoint_with_schedule_field():
    src = _read("views/Combos.vue")
    assert "api.getModelsLight()" in src
    # selLookup 按 provider::model 建映射（schedule_blocked 随之可取）
    assert "selLookup" in src
