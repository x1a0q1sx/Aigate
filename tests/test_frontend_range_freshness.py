# -*- coding: utf-8 -*-
"""前端静态回归：分析页时间窗口新鲜度（2026-10-16）。

Bug：RangePicker 的预设列表放在 Vue computed 里且内部 new Date()，无响应式依赖
→ 首次求值结果被 Vue 永久缓存 → 时间窗口冻结在页面挂载时刻。表现：早上打开
分析页，晚上 22 点「今日」趋势仍只画到早上 10 点；点「刷新」也只是重放旧窗口。
修复：窗口计算抽到 rangePresets.js，在「点击/取数的当下」重算。

无 JS 测试运行时，按仓库既有约定做源码静态扫描锁死（同
test_frontend_pelican_and_analytics.py）。
"""
from pathlib import Path

_CLIENT = Path("client/src")


def _read(rel: str) -> str:
    return (_CLIENT / rel).read_text(encoding="utf-8")


# ── 1. 工具模块存在且可注入 now ─────────────────────────────

def test_range_presets_module_exports_builders():
    src = _read("rangePresets.js")
    assert "export function buildPresets" in src
    assert "export function freshRange" in src
    assert "export function defaultCustomWindow" in src
    # now 可注入（默认 new Date()），保证可测且调用方能控制时刻
    assert "buildPresets(now = new Date())" in src
    assert "freshRange(range, now = new Date())" in src


def test_fresh_range_keeps_custom_untouched():
    """custom 是绝对窗口，不随当前时刻漂移。"""
    src = _read("rangePresets.js")
    assert "range.key === 'custom'" in src
    assert "return range" in src


# ── 2. RangePicker 不再缓存挂载时刻的窗口 ───────────────────

def test_range_picker_has_no_computed_block():
    """预设曾放在 computed（无响应式依赖被永久缓存）——不得回退。

    mounted() 里 `buildPresets(new Date())` 是允许的：那不是 computed，
    每次挂载都重算，且 pick() 会再次重算。
    """
    src = _read("components/RangePicker.vue")
    assert "computed:" not in src


def test_range_picker_pick_recomputes_window():
    src = _read("components/RangePicker.vue")
    assert "import { buildPresets" in src
    # 点击时刷新预设再取同 key 的新窗口
    assert "refreshPresets()" in src
    assert "this.refreshPresets()" in src
    # 挂载时也要构建一次（默认近7天）
    assert "mounted()" in src


# ── 3. Analytics 刷新按钮推进预设窗口 ───────────────────────

def test_analytics_refresh_advances_preset_range():
    src = _read("views/Analytics.vue")
    assert "import { freshRange } from '../rangePresets.js'" in src
    assert "this.range = freshRange(this.range)" in src
    # range-bar 回传 value，预设按钮 active 态与父级同步
    assert '<RangePicker :value="range" @change="onRangeChange" />' in src
