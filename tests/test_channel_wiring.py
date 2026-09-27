# -*- coding: utf-8 -*-
"""渠道接线的集成守卫测试。

新渠道的接入是**跨多个共享文件**的（registry / oauth_client / router /
model_catalog / checkin / oauth_usage / credential_resolver / 前端），
漏掉任何一处都会让整条链路在某一步静默断掉：
- 漏 registry → 登录入口不显示，`_ensure_provider_registered` 直接 return
- 漏 oauth_client 的 refresh 分发 → 到期后无法续期（错误文案还指向上游）
- 漏 model_catalog 的 adapter 分支 → 静默回退 openai_compat（协议完全不符）
- 漏 checkin 的能力矩阵 → 签到页显示「待探测」而不是真的去签

本文件把这些接线逐条断言，避免「实现了但没接上」这类回归。
"""
import inspect


def test_registry_has_new_providers():
    from server.core.oauth_registry import get_oauth_provider
    for code, auth_mode, api_type in (
        ("lobsterai", "lobsterai", "openai_compat"),
        ("trae", "trae", "trae"),
        ("codearts", "codearts", "codearts"),
    ):
        p = get_oauth_provider(code)
        assert p is not None, f"registry 缺 {code}"
        assert (p.extra_params or {}).get("auth_mode") == auth_mode
        assert p.adapter_api_type == api_type
        assert (p.extra_params or {}).get("refresh_style") == auth_mode
        assert p.api_base_url, f"{code} 缺 api_base_url"
        assert p.static_models, f"{code} 缺静态种子"


def test_oauth_client_login_and_refresh_wired():
    """登录编排 + 续期分发都必须接上（漏任一处链路静默断）。"""
    from server.core import oauth_client as oc
    src = inspect.getsource(oc)
    # 登录方法
    assert "async def start_trae_login" in src
    assert "async def complete_trae_login" in src
    assert "async def start_codearts_login" in src
    assert "async def complete_codearts_login" in src
    # 续期分发（_do_refresh 的 refresh_style 链）
    assert '_style == "trae"' in src
    assert '_style == "codearts"' in src
    # 会话表
    assert "_trae_sessions" in src
    assert "_codearts_sessions" in src
    # CodeArts 的 refresh_token 列必须写（_do_refresh 以该列非空为前置判据）
    assert '"refresh_token": cred.refresh_token or ""' in src


def test_router_supports_manual_callback_for_all_three():
    from server.api import oauth_router as orr
    src = inspect.getsource(orr)
    assert '"lobsterai", "trae", "codearts"' in src      # complete-callback 白名单
    for mode in ("lobsterai", "trae", "codearts"):
        assert f'"auth_mode") == "{mode}"' in src, f"authorize 缺 {mode} 分支"
    assert "complete_trae_login" in src
    assert "complete_codearts_login" in src


def test_adapter_dispatch_registered():
    from server.core.model_catalog import create_adapter_for_provider
    from server.adapters.codearts_adapter import CodeArtsAdapter
    from server.adapters.trae_adapter import TraeAdapter
    assert isinstance(create_adapter_for_provider("codearts"), CodeArtsAdapter)
    assert isinstance(create_adapter_for_provider("trae"), TraeAdapter)


def test_checkin_capability_and_dispatch():
    from server.core.checkin import CHECKIN_CAPABILITIES, claim_for_provider
    assert CHECKIN_CAPABILITIES.get("lobsterai") is True
    assert CHECKIN_CAPABILITIES.get("codearts") is True
    assert CHECKIN_CAPABILITIES.get("trae") is True
    src = inspect.getsource(claim_for_provider)
    assert "_claim_lobsterai" in src
    assert "_claim_codearts" in src
    assert "_claim_trae" in src


def test_usage_dispatch_registered():
    from server.core.oauth_usage import _dispatch
    src = inspect.getsource(_dispatch)
    assert '_lobsterai_usage' in src
    assert '_codearts_usage' in src
    assert '_trae_usage' in src


def test_codearts_credential_is_json_not_bearer():
    """CodeArts 的凭据是 JSON（AK/SK/ST），resolver 必须校验格式。

    裸 token 发出去会以「验签 401」失败，错误文案完全不指向根因。
    """
    from server.core import credential_resolver as cr
    src = inspect.getsource(cr)
    assert 'oauth_code == "codearts"' in src
    assert "access_key_id" in src


def test_frontend_badge_covers_manual_callback():
    import io
    src = io.open("client/src/views/OAuthConnections.vue", encoding="utf-8").read()
    assert "'lobsterai', 'trae', 'codearts'" in src
    assert "manual_callback" in src


