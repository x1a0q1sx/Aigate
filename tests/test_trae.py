"""Trae（字节跳动）协议与适配器测试。

协议来源：Jet-Hub 源码逐行核对（github.com/zhengwuji/Jet-Hub）
+ `docs/agents/trae.md` 的实测记录。每个用例锁死一条「反直觉、改错就出问题」
的硬约束（见各用例 docstring）；这些约束全部有实测/源码依据，不是推测。

⚠️ 本文件**不需要真实凭据、不发真实网络请求**：所有 HTTP 均被假客户端拦截。
"""
import asyncio
import json

import pytest

import server.core.trae as tr
import server.adapters.trae_adapter as ta
from server.adapters.trae_adapter import TraeAdapter


@pytest.fixture(autouse=True)
def _clear_module_caches():
    """每个用例前清空模块级缓存（目录 / 身份 / 并发合流）。

    ⚠️ 这两个缓存的 key 是 uid/token 指纹，**跨用例会串**：不清的话前一个用例
    缓存的目录会被后一个用例复用（表现为「通道路由结果莫名不对」）。
    """
    tr_cache = dict(ta._CATALOG)
    meta_cache = dict(ta._META)
    ta._CATALOG.clear()
    ta._META.clear()
    ta._INFLIGHT.clear()
    yield
    ta._CATALOG.clear()
    ta._CATALOG.update(tr_cache)
    ta._META.clear()
    ta._META.update(meta_cache)


# ── 测试夹具 ─────────────────────────────────────────

def make_httpx(calls, results, stream_lines=None, catalog_body=None):
    """假 httpx。

    - `results`：[(status, json_data)]，按**调用顺序**出（用于 oauth/签到类用例）；
    - `stream_lines`：`llm_utils_chat` 的 SSE 行（所有 STREAM 调用共用）；
    - `catalog_body`：`batch_get_detail_param` 的响应体。

    后两者**按 URL 路由**：适配器用例里「拉目录」与「发对话」是两个请求，
    按顺序出响应会让它们互相吞掉对方的响应（表现为「通道路由结果莫名不对」）。
    """
    results = list(results)
    lines = list(stream_lines or [])

    def _stream_payload():
        return lines

    class _Resp:
        def __init__(self, status, data):
            self.status_code = status
            self._data = data if data is not None else {}
            self.content = json.dumps(self._data).encode()
            self.text = json.dumps(self._data)
            self.is_success = 200 <= status < 400

        def json(self):
            return self._data

    class _StreamResp:
        def __init__(self, lines_, status=200):
            self.status_code = status
            self._lines = lines_
            self.text = ""

        async def aread(self):
            return b""

        async def aiter_lines(self):
            for line in self._lines:
                yield line

    class _StreamCtx:
        def __init__(self, resp):
            self._resp = resp

        async def __aenter__(self):
            return self._resp

        async def __aexit__(self, *a):
            return False

    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        def _by_url(self, url):
            """按 URL 路由：目录端点 → catalog_body，其余 → results 队列。"""
            if catalog_body is not None and str(url).endswith("/batch_get_detail_param"):
                return _Resp(200, catalog_body)
            return _Resp(*results.pop(0)) if results else _Resp(200, {})

        async def get(self, url, headers=None, params=None, json=None, content=None):
            calls.append({"m": "GET", "url": url, "headers": headers or {}})
            return self._by_url(url)

        async def post(self, url, headers=None, params=None, json=None, content=None):
            calls.append({"m": "POST", "url": url, "headers": headers or {},
                          "json": json, "content": content})
            return self._by_url(url)

        def stream(self, method, url, headers=None, content=None, json=None):
            calls.append({"m": "STREAM", "url": url, "headers": headers or {},
                          "json": json, "content": content})
            status = results.pop(0)[0] if results else 200
            return _StreamCtx(_StreamResp(_stream_payload(), status))

    return _Client


def _creds(uid="uid-1", **over):
    cred = {
        "access_token": "at-1", "uid": uid,
        "machine_id": "a" * 32, "device_id": "b" * 32, "domain": "",
    }
    cred.update(over)
    return cred


# ── 1. 登录 URL：18 参数一个都不能少 ──────────────────

def test_login_url_has_exactly_18_params():
    """⚠️ 真实登录 URL 是**18 个参数**（唯一权威 trae-oauth.ts:99-127）。

    早期实现只发了 5 个且回调参数名写成 `callback_url` / `redirect_uri` ——
    真实参数名是 **`auth_callback_url`**。名字错了上游拿不到回调地址，
    登录页会**永远停在授权中**（既不跳转也不回传任何东西）。
    """
    from urllib.parse import urlsplit, parse_qs
    machine_id, device_id = "m" * 32, "d" * 32
    url = tr.build_login_url(18080, machine_id, device_id)

    split = urlsplit(url)
    assert f"{split.scheme}://{split.netloc}{split.path}" == "https://www.trae.cn/authorization"
    qs = parse_qs(split.query, keep_blank_values=True)
    assert len(qs) == 18, f"参数个数应为 18，实际 {len(qs)}: {sorted(qs)}"

    expected = {
        "login_version": "1", "auth_from": "solo", "login_channel": "native_ide",
        "plugin_version": "2.3.62834", "auth_type": "local",
        "client_id": "en1oxy7wnw8j9n", "redirect": "0",
        "auth_callback_url": "http://127.0.0.1:18080/authorize",
        "machine_id": machine_id, "device_id": device_id,
        "x_device_id": device_id, "x_machine_id": machine_id,
        "x_device_brand": "PC", "x_device_type": "PC", "x_os_version": "1.0",
        "x_app_version": "0.1.52", "x_app_type": "stable",
    }
    for key, value in expected.items():
        assert qs.get(key) == [value], f"{key} 应为 {value!r}，实际 {qs.get(key)!r}"
    # login_trace_id 是回调反查 pending 的唯一凭据：hex16，且由 machine+device 尾部派生
    assert qs["login_trace_id"] == ["d" * 16]

    # ⚠️ 这两个参数名**不存在**（早期写成它们导致登录页卡死）
    assert "callback_url" not in qs
    assert "redirect_uri" not in qs


def test_login_url_port_must_be_actual_listen_port():
    """`auth_callback_url` 必须写**实际监听端口**（端口占用会回退随机端口）。

    先 listen 拿到实际端口、再构造 URL —— 否则回调会打到没人监听的地址上。
    本模块只负责按传入端口拼 URL（**不硬编码 18080**），端口回退发生在
    回调服务器侧（oauth_client 集成，见交付说明）。
    """
    assert tr.TRAE_CALLBACK_PORT == 18080          # 首选端口（对齐参考实现）
    assert tr.TRAE_CALLBACK_PATH == "/authorize"   # 登录页强制回传的路径
    url = tr.build_login_url(54321, "m" * 32, "d" * 32)
    assert "auth_callback_url=http%3A%2F%2F127.0.0.1%3A54321%2Fauthorize" in url
    # 随机端口（回退场景）同样原样采用，不会被改写成 18080
    fallback = tr.build_login_url(49152, "m" * 32, "d" * 32)
    assert "127.0.0.1%3A49152" in fallback and "18080" not in fallback
    # 回调主机名锁死 127.0.0.1（不是 localhost / 0.0.0.0）
    assert "localhost" not in url and "0.0.0.0" not in url


def test_login_url_intl_variant_uses_trae_ai_hosts():
    """国际版与国内版是**两个独立客户端**：仅域名不同，协议一致。"""
    url = tr.build_login_url(18080, "m" * 32, "d" * 32, domain="trae.ai")
    assert url.startswith("https://www.trae.ai/authorization?")
    assert tr.product_for("trae.ai")["agent_host"] == "https://api5-normal-alisg.mchost.guru"


def test_machine_trace_id_takes_tail_16():
    """login_trace_id 取拼接串的**尾部 16 字符**（不足则左补 0）。"""
    assert tr.machine_trace_id("A" * 20, "B" * 20) == ("A" * 20 + "B" * 20)[-16:]
    assert tr.machine_trace_id("ab", "cd") == "000000000000abcd"


# ── 2. 回调：直接回传 token（不是 ?code=）─────────────

def test_callback_parses_direct_token_shape():
    """⚠️ 回调**直接回传 token**，没有 OAuth 的 `?code=`。

    按 `?code=` 去找会恒判失败 → 回调服务器回 400 → 前端永远「认证中」。
    真实形态：`?refreshToken=..&userInfo={..}&userJwt={..}`。
    `userInfo` 的字段名是 **TenantID**（不是 EnterpriseID）。
    """
    user_info = json.dumps({"UserID": "u-9", "ScreenName": "小明", "TenantID": "ent-1"})
    from urllib.parse import quote
    raw = ("/authorize?refreshToken=rt-1"
           f"&userInfo={quote(user_info)}"
           f"&userJwt={quote(json.dumps({'Token': 'jwt-tok', 'RefreshToken': 'rt-jwt'}))}")
    info, reason, pkce = tr.parse_callback(raw)
    assert reason == "" and pkce is False
    assert info["refresh_token"] == "rt-1"
    assert info["uid"] == "u-9"
    assert info["nickname"] == "小明"
    assert info["enterprise_id"] == "ent-1"
    # 有 refreshToken 时**不**用 userJwt.Token 兜底（避免把短命 token 当 refresh 用）
    assert info["access_token"] == ""


def test_callback_falls_back_to_userjwt_refresh_token():
    """query 缺 refreshToken 时回退 `userJwt.RefreshToken`（login.sh:165-166）。"""
    from urllib.parse import quote
    raw = f"/authorize?userJwt={quote(json.dumps({'RefreshToken': 'rt-jwt'}))}"
    info, reason, _ = tr.parse_callback(raw)
    assert reason == "" and info["refresh_token"] == "rt-jwt"


def test_callback_userjwt_token_only_is_accepted():
    """两者都缺时用 `userJwt.Token` 兜底（此时 access_token 非空、refresh 为空）。"""
    from urllib.parse import quote
    raw = f"/authorize?userJwt={quote(json.dumps({'Token': 'jwt-tok'}))}"
    info, reason, _ = tr.parse_callback(raw)
    assert reason == ""
    assert info["access_token"] == "jwt-tok"
    assert info["refresh_token"] == ""


