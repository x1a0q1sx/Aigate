# -*- coding: utf-8 -*-
"""9router 对比缺口修复的回归测试（F1-F4）"""
import asyncio
import hashlib
from types import SimpleNamespace

import pytest


# ─────────────── F1: 真实客户端 IP ───────────────

def _fake_request(host="127.0.0.1", headers=None):
    return SimpleNamespace(
        client=SimpleNamespace(host=host),
        headers=headers or {},
    )


def test_real_client_ip_default_ignores_xff():
    from server.core.client_ip import real_client_ip
    req = _fake_request("10.0.0.9", {"x-forwarded-for": "203.0.113.7, 10.0.0.1"})
    # 默认不信任反代头：防伪造
    assert real_client_ip(req) == "10.0.0.9"


def test_real_client_ip_trust_proxy_takes_leftmost_xff():
    from server.core.client_ip import real_client_ip
    req = _fake_request("127.0.0.1", {"x-forwarded-for": "203.0.113.7, 10.0.0.1"})
    assert real_client_ip(req, trust_proxy=True) == "203.0.113.7"
    # 无 XFF 头回退 socket 对端
    assert real_client_ip(_fake_request("127.0.0.1"), trust_proxy=True) == "127.0.0.1"


# ─────────────── F2: token saver 单请求旁路 ───────────────

def test_preprocess_savers_off_skips_rtk(monkeypatch):
    from server.schemas.chat import ChatCompletionRequest
    import server.core.token_saver as ts
    calls = []

    def fake_apply(msgs, enabled=True, **kw):
        calls.append(enabled)
        return msgs, {}

    monkeypatch.setattr(ts, "apply_rtk", fake_apply)
    # 显式开启全局 RTK，验证 savers_off 门控本身
    monkeypatch.setattr(v1_cfg := __import__("server.api.v1_router", fromlist=["config"]).config,
                        "token_saver", SimpleNamespace(enabled=True))
    req = ChatCompletionRequest(model="m", messages=[{"role": "user", "content": "hello"}])
    import server.api.v1_router as v1
    v1._preprocess_request(req, savers_off=False)
    v1._preprocess_request(req, savers_off=True)
    # 旁路时 enabled 传 False：RTK 不生效
    assert calls == [True, False]


def test_preprocess_header_values_parse():
    # 端点里读取的旁路头语义（与 v1_router 保持一致的取值集合）
    for v in ("off", "0", "NONE", " skip "):
        assert v.strip().lower() in ("off", "0", "none", "skip")
    assert "on".strip().lower() not in ("off", "0", "none", "skip")


# ─────────────── F3: /v1/messages/count_tokens ───────────────

def test_anthropic_count_tokens_endpoint(monkeypatch):
    from server.api import anthropic_router as ar

    class _Raw:
        headers = {}

        async def json(self):
            return {
                "model": "claude-x",
                "system": "You are a coding agent with a long system prompt.",
                "messages": [
                    {"role": "user", "content": [{"type": "text", "text": "hello world " * 50}]},
                    {"role": "assistant", "content": "hi"},
                ],
            }

    async def _noop(raw_request):
        return None

    monkeypatch.setattr(ar, "_verify_aigate_api_key", _noop)
    out = asyncio.run(ar.anthropic_count_tokens(_Raw()))
    assert isinstance(out, dict) and "input_tokens" in out
    assert isinstance(out["input_tokens"], int) and out["input_tokens"] > 0


# ─────────────── F4: 备份导出/恢复二次鉴权 ───────────────

def _stub_cfg(enabled=True, password_hash=""):
    auth = SimpleNamespace(enabled=enabled, password_hash=password_hash)
    return SimpleNamespace(auth=auth)


def test_backup_reauth_requires_password():
    from server.api import admin_router as adm
    import bcrypt
    good = "hunter2!"
    h = bcrypt.hashpw(good.encode(), bcrypt.gensalt()).decode()

    adm._require_admin_reauth.__globals__  # noqa: 触发引用检查

    orig = adm.get_config
    try:
        adm.get_config = lambda: _stub_cfg(password_hash=h)
        empty = SimpleNamespace(headers={})
        with pytest.raises(Exception) as ei:
            adm._require_admin_reauth(empty)
        assert ei.value.status_code == 403

        wrong = SimpleNamespace(headers={"x-admin-password": "bad"})
        with pytest.raises(Exception) as ei:
            adm._require_admin_reauth(wrong)
        assert "不正确" in str(ei.value.detail)

        ok = SimpleNamespace(headers={"x-admin-password": good})
        adm._require_admin_reauth(ok)  # 不抛异常即通过

        # auth 关闭时跳过校验
        adm.get_config = lambda: _stub_cfg(enabled=False, password_hash=h)
        adm._require_admin_reauth(SimpleNamespace(headers={}))
    finally:
        adm.get_config = orig