def test_trae_encryption_limitation_is_documented():
    """TRAE 的加密信封未实现必须如实登记（不能假装支持）。"""
    import server.core.trae as tr
    import server.adapters.trae_adapter as ta
    src = inspect.getsource(tr)
    assert "x-helios" in src or "x-medusa" in src
    assert hasattr(tr, "TRAE_ENCRYPTION_NOTE")
    # 受影响模型清单在适配器（显示名标注处）
    assert hasattr(ta, "TRAE_ENCRYPTED_ONLY_MODELS")
    assert "暂不可用" in inspect.getsource(ta.TraeAdapter.display_name_for)


def test_codearts_refresh_is_serialized_by_single_flight():
    """一次性 refresh_token 必须靠 Single Flight 串行化（docstring 也要留证）。"""
    import server.core.codearts as ca
    src = inspect.getsource(ca.refresh_token)
    assert "串行" in src or "一次性轮换" in src
    from server.core import oauth_client as oc
    oc_src = inspect.getsource(oc.OAuthClient._refresh_codearts)
    assert "Single Flight" in oc_src or "单飞" in oc_src or "串行" in oc_src


# ── Freebuff（Codebuff 免费层）接线守卫 ─────────────────

def test_freebuff_registry_entry():
    from server.core.oauth_registry import get_oauth_provider
    p = get_oauth_provider("freebuff")
    assert p is not None
    assert p.adapter_api_type == "freebuff"
    ep = p.extra_params or {}
    assert ep.get("auth_mode") == "freebuff_cli"
    assert ep.get("refresh_style") == "none"     # 长期凭证，无标准刷新
    assert p.static_models, "必须有种子模型（在线目录失败时的兜底）"


def test_freebuff_login_methods_present():
    """start/poll 登录方法必须存在（CLI 授权码轮询，无回调）。"""
    from server.core import oauth_client as oc
    assert hasattr(oc.OAuthClient, "start_freebuff_login")
    assert hasattr(oc.OAuthClient, "_poll_freebuff_login")
    src = inspect.getsource(oc.OAuthClient.start_freebuff_login)
    assert "start_cli_login" in src


def test_freebuff_router_branch():
    import io
    src = io.open("server/api/oauth_router.py", encoding="utf-8").read()
    assert 'auth_mode") == "freebuff_cli"' in src
    assert "start_freebuff_login" in src


def test_freebuff_adapter_dispatch():
    from server.core.model_catalog import create_adapter_for_provider
    from server.adapters.freebuff_adapter import FreebuffAdapter
    assert isinstance(create_adapter_for_provider("freebuff"), FreebuffAdapter)


def test_freebuff_usage_dispatch_registered():
    from server.core.oauth_usage import _dispatch
    src = inspect.getsource(_dispatch)
    assert "_freebuff_usage" in src


def test_freebuff_refresh_style_none_checked_before_refresh_token():
    """⚠️ refresh_style=none 必须在 refresh_token 检查**之前**。

    freebuff/u1s1 这类长期凭证本就没有 refresh_token；先检查会得到
    "no refresh_token stored" 的假错误（实现时实测踩到）。
    """
    from server.core import oauth_client as oc
    src = inspect.getsource(oc.OAuthClient._do_refresh)
    i_none = src.index('refresh_style") == "none"')
    i_tok = src.index("not existing.refresh_token_enc")
    assert i_none < i_tok, "长期凭证分支必须在 refresh_token 检查之前"


def test_freebuff_checkin_capability_false_with_reason():
    """Freebuff 的 streak 是「用过即打卡」（官方只读，无领取端点）→ 能力 False。"""
    from server.core.checkin import CHECKIN_CAPABILITIES, CHECKIN_NOTES
    assert CHECKIN_CAPABILITIES.get("freebuff") is False
    assert "freebuff" in CHECKIN_NOTES


def test_freebuff_adapter_documents_gating():
    """三段式门控 + 静默降级风险必须在适配器/协议模块里留证。"""
    import server.core.freebuff as fb
    import server.adapters.freebuff_adapter as fa
    src = inspect.getsource(fb)
    assert "Buffy" in src
    assert "mcp__" in src or "MCP_TOOL_PREFIX" in src
    assert "freebuff-countries" in src or "limited access" in src
    a_src = inspect.getsource(fa)
    assert "静默降级" in a_src or "silent" in a_src.lower()


def test_freebuff_country_tier_documented_in_registry():
    """国别分层是上游策略，必须在注册表注释里写明（避免被当 bug 修）。"""
    import server.core.oauth_registry as reg
    src = inspect.getsource(reg)
    assert "limited access" in src or "国别" in src