def test_callback_pkce_shape_is_a_precise_error_not_invalid():
    """⚠️「带 code 的回调」**不是**无效回调（第二次修正，避免过度断言）。

    授权页并存两套流程：新流程 PKCE 带 `code` / `authCodeInfo`，老流程直传
    `refreshToken`。本实现**未实现 PKCE 交换** —— 但必须给出**精确报错**，
    否则一旦上游切到新流程，合法回调被误判为「缺少 refreshToken」，
    排查方向完全被带偏（症状与「一直认证中」一模一样）。
    """
    info, reason, pkce = tr.parse_callback("/authorize?code=abc123")
    assert info is None and pkce is True
    assert "PKCE" in reason

    # authCodeInfo 可能是 JSON（{code:...}）也可能是**纯 code 字符串**，两种都要认
    from urllib.parse import quote
    for raw in ("/authorize?authCodeInfo=" + quote(json.dumps({"code": "c-1"})),
                "/authorize?authCodeInfo=c-raw"):
        info2, reason2, pkce2 = tr.parse_callback(raw)
        assert info2 is None and pkce2 is True, raw
        assert "PKCE" in reason2


def test_callback_without_any_usable_param():
    """真正无效的回调要能把「为什么没解出来」说清楚（回调服务器写进 HTTP 响应）。"""
    info, reason, pkce = tr.parse_callback("/authorize?foo=1")
    assert info is None and pkce is False
    assert "refreshToken" in reason


def test_callback_accepts_relative_and_full_url():
    """回调 URL 允许相对形式（`/authorize?...`）与完整 URL 两种。"""
    assert tr.parse_callback("")[1] == "回调 URL 无法解析"
    info, _, _ = tr.parse_callback("http://127.0.0.1:18080/authorize?refreshToken=rt-x")
    assert info["refresh_token"] == "rt-x"


def test_nickname_mojibake_fixed():
    """中文昵称存在**双重编码乱码**（实测 `Óû§8847309959`），须回转编码。

    修不好且**不含 CJK** 时回退「用户+uid 末 4 位」—— 不把乱码写进凭据。
    """
    # 正确 UTF-8 的中文被按 latin-1 解读一次后的形态
    original = "小明"
    mojibake = original.encode("utf-8").decode("latin-1")
    assert tr.fix_nickname_mojibake(mojibake, "uid-1234") == original
    # 无法修复且无 CJK → 可读占位（末 4 位）
    assert tr.fix_nickname_mojibake("Óû§8847309959", "uid-4321") == "用户4321"
    # 本来就是正常中文的地方不动
    assert tr.fix_nickname_mojibake("张三", "uid-x") == "张三"


# ── 3. ExchangeToken ────────────────────────────────

def test_exchange_body_shape(monkeypatch):
    """ExchangeToken 请求体 4 字段：`ClientSecret` 恒 `-`、`UserID` 恒空串。

    这两个占位值不可省（缺字段会被上游拒），也不要「优化」成真实值。
    """
    calls = []
    monkeypatch.setattr(tr.httpx, "AsyncClient", make_httpx(calls, [(200, {
        "Result": {"Token": "at-2", "RefreshToken": "rt-2",
                   "TokenExpireDuration": 7200},
    })]))
    exchange, err = asyncio.run(tr.exchange_refresh_token("rt-1"))
    assert err == ""
    body = calls[0]["json"]
    assert body == {"ClientID": "en1oxy7wnw8j9n", "RefreshToken": "rt-1",
                    "ClientSecret": "-", "UserID": ""}
    assert calls[0]["url"] == "https://api.trae.com.cn/cloudide/api/v3/trae/oauth/ExchangeToken"
    # 换票端点不需要 Authorization
    assert "Authorization" not in calls[0]["headers"]
    assert exchange["access_token"] == "at-2"
    assert exchange["refresh_token"] == "rt-2"


def test_exchange_response_missing_token_is_failure(monkeypatch):
    """响应没有 Token 即失败 —— **不能把错误响应当 token 存**。"""
    calls = []
    monkeypatch.setattr(tr.httpx, "AsyncClient", make_httpx(calls, [(200, {"Result": {}})]))
    exchange, err = asyncio.run(tr.exchange_refresh_token("rt-1"))
    assert exchange is None
    assert "ExchangeToken 失败" in err


def test_exchange_non_json_html_error_page(monkeypatch):
    """凭据失效时网关返回 **HTML**：不能直接 `.json()`（异常信息毫无意义）。"""
    class _BadResp:
        status_code = 502
        text = "<html>bad gateway</html>"

        @property
        def is_success(self):
            return False

        def json(self):
            raise ValueError("not json")

    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, *a, **k):
            return _BadResp()

    monkeypatch.setattr(tr.httpx, "AsyncClient", _Client)
    exchange, err = asyncio.run(tr.exchange_refresh_token("rt-1"))
    assert exchange is None
    assert "HTTP 502" in err


def test_exchange_without_refresh_token():
    exchange, err = asyncio.run(tr.exchange_refresh_token(""))
    assert exchange is None and "refresh_token" in err


def test_expires_at_normalization_three_levels():
    """过期时间三级回退：绝对(秒/毫秒) → 相对秒数 → JWT exp；都无则 0。"""
    now = 1_700_000_000_000
    # 毫秒值直接采信
    assert tr.resolve_expires_at_ms({"token_expire_at": 1786847930141}, "", now) == 1786847930141
    # 秒值 → 毫秒
    assert tr.resolve_expires_at_ms({"token_expire_at": 1786847930}, "", now) == 1786847930000
    # 相对秒数 → 当前 + duration
    assert tr.resolve_expires_at_ms({"token_expire_duration": 3600}, "", now) == now + 3600_000
    # 都拿不到 → 0（**不编造**过期时间，调用方按未过期处理）
    assert tr.resolve_expires_at_ms({}, "", now) == 0


def test_refresh_rotates_token_but_keeps_device_identity():
    """续期：**轮换** refresh_token，但身份字段（machine_id/device_id/uid）一律不动。

    ⚠️ `machine_id` 绝不可重新生成：上游按它标识设备，换值可能触发风控或
    要求重新登录（trae.ts:104-112 的教训）。
    """
    prev = {"access_token": "at-old", "refresh_token": "rt-old",
            "machine_id": "keep-machine", "device_id": "keep-device",
            "uid": "keep-uid", "domain": ""}
    merged = tr.apply_refresh(prev, {"access_token": "at-new", "refresh_token": "rt-new"},
                              now_ms=1_700_000_000_000)
    assert merged["access_token"] == "at-new"
    assert merged["refresh_token"] == "rt-new"
    assert merged["machine_id"] == "keep-machine"
    assert merged["device_id"] == "keep-device"
    assert merged["uid"] == "keep-uid"


def test_refresh_empty_new_token_keeps_old():
    """续期响应可能不带新 refresh_token → **沿用旧的**，不能覆盖成空串。

    覆盖成空串会让下一次续期必然失败（只能重新登录）。
    """
    prev = {"access_token": "at-old", "refresh_token": "rt-old", "machine_id": "m"}
    merged = tr.apply_refresh(prev, {"access_token": "at-new"}, now_ms=1)
    assert merged["refresh_token"] == "rt-old"


# ── 4. machine_id / device_id 持久化与格式 ────────────

def test_device_ids_are_32_hex_and_distinct():
    """`machine_id` / `device_id` 都是 **32 hex**（`openssl rand -hex 16` 形态）。

    早期实现误用「16 位纯数字」生成 device_id（那是 CodeBuddy 的签到格式），
    与 Trae 协议不符。**账号间必须互异**：同一天两账号共用同一 device_id
    会被「该设备已签到」拦截。
    """
    ids = {tr.generate_device_id() for _ in range(50)}
    machines = {tr.generate_machine_id() for _ in range(50)}
    assert len(ids) == 50 and len(machines) == 50      # 无重复
    for value in ids | machines:
        assert len(value) == 32
        assert all(c in "0123456789abcdef" for c in value)


def test_build_credential_persists_device_identity():
    """凭据必须带回 machine_id / device_id（它们**不在**任何响应里）。

    丢了这两个字段 → 后续请求的 `X-Machine-Id` / `X-Device-Id` 头为空 →
    上游当异常设备处理。
    """
    cred = tr.build_credential(
        {"access_token": "at-1", "refresh_token": "rt-1", "token_expire_duration": 3600},
        {"uid": "u-1", "screen_name": "小明", "enterprise_id": "ent-1"},
        machine_id="mach-32", device_id="dev-32", now_ms=1_700_000_000_000)
    assert cred["machine_id"] == "mach-32"
    assert cred["device_id"] == "dev-32"
    assert cred["uid"] == "u-1" and cred["nickname"] == "小明"
    assert cred["expires_at"] == str(1_700_000_000_000 + 3600_000)


def test_is_expired_never_guesses_without_expiry():
    """无法解析过期时间时**不判定过期**（宁可试一次，也不逼用户白重登）。"""
    assert tr.is_expired({"expires_at": ""}) is False
    assert tr.is_expired({"expires_at": "abc"}) is False
    # 秒级 / 毫秒级 / ISO 三种形态都要能判
    assert tr.is_expired({"expires_at": "1000"}, now_ms=2_000_000) is True
    assert tr.is_expired({"expires_at": "1700000000000"}, now_ms=1_600_000_000_000) is False
    assert tr.is_expired({"expires_at": "2020-01-01T00:00:00Z"}, now_ms=1_600_000_000_000) is True


# ── 5. OpenAI → SOLO 转换（5 条规则）───────────────────

def test_solo_body_stream_forced_true_and_function_injected():
    """规则 1+2：`stream` **强制 true**（上游只支持 SSE）+ 注入 `function`。

    ⚠️ 通道缺省是 `solo_work_lite`（实测 `work` / `solo` / `work_lite` 均无效）；
    但**同一模型只在列出它的通道里可调用**，故由适配器传入该模型所属通道。
    """
    body = tr.transform_to_solo_body({"model": "glm-5.2", "stream": False})
    assert body["stream"] is True
    assert body["function"] == "solo_work_lite"
    assert tr.transform_to_solo_body({}, "", "solo_agent_remote")["function"] == "solo_agent_remote"
    # 无 model 时回退默认模型（不发出空 model）
    assert tr.transform_to_solo_body({})["model"] == "glm-5.2"


