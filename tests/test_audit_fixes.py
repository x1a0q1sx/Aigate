"""审计修复回归测试（P1-4/5/6/7/10/12/14/15/17/18/20/21/22 + P2 数项）。

每条对应 docs/findings-bughunt-2026-09-18.md 的一个编号缺陷。
"""
import asyncio
import time
from datetime import datetime, timedelta

import pytest


# ── P1-4 session sticky ─────────────────────────────────────────────
class TestStickyKeyDerivation:
    def _req(self, headers=None):
        class _R:
            def __init__(self, h):
                self.headers = h or {}
        return _R(headers)

    def test_explicit_header_wins(self):
        from server.core.client_ip import derive_conversation_id
        cid, explicit = derive_conversation_id(
            self._req({"x-conversation-id": "abc-123"}), [])
        assert cid == "abc-123" and explicit is True

    def test_stable_across_calls(self):
        """同一客户端 + 同一 system + 同一首条 user → 同 key（sticky 才能命中）"""
        from server.core.client_ip import derive_conversation_id
        msgs = [{"role": "system", "content": "you are a bot"},
                {"role": "user", "content": "hello"}]
        a, ea = derive_conversation_id(self._req(), msgs, "1.2.3.4")
        b, eb = derive_conversation_id(self._req(), list(msgs), "1.2.3.4")
        assert a == b and ea is False and eb is False

    def test_different_first_message_differs(self):
        from server.core.client_ip import derive_conversation_id
        a, _ = derive_conversation_id(
            self._req(), [{"role": "user", "content": "q1"}], "1.2.3.4")
        b, _ = derive_conversation_id(
            self._req(), [{"role": "user", "content": "q2"}], "1.2.3.4")
        assert a != b

    def test_different_client_differs(self):
        from server.core.client_ip import derive_conversation_id
        msgs = [{"role": "user", "content": "hi"}]
        a, _ = derive_conversation_id(self._req(), msgs, "1.1.1.1")
        b, _ = derive_conversation_id(self._req(), msgs, "2.2.2.2")
        assert a != b

    def test_multimodal_content_list(self):
        from server.core.client_ip import derive_conversation_id
        msgs = [{"role": "user", "content": [
            {"type": "text", "text": "describe"},
            {"type": "image_url", "image_url": {"url": "x"}},
        ]}]
        cid, _ = derive_conversation_id(self._req(), msgs, "1.2.3.4")
        assert cid.startswith("conv-")


class TestStickyCacheTtl:
    """P1-4: 必须用 monotonic 秒数比较（naive datetime 会被按本地时区折算）。"""

    def _router(self):
        from server.core.auto_router import AutoRouter
        return AutoRouter()

    def test_set_and_get_roundtrip(self):
        ar = self._router()
        ar._set_sticky("c1", 42)
        assert ar._get_sticky_model("c1") == 42

    def test_expired_entry_cleared(self):
        ar = self._router()
        ar._set_sticky("c1", 42)
        # 直接把时间戳挪到过去
        ar._sticky_cache["c1"] = (42, time.monotonic() - 10_000)
        assert ar._get_sticky_model("c1") is None
        assert "c1" not in ar._sticky_cache

    def test_unknown_key_returns_none(self):
        assert self._router()._get_sticky_model("nope") is None

    def test_capacity_prune(self):
        from server.core import auto_router as _m
        ar = self._router()
        for i in range(_m._STICKY_CACHE_MAX + 50):
            ar._set_sticky(f"c{i}", i)
        assert len(ar._sticky_cache) <= _m._STICKY_CACHE_MAX


class TestStickyNeverReturnsNone:
    """P1-4: 契约——调用方无条件读 .success，绝不能返回 None。"""

    def test_try_sticky_returns_none_only(self):
        """_try_sticky_candidate 的失败语义就是 None（由调用方回落选举）。"""
        import inspect
        from server.core.auto_router import AutoRouter
        src = inspect.getsource(AutoRouter._try_sticky_candidate)
        # 内部所有失败分支都应是 return None（而不是裸 return 或抛异常）
        assert "return None" in src

    def test_get_best_candidate_annotated_route_result(self):
        import inspect
        from server.core.auto_router import AutoRouter
        sig = inspect.signature(AutoRouter.get_best_candidate)
        assert "RouteResult" in str(sig.return_annotation)


