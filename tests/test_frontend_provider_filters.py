# -*- coding: utf-8 -*-
"""前端静态回归：服务商开关联动 + 服务商页排序 + 新增账号强制选号（2026-10-13）。

1. 模型管理 / Playground 的服务商选择隐藏已关闭服务商（enabled=false）。
2. 服务商页默认排序：固定且开启 → 开启 → 固定关闭 → 关闭。
3. CodeBuddy 新增账号先走 SSO 登出链（sso_logout_url）强制选号。

按仓库既有约定做源码静态扫描锁死（无 JS 测试运行时）。
"""
from pathlib import Path

_CLIENT = Path("client/src")


def _read(rel: str) -> str:
    return (_CLIENT / rel).read_text(encoding="utf-8")


# ── 1. 已关闭服务商从选择器隐藏 ──────────────────────────────

def test_models_page_provider_selects_use_enabled_only():
    src = _read("views/Models.vue")
    # 筛选下拉与「添加模型」下拉都走 enabledProviders；不再有直连 providers 的选项
    assert "v-for=\"p in enabledProviders\"" in src
    assert "v-for=\"p in providers\"" not in src
    # computed 定义存在且按 enabled !== false 过滤
    assert "enabledProviders()" in src
    assert "p.enabled !== false" in src


def test_playground_provider_select_hides_disabled():
    src = _read("views/Playground.vue")
    assert "p.enabled !== false" in src
    assert "providersWithModels()" in src


# ── 2. 服务商页默认排序 ─────────────────────────────────────

def test_providers_default_sort_open_then_pinned():
    """默认排序 tier = 开启(2) + 固定(1)：固定且开启 > 开启 > 固定关闭 > 关闭。"""
    src = _read("views/Providers.vue")
    assert "固定+开启优先" in src
    # tier 函数包含 isProviderDisabled 与 isPinned 两个因子
    assert "this.isProviderDisabled(x) ? 0 : 2" in src
    assert "this.isPinned(x.id) ? 1 : 0" in src
    # 默认排序值仍是 pinned（localStorage 持久化的旧值 'pinned' 继续生效）
    assert "providerSort: 'pinned'" in src


# ── 3. 新增账号强制选号（SSO 登出链）────────────────────────

def test_oauth_page_uses_sso_logout_url_when_present():
    src = _read("views/OAuthConnections.vue")
    # connect 与 connectAsNew 两处 device_poll 分支都必须优先 sso_logout_url
    assert src.count("r.sso_logout_url || r.login_url") == 2