def test_solo_body_model_written_to_both_fields():
    """规则 3：`model` **同时**写 `config_name` 与 `model`；`__dev` / `__max` 后缀去除。"""
    body = tr.transform_to_solo_body({"model": "glm-5.2"})
    assert body["config_name"] == "glm-5.2" and body["model"] == "glm-5.2"
    # 明细后缀是内部名，不是模型名
    body2 = tr.transform_to_solo_body({"model": "custom_model_1M__max"})
    assert body2["config_name"] == "custom_model_1M"
    assert body2["model"] == "custom_model_1M"
    # 显式映射优先
    body3 = tr.transform_to_solo_body({"model": "x"}, "mapped-name")
    assert body3["config_name"] == "mapped-name"


def test_solo_body_content_string_to_text_blocks():
    """规则 4：`messages[].content` 字符串 → `[{type:"text",text:...}]`。

    已是数组的 content **原样透传**（多模态 `{type:'image_url',image_url:{url}}`
    实测直发即被上游接受，无需额外转换）。
    """
    body = tr.transform_to_solo_body({"messages": [
        {"role": "user", "content": "你好"},
        {"role": "user", "content": [{"type": "image_url",
                                      "image_url": {"url": "data:image/png;base64,AA"}}]},
        {"role": "assistant", "content": None},
    ]})
    msgs = body["messages"]
    assert msgs[0]["content"] == [{"type": "text", "text": "你好"}]
    assert msgs[1]["content"] == [{"type": "image_url",
                                   "image_url": {"url": "data:image/png;base64,AA"}}]
    assert msgs[2]["content"] is None      # 无 content 的消息不塞空数组


def test_solo_body_assistant_tool_calls_function_to_function_call():
    """规则 5：assistant 的 `tool_calls[].function` → **`function_call`**。

    ⚠️ SOLO 用的是 Go 风格字段名 `function_call`；原样发 `function` 模型会
    **看不到自己调用过什么**（多步对话全坏，且全程没有报错）。
    无 `name` 的条目须剔除（上游 `FunctionCall.Name` 必填）。
    """
    body = tr.transform_to_solo_body({"messages": [
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "call_1", "type": "function",
             "function": {"name": "read_file", "arguments": "{}"}},
            {"id": "call_2", "type": "function", "function": {"arguments": "{}"}},
        ]},
    ]})
    calls = body["messages"][0]["tool_calls"]
    assert len(calls) == 1                       # 无 name 的已被剔除
    assert "function" not in calls[0]
    assert calls[0]["function_call"] == {"name": "read_file", "arguments": "{}"}


def test_solo_body_tools_parameters_serialized_to_json_string():
    """规则 6：`tools[].function.parameters`（object）→ **JSON 字符串**。

    ⚠️ SOLO 上游要求 parameters 是 **string**（OpenAI 标准是 object）。
    因此 tools **必须放进源 OpenAI 对象里再交给转换函数** —— 转换**之后**
    再补 `body["tools"]` 时这一步已执行完毕，parameters 会保持 object 形态
    发给上游被拒（真实缺陷，且错误信息不会指向这里）。
    """
    params = {"type": "object", "properties": {"path": {"type": "string"}}}
    body = tr.transform_to_solo_body({"tools": [
        {"type": "function", "function": {"name": "read_file", "parameters": params}},
        {"type": "function", "function": {"name": "no_params"}},
        {"not_a_function": True},
    ]})
    tools = body["tools"]
    assert len(tools) == 2                       # 无 function 的条目剔除
    assert isinstance(tools[0]["function"]["parameters"], str)
    assert json.loads(tools[0]["function"]["parameters"]) == params
    assert "parameters" not in tools[1]["function"]


def test_solo_body_tool_choice_normalization():
    """规则 7：`tool_choice` 归一化 —— `none` 连 `tools` 一起删。

    发 `tools` + `tool_choice:"none"` 是自相矛盾的组合，上游按前者处理。
    """
    # "none" → 删 tool_choice 与 tools
    body = tr.transform_to_solo_body({
        "tool_choice": "none",
        "tools": [{"type": "function", "function": {"name": "f", "parameters": {}}}]})
    assert "tool_choice" not in body and "tools" not in body
    # {type:"auto"} → 字符串 "auto"（工具保留）
    body2 = tr.transform_to_solo_body({"tool_choice": {"type": "auto"}})
    assert body2["tool_choice"] == "auto"
    # {type:"function",...} → 字符串 name
    body3 = tr.transform_to_solo_body(
        {"tool_choice": {"type": "function", "function": {"name": "read_file"}}})
    assert body3["tool_choice"] == "read_file"
    # 无名 function → 退 auto（不发出一个空白的 tool_choice）
    body4 = tr.transform_to_solo_body({"tool_choice": {"type": "function", "function": {}}})
    assert body4["tool_choice"] == "auto"


# ── 6. SOLO SSE 解析 ────────────────────────────────

def test_parse_sse_event_output_fields():
    """`output` 事件：正文在 `response`、思考在 `reasoning_content`。

    ⚠️ tool_calls 内层同样用 `function_call` 字段，且带 SOLO 专属的
    `namespace` / `partial_arguments` —— **必须清理**，只留标准
    `function.{name,arguments}`（下游 OpenAI 客户端不认识那些键）。
    """
    ev = tr.parse_sse_event("output", json.dumps({
        "response": "你好", "reasoning_content": "想一下",
        "tool_calls": [{"id": "c1", "function_call": {
            "name": "read_file", "arguments": "{}",
            "namespace": "solo", "partial_arguments": "x"}}],
    }))
    assert ev["response"] == "你好"
    assert ev["reasoning_content"] == "想一下"
    call = ev["tool_calls"][0]
    assert "function_call" not in call
    assert call["function"] == {"name": "read_file", "arguments": "{}"}
    assert "namespace" not in call["function"]
    assert "partial_arguments" not in call["function"]


def test_parse_sse_event_token_usage_done_error():
    """`token_usage` / `done` / `error` 三个事件各自的字段。"""
    usage = tr.parse_sse_event("token_usage", json.dumps(
        {"prompt_tokens": 21, "completion_tokens": 142, "reasoning_tokens": 30}))
    assert usage["usage"]["completion_tokens"] == 142

    done = tr.parse_sse_event("done", json.dumps({"finish_reason": "stop"}))
    assert done["finish_reason"] == "stop"

    err = tr.parse_sse_event("error", json.dumps({"code": 4008, "message": "quota exceeded"}))
    assert err["error_code"] == 4008 and err["error_message"] == "quota exceeded"


def test_parse_sse_event_never_raises():
    """非 JSON / 空 data / 未知事件：**永不抛异常**（只返回事件名）。

    上游偶尔会下发心跳或空 data 行，抛异常会让整条流被误判成失败。
    """
    assert tr.parse_sse_event("output", "<html>") == {"event": "output"}
    assert tr.parse_sse_event("output", "") == {"event": "output"}
    assert tr.parse_sse_event("metadata", "{}") == {"event": "metadata"}
    assert tr.parse_sse_event("output", '["not", "object"]') == {"event": "output"}


def test_aggregate_sse_full_stream():
    """聚合完整流：正文/思考拼接、usage、finish_reason、工具调用。"""
    result = tr.aggregate_sse([
        "event:metadata", 'data:{"session_id":"s1"}', "",
        "event:output", 'data:{"response":"你"}', "",
        "event:output", 'data:{"response":"好","reasoning_content":"想"}', "",
        "event:token_usage", 'data:{"prompt_tokens":5,"completion_tokens":9}', "",
        "event:done", 'data:{"finish_reason":"stop"}', "",
    ])
    assert result["content"] == "你好"
    assert result["reasoning_content"] == "想"
    assert result["usage"]["prompt_tokens"] == 5
    assert result["finish_reason"] == "stop"
    assert result["saw_event"] is True
    assert result["error"] is None


def test_aggregate_sse_data_without_space():
    """⚠️ 必须兼容 `data:{…}`（无空格）与 `data: {…}` 两种形态。

    实测上游不带空格；只认一种形态会让流被解析成「零事件」，
    进而被误判成空响应并**重放请求**（重复计费）。
    """
    result = tr.aggregate_sse([
        "event:output", 'data:{"response":"A"}', "",
        "event:output", 'data: {"response":"B"}', "",
    ])
    assert result["content"] == "AB"


def test_aggregate_sse_inline_error():
    """流内 `event:error` 是**业务错误**（不是传输失败）—— 必须能取出来。"""
    result = tr.aggregate_sse([
        "event:error", 'data:{"code":4001,"message":"the param is invalid"}', "",
    ])
    assert result["error"] == {"code": 4001, "message": "the param is invalid"}


def test_normalize_usage_fills_all_three_keys():
    """usage 必须补齐三个键（OpenAI 客户端要求都在）；reasoning 只在 >0 时附明细。"""
    assert tr.normalize_usage({}) == {"prompt_tokens": 0, "completion_tokens": 0,
                                      "total_tokens": 0}
    usage = tr.normalize_usage({"prompt_tokens": 1, "completion_tokens": 2})
    assert usage["total_tokens"] == 3
    assert "completion_tokens_details" not in usage      # 0 是冗余信息
    with_reasoning = tr.normalize_usage(
        {"prompt_tokens": 1, "completion_tokens": 2, "reasoning_tokens": 7})
    assert with_reasoning["completion_tokens_details"] == {"reasoning_tokens": 7}


def test_error_message_4001_points_at_real_cause():
    """⚠️ `4001` 必须先判更严重的类别且给出**指向真实成因**的提示。

    上游原文是 *"the param is invalid"* —— 它与「提示词/参数格式有误」毫无关系：
    实测该码只由**发错通道**或「仅可见但不可调用」的自定义模型触发。
    照抄原文会把排查方向完全带偏（去查 message 结构、tools 序列化…）。
    """
    msg = tr.solo_error_message(4001, "the param is invalid", "glm-5.1")
    assert "4001" in msg and "glm-5.1" in msg
    assert "通道" in msg
    # 其余错误码保持原文，不做无依据的解释
    assert tr.solo_error_message(12345, "boom") == "trae: boom (code=12345)"
    assert "ide_credits" in tr.solo_error_message(4008, "quota exceeded")


# ── 7. 模型目录：多通道 + 三条硬性过滤 ────────────────

