"""Cline 免费模型获取不到 — models URL 拼装与裸数组响应容错回归。

根因：注册表里 cline/codebuddy 的 api_base_url 填到推理端点级别
（…/chat/completions），_build_models_url 又在尾部拼 /v1/models，
得到 …/chat/completions/v1/models → 404 → 刷新只能回退静态种子，
Cline 445 个实时模型（含 ~40 个 :free 免费模型）永远进不来。
"""
import asyncio
import json

import pytest

import server.adapters.openai_compat as oa_mod
from server.adapters.openai_compat import OpenAICompatAdapter


# ── _build_models_url ────────────────────────────────────────

@pytest.mark.parametrize("base,expect", [
    # Cline：推理端点 = API 根 + 端点后缀，models 挂在根下
    ("https://api.cline.bot/api/v1/chat/completions",
     "https://api.cline.bot/api/v1/models"),
    # CodeBuddy 同款病灶
    ("https://copilot.tencent.com/v2/chat/completions",
     "https://copilot.tencent.com/v2/models"),
    # 常规形态不受影响
    ("https://api.deepseek.com/v1", "https://api.deepseek.com/v1/models"),
    ("https://api.openai.com", "https://api.openai.com/v1/models"),
    ("https://api.openai.com/", "https://api.openai.com/v1/models"),
    # 腾讯 paas 专用形态保持原样
    ("https://skill.ai.qq.com/api/paas/v3/application/xxx",
     "https://skill.ai.qq.com/api/paas/v3/application/xxx/models"),
    # messages/responses 端点后缀同理剥离
    ("https://x.example/api/v1/messages", "https://x.example/api/v1/models"),
    ("https://y.example/v1/responses", "https://y.example/v1/models"),
])
def test_models_url_derivation(base, expect):
    assert OpenAICompatAdapter()._build_models_url(base) == expect


# ── list_models 裸数组 / 字段容错 ────────────────────────────

class _Resp:
    def __init__(self, payload):
        self._payload = payload
        self.status_code = 200

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


class _Client:
    def __init__(self, payload):
        self.payload = payload
        self.last_url = None
        self.last_headers = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, url, headers=None):
        self.last_url = url
        self.last_headers = headers
        return _Resp(self.payload)


def _patch_client(monkeypatch, payload):
    fake = _Client(payload)

    def _factory(*a, **kw):
        return fake
    monkeypatch.setattr(oa_mod.httpx, "AsyncClient", _factory)
    return fake


def test_list_models_bare_array_with_free_flag(monkeypatch):
    payload = [
        {"id": "qwen/qwen3.8-27b:free", "object": "model", "created": 1, "owned_by": "qwen"},
        {"id": "anthropic/claude-sonnet-4.6", "object": "model", "created": 2, "owned_by": "anthropic"},
        "junk-non-dict",
        {"no_id": True},
    ]
    fake = _patch_client(monkeypatch, payload)
    models = asyncio.run(OpenAICompatAdapter().list_models(
        "tok", "https://api.cline.bot/api/v1/chat/completions"))
    ids = [m.model_id for m in models]
    assert ids == ["qwen/qwen3.8-27b:free", "anthropic/claude-sonnet-4.6"]
    assert models[0].is_free is True      # :free 后缀命中既有免费判定
    assert models[1].is_free is False
    assert fake.last_url == "https://api.cline.bot/api/v1/models"
    # Cline 域名方言头 + workos JWT 前缀在真实链路验证过，这里只断言拼装到位
    assert fake.last_headers.get("HTTP-Referer") == "https://cline.bot"


def test_list_models_data_wrapper_still_works(monkeypatch):
    payload = {"data": [{"id": "gpt-x"}, {"id": "free-model-lite"}]}
    _patch_client(monkeypatch, payload)
    models = asyncio.run(OpenAICompatAdapter().list_models(
        "k", "https://api.somewhere.com/v1"))
    assert [m.model_id for m in models] == ["gpt-x", "free-model-lite"]
    assert models[1].is_free is True
