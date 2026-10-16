# -*- coding: utf-8 -*-
"""前端静态回归：模型刷新结果窗口内可直接禁用失败/超时的服务商（2026-10-17）。

需求（用户原话）：「模型刷新完成时失败/超时的允许在那个窗口里直接禁用服务商」。
场景：全量刷新模型列表时，某批服务商失败/超时（如出口抖动、站点 403、超时），
用户看到失败清单后要逐个去服务商页找、再点开关——现在直接在刷新结果弹窗里禁用。

锁死的契约：
1. ModelRefreshModal 失败条目渲染「禁用」按钮 + 多条目时「全部禁用」；
2. 禁用走既有 PUT /admin/api/providers/{id}（api.updateProvider { enabled:false }），
   不新增后端接口；
3. 禁用成功后发 provider-disabled 事件，宿主页（Models/Providers）同步本地列表；
4. 已禁用的条目标记「已禁用」不重复发请求（幂等防误点）。

无 JS 测试运行时，按仓库既有约定做源码静态扫描（同 test_frontend_range_freshness.py）。
"""
from pathlib import Path

_CLIENT = Path("client/src")


def _read(rel: str) -> str:
    return (_CLIENT / rel).read_text(encoding="utf-8")


# ── 1. 弹窗组件：禁用按钮 + 全部禁用 + 幂等标记 ───────────────

def test_modal_renders_disable_buttons_on_failed_items():
    src = _read("components/ModelRefreshModal.vue")
    # 失败条目渲染禁用按钮，调用 disableProvider
    assert "disableProvider(p)" in src
    # 多个未禁用条目时可一键全部禁用
    assert "disableAllFailed" in src
    assert "failedUndisabled" in src
    # 已禁用的条目标记「已禁用」，不再渲染禁用按钮（幂等）
    assert "isDisabled(p.provider_id)" in src
    assert "已禁用" in src
    # 后端回包的 enabled=false（刷新前就已禁用）同样视为已禁用
    assert "p.enabled === false" in src


def test_modal_uses_existing_update_provider_api():
    """禁用必须复用既有服务商更新接口，不新增后端端点。"""
    src = _read("components/ModelRefreshModal.vue")
    assert "api.updateProvider(p.provider_id, { enabled: false })" in src
    assert "import api from '../api.js'" in src
    # 不得出现自造接口路径
    assert "/admin/api/providers" not in src


def test_modal_emits_provider_disabled_event():
    src = _read("components/ModelRefreshModal.vue")
    assert "'provider-disabled'" in src
    assert "this.$emit('provider-disabled', p.provider_id)" in src


def test_modal_disabled_state_resets_on_reopen():
    """每次打开新结果重置「本次已禁用」标记，避免跨结果串状态。"""
    src = _read("components/ModelRefreshModal.vue")
    assert "watch:" in src
    assert "this.disabledIds = []" in src


# ── 2. 宿主页同步本地列表 ────────────────────────────────────

def test_models_page_syncs_disabled_provider():
    src = _read("views/Models.vue")
    assert '@provider-disabled="onRefreshProviderDisabled"' in src
    assert "onRefreshProviderDisabled(providerId)" in src
    assert "p.enabled = false" in src


def test_providers_page_syncs_disabled_provider():
    src = _read("views/Providers.vue")
    assert '@provider-disabled="onRefreshProviderDisabled"' in src
    assert "onRefreshProviderDisabled(providerId)" in src
    assert "p.enabled = false" in src