def _entry(config_name, **over):
    entry = {"config_name": config_name, "config_switch": True,
             "usage": "chat_completion",
             "display_config": {"display_name": config_name.upper()}}
    entry.update(over)
    return entry


def test_parse_model_list_single_channel_variant():
    """单通道 `get_detail_param` 的解析器也要能用（兼容 `config_info_list` / `data`）。

    本插件走 batch 端点，但保留单通道解析是为了「上游若只下发单通道响应时
    不至于解析成空」（且它与 batch 共用同一个条目解析器，语义必须一致）。
    """
    body = {"config_info_list": [_entry("m1"), _entry("m2", config_switch=False)]}
    models = tr.parse_model_list(body)
    # 单通道端点**不做**三条硬性过滤（对齐 Jet-Hub：已按场景筛选过）
    assert {m["id"] for m in models} == {"m1", "m2"}
    assert tr.parse_model_list({"data": [_entry("m3")]})[0]["id"] == "m3"
    assert tr.parse_model_list(None) == []
    assert tr.parse_model_list({"config_info_list": "x"}) == []


def test_batch_models_three_hard_filters():
    """三条硬性过滤（缺一不可，不设外部开关）：

    1. `usage != "chat_completion"` —— batch 端点是**全功能配置表**，含
       summary / fast_apply / multimodal / system_diagnosis 等非对话用途；
    2. `config_switch == false` —— 上游已停用；
    3. `is_invisible_to_user == true` —— 官方 picker 不展示（内部子代理等）。
    """
    body = {"function_configs": [{"function": "solo_agent", "config_info_list": [
        _entry("keep-me"),
        _entry("is-summary", usage="summary"),
        _entry("is-multimodal", usage="multimodal"),
        _entry("disabled", config_switch=False),
        _entry("invisible", is_invisible_to_user=True),
        _entry("no-usage-field", usage=None),
    ]}]}
    for item in body["function_configs"][0]["config_info_list"]:
        if item.get("usage") is None:
            item.pop("usage")
    ids = {m["id"] for m in tr.parse_batch_model_list(body)}
    assert ids == {"keep-me", "no-usage-field"}


def test_batch_models_later_channel_overrides_earlier():
    """⚠️ **后面的覆盖前面的**，且**每条模型记住自己所属的通道**。

    同一个 `config_name` 可能出现在多个 function 中，后面的条目通常带更完整的
    配置；且**模型只在列出它的通道里可调用** —— 丢了通道信息就会发错通道，
    得到流内 `4001`（HTTP 仍是 200，极难定位）。
    """
    body = {"function_configs": [
        {"function": "solo_work_lite", "config_info_list": [
            _entry("glm-5-turbo", reasoning_effort_config={"options": ["light"]})]},
        {"function": "solo_agent_remote", "config_info_list": [
            _entry("glm-5-turbo",
                   reasoning_effort_config={"options": ["light", "high"]}),
            _entry("glm-5.1")]},
    ]}
    models = {m["id"]: m for m in tr.parse_batch_model_list(body)}
    assert len(models) == 2
    assert models["glm-5-turbo"]["channel"] == "solo_agent_remote"   # 后面的赢了
    assert models["glm-5-turbo"]["reasoning_config"]["options"] == ["light", "high"]
    assert models["glm-5.1"]["channel"] == "solo_agent_remote"


def test_batch_models_tolerates_unknown_shape():
    """响应形状不对时返回空列表，**不抛异常**（由调用方回退静态种子）。"""
    assert tr.parse_batch_model_list(None) == []
    assert tr.parse_batch_model_list({}) == []
    assert tr.parse_batch_model_list({"function_configs": "x"}) == []
    assert tr.parse_batch_model_list({"function_configs": [{"config_info_list": [1, 2]}]}) == []


def test_registry_flags_keep_undefined_when_absent():
    """「上游没说」与「上游说 false」是**两回事**：缺失保持缺省，不填 False。

    过滤方只挡**明确**命中者 —— 缺字段时填 False 会把「未声明」当成
    「未启用」，可能连带删掉整批可用模型（Jet-Hub 因此误记过 `qwen3.8-flash`）。
    """
    model = tr.parse_config_entry(_entry("m1"), "solo_agent")
    # `_entry` 显式给了 usage / config_switch / display_name，故这三项有值
    assert model["is_enabled"] is True
    assert model["usage"] == "chat_completion"
    # 而这三个上游**没说**，必须缺省（不是 False）
    assert "is_custom_model" not in model
    assert "multimodal" not in model
    assert "is_hidden" not in model
    assert "max_mode" not in model

    # 显式 false 要如实记录下来（过滤方据此剔除）
    explicit = tr.parse_config_entry(_entry("m2", config_switch=False), "solo_agent")
    assert explicit["is_enabled"] is False


def test_registry_flags_read_from_the_right_nesting_level():
    """⚠️ 六个标志位**分散在两处**，取错地方会恒得到「未声明」。

    `is_custom_model` / `max_mode` / `multimodal` / `tool_response_multimodal`
    在 `display_config` 里；`config_switch` / `is_invisible_to_user` 在**条目
    顶层**。Jet-Hub 正是「读了一个漏了另一个」才把图片能力整批漏判
    （Issue #IKHDKC：`inputModalities` 恒为 `['text']`）。
    """
    model = tr.parse_config_entry({
        "config_name": "m1",
        "config_switch": False,
        "is_invisible_to_user": True,
        "display_config": {"display_name": "M1", "is_custom_model": True,
                           "max_mode": True, "multimodal": True,
                           "tool_response_multimodal": False},
    }, "solo_agent")
    assert model["is_enabled"] is False          # 顶层
    assert model["is_hidden"] is True            # 顶层
    assert model["is_custom_model"] is True      # display_config
    assert model["max_mode"] is True             # display_config
    assert model["multimodal"] is True           # display_config
    assert model["tool_response_multimodal"] is False
    # ⚠️ 两个图片字段是**两种独立能力**，不可合并判断
    assert model["multimodal"] != model["tool_response_multimodal"]


def test_consumption_rate_requires_second_parse():
    """⚠️ `display_contact_config` 是**一个 JSON 字符串**，必须二次 parse。

    直接读 `entry["display_contact_config"]["consumption_rate"]` 永远得到
    None（字符串下标会抛 TypeError）。三条判据：
    `enable !== false` / `rate` 为有限非负数 / **`rate: 0` 是合法值（免费）**。
    """
    raw = json.dumps({"consumption_rate": {"enable": True, "data": {"rate": 0.08}}})
    assert tr.read_consumption_rate({"display_contact_config": raw}) == 0.08
    # ⚠️ 0 是免费的合法值，不能用 > 0 过滤掉
    free = json.dumps({"consumption_rate": {"enable": True, "data": {"rate": 0}}})
    assert tr.read_consumption_rate({"display_contact_config": free}) == 0.0
    # enable=false → 不显示（而不是当成 0）
    off = json.dumps({"consumption_rate": {"enable": False, "data": {"rate": 0.5}}})
    assert tr.read_consumption_rate({"display_contact_config": off}) is None
    # 非法/缺失 → None（**不编造倍率**）
    assert tr.read_consumption_rate({}) is None
    assert tr.read_consumption_rate({"display_contact_config": "not json"}) is None


def test_activity_discount_only_when_really_active():
    """`enable: true` 不等于「当前有折扣」：`discount_type:"none"` 或
    `before == after` 都必须视为无折扣（否则会显示 `x0.13→x0.13` 误导用户）。"""
    def _entry_with(current):
        return {"display_contact_config": json.dumps({
            "activity_discount": {"enable": True, "data": {"limited": {
                "current": current, "end_at": 4_000_000_000}}}})}

    assert tr.read_activity_discount(_entry_with(
        {"discount_type": "limited", "before_consumption_rate": 0.8,
         "consumption_rate": 0.08}), now_sec=1) == {"original_rate": 0.8,
                                                    "ends_at": 4_000_000_000}
    # type=none → 无活动
    assert tr.read_activity_discount(_entry_with(
        {"discount_type": "none", "before_consumption_rate": 0.13,
         "consumption_rate": 0.13}), now_sec=1) is None
    # before == after → 不是降价
    assert tr.read_activity_discount(_entry_with(
        {"discount_type": "off_peak", "before_consumption_rate": 0.13,
         "consumption_rate": 0.13}), now_sec=1) is None
    # 已过期 → 不存在（否则用户按折扣价预期、实际被按原价计费）
    expired = {"display_contact_config": json.dumps({
        "activity_discount": {"enable": True, "data": {"limited": {
            "current": {"discount_type": "limited", "before_consumption_rate": 0.8,
                        "consumption_rate": 0.08}, "end_at": 1_000}}}})}
    assert tr.read_activity_discount(expired, now_sec=2_000) is None


def test_context_window_uses_dev_not_max():
    """⚠️ 常规会话用 `context_window_tokens.dev`，**不能**用 `max`。

    真实条目形如 `{dev:200000, max:1000000}`；`max` 只在开启 Max 模式时可用。
    采信 `max` 会让调用方以为有 1M 窗口、实际请求被上游拒。
    """
    entry = _entry("m1", context_window_tokens={"dev": 200_000, "max": 1_000_000})
    model = tr.parse_config_entry(entry, "solo_agent")
    assert model["context_window"] == 200_000
    assert model["max_context_window"] == 1_000_000


def test_max_output_prefers_dev_detail():
    """多条 `model_detail_list` 时优先取 `model_name` 以 `__dev` 结尾那条的
    `max_tokens`；`__max` 那条另存为 Max 模式输出上限。"""
    entry = _entry("m1", model_detail_list=[
        {"model_name": "m1__max", "max_tokens": 384_000},
        {"model_name": "m1__dev", "max_tokens": 32_000},
    ])
    model = tr.parse_config_entry(entry, "solo_agent")
    assert model["max_output_tokens"] == 32_000
    assert model["max_mode_output_tokens"] == 384_000