# ── P1-7 fusion judge attempt ───────────────────────────────────────
class TestDecisionAttemptCoercion:
    def test_string_attempt_does_not_raise(self):
        """fusion 传 attempt="judge" 时不能抛 ValueError（此前把 select+finish 一起吞掉）"""
        from server.core import route_decision as rd
        rd.begin_decision("t-judge", "auto", "auto")
        try:
            rd.add_attempt("t-judge", provider="p", status="failed", attempt="judge",
                           error="judge failed")
            trace = rd._active.get("t-judge")
            assert trace is not None
            assert trace.attempts, "attempt 应被记录"
            assert str(trace.attempts[-1]["attempt"]) == "judge"
        finally:
            rd._active.pop("t-judge", None)

    def test_int_attempt_still_int(self):
        from server.core import route_decision as rd
        rd.begin_decision("t-int", "auto", "auto")
        try:
            rd.add_attempt("t-int", provider="p", status="ok", attempt=3)
            trace = rd._active.get("t-int")
            assert trace.attempts[-1]["attempt"] == 3
        finally:
            rd._active.pop("t-int", None)


# ── P1-10 rate_limiter ──────────────────────────────────────────────
class TestRateLimiterWindows:
    def test_day_window_rolls_rpd_tpd(self):
        """P2: 跨天必须重置 rpd/tpd（此前只增不减 → 日限永不生效）"""
        from server.core.rate_limiter import RateLimiter
        from server.models.rate_limit import RateLimitState

        rl = RateLimiter()
        st = RateLimitState(model_id=1, key_id=None, rpm_current=5, rpd_current=99,
                            tpm_current=7, tpd_current=12345,
                            window_start=datetime.utcnow() - timedelta(days=1))
        changed = rl._roll_windows(st)
        assert changed is True
        assert st.rpd_current == 0 and st.tpd_current == 0
        assert st.rpm_current == 0 and st.tpm_current == 0

    def test_same_minute_no_reset(self):
        from server.core.rate_limiter import RateLimiter
        from server.models.rate_limit import RateLimitState
        rl = RateLimiter()
        st = RateLimitState(model_id=1, key_id=None, rpm_current=5, rpd_current=9,
                            tpm_current=7, tpd_current=11,
                            window_start=rl._get_window_start())
        assert rl._roll_windows(st) is False
        assert st.rpm_current == 5 and st.rpd_current == 9

    def test_day_limit_configurable(self):
        from server.core.rate_limiter import RateLimiter
        rl = RateLimiter(default_rpd=100, default_tpd=1000)
        assert rl.default_rpd == 100 and rl.default_tpd == 1000

    def test_daily_limit_defaults_off(self):
        from server.core.rate_limiter import RateLimiter
        rl = RateLimiter()
        assert rl.default_rpd == 0 and rl.default_tpd == 0


class TestRateLimiterUpsert:
    def test_uses_dialect_insert(self):
        """P1-10: 必须用方言版 insert（通用版没有 on_conflict_do_nothing）"""
        import inspect
        from server.core.rate_limiter import RateLimiter
        src = inspect.getsource(RateLimiter.get_or_create_state)
        assert "dialects.sqlite import insert" in src
        assert "from sqlalchemy import insert" not in src

    def test_integrity_error_handled(self):
        import inspect
        from server.core.rate_limiter import RateLimiter
        src = inspect.getsource(RateLimiter.get_or_create_state)
        assert "IntegrityError" in src


# ── P1-12 headroom ─────────────────────────────────────────────────
class TestHeadroomWired:
    def test_auto_router_checks_headroom(self):
        """P1-12: headroom 必须真的参与路由（此前只是展示）"""
        import inspect
        from server.core.auto_router import AutoRouter
        src = inspect.getsource(AutoRouter.get_best_candidate)
        assert "headroom" in src
        assert "_headroom_blocked" in src

    def test_headroom_failure_is_safe(self):
        """统计失败不能饿死候选（必须 try/except 包住）"""
        import inspect
        from server.core.auto_router import AutoRouter
        src = inspect.getsource(AutoRouter.get_best_candidate)
        assert "headroom check skipped" in src


# ── P1-14 single-flight ────────────────────────────────────────────
class TestSingleFlight:
    def test_waiter_returns_leader_result(self):
        """P1-14: 合并方必须回传 leader 的真实结果，不能无条件报成功"""
        import inspect
        from server.core.oauth_client import OAuthClient
        src = inspect.getsource(OAuthClient.refresh_token)
        assert "merged into inflight refresh" not in src
        assert "asyncio.shield" in src

    def test_set_guarded_by_done(self):
        import inspect
        from server.core.oauth_client import OAuthClient
        src = inspect.getsource(OAuthClient.refresh_token)
        assert "fut.done()" in src


