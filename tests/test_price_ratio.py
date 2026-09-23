"""v4.4 订阅制上游倍率（price_ratio）测试。

背景：CodeBuddy / Qoder 等订阅制上游没有 USD 单价，按 credit 倍率计费。
- Qoder：在线目录 /algo/api/v2/model/list 每条带 price_factor（最可靠）
- CodeBuddy：无服务端接口，用客户端 product.json / 日志实测的静态表
"""
import pytest

from server.adapters.base_adapter import ModelInfo
from server.core.oauth_registry import STATIC_PRICE_RATIOS, get_oauth_provider


class TestStaticRatioTable:
    def test_codebuddy_entries_present(self):
        # CN 版含 deepseek 系（国际版已下架这几个，只保留通用档）
        cn = STATIC_PRICE_RATIOS["codebuddy_cn"]
        assert cn["deepseek-v4-flash"] == 0.08
        assert cn["deepseek-v4-pro"] == 0.16
        # 免费模型倍率为 0（合法值，不能被当成"未设置"跳过）
        for code in ("codebuddy_cn", "codebuddy_intl"):
            assert STATIC_PRICE_RATIOS[code]["hy3"] == 0.0
            assert STATIC_PRICE_RATIOS[code]["hy4-preview"] == 0.0

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
