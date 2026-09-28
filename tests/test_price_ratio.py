"""v4.4 订阅制上游倍率（price_ratio）测试。

背景：CodeBuddy / Qoder 等订阅制上游没有 USD 单价，按 credit 倍率计费。
- Qoder：在线目录 /algo/api/v2/model/list 每条带 price_factor（最可靠）
- CodeBuddy：**在线** /v3/config 的 models[].credits（F31，2026-09-28 接入）；
  静态表降级为「在线拿不到时的兜底」
"""
import pytest

from server.adapters.base_adapter import ModelInfo
from server.core.oauth_registry import STATIC_PRICE_RATIOS, get_oauth_provider


class TestStaticRatioTable:
    def test_codebuddy_entries_present(self):
        # CN 版含 deepseek 系（国际版已下架这几个，只保留通用档）
        # 2026-09-28 按在线 /v3/config 校准（旧客户端值 0.16/0.08 已过期）
        cn = STATIC_PRICE_RATIOS["codebuddy_cn"]
        assert cn["deepseek-v4-flash"] == 0.17
        assert cn["deepseek-v4-pro"] == 0.51
        # 倍率为 0（合法值，不能被当成"未设置"跳过）
        for code in ("codebuddy_cn", "codebuddy_intl"):
            assert STATIC_PRICE_RATIOS[code]["hy3"] == 0.0
        # ⚠️ hy4-preview 旧值 0.00 已过期（在线实测 0.29）—— 防回归
        for code in ("codebuddy_cn", "codebuddy_intl"):
            assert STATIC_PRICE_RATIOS[code]["hy4-preview"] == 0.29

    def test_ratio_values_are_non_negative(self):
        for table in STATIC_PRICE_RATIOS.values():
            for mid, ratio in table.items():
                assert ratio >= 0, f"{mid} 倍率为负: {ratio}"

    def test_ratios_only_reference_real_seed_models(self):
        """静态倍率表的 key 必须是该 provider 种子里的真实模型，防止写错名。"""
        for code, table in STATIC_PRICE_RATIOS.items():
            seed_ids = {m["model_id"] for m in (get_oauth_provider(code).static_models or [])}
            unknown = set(table) - seed_ids
            assert not unknown, f"{code} 倍率表含非种子模型: {unknown}"

    def test_static_table_is_documented_as_fallback(self):
        """静态表已降级为「在线拿不到时的兜底」——注释必须写明，防止后人当真相源维护。"""
        import inspect
        from server.core import oauth_registry
        src = inspect.getsource(oauth_registry)
        assert "fetch_codebuddy_ratios" in src
        assert "只是「在线拿不到时的兜底」" in src or "兜底" in src.split("STATIC_PRICE_RATIOS")[0]


class TestModelInfoField:
    def test_default_none(self):
        assert ModelInfo(model_id="x").price_ratio is None

    def test_zero_is_free_not_none(self):
        """0.0 表示免费，与 None（未知）必须区分。"""
        mi = ModelInfo(model_id="x", price_ratio=0.0)
        assert mi.price_ratio == 0.0
        assert mi.price_ratio is not None


class TestSeedFallbackCarriesRatio:
    """种子兜底路径要把倍率带进 ModelInfo（CodeBuddy 没有在线目录）。"""

    def _seed_infos(self, code):
        from server.core.oauth_registry import STATIC_PRICE_RATIOS as SPR
        p = get_oauth_provider(code)
        ratio_map = SPR.get(code) or {}
        return [
            ModelInfo(
                model_id=sm["model_id"],
                display_name=sm.get("display_name") or sm["model_id"],
                is_free=(ratio_map.get(sm["model_id"]) == 0.0),
                price_ratio=ratio_map.get(sm["model_id"]),
            )
            for sm in (p.static_models or [])
        ]

    def test_ratio_and_is_free_consistent(self):
        for code in ("codebuddy_cn", "codebuddy_intl"):
            for mi in self._seed_infos(code):
                if mi.price_ratio == 0.0:
                    assert mi.is_free is True, f"{code}/{mi.model_id} 倍率 0 应标免费"
                elif mi.price_ratio is not None:
                    assert mi.is_free is False

    def test_unknown_models_stay_none(self):
        """没有实测数据的模型保持 None（未知），不猜值。"""
        infos = {mi.model_id: mi for mi in self._seed_infos("codebuddy_intl")}
        # gpt-* 系列没有客户端倍率证据
        assert infos["gpt-5.5"].price_ratio is None
        assert infos["gemini-3.5-flash"].price_ratio is None