# ── P1-15 refresh failure classification ───────────────────────────
class TestRefreshCredentialDead:
    def test_401_is_dead(self):
        from server.core.oauth_client import _refresh_credential_dead
        assert _refresh_credential_dead(401, "") is True

    def test_400_invalid_grant_is_dead(self):
        from server.core.oauth_client import _refresh_credential_dead
        assert _refresh_credential_dead(400, '{"error":"invalid_grant"}') is True

    def test_429_is_transient(self):
        from server.core.oauth_client import _refresh_credential_dead
        assert _refresh_credential_dead(429, "rate limited") is False

    def test_5xx_is_transient(self):
        from server.core.oauth_client import _refresh_credential_dead
        for code in (500, 502, 503, 504):
            assert _refresh_credential_dead(code, "upstream down") is False

    def test_400_without_marker_is_transient(self):
        from server.core.oauth_client import _refresh_credential_dead
        assert _refresh_credential_dead(400, "bad request") is False

    def test_403_without_marker_is_transient(self):
        from server.core.oauth_client import _refresh_credential_dead
        assert _refresh_credential_dead(403, "risk control") is False


# ── P1-17 manual price protection ──────────────────────────────────
class TestManualPriceProtection:
    def _src(self):
        import inspect
        from server.core.model_catalog import ModelCatalog
        return inspect.getsource(ModelCatalog._refresh_models_inner)

    def test_pricing_source_write_guarded(self):
        """P1-17: pricing_source 写入必须在 not manual_priced 分支内"""
        src = self._src()
        idx = src.find("pricing_source = pricing_result.source_url")
        assert idx > 0
        # 该行之前最近的分支判断应是 not manual_priced
        head = src[:idx]
        assert "if not manual_priced:" in head[-400:]

    def test_success_rate_none_guarded(self):
        assert "if _sr is not None:" in self._src()


# ── P1-18 intel manual source ──────────────────────────────────────
class TestIntelManualSource:
    def test_upsert_sets_manual(self):
        """P1-18: 面板录入必须打 manual 标记，否则每周同步被覆盖"""
        import inspect
        from server.api import admin_routing
        src = inspect.getsource(admin_routing.upsert_intel)
        assert 'source="manual"' in src
        assert 'existing.source = "manual"' in src


# ── P1-20 delete cleanup ───────────────────────────────────────────
class TestDeleteCleanup:
    def test_model_ids_selected_before_delete(self):
        """P1-20: id 必须在 DELETE 之前取出（此前先删再查 → 恒空）"""
        import inspect
        from server.api import admin_router
        src = inspect.getsource(admin_router.delete_provider)
        i_select = src.find("model_ids = list")
        i_delete = src.find("delete(_M).where")
        assert i_select > 0 and i_delete > 0
        assert i_select < i_delete, "model_ids 必须在 DELETE 之前查询"

    def test_orphan_tables_cleaned(self):
        import inspect
        from server.api import admin_router
        src = inspect.getsource(admin_router.delete_provider)
        for t in ("_HC", "_RL", "_MAK"):
            assert t in src, f"应清理 {t}"

    def test_delete_key_forgets_rotator(self):
        import inspect
        from server.core.key_manager import KeyManager
        src = inspect.getsource(KeyManager.delete_key)
        assert "forget_key" in src
        assert "ModelApiKey" in src

    def test_forget_key_clears_cursors(self):
        from server.core.key_rotator import KeyRotator
        kr = KeyRotator.__new__(KeyRotator)
        kr._cursor = {1: 7, 2: 8}
        kr._model_cursor = {10: 7, 11: 9}
        kr._fail_count = {7: 3}
        kr._cooldown_until = {7: datetime.utcnow() + timedelta(hours=1)}
        kr._hard_disabled = {7}
        kr.forget_key(7)
        assert 7 not in kr._hard_disabled
        assert 7 not in kr._fail_count
        assert 1 not in kr._cursor and 2 in kr._cursor
        assert 10 not in kr._model_cursor and 11 in kr._model_cursor


# ── P1-21 reauth on plaintext secrets ──────────────────────────────
class TestPlaintextReauth:
    def test_export_requires_reauth(self):
        import inspect
        from server.api import admin_router
        src = inspect.getsource(admin_router.export_providers)
        assert "_require_admin_reauth" in src

    def test_reveal_requires_reauth(self):
        import inspect
        from server.api import admin_router
        for fn in (admin_router.reveal_key, admin_router.get_aigate_key):
            assert "_require_admin_reauth" in inspect.getsource(fn)