def test_reasoning_config_single_value_options():
    """`reasoning_effort_config.options` 是**单值**字符串数组（既是档位名、
    也是发给上游的 wire 值）—— 与 LobsterAI 的 level/openclawLevel 双字段不同。"""
    model = tr.parse_config_entry(_entry("m1", reasoning_effort_config={
        "default_level": "high", "options": ["light", "high", "extra_high"],
        "support_thinking": True}), "solo_agent")
    assert model["reasoning_config"]["options"] == ["light", "high", "extra_high"]
    assert model["reasoning_config"]["support_thinking"] is True
    # 一个字段都没有 → 不声明档位（不给用户一个发了也没用的选择器）
    assert "reasoning_config" not in tr.parse_config_entry(_entry("m2"), "solo_agent")
    # 对象数组形态的防御性兼容
    obj_form = tr.parse_config_entry(_entry("m3", reasoning_effort_config={
        "options": [{"level": "low", "openclawLevel": "high"}]}), "solo_agent")
    assert obj_form["reasoning_config"]["options"] == ["high"]


# ── 8. 适配器：模型按通道路由 ────────────────────────

def _adapter(monkeypatch, catalog_body, calls=None, meta=None, stream_lines=None,
             results=None):
    """构造一个凭据/目录都已就绪的适配器（不碰真实 DB、不发真实请求）。

    - `catalog_body`：`batch_get_detail_param` 的响应（按 URL 路由）；
    - `meta`：覆盖 `_resolve_meta` 的返回（默认用 `_creds()`）；
    - `results`：其它 POST 的按序响应（如对话端点的 4xx/5xx）。
    """
    calls = calls if calls is not None else []
    monkeypatch.setattr(
        "server.adapters.trae_adapter.httpx.AsyncClient",
        make_httpx(calls, list(results or []), stream_lines, catalog_body))
    adapter = TraeAdapter()
    cred = _creds()

    async def _resolve_meta(api_key):
        meta_out = dict(cred)
        if meta:
            meta_out.update(meta)
        return meta_out

    monkeypatch.setattr(adapter, "_resolve_meta", _resolve_meta)
    return adapter, calls


def test_adapter_routes_model_to_its_own_channel(monkeypatch):
    """⚠️ **模型只在列出它的通道里可调用** —— 发错通道会得到流内 `4001`。

    实测路由矩阵：`glm-5.1` / `qwen-3.5` 只在 `solo_agent_remote` 可用；
    `glm-5-turbo` / `sagitta` 只在 `solo_work_lite` 可用。旧实现把 function
    写死 `solo_work_lite`，于是 agent 专有模型一用就报 4001。
    """
    catalog_body = {"function_configs": [
        {"function": "solo_work_lite", "config_info_list": [_entry("glm-5-turbo")]},
        {"function": "solo_agent_remote", "config_info_list": [_entry("glm-5.1")]},
    ]}
    stream = ["event:output", 'data:{"response":"ok"}', "",
              "event:done", 'data:{"finish_reason":"stop"}', ""]
    calls = []
    adapter, _ = _adapter(monkeypatch, catalog_body, calls, stream_lines=stream)

    request = {"model": "glm-5.1", "messages": [{"role": "user", "content": "hi"}]}
    chunks = []
    for chunk in _run(adapter.stream_chat_completion, request, "at-1", "https://trae-api-cn.mchost.guru"):
        chunks.append(chunk)
    sent = json.loads(calls[-1]["content"].decode("utf-8"))
    assert sent["function"] == "solo_agent_remote"
    assert sent["config_name"] == "glm-5.1" and sent["model"] == "glm-5.1"
    assert sent["stream"] is True
    assert chunks and chunks[-1]["choices"][0]["finish_reason"] == "stop"


def test_adapter_falls_back_to_default_channel_for_unknown_model(monkeypatch):
    """目录里查不到的模型回退默认通道（不因为一次目录失败就拒绝服务）。"""
    catalog_body = {"function_configs": [
        {"function": "solo_agent", "config_info_list": [_entry("known")]}]}
    stream = ["event:done", 'data:{"finish_reason":"stop"}', ""]
    calls = []
    adapter, _ = _adapter(monkeypatch, catalog_body, calls, stream_lines=stream)
    request = {"model": "unknown-model", "messages": [{"role": "user", "content": "hi"}]}
    _run(adapter.stream_chat_completion, request, "at-1", "https://trae-api-cn.mchost.guru")
    sent = json.loads(calls[-1]["content"].decode("utf-8"))
    assert sent["function"] == "solo_work_lite"


def test_adapter_sends_full_auth_header_set(monkeypatch):
    """鉴权头是十余个一起发的：`Authorization: Cloud-IDE-JWT <token>` 与
    `X-Cloudide-Token` / `X-Ide-Token` 三处**同一个 token**，实测缺任一个都可能被拒。"""
    catalog_body = {"function_configs": [
        {"function": "solo_work_lite", "config_info_list": [_entry("m1")]}]}
    stream = ["event:done", 'data:{"finish_reason":"stop"}', ""]
    calls = []
    adapter, _ = _adapter(monkeypatch, catalog_body, calls, stream_lines=stream)
    _run(adapter.stream_chat_completion,
         {"model": "m1", "messages": [{"role": "user", "content": "hi"}]},
         "at-1", "https://trae-api-cn.mchost.guru")
    headers = calls[-1]["headers"]
    assert headers["Authorization"] == "Cloud-IDE-JWT at-1"
    assert headers["X-Cloudide-Token"] == "at-1"
    assert headers["X-Ide-Token"] == "at-1"
    assert headers["X-Uid"] == "uid-1"
    assert headers["X-Machine-Id"] == "a" * 32
    assert headers["X-Device-Id"] == "b" * 32
    assert headers["Request-Traffic-Type"] == "prod"
    assert headers["X-App-Id"] == "6eefa01c-1036-4c7e-9ca5-d891f63bfcd8"
    assert headers["Content-Type"] == "application/json"
    assert calls[-1]["url"].endswith("/api/agent/v3/llm_utils_chat")


def test_adapter_streams_solo_events_to_openai_chunks(monkeypatch):
    """SOLO → OpenAI：正文/思考/工具/usage/finish 各自的落点。

    正文进 `delta.content`、思考进 `delta.reasoning_content`、
    工具进 `delta.tool_calls`（补 index/type），最后一条带 `finish_reason`。
    """
    catalog_body = {"function_configs": [
        {"function": "solo_work_lite", "config_info_list": [_entry("m1")]}]}
    stream = [
        "event:metadata", 'data:{"session_id":"s1"}', "",
        "event:output", 'data:{"response":"你"}', "",
        "event:output", 'data:{"reasoning_content":"想","tool_calls":[{"id":"c1","index":0,'
                        '"function_call":{"name":"read_file","arguments":"{\\"p\\":1}"}}]}', "",
        "event:token_usage", 'data:{"prompt_tokens":5,"completion_tokens":7}', "",
        "event:done", 'data:{"finish_reason":"tool_calls"}', "",
    ]
    calls = []
    adapter, _ = _adapter(monkeypatch, catalog_body, calls, stream_lines=stream)
    chunks = list(_run(adapter.stream_chat_completion,
                       {"model": "m1", "messages": [{"role": "user", "content": "hi"}]},
                       "at-1", "https://trae-api-cn.mchost.guru"))
    assert chunks[0]["choices"][0]["delta"]["role"] == "assistant"
    deltas = [c["choices"][0]["delta"] for c in chunks]
    assert "".join(d.get("content") or "" for d in deltas) == "你"
    assert "".join(d.get("reasoning_content") or "" for d in deltas) == "想"
    tool_deltas = [d for d in deltas if d.get("tool_calls")]
    assert tool_deltas[0]["tool_calls"][0]["function"]["name"] == "read_file"
    assert tool_deltas[0]["tool_calls"][0]["index"] == 0
    assert tool_deltas[0]["tool_calls"][0]["type"] == "function"
    last = chunks[-1]
    assert last["choices"][0]["finish_reason"] == "tool_calls"
    assert last["usage"] == {"prompt_tokens": 5, "completion_tokens": 7, "total_tokens": 12}
    assert all(c["object"] == "chat.completion.chunk" for c in chunks)


def test_adapter_flushes_finish_when_upstream_omits_done(monkeypatch):
    """上游没发 `done` 就断流 → 必须补一个终结 chunk（否则客户端一直等）。"""
    catalog_body = {"function_configs": [
        {"function": "solo_work_lite", "config_info_list": [_entry("m1")]}]}
    stream = ["event:output", 'data:{"response":"partial"}', ""]
    calls = []
    adapter, _ = _adapter(monkeypatch, catalog_body, calls, stream_lines=stream)
    chunks = list(_run(adapter.stream_chat_completion,
                       {"model": "m1", "messages": [{"role": "user", "content": "hi"}]},
                       "at-1", "https://trae-api-cn.mchost.guru"))
    assert chunks[-1]["choices"][0]["finish_reason"] == "stop"


def test_adapter_empty_response_retries_exactly_once(monkeypatch):
    """⚠️ HTTP 200 **零事件**仅在首个模型事件前重试一次。

    上游有时「会话创建成功、然后一个事件都不发就结束流」，这是可重试的瞬时
    故障（CN 项目称 empty response）。做成重试是安全的，因为只在「一个事件都
    没收到」时才重试 —— 一旦已有 output / usage / tool_calls，重放会让上游
    **重复计费并可能重复执行工具**。
    """
    catalog_body = {"function_configs": [
        {"function": "solo_work_lite", "config_info_list": [_entry("m1")]}]}
    calls = []
    adapter, _ = _adapter(monkeypatch, catalog_body, calls, stream_lines=[])  # 两次都零事件
    with pytest.raises(RuntimeError) as exc:
        list(_run(adapter.stream_chat_completion,
                  {"model": "m1", "messages": [{"role": "user", "content": "hi"}]},
                  "at-1", "https://trae-api-cn.mchost.guru"))
    assert "no events" in str(exc.value)
    chat_calls = [c for c in calls if c["url"].endswith("/llm_utils_chat")]
    assert len(chat_calls) == 2                      # 恰好重试一次，不是无限重试