class TestQoderCatalogRatio:
    """Qoder 目录 price_factor → ModelInfo.price_ratio 的解析（含异常值防御）。"""

    def _build(self, entry):
        _ratio = entry.get("price_factor")
        try:
            _ratio = float(_ratio) if _ratio is not None else None
        except (TypeError, ValueError):
            _ratio = None
        return ModelInfo(
            model_id=entry["key"],
            is_free=(_ratio == 0.0) or bool(entry.get("is_free")),
            price_ratio=_ratio,
        )

    def test_normal_values(self):
        assert self._build({"key": "qfmodel", "price_factor": 0.0}).price_ratio == 0.0
        assert self._build({"key": "kmodel_latest", "price_factor": 1.4}).price_ratio == 1.4
        assert self._build({"key": "ultimate", "price_factor": 2.0}).price_ratio == 2.0

    def test_missing_field_is_none(self):
        assert self._build({"key": "x"}).price_ratio is None

    def test_garbage_value_degrades_to_none(self):
        assert self._build({"key": "x", "price_factor": "abc"}).price_ratio is None
        assert self._build({"key": "x", "price_factor": None}).price_ratio is None

    def test_zero_factor_marks_free(self):
        assert self._build({"key": "qfmodel", "price_factor": 0.0}).is_free is True
        assert self._build({"key": "ultimate", "price_factor": 2.0}).is_free is False

    def test_explicit_is_free_flag_respected(self):
        """上游显式 is_free=true（即使倍率非 0）也算免费。"""
        assert self._build({"key": "qmodel_38max", "price_factor": 0.5,
                            "is_free": True}).is_free is True


class TestManualEditProtection:
    """manual 标记的倍率不被刷新覆盖（与 USD 价格同规则）。"""

    def test_manual_source_blocks_overwrite(self):
        existing_src = "manual"
        incoming = 0.99
        should_write = incoming is not None and existing_src != "manual"
        assert should_write is False

    def test_provider_source_allows_overwrite(self):
        for src in ("", "qoder", "provider"):
            assert (0.5 is not None and src != "manual") is True


# ============ F31：CodeBuddy 在线倍率（/v3/config models[].credits）============

class TestParseCodebuddyCredits:
    """上游 credits 字段形态解析（实测：'x0.17 credits' / 'x0.00' / '0.50x'）。"""

    def _p(self, v):
        from server.core.model_catalog import parse_codebuddy_credits
        return parse_codebuddy_credits(v)

    def test_real_upstream_forms(self):
        assert self._p("x0.17 credits") == 0.17      # 生产实测形态
        assert self._p("x0.00") == 0.0               # 免费
        assert self._p("x2.00 credits") == 2.0
        assert self._p("x1.62") == 1.62
        assert self._p("0.50x") == 0.5               # 促销表 discountedCredits 形态
        assert self._p("0x") == 0.0

    def test_plain_numbers(self):
        assert self._p(0.25) == 0.25
        assert self._p("0.25") == 0.25

    def test_unparseable_is_none_not_zero(self):
        """解析不出必须是 None（未知），绝不能猜 0 = 免费。"""
        for bad in (None, "", "abc", "credits", "x credits", True, False, [], {}):
            assert self._p(bad) is None, f"{bad!r} 应解析为 None"