# ── P1-22 gemini streaming ─────────────────────────────────────────
class TestGeminiStreaming:
    def test_stream_flag_passed(self):
        import inspect
        from server.core.gemini_converter import gemini_to_chat
        assert "stream" in inspect.signature(gemini_to_chat).parameters

    def test_no_hardcoded_false(self):
        import inspect
        from server.core.gemini_converter import gemini_to_chat
        src = inspect.getsource(gemini_to_chat)
        assert '"stream": False' not in src
        assert "bool(stream)" in src

    def test_router_passes_stream_for_sse_action(self):
        import inspect
        from server.api import gemini_router
        src = inspect.getsource(gemini_router)
        assert 'stream=(action == "streamGenerateContent")' in src

    def test_chunk_to_gemini_keeps_tool_calls(self):
        from server.core.gemini_converter import chunk_to_gemini
        chunk = {"choices": [{"delta": {"tool_calls": [
            {"function": {"name": "get_weather", "arguments": '{"city":"SF"}'}}]}}]}
        g = chunk_to_gemini(chunk)
        assert g is not None
        parts = g["candidates"][0]["content"]["parts"]
        assert any("functionCall" in p for p in parts)
        fc = [p for p in parts if "functionCall" in p][0]["functionCall"]
        assert fc["name"] == "get_weather" and fc["args"] == {"city": "SF"}

    def test_chunk_to_gemini_surfaces_error(self):
        from server.core.gemini_converter import chunk_to_gemini
        g = chunk_to_gemini({"error": {"code": 502, "message": "boom"}})
        assert g and g.get("error")

    def test_sse_generator_has_finally_aclose(self):
        import inspect
        from server.api import gemini_router
        src = inspect.getsource(gemini_router._openai_sse_to_gemini_stream)
        assert "finally:" in src and "aclose" in src


# ── P2 items ───────────────────────────────────────────────────────
class TestRawResponseNotLeaked:
    def test_response_pop_before_return(self):
        import inspect
        from server.api import v1_router
        src = inspect.getsource(v1_router._chat_completions_impl)
        assert 'response.pop("_raw_response", None)' in src


class TestBuiltinPricingLongestMatch:
    def test_longest_prefix_wins(self):
        """P2: gpt-4o-mini-* 必须命中 mini 档而非 gpt-4o 档"""
        from server.core.model_catalog import get_builtin_pricing, BUILTIN_PRICING
        if "gpt-4o" not in BUILTIN_PRICING or "gpt-4o-mini" not in BUILTIN_PRICING:
            pytest.skip("内置表无 gpt-4o 系列")
        got = get_builtin_pricing("gpt-4o-mini-2024-07-18")
        assert got == BUILTIN_PRICING["gpt-4o-mini"]

    def test_exact_match_still_first(self):
        from server.core.model_catalog import get_builtin_pricing, BUILTIN_PRICING
        key = next(iter(BUILTIN_PRICING))
        assert get_builtin_pricing(key) == BUILTIN_PRICING[key]

    def test_no_match_returns_none(self):
        from server.core.model_catalog import get_builtin_pricing
        assert get_builtin_pricing("definitely-not-a-model-xyz") is None


class TestForceStreamUsage:
    def test_stream_options_injected(self):
        """P2: 强制流式时注入 stream_options，否则聚合 usage 恒 0"""
        from server.core.provider_quirks import transform_payload, quirks_for
        q = quirks_for("https://copilot.tencent.com/v2/chat/completions")
        assert q is not None and q.force_stream
        out = transform_payload({"model": "m", "messages": [{"role": "user", "content": "hi"}]}, q)
        assert out.get("stream") is True
        assert out.get("stream_options") == {"include_usage": True}

    def test_existing_stream_options_preserved(self):
        from server.core.provider_quirks import transform_payload, quirks_for
        q = quirks_for("https://copilot.tencent.com/v2/chat/completions")
        out = transform_payload({"messages": [{"role": "user", "content": "hi"}],
                                 "stream_options": {"include_usage": False}}, q)
        assert out["stream_options"] == {"include_usage": False}


class TestCodexDeltaItemId:
    def test_delta_slot_aliasing(self):
        import inspect
        from server.adapters.codex_responses import CodexResponsesAdapter
        src = inspect.getsource(CodexResponsesAdapter)
        # delta 事件按 item_id 建槽/别名
        assert "_item_id" in src
        assert '"item_id": item.get("id") or ""' in src