def test_adapter_does_not_replay_after_first_event(monkeypatch):
    """⚠️ 一旦收到任何上游事件（含 metadata）就**绝不重放** —— 防重复计费。

    这里的事件流只含 metadata 且没有任何正文；实现必须在收到它的「开工」标记后
    停止（总对话请求数恒为 1），只补一个**终结 chunk**（不带正文）。

    ⚠️ 只有 metadata 就断流**不能**当成「零事件」去重放：上游可能确实建立过会话。
    Jet-Hub 的语义正是如此（`sawAnyUpstreamEvent` 一置位就只走 finish 收尾）。
    """
    catalog_body = {"function_configs": [
        {"function": "solo_work_lite", "config_info_list": [_entry("m1")]}]}
    calls = []
    adapter, _ = _adapter(monkeypatch, catalog_body, calls,
                          stream_lines=["event:metadata", 'data:{"session_id":"s"}', ""])
    chunks = list(_run(adapter.stream_chat_completion,
                       {"model": "m1", "messages": [{"role": "user", "content": "hi"}]},
                       "at-1", "https://trae-api-cn.mchost.guru"))
    chat_calls = [c for c in calls if c["url"].endswith("/llm_utils_chat")]
    assert len(chat_calls) == 1
    # metadata 只标记「开工」：产出仅有一个终结 chunk，正文为空
    assert len(chunks) == 1
    assert chunks[0]["choices"][0]["delta"] == {}
    assert chunks[0]["choices"][0]["finish_reason"] == "stop"


def test_adapter_stream_error_chunk_for_inline_error(monkeypatch):
    """流内 `event:error` → yield `{"error": ...}`（AIGate 路由据此走回退）。

    非流式出口把它转成抛错（不能把业务错误当成一个空回复）。
    """
    catalog_body = {"function_configs": [
        {"function": "solo_work_lite", "config_info_list": [_entry("m1")]}]}
    stream = ["event:error", 'data:{"code":4008,"message":"quota exceeded"}', ""]
    calls = []
    adapter, _ = _adapter(monkeypatch, catalog_body, calls, stream_lines=stream)
    chunks = list(_run(adapter.stream_chat_completion,
                       {"model": "m1", "messages": [{"role": "user", "content": "hi"}]},
                       "at-1", "https://trae-api-cn.mchost.guru"))
    assert chunks and "error" in chunks[0]
    assert "ide_credits" in chunks[0]["error"]


def test_adapter_non_stream_aggregates_own_stream(monkeypatch):
    """非流式出口聚合同一条流（SOLO 端点只有 SSE）→ 标准 chat.completion。"""
    catalog_body = {"function_configs": [
        {"function": "solo_work_lite", "config_info_list": [_entry("m1")]}]}
    stream = [
        "event:output", 'data:{"response":"你"}', "",
        "event:output", 'data:{"response":"好","reasoning_content":"想"}', "",
        "event:token_usage", 'data:{"prompt_tokens":3,"completion_tokens":4}', "",
        "event:done", 'data:{"finish_reason":"stop"}', "",
    ]
    calls = []
    adapter, _ = _adapter(monkeypatch, catalog_body, calls, stream_lines=stream)
    result = _run(adapter.chat_completion,
                  {"model": "m1", "messages": [{"role": "user", "content": "hi"}]},
                  "at-1", "https://trae-api-cn.mchost.guru")
    assert result["object"] == "chat.completion"
    assert result["choices"][0]["message"]["content"] == "你好"
    assert result["choices"][0]["message"]["reasoning_content"] == "想"
    assert result["usage"]["total_tokens"] == 7


def test_clean_body_keeps_legal_falsy_values(monkeypatch):
    """⚠️ 剥空值**不能**剥掉合法假值：`temperature: 0` 被丢掉会让上游按默认
    温度回答（静默的行为变化）；`stream: false` 也不能丢。

    但「纯 tool_calls 的 assistant」的 `content: null` 必须**补回来** ——
    参考实现对这种消息发的是显式 null，少一个键在部分上游会变成「消息缺字段」。
    """
    from server.adapters.trae_adapter import _clean_body
    from server.schemas.chat import ChatCompletionRequest
    req = ChatCompletionRequest(
        model="m1", stream=False, temperature=0.0, top_p=0.0, max_tokens=100,
        messages=[{"role": "user", "content": "hi"}], extra={"internal": True})
    cleaned = _clean_body(req)
    assert cleaned["temperature"] == 0.0            # 合法假值必须保留
    assert cleaned["top_p"] == 0.0
    assert cleaned["stream"] is False
    assert "extra" not in cleaned                   # AIGate 内部容器不进上游
    # 纯 tool_calls 的 assistant：content 补成显式 null
    plain = _clean_body({"messages": [
        {"role": "assistant", "tool_calls": [
            {"id": "c1", "function": {"name": "f", "arguments": "{}"}}]}]})
    assert "content" in plain["messages"][0]
    assert plain["messages"][0]["content"] is None


def test_adapter_requires_uid():
    """缺 uid 直接报错 —— `X-Uid` 是必填头，发一个空值会被上游当异常设备。

    报错文案要指向**可操作的方向**（重新连接账号），不是含糊的「缺少字段」。
    """
    adapter = TraeAdapter()
    with pytest.raises(RuntimeError) as exc:
        adapter._credential("at-1", {"machine_id": "m"}, "")
    assert "uid" in str(exc.value)


def test_adapter_omits_device_headers_when_missing(monkeypatch, caplog):
    """machine_id/device_id 缺失时**省略头**并留痕，而不是发空值。

    空值头会被上游当异常设备，比不发的后果更糟。
    """
    cred = {"access_token": "at-1", "uid": "uid-1", "machine_id": "", "device_id": ""}
    with caplog.at_level("WARNING"):
        headers = tr.solo_headers(cred, True)
        adapter = TraeAdapter()
        adapter._credential("at-1", {"uid": "uid-1"}, "")
    assert "X-Machine-Id" not in headers
    assert "X-Device-Id" not in headers
    assert any("machine_id" in r.message for r in caplog.records)


# ── 9. 签到：必须先 status 后 claim、claim 后补查 ────────

def test_checkin_prechecks_status_before_claim(monkeypatch):
    """⚠️ 必须先 status 预检。

    `checkin_credits/claim` 对「今天已签到」是**幂等**的：实测重复领取同样返回
    `{code:0, message:"success"}`，与真正成功**无法区分**。不预检就会把一个
    已签到的账号报成「领取成功」（真实用户报障）。
    """
    calls = []
    monkeypatch.setattr(tr.httpx, "AsyncClient", make_httpx(calls, [
        (200, {"code": 0, "checked_in": True, "credits": 150, "enable": True}),
    ]))
    out = asyncio.run(tr.claim_checkin(_creds()))
    assert out["kind"] == "already_claimed"
    assert out["credit"] == 150
    # 只有一次请求（status），**没有**发 claim
    assert len(calls) == 1
    assert calls[0]["url"].endswith("/checkin_credits/status")


def test_checkin_claims_then_rechecks_status_for_credits(monkeypatch):
    """⚠️ claim 响应**不含积分数**，必须补查 status 取真实所得。

    claim 的完整响应就是 `{"code":0,"message":"success"}`。早期实现读它的
    `credits` 字段（该字段根本不存在）→ 恒为 0 → 界面显示「领取成功 +0 积分」
    （真实用户报障），而 IDE 里明明写着 150。
    """
    calls = []
    monkeypatch.setattr(tr.httpx, "AsyncClient", make_httpx(calls, [
        (200, {"code": 0, "checked_in": False, "credits": 0, "enable": True}),
        (200, {"code": 0, "message": "success"}),                     # claim：无积分数
        (200, {"code": 0, "checked_in": True, "credits": 150, "enable": True}),
    ]))
    out = asyncio.run(tr.claim_checkin(_creds()))
    assert out["kind"] == "claimed"
    assert out["credit"] == 150                     # 来自**补查的 status**
    urls = [c["url"] for c in calls]
    assert urls[0].endswith("/checkin_credits/status")
    assert urls[1].endswith("/checkin_credits/claim")
    assert urls[2].endswith("/checkin_credits/status")     # 补查
    # claim body 是 `{}`（不是 {"req_source":2}）
    assert calls[1]["json"] == {}


def test_checkin_body_is_empty_dict(monkeypatch):
    """status/claim 的 body 都是 `{}` —— 发 `{"req_source":2}` 是错的。"""
    calls = []
    monkeypatch.setattr(tr.httpx, "AsyncClient", make_httpx(calls, [
        (200, {"code": 0, "checked_in": True, "enable": True})]))
    asyncio.run(tr.fetch_checkin_status(_creds()))
    assert calls[0]["json"] == {}


def test_checkin_9074_is_business_error_with_300s_cooldown(monkeypatch):
    """⚠️ `9074` 归为**业务错误**（300s 冷却），**不换设备号重试**。

    9074 的限流范围是 **device_id 而非账号**，但设备身份现在由 uid 确定性派生、
    每账号独立 —— 「换个派生 id 立刻成功」的旧前提已不成立（那条链路已废弃）。
    """
    calls = []
    monkeypatch.setattr(tr.httpx, "AsyncClient", make_httpx(calls, [
        (200, {"code": 0, "checked_in": False, "enable": True}),
        (200, {"code": 9074, "message": "too many users, retry later"}),
    ]))
    out = asyncio.run(tr.claim_checkin(_creds()))
    assert out["kind"] == "failed"
    assert out["upstream_code"] == 9074
    assert out["error_type"] == "BusinessError"
    assert out["cooldown_secs"] == 300
    # 只有两次请求：status + claim。**没有**「换设备号再试一次」
    assert len(calls) == 2


def test_checkin_business_code_as_string_is_not_success(monkeypatch):
    """⚠️ 业务码必须兼容**字符串**形态（`"9074"`）。

    部分网关把 code 以字符串下发；只认数字会把它误判成成功（缺省 0），
    于是「签到失败」被报成「签到成功」。
    """
    assert tr.read_claim_code({"code": "9074"}) == 9074
    assert tr.read_claim_code({"code": 9074}) == 9074
    assert tr.read_claim_code({}) == 0
    assert tr.read_claim_code({"code": "abc"}) == -1
    assert tr.read_claim_code({"code": True}) == -1


def test_checkin_error_classification_matches_reference():
    """错误分类（对齐 trae-mate `cooldown.rs`）：

    `200+1005 → PlanLimit(12h)` / `429 → SoftRate(60s)` / `401 → SessionDead(永久)`
    / `404 → NotFound(60s)` / `5xx → Server(600s)` / `4xx → Client(600s)`
    / `业务码非0 → BusinessError(300s)`。
    """
    assert tr.classify_checkin_error(200, 1005) == ("PlanLimit", 43_200)
    assert tr.classify_checkin_error(429, 0) == ("SoftRate", 60)
    assert tr.classify_checkin_error(401, None) == ("SessionDead", -1)
    assert tr.classify_checkin_error(404, 0) == ("NotFound", 60)
    assert tr.classify_checkin_error(503, 0) == ("Server", 600)
    assert tr.classify_checkin_error(400, 0) == ("Client", 600)
    assert tr.classify_checkin_error(200, 12345) == ("BusinessError", 300)