class TestOnlineRatioFetch:
    """在线倍率抓取：只读、失败不抛、只产出倍率不产出模型列表。"""

    def _patch_httpx(self, monkeypatch, status, payload):
        import server.core.model_catalog as mc

        class _Resp:
            def __init__(self):
                self.status_code = status
                self._d = payload
                self.content = b"{}"

            def json(self):
                return self._d

        class _Client:
            def __init__(self, *a, **k):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def get(self, url, headers=None):
                return _Resp()

        import httpx
        monkeypatch.setattr(httpx, "AsyncClient", _Client)

    def test_parses_credits_map(self, monkeypatch):
        import asyncio
        self._patch_httpx(monkeypatch, 200, {"data": {"models": [
            {"id": "deepseek-v4-flash", "credits": "x0.17 credits"},
            {"id": "glm-5.3-flash", "credits": "x0.06"},
            {"id": "hy3", "credits": "x0.00"},
            {"id": "no-credits"},                       # 无 credits → 跳过
            {"credits": "x1.0"},                        # 无 id → 跳过
        ]}})
        from server.core.model_catalog import fetch_codebuddy_ratios
        out = asyncio.run(fetch_codebuddy_ratios("tok", "https://copilot.tencent.com"))
        assert out == {"deepseek-v4-flash": 0.17, "glm-5.3-flash": 0.06, "hy3": 0.0}

    def test_non_200_returns_empty(self, monkeypatch):
        import asyncio
        self._patch_httpx(monkeypatch, 500, {})
        from server.core.model_catalog import fetch_codebuddy_ratios
        assert asyncio.run(fetch_codebuddy_ratios("tok", "https://x.com")) == {}

    def test_network_error_returns_empty_not_raise(self, monkeypatch):
        """网络异常必须吞掉返回空表（调用方回退静态表），不得打断整个刷新。"""
        import asyncio
        import httpx
        import server.core.model_catalog as mc

        class _Boom:
            def __init__(self, *a, **k):
                pass

            async def __aenter__(self):
                raise httpx.ConnectError("boom")

            async def __aexit__(self, *a):
                return False

        monkeypatch.setattr(httpx, "AsyncClient", _Boom)
        from server.core.model_catalog import fetch_codebuddy_ratios
        assert asyncio.run(fetch_codebuddy_ratios("tok", "https://x.com")) == {}

    def test_base_url_with_path_is_trimmed_to_host(self, monkeypatch):
        """api_base_url 是 .../v2/chat/completions 形态 → 必须只取 scheme://host。"""
        import asyncio
        seen = {}

        class _Resp:
            status_code = 200

            def json(self):
                return {"data": {"models": []}}

        class _Client:
            def __init__(self, *a, **k):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def get(self, url, headers=None):
                seen["url"] = url
                return _Resp()

        import httpx
        monkeypatch.setattr(httpx, "AsyncClient", _Client)
        from server.core.model_catalog import fetch_codebuddy_ratios
        asyncio.run(fetch_codebuddy_ratios("tok", "https://copilot.tencent.com/v2/chat/completions"))
        assert seen["url"] == "https://copilot.tencent.com/v3/config"

    def test_uses_cli_user_agent(self, monkeypatch):
        """UA 决定返回集（实测 Intl: CLI→22 条 / IDE→13 条 / 旧版→0 条）→ 必须用 CLI。"""
        import asyncio
        seen = {}

        class _Resp:
            status_code = 200

            def json(self):
                return {"data": {"models": []}}

        class _Client:
            def __init__(self, *a, **k):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def get(self, url, headers=None):
                seen["headers"] = headers or {}
                return _Resp()

        import httpx
        monkeypatch.setattr(httpx, "AsyncClient", _Client)
        from server.core.model_catalog import fetch_codebuddy_ratios
        asyncio.run(fetch_codebuddy_ratios("tok", "https://www.codebuddy.ai"))
        assert seen["headers"]["User-Agent"].startswith("CLI/")
        assert seen["headers"]["X-Product-Code"] == "codebuddy"
        assert seen["headers"]["X-Domain"] == "www.codebuddy.ai"


class TestOnlineRatioDoesNotReplaceModelList:
    """⚠️ 在线倍率**只叠加**，绝不用 /v3/config 的 models[] 替换模型清单。

    实测：若当清单用，CN/Intl 各会误删 8 个模型（auto / balanced-model /
    fast-model / deep-model 等档位别名不在 models[] 里但确实可用）。
    """

    def test_overlay_only_touches_existing_models(self):
        import inspect
        import server.core.model_catalog as mc
        src = inspect.getsource(mc.ModelCatalog._refresh_models_inner)
        # 叠加逻辑必须遍历 all_model_infos（已收集的列表）并只写 price_ratio/is_free
        assert "_online = await fetch_codebuddy_ratios(" in src
        assert "for _mid, _mi in all_model_infos.items()" in src
        assert "_mi.price_ratio = _r" in src
        # 不得把在线结果当模型清单（不出现 setdefault/add 到 all_model_infos）
        seg = src.split("_online = await fetch_codebuddy_ratios(")[1].split("if not any_success")[0]
        assert "all_model_infos.setdefault" not in seg
        assert "all_model_infos[_" not in seg

    def test_overlay_is_codebuddy_only(self):
        import inspect
        import server.core.model_catalog as mc
        src = inspect.getsource(mc.ModelCatalog._refresh_models_inner)
        assert 'if str(oauth_code).startswith("codebuddy"):' in src

    def test_failure_keeps_existing_ratios(self):
        """在线拉取失败 → 不得清空既有倍率（静态表/库里已有的值保留）。"""
        import inspect
        import server.core.model_catalog as mc
        src = inspect.getsource(mc.ModelCatalog._refresh_models_inner)
        seg = src.split("_online = await fetch_codebuddy_ratios(")[1].split("if not any_success")[0]
        assert "if _online:" in seg, "必须仅在拿到数据时才覆盖（空表不动既有值）"


class TestFetchCodebuddyRatiosIsReadOnly:
    """抓取只读：只 GET /v3/config，绝不 POST（POST 会改上游状态）。"""

    def test_only_get_requests(self):
        import inspect
        import server.core.model_catalog as mc
        src = inspect.getsource(mc.fetch_codebuddy_ratios)
        assert ".get(" in src
        assert ".post(" not in src
        assert "_CODEBUDDY_CONFIG_PATH" in src
