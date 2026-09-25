# -*- coding: utf-8 -*-
"""错误信息落库不截断的回归测试。

背景：此前链路上有 120 / 300 / 500 三处截断，用户看到的 error_msg 根本不含
上游原文 —— 排障必须去翻 pm2 日志。现在统一分工：

- **控制流判据**（is_context_error / 冷却提示）用短形态 `err_short`
- **落库 error_msg** 用全量（`_full_err_text` / `_compose_full_error`），
  上限 `ERROR_MSG_MAX_CHARS` 只作防御
- **回给下游客户端**的 attempts 剥掉 `error_detail`（`_strip_error_detail`），
  避免把上游 HTML 错误页塞给客户端
"""
import server.api.v1_router as vr


class _FakeHTTPError(Exception):
    def __init__(self, msg):
        super().__init__(msg)
        self.response = None


def test_error_extract_keeps_full_body():
    """上游响应体不再砍到 5000 —— 500 字符量级的错误也要完整保留。"""
    body = "x" * 3000
    e = _FakeHTTPError(f"Client error '429 Too Many Requests'\nResponse: {body}")
    assert vr._extract_error_body(e) == body


def test_full_err_text_includes_head_and_body():
    """完整错误 = 异常概要（含状态码/URL）+ 上游原文。"""
    body = '{"error":{"code":"rate_limit_exceeded","message":"slow down"}}'
    e = _FakeHTTPError(f"Client error '429 Too Many Requests' for url 'https://x/v1'\nResponse: {body}")
    full = vr._full_err_text(e)
    assert "429" in full          # 概要保住了状态码
    assert body in full           # 原文保住了错误码
    assert "\n" in full           # 两者分行


def test_full_err_text_defense_cap_only():
    """防御上限仅在超大时生效，且明确标注已截断。"""
    e = _FakeHTTPError("boom\nResponse: " + "y" * (vr.ERROR_MSG_MAX_CHARS + 5000))
    full = vr._full_err_text(e)
    assert len(full) <= vr.ERROR_MSG_MAX_CHARS + 200
    assert "已截断" in full


def test_full_err_text_without_body():
    """无上游原文时用异常自身文本（不能丢错误类型）。"""
    e = _FakeHTTPError("ConnectionError: connection reset")
    full = vr._full_err_text(e)
    assert "ConnectionError" in full
    assert "connection reset" in full


def test_strip_error_detail_for_client():
    """回客户端的 attempts 必须剥掉 error_detail（全量原文只给日志）。"""
    attempts = [
        {"attempt": 0, "model": "p/m", "error": "short", "error_detail": "LONG" * 500},
        {"attempt": 1, "model": "p/m2", "error": "another"},
    ]
    out = vr._strip_error_detail(attempts)
    assert all("error_detail" not in a for a in out)
    assert out[0]["error"] == "short"       # 短形态保留
    assert out[1]["error"] == "another"
    # 无 error_detail 时原样返回
    assert vr._strip_error_detail([{"error": "x"}]) == [{"error": "x"}]


def test_compose_full_error_collects_attempt_details():
    """终态 error_msg = 短概要 + 各次尝试的完整原因（含上游原文）。"""
    attempts = [
        {"attempt": 0, "model": "prov-a/model-1", "error": "HTTP 429",
         "error_detail": "HTTP 429\n" + '{"code":"quota_exceeded"}'},
        {"attempt": 1, "model": "prov-b/model-2", "error": "timeout"},
    ]
    out = vr._compose_full_error("all candidates failed", attempts)
    assert "all candidates failed" in out
    assert "prov-a/model-1" in out and "quota_exceeded" in out   # 上游原文在
    assert "prov-b/model-2" in out and "timeout" in out          # 短形态也带上
    # 同一个 detail 只出现一次（去重）
    dup = vr._compose_full_error("x", [{"model": "m", "error_detail": "D"},
                                       {"model": "m", "error_detail": "D"}])
    assert dup.count("D") == 1


def test_compose_full_error_defense_cap():
    attempts = [{"model": f"m{i}", "error_detail": "Z" * 5000} for i in range(10)]
    out = vr._compose_full_error("boom", attempts)
    assert len(out) <= vr.ERROR_MSG_MAX_CHARS + 200
    assert "已截断" in out


def test_api_error_message_stays_short():
    """回给下游客户端的 message 保持短形态（客户端不需要上游 HTML）。"""
    err = vr._api_error("E" * 5000, status=503)
    assert len(err["error"]["message"]) <= 800


def test_source_guard_no_short_truncation_in_log_writes():
    """源码守卫：写 error_msg 的路径不得再出现 120/300/500 的硬截断。"""
    import inspect
    src = inspect.getsource(vr)
    # 落库路径必须引用新 helper
    assert "_compose_full_error" in src
    assert "_full_err_text" in src
    assert "ERROR_MSG_MAX_CHARS" in src
    # 旧的短截断不得出现在 error_msg= 赋值里
    for bad in ("error_msg=err_short[:", "error_msg=(error_msg or \"\")[:500]",
                "error_msg=(error_msg or \"\")[:300]"):
        assert bad not in src, f"仍有短截断：{bad}"
