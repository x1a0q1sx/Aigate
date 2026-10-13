# -*- coding: utf-8 -*-
"""前端静态回归：Playground 鹈鹕一键测试 + 分析页布局调整（2026-10-12）。

1. Playground「鹈鹕一键测试」：发送键旁按钮 → 流式生成（apiStream SSE 解析）
   → extractHtml 提取 → iframe sandbox 预览 + Blob 下载（文件名 服务商-模型-日期时间）
2. 分析页：实时区整体删除（2026-10-13 用户要求）；请求日志（LogsPanel）
   位于用量趋势上方。

均为纯前端改动，按仓库既有约定做源码静态扫描锁死（无 JS 测试运行时）。
"""
from pathlib import Path

_CLIENT = Path("client/src")


def _read(rel: str) -> str:
    return (_CLIENT / rel).read_text(encoding="utf-8")


# ── 1. Playground 鹈鹕一键测试 ──────────────────────────────

def test_playground_has_pelican_button_next_to_send():
    src = _read("views/Playground.vue")
    assert "鹈鹕一键测试" in src
    assert "runPelican" in src
    # 发送键与鹈鹕键同一按钮行
    assert 'class="btn-row"' in src
    assert "发送 🚀" in src


def test_pelican_prompt_is_exact():
    src = _read("views/Playground.vue")
    assert "创建一个HTML，内容是SVG绘制一个鹈鹕骑自行车的2D动画，你不需要任何测试" in src


def test_pelican_uses_stream_and_extracts_html():
    src = _read("views/Playground.vue")
    # 流式请求走 apiStream（SSE 解析 delta.content 增量累积）
    assert "apiStream" in src
    assert "delta?.content" in src or "delta.content" in src
    # HTML 提取：围栏 / DOCTYPE / svg 兜底
    assert "extractHtml" in src
    assert "<!DOCTYPE" in src and "<svg" in src


def test_pelican_captures_reasoning_for_timeout_diagnosis():
    """慢模型只有思考没有正文时，必须能提示「生成太慢撞超时」而非裸报空。"""
    src = _read("views/Playground.vue")
    assert "reasoning_content" in src
    assert "只有思考" in src or "没有正文" in src


def test_pelican_preview_iframe_is_sandboxed():
    src = _read("views/Playground.vue")
    assert 'sandbox="allow-scripts"' in src
    assert ":srcdoc=" in src


def test_pelican_download_filename_pattern():
    src = _read("views/Playground.vue")
    # 服务商-模型-日期时间.html（下载链接 + Blob URL）
    assert "stamp()" in src
    assert ".html" in src
    assert "URL.createObjectURL" in src
    assert ":download=" in src


def test_api_js_exports_stream_helper():
    src = _read("api.js")
    assert "async function apiStream" in src
    # 命名导出必须包含（Playground 用 import { apiStream }）
    assert "  apiStream" in src.split("export {")[1]


# ── 2. 分析页布局调整 ──────────────────────────────────────

def test_analytics_realtime_panel_removed():
    """实时区整体删除（2026-10-13 用户要求）：组件文件 + 页面引用都不在。"""
    src = _read("views/Analytics.vue")
    assert "RealtimePanel" not in src
    assert "实时区已并入本页" not in src
    assert not (_CLIENT / "components/RealtimePanel.vue").exists()


def test_monitor_route_no_realtime_hash():
    """旧 /monitor 地址仍重定向到分析页，但不再带 #realtime 锚点。"""
    src = _read("router.js")
    assert "{ path: '/monitor', redirect: { path: '/analytics' } }" in src


def test_analytics_logs_panel_moved_above_trend():
    src = _read("views/Analytics.vue")
    logs_idx = src.index('<LogsPanel ref="logs"')
    trend_idx = src.index("用量趋势")
    assert logs_idx < trend_idx, "请求日志（LogsPanel）必须位于用量趋势上方"
    # 不得残留第二份 LogsPanel（搬移而非复制）
    assert src.count("<LogsPanel") == 1


def test_analytics_see_logs_scrolls_to_panel():
    src = _read("views/Analytics.vue")
    # 日志区在按钮上方，看日志需滚动过去
    assert "scrollIntoView" in src