def test_checkin_network_failure_is_not_reported_as_claimed(monkeypatch):
    """网络异常与业务失败必须分开：状态查不到时**不能**判「已签到」。"""
    calls = []
    monkeypatch.setattr(tr.httpx, "AsyncClient", make_httpx(calls, [(500, {})]))
    out = asyncio.run(tr.claim_checkin(_creds()))
    assert out["kind"] == "failed"
    assert "状态查询失败" in out["message"]


def test_checkin_inactive_when_activity_disabled(monkeypatch):
    """`enable: false` → inactive（不是 failed：不算「今天已完成」也不该重试）。"""
    calls = []
    monkeypatch.setattr(tr.httpx, "AsyncClient", make_httpx(calls, [
        (200, {"code": 0, "checked_in": False, "enable": False})]))
    out = asyncio.run(tr.claim_checkin(_creds()))
    assert out["kind"] == "inactive"
    assert len(calls) == 1


# ── 10. 签到设备身份由 uid 确定性派生 ──────────────────

def test_checkin_device_identity_derived_from_uid():
    """⚠️ 签到设备身份三件套由 **uid 确定性派生**（对齐 trae-mate `device_map.rs`）。

    与旧实现的关键差异：旧版由 `credential.device_id`（32 hex）派生，而实测
    签到成功用的是**基于 user_id 的 15 位数字** + UUID v4 market id + 64 hex
    session id。同一 uid 永远得到同一套值 → 多账号天然互异，规避
    「每设备每天一次」配额。
    """
    headers = tr.derive_checkin_headers("user-abc-123")
    # 与 Node 参考实现逐字节一致（用 Jet-Hub 的 seededStream 交叉验算过）
    assert headers["X-Device-Id"] == "677321100802992"
    assert headers["X-Market-User-Id"] == "470d4cf0-ebb2-44c5-8871-d2b82fd4817c"
    assert headers["Vscode-Sessionid"] == (
        "0cb35b1277fb65ab1c7b50304910945fd8bb76a7b8c7917abb1bcef5bc5b5088")
    # 确定性：同 uid 必须完全一致（跨进程/重启稳定）
    assert tr.derive_checkin_headers("user-abc-123") == headers
    # 不同 uid → 不同设备身份（每账号独立）
    other = tr.derive_checkin_headers("user-xyz-999")
    assert other["X-Device-Id"] != headers["X-Device-Id"]
    # 15 位纯数字
    assert len(headers["X-Device-Id"]) == 15
    assert headers["X-Device-Id"].isdigit()
    # UUID v4 形态（version nibble = 4，variant nibble ∈ {8,9,a,b}）
    market = headers["X-Market-User-Id"]
    assert market[14] == "4"
    assert market[19] in "89ab"
    assert len(headers["Vscode-Sessionid"]) == 64


def test_checkin_headers_are_full_client_set(monkeypatch):
    """签到必须用**完整**约 20 个客户端头（早期精简到 6 个时签到一直失败）。"""
    calls = []
    monkeypatch.setattr(tr.httpx, "AsyncClient", make_httpx(calls, [
        (200, {"code": 0, "checked_in": True, "enable": True})]))
    asyncio.run(tr.fetch_checkin_status(_creds()))
    headers = calls[0]["headers"]
    for key in ("X-Market-Client-Id", "X-Market-User-Id", "X-User-Region",
                "X-Device-Id", "X-Lgw-Req-Sdk-Type", "Package-Type", "X-Lscbd-Aid",
                "X-Lscbd-Platform", "App-Version", "X-Tt-Trace-Id",
                "Vscode-Sessionid", "X-Request-Id", "Sec-Fetch-Dest"):
        assert key in headers, f"缺头 {key}"
    assert headers["Authorization"] == "Cloud-IDE-JWT at-1"
    assert headers["X-Lscbd-Aid"] == "787976"
    assert headers["X-Lgw-Req-Sdk-Type"] == "3"
    assert headers["Package-Type"] == "stable_cn"


def test_checkin_request_id_refreshes_each_call():
    """`X-Request-Id` / `X-Tt-Trace-Id` **每请求刷新**（设备身份才是确定性的）。"""
    first = tr.checkin_headers("at-1", "uid-1")
    second = tr.checkin_headers("at-1", "uid-1")
    assert first["X-Request-Id"] != second["X-Request-Id"]
    assert first["X-Tt-Trace-Id"] != second["X-Tt-Trace-Id"]
    # 设备身份保持不变
    assert first["X-Device-Id"] == second["X-Device-Id"]
    assert first["Vscode-Sessionid"] == second["Vscode-Sessionid"]


# ── 11. 额度 ────────────────────────────────────────

def test_parse_ent_usage_sums_per_package():
    """⚠️ `remain = Σ(credits_limit − credits_amount)`（**逐包**求和）。

    只读外层拿不到该值（外层没有这个字段）—— 必须遍历
    `user_entitlement_pack_list` 里每个包的 quota 与 usage。
    """
    body = {"user_entitlement_pack_list": [
        {"entitlement_base_info": {"name": "签到奖励",
                                   "quota": {"credits_limit": 150}},
         "usage": {"credits_amount": 20}},
        {"entitlement_base_info": {"name": "免费额度",
                                   "quota": {"credits_limit": 1000}},
         "usage": {"credits_amount": 900}},
        {"entitlement_base_info": {"name": "无 quota 的包"}},
    ]}
    parsed = tr.parse_ent_usage(body)
    assert parsed["remaining"] == 230.0        # (150-20) + (1000-900)
    assert parsed["total_limit"] == 1150.0
    assert len(parsed["packages"]) == 2       # 缺 quota 的条目跳过（不编造）


def test_credits_body_requires_require_usage(monkeypatch):
    """⚠️ 余额 body 必须含 `require_usage: true`。

    不带它拿不到 `usage`，余额会**恒等于额度**（看起来「一点没用」）。
    """
    calls = []
    monkeypatch.setattr(tr.httpx, "AsyncClient", make_httpx(calls, [(200, {
        "user_entitlement_pack_list": [
            {"entitlement_base_info": {"name": "包", "quota": {"credits_limit": 10}},
             "usage": {"credits_amount": 4}}]})]))
    result = asyncio.run(tr.fetch_credits(_creds()))
    assert calls[0]["json"] == {"require_usage": True, "req_source": 2}
    assert calls[0]["url"].endswith("/trae/api/v2/pay/ide_user_ent_usage")
    assert result["remaining"] == 6.0


def test_credits_failure_is_distinguishable_from_zero_balance(monkeypatch):
    """查不到要能区分于「余额为 0」（否则会把解析失败伪装成 0 积分）。"""
    calls = []
    monkeypatch.setattr(tr.httpx, "AsyncClient", make_httpx(calls, [(401, {})]))
    result = asyncio.run(tr.fetch_credits(_creds()))
    assert result["remaining"] == 0.0
    assert result["packages"] == []
    assert "401" in result["error"]


# ── 12. 错误分类（4008 必须先于 4011）───────────────

def test_classify_error_quota_before_soft_rate():
    """⚠️ `quota-exceeded`(4008) 必须排在 `soft-rate`(4011) **之前**。

    两者可能同时出现在一个响应体里（网关把多个错误码拼在 msg 中）。
    `quota-exceeded` 需长冷却、`soft-rate` 只需短冷却 —— 让较轻的类别抢先命中，
    会让一个已耗尽额度的账号在 60 秒后被反复重试，用户看到的却是「稍后再试」。
    """
    body = '{"code":4008,"message":"4011 rate limited, quota exceeded the quota"}'
    assert tr.classify_error(200, body) == "quota-exceeded"
    assert tr.classify_error(200, '{"code":4011}') == "soft-rate"
    assert tr.classify_error(200, '{"code":1005,"message":"plan limit"}') == "hard-plan"
    assert tr.classify_error(401, "") == "session-dead"
    assert tr.classify_error(429, "") == "soft-rate"
    assert tr.classify_error(404, "") == "not-found"
    assert tr.classify_error(503, "") == "server"
    assert tr.classify_error(400, "") == "client"
    assert tr.classify_error(200, "") == "none"


def test_terminal_and_rotate_semantics():
    """会话失效是**终态**（重试无意义，只能重新登录）；其余都该换号。"""
    assert tr.is_terminal_error("session-dead") is True
    assert tr.is_terminal_error("soft-rate") is False
    assert tr.should_rotate_account("none") is False
    assert tr.should_rotate_account("quota-exceeded") is True


# ── 13. 历史裁剪（不切断工具配对）───────────────────

def test_trim_history_never_cuts_tool_pairing():
    """⚠️ 历史裁剪的三条硬约束：

    1. 从**最早的非 system** 消息开始丢；
    2. **以「轮」为单位**，绝不切断 `tool_calls` 与其后的 `role:"tool"` 结果
       （丢一半会被上游 400 拒绝整个请求）；
    3. **system 消息永不裁剪**。

    上游超限时是**静默断流**（不发错误码，流就那么断掉），不裁剪的失败极难排查。
    """
    big = "x" * 500
    messages = [
        {"role": "system", "content": "S" * 300},                     # 永不裁剪
        {"role": "user", "content": big},                              # 第 1 轮
        {"role": "assistant", "content": None,
         "tool_calls": [{"id": "c1", "type": "function",
                         "function": {"name": "f", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "c1", "content": big},
        {"role": "user", "content": "最近的问题"},                     # 第 2 轮（必须保住）
    ]
    trimmed = tr.trim_history(messages, max_chars=400)
    roles = [m["role"] for m in trimmed]
    assert "system" in roles                                   # system 保住
    assert trimmed[-1]["content"] == "最近的问题"               # 最近历史保住
    # 工具配对完整性：有 tool_calls 的 assistant 与紧随的 tool 要么都在、要么都不在
    has_call = any(m.get("tool_calls") for m in trimmed)
    has_tool = any(m.get("role") == "tool" for m in trimmed)
    assert has_call == has_tool, trimmed
    # 未超预算时**原样返回**（不做任何改写）
    assert tr.trim_history([{"role": "user", "content": "hi"}], max_chars=1000) == \
        [{"role": "user", "content": "hi"}]


def test_trim_history_drops_from_the_oldest_non_system():
    """超限时丢的是**最早的非 system** 消息，而不是 system 或最近的。"""
    messages = [{"role": "system", "content": "sys"}]
    messages += [{"role": "user", "content": f"m{i}" + "y" * 200} for i in range(5)]
    trimmed = tr.trim_history(messages, max_chars=300)
    assert trimmed[0]["role"] == "system"
    assert trimmed[-1]["content"].startswith("m4")             # 最新一条仍在


def test_clamp_max_tokens_only_clamps_positive_ints():
    """输出额度收敛：客户端索要 131072 会把上游打成 4xx，故按 64000 收敛。

    非正整数原样返回（**不编造**数值）。
    """
    assert tr.clamp_max_tokens(131_072) == 64_000
    assert tr.clamp_max_tokens(1_000) == 1_000
    assert tr.clamp_max_tokens(None) is None
    assert tr.clamp_max_tokens(0) == 0
    assert tr.clamp_max_tokens(-5) == -5
    assert tr.clamp_max_tokens(50_000, limit=0) == 50_000      # limit<=0 表示不收敛


# ── 14. 适配器模型目录出口 ───────────────────────────

def test_list_models_filters_custom_models_and_marks_free(monkeypatch):
    """`list_models` 剔除 `is_custom_model == true`（必然流内 4001），
    并把 `rate == 0` 如实标为免费。

    ⚠️ `rate` 为 0 必须能与「无倍率信息」区分开：用 `> 0` 过滤会恰好漏掉
    用户最关心的免费模型。
    """
    free_cfg = json.dumps({"consumption_rate": {"enable": True, "data": {"rate": 0}}})
    paid_cfg = json.dumps({"consumption_rate": {"enable": True, "data": {"rate": 0.08}}})
    catalog_body = {"function_configs": [{"function": "solo_agent", "config_info_list": [
        _entry("free-model", display_contact_config=free_cfg,
               context_window_tokens={"dev": 200_000}),
        _entry("paid-model", display_contact_config=paid_cfg),
        _entry("no-rate-model"),
        _entry("custom", display_config={"display_name": "Custom",
                                         "is_custom_model": True}),
    ]}]}
    calls = []
    adapter, _ = _adapter(monkeypatch, catalog_body, calls)
    models = asyncio.run(adapter.list_models("at-1", "https://trae-api-cn.mchost.guru"))
    by_id = {m.model_id: m for m in models}
    assert "custom" not in by_id                      # 自定义模型必须剔除
    assert by_id["free-model"].is_free is True
    assert by_id["free-model"].display_name == "FREE-MODEL · 免费"
    assert by_id["free-model"].price_ratio == 0.0
    assert by_id["paid-model"].is_free is False
    assert by_id["paid-model"].display_name == "PAID-MODEL · x0.08"
    # 没有倍率信息 → 只显示模型名（**不编造** x1）
    assert by_id["no-rate-model"].display_name == "NO-RATE-MODEL"
    assert by_id["no-rate-model"].price_ratio is None
    assert by_id["free-model"].context_length == 200_000


def test_list_models_marks_encrypted_only_models(monkeypatch):
    """⚠️ 依赖 CN 加密信封的模型**如实标注**（不静默列出一个注定失败的模型）。

    真实 CN IDE 的 `llm_utils_chat` 请求体是加密的（配 `x-helios` / `x-medusa`
    等头），Jet-Hub 与本实现**都未实现**该加密。标注而非过滤：模型仍可被点名
    调用，失败时由上游如实报错 —— Jet-Hub 反复强调「把某一刻的快照写成判据
    会让后人误删可用模型」（`is_custom_model` 名单就整体失效过）。
    """
    catalog_body = {"function_configs": [{"function": "solo_agent", "config_info_list": [
        _entry("deepseek-v4-flash"), _entry("glm-5.2")]}]}
    calls = []
    adapter, _ = _adapter(monkeypatch, catalog_body, calls)
    models = asyncio.run(adapter.list_models("at-1", "https://trae-api-cn.mchost.guru"))
    by_id = {m.model_id: m for m in models}
    assert "暂不可用" in by_id["deepseek-v4-flash"].display_name
    assert "暂不可用" not in by_id["glm-5.2"].display_name
    assert "deepseek-v4-flash" in by_id       # 仍在目录里（不过滤）


def test_list_models_vision_is_per_model(monkeypatch):
    """⚠️ 图片能力**逐模型**判定（远端 `display_config.multimodal`）。

    它是 AIGate 的**准入闸门**：未声明 `image` 时附件在入库阶段就被拒，用户看到
    「当前模型不支持图片」——而图根本没发到上游，报错还指向**模型**而非插件。
    Jet-Hub 因此吃过 Issue #IKHDKC：早期恒返回 `['text']` 证伪了「SOLO 通道不
    支持图片」的假设（直发红图答「红色」、蓝图答「蓝色」、无图答「无法确定」）。
    未声明一律按不支持（保守，不臆造能力）。
    """
    catalog_body = {"function_configs": [{"function": "solo_agent", "config_info_list": [
        _entry("with-image", display_config={"display_name": "A", "multimodal": True}),
        _entry("no-image", display_config={"display_name": "B", "multimodal": False}),
        _entry("undeclared")]}]}
    calls = []
    adapter, _ = _adapter(monkeypatch, catalog_body, calls)
    models = asyncio.run(adapter.list_models("at-1", "https://trae-api-cn.mchost.guru"))
    by_id = {m.model_id: m for m in models}
    assert by_id["with-image"].input_modalities == ["text", "image"]
    assert by_id["with-image"].supports_vision is True
    assert by_id["no-image"].input_modalities == ["text"]
    assert by_id["undeclared"].input_modalities == ["text"]


def test_list_models_raises_when_catalog_unavailable(monkeypatch):
    """目录拉不到时**抛错**（由 model_catalog 回退注册表静态种子），不返回空列表。"""
    calls = []
    # catalog_body=None → 目录端点的响应从 results 队列取（这里是 500）
    adapter, _ = _adapter(monkeypatch, None, calls, results=[(500, {})])
    with pytest.raises(RuntimeError):
        asyncio.run(adapter.list_models("at-1", "https://trae-api-cn.mchost.guru"))


def test_fetch_models_uses_batch_endpoint_with_all_functions(monkeypatch):
    """⚠️ 必须用 **batch** 端点并传**全部 22 个 function**。

    单 function 的 `get_detail_param` 只能拿一个通道的目录；真实 CN IDE 传的是
    全部 22 个 —— 只传几个聊天通道会让非聊天通道的条目在响应里**位置错乱**，
    甚至被解析器跳过。
    """
    calls = []
    monkeypatch.setattr(tr.httpx, "AsyncClient", make_httpx(calls, [(200, {
        "function_configs": [{"function": "solo_agent", "config_info_list": [
            _entry("m1")]}]})]))
    models = asyncio.run(tr.fetch_models(_creds()))
    assert [m["id"] for m in models] == ["m1"]
    assert calls[0]["url"].endswith("/api/ide/v1/batch_get_detail_param")
    body = calls[0]["json"]
    assert len(body["functions"]) == 22
    assert "solo_agent" in body["functions"]
    assert body["show_custom_model"] is True
    # 模型列表端点用**非流式** Accept（同一个 SOLO 头集）
    assert calls[0]["headers"]["Accept"] == "application/json"
    assert calls[0]["headers"]["Authorization"] == "Cloud-IDE-JWT at-1"


def test_fetch_models_never_raises(monkeypatch):
    """模型列表拉取失败返回空列表（**永不抛异常**）—— 不该阻塞推理。"""
    class _Boom:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            raise OSError("network down")

        async def __aexit__(self, *a):
            return False

    monkeypatch.setattr(tr.httpx, "AsyncClient", _Boom)
    assert asyncio.run(tr.fetch_models(_creds())) == []
    assert asyncio.run(tr.fetch_models({})) == []


# ── 15. 健康探测 ────────────────────────────────────

def test_health_check_reports_degraded_for_unknown_model(monkeypatch):
    """目录里没有该模型 → degraded（不是 unhealthy：连接本身是好的）。"""
    catalog_body = {"function_configs": [{"function": "solo_agent", "config_info_list": [
        _entry("known")]}]}
    calls = []
    adapter, _ = _adapter(monkeypatch, catalog_body, calls)
    ok = asyncio.run(adapter.health_check("known", "at-1", "https://trae-api-cn.mchost.guru"))
    assert ok.status == "healthy" and ok.latency_ms >= 0
    bad = asyncio.run(adapter.health_check("nope", "at-1", "https://trae-api-cn.mchost.guru"))
    assert bad.status == "degraded" and "nope" in bad.error_message


def test_health_check_unhealthy_on_catalog_failure(monkeypatch):
    calls = []
    adapter, _ = _adapter(monkeypatch, None, calls, results=[(503, {})])
    result = asyncio.run(adapter.health_check("m", "at-1", "https://trae-api-cn.mchost.guru"))
    assert result.status == "unhealthy"


# ── 16. 适配器契约 ──────────────────────────────────

def test_adapter_implements_base_interface():
    """适配器必须实现 BaseAdapter 的四个抽象方法（签名严格对齐）。"""
    import inspect
    from server.adapters.base_adapter import BaseAdapter
    assert issubclass(TraeAdapter, BaseAdapter)
    for name in ("chat_completion", "stream_chat_completion", "list_models",
                 "health_check"):
        assert callable(getattr(TraeAdapter, name))
    sig = inspect.signature(TraeAdapter.health_check)
    assert list(sig.parameters) == ["self", "model", "api_key", "base_url",
                                    "extra_headers", "timeout"]
    assert sig.parameters["timeout"].default == 10


# ── 小工具 ──────────────────────────────────────────

def _run(async_gen_fn, *args):
    """同步驱动一个 async generator（测试里不想引入 pytest-asyncio 依赖）。

    ⚠️ 注意 `stream_chat_completion` 的签名是
    `(request, api_key, base_url, extra_headers=None)`。
    """
    gen = async_gen_fn(*args)

    async def _collect():
        if hasattr(gen, "__aiter__"):
            return [item async for item in gen]
        return await gen

    return asyncio.run(_collect())
