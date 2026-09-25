"""Trae（字节跳动，trae.cn / trae.ai）协议实现。

协议来源：Jet-Hub 源码逐行核对（github.com/zhengwuji/Jet-Hub，MIT）
+ `docs/agents/trae.md` 的实测记录（2026-09）。本模块只承载**协议细节**，
登录编排在 oauth_client、配额/签到在 oauth_usage / checkin（由主 agent 接线）。

## 与其它渠道的差异（全部是实测结论，不是推测）

1. **回调直接回传 token，没有 `?code=`**（`trae-oauth.ts:236-247`）：
   真实回调形如
   `http://127.0.0.1:18080/authorize?refreshToken=..&userInfo={..}&userJwt={..}`。
   按 OAuth 惯例去找 `code` 会恒判失败 → 回调服务器回 400 → 前端永远「认证中」。
   ⚠️ 但「带 code 的回调」**不是**无效回调：授权页并存 PKCE 新流程，
   本实现**未实现** PKCE 交换，故对这种形态给出**精确报错**而非「缺 refreshToken」。
2. **登录 URL 必须 18 个参数**（唯一权威 `trae-oauth.ts:99-127`）。早期实现只发 5 个
   且回调参数名写成 `callback_url` / `redirect_uri` —— 真实参数名是
   **`auth_callback_url`**，名字错了登录页会**永远停在授权中**（既不跳转也不回传）。
3. **凭据必须持久化 `machine_id` / `device_id`**，且 `machine_id` **绝不可重新生成**
   （上游按它标识设备，换值可能触发风控或要求重新登录）。
   二者都是 **32 hex**（`openssl rand -hex 16` 形态），不是 16 位数字。
4. **`refresh_token` 会轮换**：ExchangeToken 每次返回新 access + 新 refresh，
   续期后必须回写（否则下一次续期用旧票失败）。身份字段（machine_id/device_id）
   **一律不动**。
5. **请求与响应都要转换**（非 OpenAI 兼容）：请求体走 `transform_to_solo_body`，
   响应是 SOLO 自定义 SSE（`output` / `token_usage` / `done` / `error` …）。
6. **模型只在列出它的通道里可调用**：发错 `function` 会得到**流内** `4001`
   （HTTP 仍是 200）。故模型目录必须按通道合并、并记住每条模型的所属通道。
7. **HTTP 200 零事件只允许在首个模型事件之前重试一次**：一旦已有 output / usage /
   tool_calls 就绝不重放 —— 重放会让上游**重复计费并可能重复执行工具**。
8. **历史超过 ~480K 字符上游会静默断流**（不发错误码，流就那么断掉），
   故裁剪必须发生在序列化之后，且**绝不能切断 tool_call / tool 配对**。

## ⚠️ 未实现：真实 CN IDE 的请求体加密（如实登记，不假装支持）

Reqable 抓包（`Trae CN.exe 3.3.94`）显示 `llm_utils_chat` / `create_agent_task`
的**请求体是加密的**（配 `x-helios` / `x-medusa` / `x-neptune` / `x-request-pin` /
`x-requested-at` 五个头）。Jet-Hub 同样**未实现**该信封，实测仅换版本头解不开 ——
依赖该加密的模型（如 `deepseek-v4-flash` 等实际走加密通道的那批）
**不可用**。本模块走的是**旧 SOLO 明文协议**（`solo_work_lite` 等通道），
它逐请求可用；`list_models` 会在返回的显示名上标注该限制。
另注：真实 IDE 的 `x-machine-id` 是 64 hex、`x-device-id` 是 16 位数字，
与旧 SOLO 协议的 32hex/32hex **不同**，不可照搬。
"""
from __future__ import annotations

import hashlib
import json
import logging
import secrets
import time
import uuid
from typing import Dict, List, Optional, Tuple
from urllib.parse import parse_qs, quote, unquote, urlparse

import httpx

logger = logging.getLogger(__name__)

# ── 常量（照抄 Jet-Hub trae.ts / trae-product.ts / trae-oauth.ts）──
TRAE_CN_AGENT_HOST = "https://trae-api-cn.mchost.guru"
TRAE_CN_UG_HOST = "https://api.trae.cn"
TRAE_CN_OAUTH_HOST = "https://api.trae.com.cn"
TRAE_CN_CONSOLE_HOST = "https://www.trae.cn"
# 国际版（日志 x-tt-net-final-domain 实测出站域名；不是把 -cn 换成 -sg 的猜测值）
TRAE_INTL_AGENT_HOST = "https://api5-normal-alisg.mchost.guru"
TRAE_INTL_UG_HOST = "https://api.trae.ai"
TRAE_INTL_OAUTH_HOST = "https://api.trae.ai"
TRAE_INTL_CONSOLE_HOST = "https://www.trae.ai"

TRAE_CHAT_PATH = "/api/agent/v3/llm_utils_chat"
TRAE_MODELS_PATH = "/api/ide/v1/get_detail_param"
TRAE_BATCH_MODELS_PATH = "/api/ide/v1/batch_get_detail_param"
TRAE_EXCHANGE_PATH = "/cloudide/api/v3/trae/oauth/ExchangeToken"
TRAE_USER_INFO_PATH = "/cloudide/api/v3/trae/GetUserInfo"
TRAE_CHECKIN_STATUS_PATH = "/trae/api/v2/ug/checkin_credits/status"
TRAE_CHECKIN_CLAIM_PATH = "/trae/api/v2/ug/checkin_credits/claim"
TRAE_ENT_USAGE_PATH = "/trae/api/v2/pay/ide_user_ent_usage"
TRAE_CALLBACK_PATH = "/authorize"

TRAE_CALLBACK_PORT = 18080
TRAE_REQUEST_TIMEOUT_MS = 30_000
TRAE_LOGIN_TIMEOUT_MS = 10 * 60 * 1000

# 产品身份常量（照抄 trae-product.ts 的 TRAE / TRAE_INTL 两套配置）
TRAE_CLIENT_ID = "en1oxy7wnw8j9n"
TRAE_APP_ID = "6eefa01c-1036-4c7e-9ca5-d891f63bfcd8"
TRAE_IDE_VERSION = "0.1.52"
TRAE_IDE_VERSION_CODE = "20260811"
# 登录门户认的**插件**版本，与 ideVersion 是两个独立字段（不可混用）
TRAE_PLUGIN_VERSION = "2.3.62834"
TRAE_DEVICE_BRAND = "Apple"
TRAE_OS_VERSION = "macOS 15.7.4"
TRAE_USER_AGENT = "Trae/0.1.52"
TRAE_DEFAULT_FUNCTION = "solo_work_lite"
TRAE_DEFAULT_MODEL = "glm-5.2"
TRAE_FALLBACK_MAX_OUTPUT_TOKENS = 32_000

# 默认拉取的对话通道（顺序即优先级，第一位是官方 Auto Mode 的目录）
TRAE_DEFAULT_CHANNELS = ("solo_agent", "solo_work_lite", "solo_agent_remote")

# 真实 CN IDE 交给 batch_get_detail_param 的**全部** 22 个 function
# （照抄 trae-auth.ts:506-514）。只传几个聊天通道会让非聊天通道的条目
# 在响应里出现位置错乱，甚至被解析器跳过。
TRAE_BATCH_FUNCTIONS = (
    "ui_builder_v2", "solo_coder", "chat_v3", "solo_builder",
    "builder_v3", "builder", "chat", "inline_chat", "git_ai",
    "custom_agent_generation", "utils", "code_reviewer",
    "code_review_summary", "solo_agent", "solo_agent_remote",
    "solo_work_remote", "solo_agent_lite", "solo_work_lite",
    "solo_design_lite", "solo_design_remote", "multimodal",
    "system_diagnosis",
)

# 历史字符预算（上下沿都取自 CN 项目的实测阈值）
TRAE_MAX_HISTORY_CHARS = 480_000
# 单次输出安全上限：CN 项目实测 agent-remote 单响应 64000，客户端索要 131072
# 会把上游打成 4xx。取 **下沿**（主流模型远端只声明 32000）——保留为兜底保险。
TRAE_MAX_COMPLETION_TOKENS = 64_000

# 业务码
TRAE_CODE_CHANNEL_ERROR = 4001     # 通道/参数错误（发错 function 或自定义模型）
TRAE_CODE_QUOTA_EXCEEDED = 4008    # ide_credits 耗尽
TRAE_CODE_PLAN_LIMIT = 1005        # plan 权益不足
TRAE_CODE_RATE_LIMITED = 4011      # 频率超限
TRAE_CODE_CHECKIN_BUSY = 9074      # 签到限流（范围是 device_id，不是账号）

# 签到错误冷却（对齐 trae-mate cooldown.rs）
TRAE_COOLDOWN_PLAN_LIMIT = 43_200
TRAE_COOLDOWN_SOFT_RATE = 60
TRAE_COOLDOWN_BIZ_ERROR = 300
TRAE_COOLDOWN_SERVER = 600

# list_models 显示名上如实标注的限制（加密信封未实现）
TRAE_ENCRYPTION_NOTE = "（走旧 SOLO 明文协议；CN IDE 加密信封未实现）"

_TOTAL_CALL_KEYS = ("prompt_tokens", "completion_tokens", "total_tokens")


# ── 产品配置 ─────────────────────────────────────────

def is_intl(domain: str) -> bool:
    """域名 → 是否国际版（trae.ai 系）。空域名按国内版。"""
    return "trae.ai" in str(domain or "").lower()


def product_for(domain: str = "") -> dict:
    """按域名取产品配置（照抄 trae-product.ts 的两套配置，仅域名不同）。"""
    if is_intl(domain):
        return {
            "code": "trae_intl", "site": "global", "display_name": "TRAE (国际版)",
            "agent_host": TRAE_INTL_AGENT_HOST, "ug_host": TRAE_INTL_UG_HOST,
            "oauth_host": TRAE_INTL_OAUTH_HOST, "console_host": TRAE_INTL_CONSOLE_HOST,
            "client_id": TRAE_CLIENT_ID, "app_id": TRAE_APP_ID,
            "ide_version": TRAE_IDE_VERSION, "ide_version_code": TRAE_IDE_VERSION_CODE,
            "plugin_version": TRAE_PLUGIN_VERSION, "device_brand": TRAE_DEVICE_BRAND,
            "os_version": TRAE_OS_VERSION, "user_agent": TRAE_USER_AGENT,
            "function": TRAE_DEFAULT_FUNCTION,
        }
    return {
        "code": "trae", "site": "cn", "display_name": "TRAE (字节)",
        "agent_host": TRAE_CN_AGENT_HOST, "ug_host": TRAE_CN_UG_HOST,
        "oauth_host": TRAE_CN_OAUTH_HOST, "console_host": TRAE_CN_CONSOLE_HOST,
        "client_id": TRAE_CLIENT_ID, "app_id": TRAE_APP_ID,
        "ide_version": TRAE_IDE_VERSION, "ide_version_code": TRAE_IDE_VERSION_CODE,
        "plugin_version": TRAE_PLUGIN_VERSION, "device_brand": TRAE_DEVICE_BRAND,
        "os_version": TRAE_OS_VERSION, "user_agent": TRAE_USER_AGENT,
        "function": TRAE_DEFAULT_FUNCTION,
    }


# ── 设备身份 ─────────────────────────────────────────

def generate_machine_id() -> str:
    """生成 32 hex 的 machine_id（16 字节随机 → 32 hex）。

    ⚠️ 登录时生成后**绝不可重新生成**（对齐 `trae.ts:104-112`）：
    上游按它标识设备，换值可能触发风控或要求重新登录。
    """
    return secrets.token_hex(16)


def generate_device_id() -> str:
    """生成 32 hex 的 device_id（登录设备号，**账号间必须互异**）。

    早期实现误用「16 位纯数字」（那是 CodeBuddy 的签到格式），与 Trae 协议不符：
    该值随登录 URL 的 `device_id` / `x_device_id` 下发，也写进凭据。
    同一天两个账号共用同一 device_id 会被「该设备已签到」拦截。
    """
    return secrets.token_hex(16)


def machine_trace_id(machine_id: str, device_id: str) -> str:
    """由 machine_id + device_id 派生稳定 `login_trace_id`（hex16）。

    对齐 Go 端 `machineTraceID`：取拼接串的**尾部 16 字符**（不足则左补 0）。
    回调不保证回传 machine_id/device_id，但会回传 login_trace_id —— 它是回调
    反查 pending 会话的唯一凭据。
    """
    joined = f"{machine_id}{device_id}"
    return joined[-16:] if len(joined) >= 16 else joined.rjust(16, "0")


# ── 登录 URL（18 参数，一个都不能少）──────────────────

def build_login_url(port: int, machine_id: str, device_id: str,
                    domain: str = "") -> str:
    """构造登录 URL（**完整 18 参数**，照抄 `trae-oauth.ts:99-127`）。

    ⚠️ 为什么参数一个都不能少（真实缺陷，用户症状是「网页一直停在认证中」）：
    1. 回调地址的参数名是 **`auth_callback_url`**，不是 `callback_url` /
       `redirect_uri` —— 名字错了上游拿不到回调地址，登录页既不跳转也不回传；
    2. `auth_from` / `login_channel` / `auth_type` / `redirect` 决定走哪条授权
       通道，缺失时不会走到本地回传分支；
    3. `login_trace_id` 是回调反查 pending 的唯一凭据；
    4. `x_*` 系列是客户端形态伪装，缺席可能被风控拦截。

    @param port 回调服务器**实际**监听的端口（被占用时会回退随机端口，
           必须先 listen 拿到实际端口再构造 URL，否则回调打到没人监听的地址）
    """
    product = product_for(domain)
    callback_url = f"http://127.0.0.1:{int(port)}{TRAE_CALLBACK_PATH}"
    # 逐字段 urlencode（quote 而非 urlencode：保持与 URLSearchParams 一致的
    # 空格/符号编码，且不用 dict 顺序参与语义）
    pairs = [
        ("login_version", "1"),
        ("auth_from", "solo"),
        ("login_channel", "native_ide"),
        ("plugin_version", product["plugin_version"]),
        ("auth_type", "local"),
        ("client_id", product["client_id"]),
        ("redirect", "0"),
        ("login_trace_id", machine_trace_id(machine_id, device_id)),
        ("auth_callback_url", callback_url),
        ("machine_id", machine_id),
        ("device_id", device_id),
        ("x_device_id", device_id),
        ("x_machine_id", machine_id),
        ("x_device_brand", "PC"),
        ("x_device_type", "PC"),
        ("x_os_version", "1.0"),
        ("x_app_version", product["ide_version"]),
        ("x_app_type", "stable"),
    ]
    query = "&".join(f"{k}={quote(str(v), safe='')}" for k, v in pairs)
    return f"{product['console_host']}/authorization?{query}"


# ── 回调解析 ─────────────────────────────────────────

def _parsed_query(raw_url: str) -> Optional[Dict[str, List[str]]]:
    """解析回调 URL 的 query；允许传入相对形式（`/authorize?...`）。"""
    s = str(raw_url or "")
    if not s:
        return None
    if not s.startswith(("http://", "https://")):
        # 相对形式：补全成一个可解析的 URL（host 无所谓，只用 query）
        s = f"http://127.0.0.1{s if s.startswith('/') else '/' + s}"
    try:
        return parse_qs(urlparse(s).query, keep_blank_values=True, strict_parsing=False)
    except ValueError:
        return None


def _first(query: Dict[str, List[str]], *keys: str) -> str:
    """取第一个**非空**候选（不能用 `or` 串空串：空串会短路掉后续来源）。"""
    for k in keys:
        for v in query.get(k) or []:
            if isinstance(v, str) and v.strip():
                return v.strip()
    return ""


def _json_param(raw: Optional[str]) -> Optional[dict]:
    """解析回调里 URL 编码的 JSON 参数（`userInfo` / `userJwt` / `authCodeInfo`）。

    已解码一层，但 Trae 的 `userInfo` 中文存在**双重编码**（实测昵称乱码
    `Óû§8847309959`），故再容错解一层。非对象（数组/标量/非法 JSON）返回 None。
    """
    if not raw:
        return None
    candidates = [raw]
    try:
        unescaped = unquote(raw)
        if unescaped != raw:
            candidates.append(unescaped)
    except Exception:
        pass
    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except (ValueError, TypeError):
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


def _json_str(source: Optional[dict], key: str) -> str:
    """从 JSON 对象读字符串（兼容后端把数字返回成 number）。"""
    if not isinstance(source, dict):
        return ""
    value = source.get(key)
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return ""
    if isinstance(value, (int, float)):
        return str(value)
    return ""


def fix_nickname_mojibake(raw: str, uid: str) -> str:
    """修复回调 `userInfo.ScreenName` 的双重编码乱码。

    对齐 `login.sh:135-150` 的 `fix_mojibake`：中文昵称被按 latin-1 误解一次后
    得到 `Óû§8847309959` 这类乱码，回转编码可还原。修不好且**不含 CJK** 时
    回退「用户+uid 末 4 位」（**不把乱码写进凭据**）。
    """
    if not raw:
        return raw
    try:
        fixed = raw.encode("latin-1", "strict").decode("utf-8", "strict")
        if fixed and "\ufffd" not in fixed and all(ord(c) >= 32 for c in fixed):
            return fixed
    except (UnicodeDecodeError, UnicodeEncodeError):
        pass
    if not any("\u4e00" <= c <= "\u9fff" for c in raw):
        return f"用户{uid[-4:]}" if uid else raw
    return raw


def parse_callback(raw_url: str) -> Tuple[Optional[dict], str, bool]:
    """解析登录回调 URL → `(info, reason, auth_code_flow)`。

    ⚠️ **回调直接回传 token**，没有 OAuth 的 `?code=`
    （真实形态 `?refreshToken=..&userInfo={..}&userJwt={..}`）。按 `?code=`
    去找会恒判失败 → 回调服务器回 400 → 前端永远「认证中」。

    ⚠️ 但「带 code 的回调」**不是**无效回调：授权页并存两套流程
    （新流程 PKCE 带 `code` / `authCodeInfo`；老流程直传 `refreshToken`）。
    本实现**未实现 PKCE 交换**，故对这种形态单独给出**精确报错**
    （`auth_code_flow=True`），而不是含糊地说「缺少 refreshToken」。

    返回 (info, reason, auth_code_flow)：
    - 成功 → (info, "", False)
    - 失败 → (None, 可读原因, 是否 PKCE 形态)
    """
    query = _parsed_query(raw_url)
    if query is None:
        return None, "回调 URL 无法解析", False

    user_info = _json_param(_first(query, "userInfo"))
    user_jwt = _json_param(_first(query, "userJwt"))

    # 对齐 login.sh:165-166：query 缺 refreshToken 时回退 userJwt.RefreshToken
    refresh_token = _first(query, "refreshToken") or _json_str(user_jwt, "RefreshToken")
    uid = _json_str(user_info, "UserID")
    nickname = fix_nickname_mojibake(_json_str(user_info, "ScreenName"), uid)
    # ⚠️ 回调里字段名是 **TenantID**（不是 EnterpriseID）
    enterprise_id = _json_str(user_info, "TenantID")

    jwt_token = _json_str(user_jwt, "Token")
    # 仅在「无 refreshToken」时才用 userJwt.Token 兜底
    access_token = jwt_token if not refresh_token else ""

    # ── PKCE 形态探测：code / authCode / authCodeInfo(.code) ──
    auth_code_info = _json_param(_first(query, "authCodeInfo"))
    auth_code = (
        _first(query, "code", "authCode")
        or _json_str(auth_code_info, "code")
        or _json_str(auth_code_info, "authCode")
        # authCodeInfo 可能是**纯 code 字符串**（非 JSON），parse 解不出 → 兜底
        or _first(query, "authCodeInfo")
    )

    if not refresh_token and not access_token:
        if auth_code:
            # ⚠️ 关键：这是**合法回调**，只是走了尚未支持的 PKCE 分支。
            # 不能判为「无效」，否则用户看到的错误会指向完全错误的方向。
            return None, (
                "上游返回了 PKCE 授权码（code/authCodeInfo），本实现暂不支持该流程；"
                "请确认 Trae 授权页是否已切换到新流程"), True
        return None, "回调未携带 refreshToken / userJwt.Token / code", False

    info = {
        "refresh_token": refresh_token,
        "access_token": access_token,
        "uid": uid,
        "nickname": nickname,
        "enterprise_id": enterprise_id,
        "auth_code": auth_code,
    }
    return info, "", False


# ── 请求头 ───────────────────────────────────────────

def _solo_headers(cred: dict, product: dict, stream: bool) -> Dict[str, str]:
    """SOLO 对话/模型列表请求头（照抄 `trae.ts:176-208` 的十余个头）。

    ⚠️ 同一个 token 要重复设在 `Authorization` / `X-Cloudide-Token` /
    `X-Ide-Token` 三处，实测缺任一个都可能被上游拒绝。
    `X-Machine-Id` / `X-Device-Id` 只在非空时附加（空值会被上游当异常设备）。
    """
    access = str(cred.get("access_token") or "")
    headers = {
        "Content-Type": "application/json",
        "Accept": "text/event-stream" if stream else "application/json",
        "User-Agent": product["user_agent"],
        "Authorization": f"Cloud-IDE-JWT {access}",
        "X-Cloudide-Token": access,
        "X-Ide-Token": access,
        "X-Uid": str(cred.get("uid") or ""),
        "X-App-Id": product["app_id"],
        "X-App-Version": "default",
        "X-Ide-Version": product["ide_version"],
        "X-Ide-Version-Code": product["ide_version_code"],
        "X-App-Version-Code": product["ide_version_code"],
        "X-Ide-Version-Type": "stable",
        "X-Device-Type": "macos",
        "X-OS-Version": product["os_version"],
        "X-Device-Brand": product["device_brand"],
        "Request-Traffic-Type": "prod",
    }
    machine_id = str(cred.get("machine_id") or "")
    if machine_id:
        headers["X-Machine-Id"] = machine_id
    device_id = str(cred.get("device_id") or "")
    if device_id:
        headers["X-Device-Id"] = device_id
    return headers


def solo_headers(cred: dict, stream: bool = True, domain: str = "") -> Dict[str, str]:
    """对外暴露的 SOLO 请求头（domain 决定国内版/国际版的产品字段）。"""
    return _solo_headers(cred, product_for(domain), stream)


def oauth_headers(domain: str = "") -> Dict[str, str]:
    """ExchangeToken / GetUserInfo 请求头（无签名，仅 UA）。"""
    return {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": product_for(domain)["user_agent"],
    }


# ── 签到设备身份（对齐 trae-mate device_map.rs）─────────

def seeded_stream(seed: str, salt: str, nbytes: int) -> bytes:
    """SHA-256 确定性伪随机流：`SHA256(utf8("salt:seed") ++ counterBE32)` 串联。

    输入 `(seed, salt)` 永远产生相同输出；跨进程/重启稳定，无需持久化派生结果。
    与 `trae.ts:325-345` 的 `seededStream` **逐字节等价**。
    """
    prefix = f"{salt}:{seed}".encode("utf-8")
    out = bytearray()
    counter = 0
    while len(out) < nbytes:
        digest = hashlib.sha256(prefix + counter.to_bytes(4, "big")).digest()
        out.extend(digest[: max(0, nbytes - len(out))])
        counter += 1
    return bytes(out[:nbytes])


def derive_device_id(user_id: str) -> str:
    """由 user_id 派生 **15 位数字** 签到设备号（`X-Device-Id`）。

    每条账号基于其 uid 永远得到同一套设备标识 → 多账号签到天然互异，
    规避服务端「每设备每天一次」配额（对齐 `device_map.rs` 的 devid salt）。
    """
    return "".join(str(b % 10) for b in seeded_stream(user_id, "devid", 15))


def derive_market_user_id(user_id: str) -> str:
    """由 user_id 派生 UUID v4 形态的 `X-Market-User-Id`（置 version/variant 位）。"""
    bs = bytearray(seeded_stream(user_id, "market", 16))
    bs[6] = (bs[6] & 0x0F) | 0x40
    bs[8] = (bs[8] & 0x3F) | 0x80
    hexs = bs.hex()
    return (f"{hexs[0:8]}-{hexs[8:12]}-{hexs[12:16]}-"
            f"{hexs[16:20]}-{hexs[20:32]}")


def derive_session_id(user_id: str) -> str:
    """由 user_id 派生 64 hex 的 `Vscode-Sessionid`（32 字节）。"""
    return seeded_stream(user_id, "sess", 32).hex()


def derive_checkin_headers(user_id: str) -> Dict[str, str]:
    """签到设备身份三件套（**不依赖凭据**，只由 uid 决定）。

    与旧实现的关键差异：旧版由 `credential.device_id` 派生（32 hex），
    实测签到成功用的是**基于 user_id 确定性派生的 15 位数字**。
    """
    return {
        "X-Device-Id": derive_device_id(user_id),
        "X-Market-User-Id": derive_market_user_id(user_id),
        "Vscode-Sessionid": derive_session_id(user_id),
    }


def checkin_headers(token: str, user_id: str, ide_version: str = "",
                    domain: str = "") -> Dict[str, str]:
    """签到/余额专用**完整**客户端请求头（对齐 trae-mate 的 `build_headers`）。

    ⚠️ 与 `solo_headers` 是两套完全不同的头：签到实测**必须**用这一套约 20 个头
    （早期实现只有 6 个精简头，签到一直失败）。`X-Request-Id` / `X-Tt-Trace-Id`
    **每请求刷新**（非确定性），设备身份三件套则**由 uid 确定性派生**。
    """
    product = product_for(domain)
    headers = {
        "Content-Type": "application/json",
        "Accept": "*/*",
        "Accept-Encoding": "gzip, deflate",
        "Accept-Language": "zh-CN",
        "User-Agent": "VSCode 1.107.1 (TRAE SOLO CN)",
        "Authorization": f"Cloud-IDE-JWT {token}",
        "X-Market-Client-Id": "VSCode 1.107.1",
        "X-User-Region": "CN",
        "X-Lgw-Req-Sdk-Type": "3",
        "Package-Type": "stable_cn",
        "X-Lscbd-Aid": "787976",
        "X-Lscbd-Platform": "windows",
        "App-Version": ide_version or product["ide_version"],
        "X-Tt-Trace-Id": f"00-{secrets.token_hex(8)}-01",
        "X-Request-Id": str(uuid.uuid4()),
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "no-cors",
        "Sec-Fetch-Site": "none",
    }
    headers.update(derive_checkin_headers(user_id))
    return headers


# ── ExchangeToken / GetUserInfo ──────────────────────

def _read_str(source: dict, *keys: str) -> str:
    for k in keys:
        v = source.get(k)
        if isinstance(v, str) and v:
            return v
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            return str(v)
    return ""


def _read_num(source: dict, *keys: str) -> float:
    for k in keys:
        v = source.get(k)
        if isinstance(v, bool):
            continue
        if isinstance(v, (int, float)):
            return float(v)
        if isinstance(v, str):
            try:
                return float(v.strip())
            except ValueError:
                continue
    return 0.0


def exchange_body(client_id: str, refresh_token: str) -> dict:
    """ExchangeToken / 续期**共用**的请求体（照抄 `trae-oauth.ts:375-380`）。

    `ClientSecret` 恒为 `-`、`UserID` 恒为空串 —— 这两个占位值不可省
    （缺字段会被上游拒），也不要「优化」成真实值。
    """
    return {
        "ClientID": client_id,
        "RefreshToken": refresh_token,
        "ClientSecret": "-",
        "UserID": "",
    }


def parse_exchange_response(body) -> Optional[dict]:
    """解析 ExchangeToken 响应（Go 结构 `{Result:{Token,…}}`，兼容小写）。

    `Token` 缺失即返回 None（**不能把错误响应当 token 存**）。
    """
    if not isinstance(body, dict):
        return None
    result = body.get("Result") if isinstance(body.get("Result"), dict) else body.get("result")
    if not isinstance(result, dict):
        return None
    access_token = _read_str(result, "Token", "token", "accessToken")
    if not access_token:
        return None
    return {
        "access_token": access_token,
        "refresh_token": _read_str(result, "RefreshToken", "refreshToken"),
        "token_expire_at": _read_num(result, "TokenExpireAt", "tokenExpireAt"),
        "token_expire_duration": _read_num(result, "TokenExpireDuration", "tokenExpireDuration"),
        "refresh_expire_at": _read_num(result, "RefreshExpireAt", "refreshExpireAt"),
    }


def parse_user_info_response(body) -> Optional[dict]:
    """解析 GetUserInfo 响应（Go 结构 `{Result:{UserID,ScreenName,EnterpriseID}}`）。"""
    if not isinstance(body, dict):
        return None
    result = body.get("Result") if isinstance(body.get("Result"), dict) else body.get("result")
    if not isinstance(result, dict):
        return None
    uid = _read_str(result, "UserID", "userId", "uid")
    if not uid:
        return None
    return {
        "uid": uid,
        "screen_name": _read_str(result, "ScreenName", "screenName") or uid,
        "enterprise_id": _read_str(result, "EnterpriseID", "enterpriseId"),
    }


def read_jwt_exp_ms(token: str) -> Optional[int]:
    """从 JWT 的 `exp` 读毫秒时间戳（无签名校验，只读载荷）。解析不出返回 None。"""
    import base64
    parts = str(token or "").split(".")
    if len(parts) < 2:
        return None
    payload = parts[1]
    try:
        padded = payload + "=" * (-len(payload) % 4)
        data = json.loads(base64.urlsafe_b64decode(padded).decode("utf-8", "replace"))
    except Exception:
        return None
    exp = data.get("exp") if isinstance(data, dict) else None
    if isinstance(exp, bool):
        return None
    if isinstance(exp, (int, float)) and exp > 0:
        return int(exp * 1000) if exp < 1e12 else int(exp)
    return None


def resolve_expires_at_ms(exchange: dict, access_token: str = "",
                          now_ms: Optional[int] = None) -> int:
    """归一化过期毫秒时间戳（对齐 `buildTraeCredential` 的三级回退 + JWT 兜底）。

    1. `tokenExpireAt`（>1e12 视为毫秒，否则视为秒）；
    2. `tokenExpireDuration`（相对秒数，以当前时刻为基准）；
    3. `access_token` 的 JWT exp；
    4. 都拿不到 → 0（调用方按「未过期」处理，不编造过期时间）。
    """
    now = int(now_ms if now_ms is not None else time.time() * 1000)
    expire_at = float(exchange.get("token_expire_at") or 0)
    if expire_at > 1e12:
        return int(expire_at)
    if expire_at > 0:
        return int(expire_at * 1000)
    duration = float(exchange.get("token_expire_duration") or 0)
    if duration > 0:
        return now + int(duration * 1000)
    jwt_exp = read_jwt_exp_ms(access_token or str(exchange.get("access_token") or ""))
    return int(jwt_exp) if jwt_exp else 0


def build_credential(exchange: dict, user_info: dict, machine_id: str,
                     device_id: str, domain: str = "",
                     now_ms: Optional[int] = None) -> dict:
    """组装可持久化的凭据（身份字段由调用方传入 —— 它们不在响应里）。

    ⚠️ `machine_id` / `device_id` 必须随凭据持久化且**永不变**。
    """
    access_token = str(exchange.get("access_token") or "")
    expires_at = resolve_expires_at_ms(exchange, access_token, now_ms)
    return {
        "access_token": access_token,
        "refresh_token": str(exchange.get("refresh_token") or ""),
        "expires_at": str(expires_at) if expires_at else "",
        "expires_in": (max(60, int((expires_at - now_ms) / 1000))
                       if expires_at and now_ms else 3600),
        "uid": str(user_info.get("uid") or ""),
        "nickname": str(user_info.get("nickname") or user_info.get("screen_name") or ""),
        "enterprise_id": str(user_info.get("enterprise_id") or ""),
        "machine_id": machine_id,
        "device_id": device_id,
        "domain": str(domain or ""),
    }


def apply_refresh(previous: dict, exchange: dict,
                  now_ms: Optional[int] = None) -> dict:
    """续期结果合并进旧凭据（**身份字段一律不动**）。

    `refresh_token` 会被轮换，但响应可能不带新值 —— 此时**沿用旧的**，
    不能覆盖成空串（否则下一次续期必然失败）。
    """
    merged = dict(previous or {})
    new_refresh = str(exchange.get("refresh_token") or "")
    access_token = str(exchange.get("access_token") or "")
    expires_at = resolve_expires_at_ms(exchange, access_token, now_ms)
    merged.update({
        "access_token": access_token,
        "refresh_token": new_refresh or str((previous or {}).get("refresh_token") or ""),
        "expires_at": str(expires_at) if expires_at else str((previous or {}).get("expires_at") or ""),
    })
    if expires_at and now_ms:
        merged["expires_in"] = max(60, int((expires_at - now_ms) / 1000))
    return merged


async def exchange_refresh_token(refresh_token: str, domain: str = "",
                                 timeout: float = 30.0) -> Tuple[Optional[dict], str]:
    """ExchangeToken：refreshToken → 新 access（并**轮换** refreshToken）。

    返回 (exchange_dict, error)。**永不抛异常**（网络/解析异常转成 error 文本）。
    """
    product = product_for(domain)
    plain = str(refresh_token or "")
    if not plain:
        return None, "缺少 refresh_token（请重新登录该账号）"
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            r = await client.post(
                f"{product['oauth_host']}{TRAE_EXCHANGE_PATH}",
                headers=oauth_headers(domain),
                json=exchange_body(product["client_id"], plain))
    except Exception as e:
        return None, f"ExchangeToken 网络失败：{type(e).__name__}"
    # 先取 text 再解析：凭据失效时网关会返回 **HTML 错误页**，
    # 直接 .json() 抛出的 "Unexpected token '<'" 对用户毫无意义
    text = r.text or ""
    try:
        parsed = r.json()
    except Exception:
        parsed = None
    exchange = parse_exchange_response(parsed)
    if exchange is None:
        return None, f"ExchangeToken 失败（HTTP {r.status_code}）：{text[:200]}"
    return exchange, ""


async def fetch_user_info(access_token: str, domain: str = "",
                          timeout: float = 20.0) -> Tuple[Optional[dict], str]:
    """GetUserInfo 补 uid/nickname/enterprise_id（**失败不阻塞登录**）。

    对齐 `trae-oauth.ts:405-430`：拿不到就用回调里的 userInfo 兜底。
    """
    product = product_for(domain)
    headers = oauth_headers(domain)
    headers["X-Cloudide-Token"] = access_token
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            r = await client.post(
                f"{product['oauth_host']}{TRAE_USER_INFO_PATH}",
                headers=headers,
                json={"ReqSource": "IDE", "IDEVersion": product["ide_version"]})
    except Exception as e:
        return None, f"GetUserInfo 网络失败：{type(e).__name__}"
    if not r.is_success:
        return None, f"GetUserInfo HTTP {r.status_code}"
    try:
        info = parse_user_info_response(r.json())
    except Exception:
        return None, "GetUserInfo 响应不是 JSON"
    if info is None:
        return None, "GetUserInfo 响应缺少 UserID"
    return info, ""


# ── OpenAI → SOLO 请求体转换 ─────────────────────────

def _normalize_tools(body: dict) -> None:
    """`tools[].function.parameters` 序列化为 **JSON 字符串**。

    ⚠️ SOLO 上游要求 parameters 是 string（OpenAI 标准是 object）。
    这一步只对**转换时已存在**的 tools 生效 —— 故 tools 必须放进源 OpenAI
    对象里再交给 `transform_to_solo_body`，转换后补 `body["tools"]` 会保持
    object 形态发给上游被拒（真实缺陷，错误信息不会指向这里）。
    无 `function` 的条目直接剔除（上游 `FunctionCall.Name` 必填）。
    """
    raw = body.get("tools")
    if not isinstance(raw, list) or not raw:
        return
    out = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        fn = item.get("function")
        if not isinstance(fn, dict):
            continue
        params = fn.get("parameters")
        if isinstance(params, (dict, list)):
            fn["parameters"] = json.dumps(params, ensure_ascii=False)
        out.append(item)
    if out:
        body["tools"] = out
    else:
        body.pop("tools", None)


def _normalize_tool_choice(body: dict) -> None:
    """`tool_choice` 归一化（对齐 Go `normalizeToolChoice`）。

    - `"none"` / `{type:"none"}` → 删 `tool_choice` **并删 `tools`**；
    - `{type:"auto"/"required"}` → 字符串；
    - `{type:"function",function:{name}}` → 字符串 name（无名则退 `auto`）；
    - 其它形态 → 删（不猜）。
    """
    if "tool_choice" not in body:
        return
    tc = body.get("tool_choice")

    def _suppress() -> None:
        body.pop("tools", None)
        body.pop("functions", None)

    if isinstance(tc, str):
        if tc.strip().lower() == "none":
            body.pop("tool_choice", None)
            _suppress()
        return
    if isinstance(tc, dict):
        typ = str(tc.get("type") or "").strip().lower()
        if typ == "none":
            body.pop("tool_choice", None)
            _suppress()
        elif typ in ("auto", "required"):
            body["tool_choice"] = typ
        elif typ == "function":
            fn = tc.get("function") if isinstance(tc.get("function"), dict) else {}
            name = str(fn.get("name") or tc.get("name") or "").strip()
            body["tool_choice"] = name or "auto"
        else:
            body.pop("tool_choice", None)
        return
    body.pop("tool_choice", None)


def _transform_message(msg: dict) -> dict:
    """单条消息转换（assistant 的 tool_calls 字段名 + content 形态）。"""
    result = dict(msg)
    if result.get("role") == "assistant":
        tcs = result.get("tool_calls")
        if isinstance(tcs, list):
            kept = []
            for tc in tcs:
                if not isinstance(tc, dict):
                    continue
                call = dict(tc)
                # function → function_call（SOLO 字段名）
                if isinstance(call.get("function"), dict):
                    call["function_call"] = call.pop("function")
                fc = call.get("function_call")
                # 无 name 的 tool_call 必须剔除（上游 FunctionCall.Name 必填）
                if not isinstance(fc, dict) or not str(fc.get("name") or "").strip():
                    continue
                kept.append(call)
            if kept:
                result["tool_calls"] = kept
            else:
                result.pop("tool_calls", None)
    content = result.get("content")
    # 字符串 → [{type:"text",text:...}]；已是数组则**原样透传**（多模态图片
    # 形态 `{type:'image_url',image_url:{url}}` 实测直发即被接受）
    if isinstance(content, str):
        result["content"] = [{"type": "text", "text": content}]
    return result


def transform_to_solo_body(openai_body: dict, model_mapping: str = "",
                           channel: str = "") -> dict:
    """OpenAI 请求体 → SOLO 请求体（照抄 `trae.ts:1342-1374` 的全部改写规则）。

    七条规则（缺一条就会以上游 `4001` 或静默丢内容暴露）：

    1. `stream` **强制 true**（上游只支持 SSE；非流式由适配器聚合同一流）；
    2. 注入 `function`（聊天通道）。⚠️ **同一模型只在列出它的通道里可调用**，
       故必须传入该模型所属通道，缺省才回退 `solo_work_lite`；
    3. `model` **同时**写入 `config_name` 与 `model` 两个字段；内部名后缀
       `__dev` / `__max` 需去除（那是明细后缀，不是模型名）；
    4. `messages[].content` 字符串 → `[{type:"text",text:...}]`；
    5. assistant 的 `tool_calls[].function` → **`function_call`**，
       无 `name` 的条目剔除；
    6. `tools[].function.parameters`（object）→ **JSON 字符串**；
    7. `tool_choice` 归一化（`none` 连 tools 一起删）。
    """
    body = dict(openai_body or {})
    body["stream"] = True
    body["function"] = channel or TRAE_DEFAULT_FUNCTION

    msgs = body.get("messages")
    if isinstance(msgs, list):
        body["messages"] = [_transform_message(m) if isinstance(m, dict) else m
                            for m in msgs]

    model = body.get("model") if isinstance(body.get("model"), str) else ""
    base_model = model.split("__")[0] if "__" in model else model
    config_name = model_mapping or base_model or TRAE_DEFAULT_MODEL
    body["config_name"] = config_name
    body["model"] = config_name

    _normalize_tool_choice(body)
    _normalize_tools(body)
    return body


# ── 历史裁剪（不切断工具配对）────────────────────────

def _wire_size(message) -> int:
    """一条 wire 消息的字符体量（按 JSON 序列化长度估算）。"""
    try:
        return len(json.dumps(message, ensure_ascii=False))
    except (TypeError, ValueError):
        return 0


def trim_history(messages: list,
                 max_chars: int = TRAE_MAX_HISTORY_CHARS) -> list:
    """把 wire 消息裁剪到字符预算内（上游超限会**静默断流**）。

    三条不可违反的约束：
    1. 从最早的**非 system** 消息开始丢 —— 最近历史（尤其本轮工具结果）
       必须保住，丢尾部比丢开头更糟；
    2. **以「轮」为单位裁剪，绝不切断 tool_call / tool 配对** —— 带
       `tool_calls` 的 assistant 必须连同其后连续的 `role:"tool"` 一起丢，
       丢一半会被上游 400 拒绝整个请求；
    3. **system 消息永不裁剪**（不假设它都在开头，逐条判断而非下标切片）。

    裁剪后仍超限（如单条 system 就超预算）时**不再继续丢**：继续丢只会把
    请求变成空内容，同样失败且更难诊断，交由上游定夺。
    """
    if not isinstance(messages, list) or not messages:
        return messages or []
    total = sum(_wire_size(m) for m in messages)
    if total <= max_chars:
        return list(messages)

    drop = set()
    index = 0
    while index < len(messages) and total > max_chars:
        msg = messages[index] if isinstance(messages[index], dict) else {}
        if msg.get("role") == "system":
            index += 1
            continue
        round_end = index + 1
        tcs = msg.get("tool_calls")
        if isinstance(tcs, list) and tcs:
            while (round_end < len(messages)
                   and isinstance(messages[round_end], dict)
                   and messages[round_end].get("role") == "tool"):
                round_end += 1
        for cursor in range(index, round_end):
            drop.add(cursor)
            total -= _wire_size(messages[cursor])
        index = round_end
    return [m for pos, m in enumerate(messages) if pos not in drop]


def clamp_max_tokens(value, limit: int = TRAE_MAX_COMPLETION_TOKENS):
    """把输出额度收敛到安全上限（只收敛正整数，其它原样返回、不编造数值）。"""
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return value
    if value <= 0 or limit <= 0:
        return value
    return int(min(value, limit))


# ── SOLO → OpenAI SSE 解析 ───────────────────────────

SOLO_EVENT_TYPES = ("metadata", "timing_cost", "output", "extra_info",
                    "token_usage", "done", "error")


def normalize_tool_calls(calls) -> List[dict]:
    """归一化 SOLO tool_calls：`function_call` → `function`，清理 SOLO 专属字段。

    `namespace` / `partial_arguments` 是 SOLO 内部字段，下游 OpenAI 客户端
    不认识 —— 留着会让 tool 参数解析器读到多余键（对齐 `trae.ts:1540`）。
    """
    out = []
    for call in calls or []:
        if not isinstance(call, dict):
            continue
        item = dict(call)
        if isinstance(item.get("function_call"), dict):
            item["function"] = dict(item.pop("function_call"))
        fn = item.get("function")
        if isinstance(fn, dict):
            fn.pop("namespace", None)
            fn.pop("partial_arguments", None)
        out.append(item)
    return out


def parse_sse_event(event_name: str, data_line: str) -> Optional[dict]:
    """解析一条 SOLO 事件 → dict（对齐 Go `ParseSOLOLine`）。

    返回 `{"event": 事件名, ...}`；data 非法 JSON 时**只返回事件名**（不抛）。
    事件语义：
    - `output`：正文 `response`、思考 `reasoning_content`、工具 `tool_calls`
    - `token_usage`：`prompt_tokens` / `completion_tokens` / `reasoning_tokens`
    - `done`：`finish_reason`
    - `error`：`code` / `message`
    """
    event = str(event_name or "").strip()
    if not data_line:
        return {"event": event}
    try:
        raw = json.loads(data_line)
    except (ValueError, TypeError):
        return {"event": event}
    if not isinstance(raw, dict):
        return {"event": event}

    ev = {"event": event}
    if event == "output":
        if isinstance(raw.get("response"), str):
            ev["response"] = raw["response"]
        if isinstance(raw.get("reasoning_content"), str):
            ev["reasoning_content"] = raw["reasoning_content"]
        if isinstance(raw.get("tool_calls"), list):
            ev["tool_calls"] = normalize_tool_calls(raw["tool_calls"])
    elif event == "token_usage":
        ev["usage"] = raw
    elif event == "done":
        if isinstance(raw.get("finish_reason"), str):
            ev["finish_reason"] = raw["finish_reason"]
    elif event == "error":
        code = raw.get("code")
        if isinstance(code, bool):
            code = None
        if isinstance(code, (int, float)):
            ev["error_code"] = int(code)
        elif isinstance(code, str) and code.strip().lstrip("-").isdigit():
            ev["error_code"] = int(code.strip())
        if isinstance(raw.get("message"), str):
            ev["error_message"] = raw["message"]
    return ev


def aggregate_sse(lines) -> dict:
    """聚合完整 SOLO SSE 行序列 → 一次性的 OpenAI 结果（非流式场景用）。

    返回 `{content, reasoning_content, tool_calls, finish_reason, usage, error}`。
    `error` 非 None 时表示流内 `event:error`（业务错误，**不是**传输失败）。
    """
    result = {
        "content": "", "reasoning_content": "", "tool_calls": [],
        "finish_reason": "stop", "usage": None, "error": None,
        "saw_event": False,
    }
    st = None  # {"event":..., "data":...}
    for raw_line in lines or []:
        line = str(raw_line).rstrip()
        if not line.strip():
            if st is not None:
                ev = parse_sse_event(st["event"], st["data"])
                st = None
                if ev is None:
                    continue
                result["saw_event"] = True
                name = ev.get("event")
                if name == "output":
                    if ev.get("response"):
                        result["content"] += ev["response"]
                    if ev.get("reasoning_content"):
                        result["reasoning_content"] += ev["reasoning_content"]
                    if ev.get("tool_calls"):
                        result["tool_calls"].extend(ev["tool_calls"])
                elif name == "token_usage":
                    result["usage"] = ev.get("usage")
                elif name == "done":
                    if ev.get("finish_reason"):
                        result["finish_reason"] = ev["finish_reason"]
                elif name == "error":
                    result["error"] = {
                        "code": ev.get("error_code", -1),
                        "message": ev.get("error_message", "unknown error"),
                    }
            continue
        stripped = line.strip()
        if stripped.startswith("event:"):
            new_event = stripped[len("event:"):].strip()
            if st is not None:
                st["event"] = new_event
            else:
                st = {"event": new_event, "data": ""}
            continue
        if stripped.startswith("data:"):
            data = stripped[len("data:"):]
            if st is not None:
                st["data"] += data
            continue
        # 注释行（":"）/ 其它：忽略
    if not result["tool_calls"]:
        result.pop("tool_calls")
    return result


def normalize_usage(raw) -> dict:
    """SOLO `token_usage` → OpenAI usage（含 reasoning_tokens 明细）。

    字段缺失时填 0（OpenAI 客户端要求三个键都在）；`reasoning_tokens`
    只在 >0 时附上明细（0 是冗余信息）。
    """
    raw = raw if isinstance(raw, dict) else {}
    prompt = raw.get("prompt_tokens")
    completion = raw.get("completion_tokens")
    prompt = int(prompt) if isinstance(prompt, (int, float)) and not isinstance(prompt, bool) else 0
    completion = int(completion) if isinstance(completion, (int, float)) and not isinstance(completion, bool) else 0
    total = raw.get("total_tokens")
    total = int(total) if isinstance(total, (int, float)) and not isinstance(total, bool) else prompt + completion
    usage = {"prompt_tokens": prompt, "completion_tokens": completion,
             "total_tokens": total}
    reasoning = raw.get("reasoning_tokens")
    if isinstance(reasoning, (int, float)) and not isinstance(reasoning, bool) and reasoning > 0:
        usage["completion_tokens_details"] = {"reasoning_tokens": int(reasoning)}
    return usage


def solo_error_message(code: int, message: str, model: str = "") -> str:
    """组合流内 `event:error` 的文案（对 4001 追加可操作提示）。

    ⚠️ `4001` 的上游原文是 *"We're sorry, the param is invalid..."* —— 它与
    「提示词/参数格式有误」**毫无关系**。实测该码只由**发错通道**或
    「仅可见但不可调用」的自定义模型触发，原文会把排查方向完全带偏。
    其余错误码保持原文，**不做无依据的解释**。

    ⚠️ 不列具体模型名：Jet-Hub 曾把某一刻的快照写成判据，结果复测时名单
    （`deepseek-v4-flash` / `glm-5.3-flash` …）已全部失效，差点误删可用模型。
    """
    base = f"trae: {message} (code={code})"
    if code == TRAE_CODE_CHANNEL_ERROR:
        return (f"{base} —— 模型「{model}」不被上游接受：通常是**发错了通道**"
                f"（该模型只在列出它的 function 里可调用），或它是「仅可见但"
                f"不可调用」的自定义模型。请改用模型列表中其它模型。")
    if code == TRAE_CODE_QUOTA_EXCEEDED:
        return f"{base} —— ide_credits 已耗尽，需等每日额度重置或次日签到"
    if code == TRAE_CODE_PLAN_LIMIT:
        return f"{base} —— 套餐权益不足（1005）"
    if code == TRAE_CODE_RATE_LIMITED:
        return f"{base} —— 频率超限（4011），稍后再试"
    return base


# ── 模型目录 ─────────────────────────────────────────

def _bool_field(source: dict, *keys: str) -> Optional[bool]:
    """读布尔字段：只有**明确**的布尔语义才返回值（缺失 → None）。

    「上游没说」（None）与「上游说 false」是两回事 —— 调用方据此决定是否过滤。
    不接受任意 truthy 值（空字符串是 falsy，把它当 false 是没有依据的断言）。
    """
    for k in keys:
        if k not in source:
            continue
        v = source[k]
        if isinstance(v, bool):
            return v
        if v == 1 or v == "true":
            return True
        if v == 0 or v == "false":
            return False
    return None


def _parse_json_object(raw) -> Optional[dict]:
    """宽松解析 JSON 对象字符串（数组/标量/非法 JSON → None）。"""
    if not isinstance(raw, str) or not raw:
        return None
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def read_consumption_rate(entry: dict) -> Optional[float]:
    """解析 `display_contact_config` 里的消耗倍率（**必须二次 parse**）。

    ⚠️ `display_contact_config` 是一个 **JSON 字符串**（不是对象）：
    直接读 `entry["display_contact_config"]["consumption_rate"]` 永远得到 None。

    三条判据（缺一不可）：
    1. `consumption_rate.enable !== false`（上游显式关闭时**不显示**，而非当成 0）；
    2. `data.rate` 是有限非负数（实测是**裸数字** `0.08`，不是字符串 `"x0.08"`）；
    3. ⚠️ **`rate: 0` 是合法值（免费）** —— 用 `> 0` 过滤会恰好漏掉用户最关心的
       免费模型（与 Qoder 的 `price_factor: 0` 同一个坑）。

    解析失败一律返回 None（**不编造倍率**）。
    """
    config = _parse_json_object(entry.get("display_contact_config"))
    if config is None:
        return None
    rate = config.get("consumption_rate")
    if not isinstance(rate, dict):
        return None
    if _bool_field(rate, "enable") is False:
        return None
    data = rate.get("data")
    if not isinstance(data, dict):
        return None
    value = data.get("rate")
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)) and value >= 0:
        return float(value)
    if isinstance(value, str):
        try:
            parsed = float(value.strip())
        except ValueError:
            return None
        return parsed if parsed >= 0 else None
    return None


def read_activity_discount(entry: dict, now_sec: Optional[int] = None) -> Optional[dict]:
    """解析 `activity_discount`，**只在当前确实生效**时返回原价与截止时间。

    ⚠️ 不能只看 `enable: true`：实测 `off_peak` 型条目形如
    `{type:"none", before:0.13, after:0.13, discount:100}` —— `discount:100`
    表示「无折扣」（百分比制），`before == after`。照显会显示 `x0.13→x0.13`，
    让用户以为有活动（与 Qoder 的 `promotion.active === false` 同类）。

    三条判据：1) `enable !== false`；2) `discount_type` 存在且**不是** `"none"`；
    3) `before_consumption_rate` 为正且**严格大于** after。
    已过期的活动（`end_at <= now`）同样视为不存在。
    """
    now = int(now_sec if now_sec is not None else time.time())
    config = _parse_json_object(entry.get("display_contact_config"))
    if config is None:
        return None
    discount = config.get("activity_discount")
    if not isinstance(discount, dict):
        return None
    if _bool_field(discount, "enable") is False:
        return None
    data = discount.get("data")
    if not isinstance(data, dict):
        return None
    # `current` 取 `data.current`（对齐 Jet-Hub），缺失时**再扫一层子对象** ——
    # `end_at` 本来就是从子对象里扫的（`limited` / `subsidy` / `off_peak` 各型），
    # `current` 与 `end_at` 同处一层时这个兜底才会命中；扫不到则行为与参考实现一致。
    current = data.get("current")
    if not isinstance(current, dict):
        current = next((v for v in data.values()
                        if isinstance(v, dict) and isinstance(v.get("current"), dict)), None)
        current = current.get("current") if isinstance(current, dict) else None
    if not isinstance(current, dict):
        return None
    dtype = str(current.get("discount_type") or "").strip().lower()
    if not dtype or dtype == "none":
        return None
    before = current.get("before_consumption_rate")
    after = current.get("consumption_rate")
    before = float(before) if isinstance(before, (int, float)) and not isinstance(before, bool) else None
    after = float(after) if isinstance(after, (int, float)) and not isinstance(after, bool) else None
    if before is None or before <= 0:
        return None
    if after is not None and before <= after:
        return None
    ends_at = None
    for value in data.values():
        if not isinstance(value, dict):
            continue
        end = value.get("end_at")
        if isinstance(end, (int, float)) and not isinstance(end, bool) and end > 0:
            ends_at = int(end)
            break
    if ends_at is not None and ends_at <= now:
        return None
    out = {"original_rate": before}
    if ends_at is not None:
        out["ends_at"] = ends_at
    return out


def _read_context_window(entry: dict) -> Optional[int]:
    """读 `context_window_tokens.dev`（缺失回退 `max`）。

    ⚠️ 常规会话用 `dev`（条目形如 `{dev:200000, max:1000000}`）；`max` 只在
    开启 Max 模式时可用。无脑采信 `max` 会让调用方以为有 1M 窗口、实际请求被拒。
    """
    raw = entry.get("context_window_tokens")
    if not isinstance(raw, dict):
        return None
    for k in ("dev", "Dev", "max", "Max"):
        v = raw.get(k)
        if isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0:
            return int(v)
    return None


def _read_max_context_window(entry: dict) -> Optional[int]:
    """读 `context_window_tokens.max`（Max 模式专用窗口，通常 1000000）。"""
    raw = entry.get("context_window_tokens")
    if not isinstance(raw, dict):
        return None
    for k in ("max", "Max"):
        v = raw.get(k)
        if isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0:
            return int(v)
    return None


def _read_detail_max_tokens(entry: dict, suffix: str = "__dev") -> Optional[int]:
    """读 `model_detail_list[].max_tokens`（优先取 `model_name` 以 `suffix` 结尾那条）。"""
    raw = entry.get("model_detail_list")
    if not isinstance(raw, list) or not raw:
        return None
    details = [d for d in raw if isinstance(d, dict)]
    if not details:
        return None
    chosen = next((d for d in details
                   if str(d.get("model_name") or "").endswith(suffix)), details[0])
    v = chosen.get("max_tokens")
    if isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0:
        return int(v)
    return None


def _read_reasoning_config(entry: dict) -> Optional[dict]:
    """读 `reasoning_effort_config`（`options` 是**单值**字符串数组）。

    ⚠️ 与 LobsterAI 的 `level` / `openclawLevel` 双字段形态不同：Trae 的
    `options` 里的字符串**既是产品侧档位名、也是发给上游的 wire 值**，
    不需要映射表。一个字段都没有时返回 None（调用方据此不声明档位，
    避免给用户一个发了也没用的选择器）。
    """
    raw = entry.get("reasoning_effort_config")
    if not isinstance(raw, dict):
        return None
    options = []
    raw_options = raw.get("options")
    if isinstance(raw_options, list):
        for item in raw_options:
            if isinstance(item, str) and item.strip():
                options.append(item.strip())
            elif isinstance(item, dict):
                # 防御性兼容：上游若改成对象数组形态，不要解析成空数组
                wire = str(item.get("openclawLevel") or item.get("level") or "").strip()
                if wire:
                    options.append(wire)
    default_level = str(raw.get("default_level") or "").strip()
    support = _bool_field(raw, "support_thinking")
    if not options and not default_level and support is None:
        return None
    out = {"options": options}
    if default_level:
        out["default_level"] = default_level
    if support is not None:
        out["support_thinking"] = support
    return out


def parse_config_entry(entry: dict, channel: str = "") -> Optional[dict]:
    """解析单条 `config_info_list` 条目（两个端点的条目形状一致）。

    ⚠️ 六个标志位**分散在两处**，取错地方会恒得到「未声明」：
    `is_custom_model` / `max_mode` / `multimodal` / `tool_response_multimodal`
    在 `display_config` 里，而 `config_switch` / `is_invisible_to_user`
    在**条目顶层**（Jet-Hub 曾因只读了一处而整批漏判图片能力）。
    """
    if not isinstance(entry, dict):
        return None
    mid = _read_str(entry, "config_name", "ConfigName")
    if not mid:
        return None
    display = entry.get("display_config")
    display = display if isinstance(display, dict) else {}
    name = _read_str(display, "display_name") or mid
    model = {
        "id": mid,
        "name": name,
        "channel": channel,
    }
    # 「上游没说」保持缺省（不填 False）：过滤方只挡明确命中者 —— 缺字段时
    # 填 False 会把「未声明」当成「未启用」，可能连带删掉整批可用模型。
    flag_sources = (
        ("is_custom_model", display, "is_custom_model"),
        ("max_mode", display, "max_mode"),
        ("multimodal", display, "multimodal"),
        ("tool_response_multimodal", display, "tool_response_multimodal"),
        ("is_enabled", entry, "config_switch"),
        ("is_hidden", entry, "is_invisible_to_user"),
    )
    for key, source, source_key in flag_sources:
        value = _bool_field(source, source_key, source_key[0].upper() + source_key[1:])
        if value is not None:
            model[key] = value
    usage = _read_str(entry, "usage", "Usage")
    if usage:
        model["usage"] = usage
    ctx = _read_context_window(entry)
    if ctx:
        model["context_window"] = ctx
    max_ctx = _read_max_context_window(entry)
    if max_ctx:
        model["max_context_window"] = max_ctx
    max_out = _read_detail_max_tokens(entry)
    if max_out:
        model["max_output_tokens"] = max_out
    max_mode_out = _read_detail_max_tokens(entry, "__max")
    if max_mode_out:
        model["max_mode_output_tokens"] = max_mode_out
    reasoning = _read_reasoning_config(entry)
    if reasoning:
        model["reasoning_config"] = reasoning
    rate = read_consumption_rate(entry)
    if rate is not None:
        model["credits_rate"] = rate
    discount = read_activity_discount(entry)
    if discount:
        model["original_credits_rate"] = discount["original_rate"]
        if discount.get("ends_at"):
            model["discount_ends_at"] = discount["ends_at"]
    return model


def parse_model_list(body) -> List[dict]:
    """解析单通道 `get_detail_param` 响应（`config_info_list`，兼容 `data`）。"""
    if not isinstance(body, dict):
        return []
    raw = body.get("config_info_list")
    if not isinstance(raw, list):
        raw = body.get("data")
    if not isinstance(raw, list):
        return []
    out = []
    for item in raw:
        model = parse_config_entry(item, "")
        if model:
            out.append(model)
    return out


def parse_batch_model_list(body) -> List[dict]:
    """解析多通道 `batch_get_detail_param` 响应（**每个 function 一套目录**）。

    ## 合并规则：**后面的覆盖前面的**

    同一个 `config_name` 可能出现在多个 function 中；后面的条目通常带更完整的
    配置（`display_contact_config` / `reasoning_effort_config` /
    `model_detail_list`），直接覆盖保证用到最完整那条。

    ## 三条硬性过滤（缺一不可，不设外部开关）

    1. `usage !== "chat_completion"` 排除 —— batch 端点是**全功能配置表**，
       含 summary / fast_apply / multimodal / system_diagnosis 等非对话用途；
    2. `config_switch === false` 排除（上游已停用）；
    3. `is_invisible_to_user === true` 排除（官方 picker 不展示；使目录与
       官方 Auto Mode 选择器一致）。

    ⚠️ **`is_custom_model` 不在这里过滤**：它与「官方可见」是两个独立维度，
    由适配器的 `list_models` 挡（保留全量目录以便已持久化的模型仍可解析）。
    """
    if not isinstance(body, dict):
        return []
    groups = body.get("function_configs")
    if not isinstance(groups, list):
        return []
    by_id: Dict[str, dict] = {}
    for group in groups:
        if not isinstance(group, dict):
            continue
        channel = _read_str(group, "function", "Function")
        lst = group.get("config_info_list")
        if not isinstance(lst, list):
            continue
        for item in lst:
            model = parse_config_entry(item, channel)
            if model is None:
                continue
            if model.get("usage") and model["usage"] != "chat_completion":
                continue
            if model.get("is_enabled") is False:
                continue
            if model.get("is_hidden") is True:
                continue
            by_id[model["id"]] = model
    return list(by_id.values())


def batch_model_body(functions=None) -> dict:
    """`batch_get_detail_param` 请求体（照抄 `trae-auth.ts:505-522`）。

    ⚠️ 必须传**全部** 22 个 function（真实 CN IDE 的做法）：只传几个聊天通道
    会让非聊天通道的条目在响应里出现位置错乱，甚至被解析器跳过。
    """
    return {
        "functions": list(functions or TRAE_BATCH_FUNCTIONS),
        "agent_type": "",
        "current_config_info": {"config_name": "", "is_custom_model": False},
        "mode_type": 0,
        "access_type": 0,
        "ab_force_vids": "",
        "ab_autotest_advanced_mode": 0,
        "show_custom_model": True,
    }


async def fetch_models(cred: dict, domain: str = "",
                       timeout: float = 30.0) -> List[dict]:
    """拉取远端模型目录（多通道）；失败返回空列表（由调用方回退静态种子）。

    **永不抛异常** —— 模型列表拉取失败不该阻塞推理（与 lobsterai 同语义）。
    """
    if not cred.get("access_token"):
        return []
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            r = await client.post(
                f"{product_for(domain)['agent_host']}{TRAE_BATCH_MODELS_PATH}",
                headers=_solo_headers(cred, product_for(domain), False),
                json=batch_model_body())
        if not r.is_success:
            logger.warning("trae fetch_models HTTP %s", r.status_code)
            return []
        return parse_batch_model_list(r.json())
    except Exception as e:
        logger.warning("trae fetch_models failed: %s", e)
        return []


# ── 额度 ─────────────────────────────────────────────

def parse_ent_usage(body) -> dict:
    """解析 `ide_user_ent_usage` 响应 → `{remaining, packages, total_limit}`。

    `remain = Σ(credits_limit − credits_amount)`（**每个资源包各自求和**，
    不能只读外层 —— 外层没有该字段）。条目缺 `quota` 的跳过（不编造）。
    """
    out = {"remaining": 0.0, "packages": [], "total_limit": 0.0}
    if not isinstance(body, dict):
        return out
    pack_list = body.get("user_entitlement_pack_list")
    if not isinstance(pack_list, list):
        return out
    for item in pack_list:
        if not isinstance(item, dict):
            continue
        base = item.get("entitlement_base_info")
        if not isinstance(base, dict):
            continue
        quota = base.get("quota")
        if not isinstance(quota, dict):
            continue
        limit = quota.get("credits_limit")
        if not isinstance(limit, (int, float)) or isinstance(limit, bool) or limit <= 0:
            continue
        usage = item.get("usage")
        used = 0.0
        if isinstance(usage, dict):
            value = usage.get("credits_amount")
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                used = float(value)
        remaining = float(limit) - used
        name = _read_str(base, "name") or "资源包"
        out["packages"].append({"name": name, "total": float(limit),
                                "used": used, "remaining": remaining})
        out["total_limit"] += float(limit)
        out["remaining"] += remaining
    return out


async def fetch_credits(cred: dict, domain: str = "",
                        timeout: float = 30.0) -> dict:
    """查询积分余额（`ide_user_ent_usage`）。

    body 必须是 `{"require_usage": true, "req_source": 2}` —— **不带
    `require_usage` 拿不到 `usage`，余额会恒等于额度**。
    失败返回空结构（`remaining=0` 且无 packages），由调用方判定「查不到」。
    """
    uid = str(cred.get("uid") or "")
    if not uid or not cred.get("access_token"):
        return {"remaining": 0.0, "packages": [], "total_limit": 0.0,
                "error": "缺少 uid 或 access_token"}
    product = product_for(domain)
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            r = await client.post(
                f"{product['ug_host']}{TRAE_ENT_USAGE_PATH}",
                headers=checkin_headers(str(cred.get("access_token") or ""), uid,
                                        product["ide_version"], domain),
                json={"require_usage": True, "req_source": 2})
    except Exception as e:
        return {"remaining": 0.0, "packages": [], "total_limit": 0.0,
                "error": f"网络失败：{type(e).__name__}"}
    if not r.is_success:
        return {"remaining": 0.0, "packages": [], "total_limit": 0.0,
                "error": f"HTTP {r.status_code}"}
    try:
        parsed = parse_ent_usage(r.json())
    except Exception:
        return {"remaining": 0.0, "packages": [], "total_limit": 0.0,
                "error": "响应不是 JSON"}
    parsed["error"] = ""
    return parsed


# ── 签到（两步：status 预检 → claim → 补查 status）─────

def read_claim_code(body) -> int:
    """读业务码（兼容数字与字符串形态）。

    ⚠️ 不能只判 `isinstance(x, int)`：部分网关把 `code` 以字符串 `"9074"`
    下发，只认数字会把它误判成成功（缺省 0）。
    """
    if not isinstance(body, dict) or "code" not in body:
        return 0
    raw = body.get("code")
    if isinstance(raw, bool):
        return -1
    if isinstance(raw, (int, float)):
        return int(raw)
    if isinstance(raw, str) and raw.strip().lstrip("-").isdigit():
        return int(raw.strip())
    return -1


def classify_checkin_error(http_status: int, code: Optional[int]) -> Tuple[str, int]:
    """签到错误分类 → `(type, cooldown_secs)`（对齐 trae-mate `cooldown.rs`）。

    `-1` 表示**永久冷却**（会话失效，只能重新登录）。
    """
    if http_status == 200 and code == TRAE_CODE_PLAN_LIMIT:
        return "PlanLimit", TRAE_COOLDOWN_PLAN_LIMIT
    if http_status == 429:
        return "SoftRate", TRAE_COOLDOWN_SOFT_RATE
    if http_status == 401:
        return "SessionDead", -1
    if http_status == 404:
        return "NotFound", TRAE_COOLDOWN_SOFT_RATE
    if 500 <= http_status < 600:
        return "Server", TRAE_COOLDOWN_SERVER
    if 400 <= http_status < 500:
        return "Client", TRAE_COOLDOWN_SERVER
    if code is not None and code != 0:
        return "BusinessError", TRAE_COOLDOWN_BIZ_ERROR
    return "Unknown", 0


def parse_checkin_status(body) -> dict:
    """解析 `checkin_credits/status` → `{active, checked_in, credits, streak_days}`。"""
    body = body if isinstance(body, dict) else {}
    credits = body.get("credits")
    streak = body.get("streak_days")
    return {
        "active": body.get("enable") is True,
        "checked_in": body.get("checked_in") is True,
        "credits": int(credits) if isinstance(credits, (int, float))
        and not isinstance(credits, bool) else 0,
        "streak_days": int(streak) if isinstance(streak, (int, float))
        and not isinstance(streak, bool) else 0,
        "code": read_claim_code(body),
    }


async def _checkin_post(path: str, cred: dict, payload: dict, domain: str,
                        timeout: float = 30.0) -> Tuple[int, Optional[dict], str]:
    """签到类 POST 的公共实现（**完整客户端头** + 先取 text 再解析）。

    401/403 时上游可能返回 HTML 而非 JSON，故先取 text —— 直接 `.json()`
    会抛异常并丢失「凭据失效」这个关键判据。
    """
    product = product_for(domain)
    uid = str(cred.get("uid") or "")
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            r = await client.post(
                f"{product['ug_host']}{path}",
                headers=checkin_headers(str(cred.get("access_token") or ""), uid,
                                        product["ide_version"], domain),
                json=payload)
    except Exception as e:
        return 0, None, f"{type(e).__name__}: {e}"
    try:
        parsed = r.json()
    except Exception:
        parsed = None
    return r.status_code, parsed, r.text or ""


async def fetch_checkin_status(cred: dict, domain: str = "",
                               timeout: float = 30.0) -> Tuple[Optional[dict], str, int]:
    """签到状态查询 → `(status_dict, error, http_status)`。

    body 是 `{}`（**不是** `{"req_source":2}`）。
    `error` 非空表示查询失败（网络/非 2xx/非 JSON），调用方据此**不能**判
    「已签到」也**不能**判「活动未开启」—— 后者会把一次 500 报成正常状态。
    """
    code, body, text = await _checkin_post(TRAE_CHECKIN_STATUS_PATH, cred, {},
                                           domain, timeout)
    if code == 0:
        return None, f"状态查询网络失败：{text[:200]}", 0
    if not 200 <= code < 300:
        return None, f"状态查询 HTTP {code}：{str(text)[:200]}", code
    if body is None:
        return None, f"状态响应不是 JSON（HTTP {code}）：{str(text)[:200]}", code
    parsed = parse_checkin_status(body)
    if parsed["code"] != 0:
        return None, f"状态查询失败（code={parsed['code']}）", code
    return parsed, "", code


async def claim_checkin(cred: dict, domain: str = "",
                        timeout: float = 30.0) -> dict:
    """签到两步流程：**先 status 预检 → 再 claim → 成功后补查 status**。

    ## 为什么必须先查 status（不可省略）

    `checkin_credits/claim` 对「今天已签到」是**幂等**的：实测重复领取同样返回
    `{code:0, message:"success"}`，与真正成功**无法区分**。不预检就会把已签到的
    账号报成「领取成功」（用户报障）。

    ## 为什么 claim 后要补查 status

    ⚠️ claim 响应**不含积分数** —— 它的完整响应就是
    `{"code":0,"message":"success"}`。早期实现读 claim 的 `credits` 字段，
    该字段根本不存在，于是恒为 0，界面显示「领取成功 +0 积分」（用户报障），
    而 IDE 里明明写着 150。真实数值只在 **status 端点**的 `credits` 里。
    补查失败时 `credit=0` 但**仍是 claimed**（不因补查失败把成功判成失败）。

    ## 9074 的处理

    `9074`（"too many users, retry later"）归为**业务错误**（300s 冷却），
    **不换设备号重试**：设备身份已由 uid 确定性派生、每账号独立，
    「换个派生 id 立刻成功」的旧前提不成立（旧实现那条链路已废弃）。

    返回 dict：`{kind, credit, streak_days, message, error_type, cooldown_secs}`。
    `kind` ∈ claimed / already_claimed / inactive / failed。
    **永不抛异常。**
    """
    out = {"kind": "failed", "credit": 0, "streak_days": 0, "message": "",
           "error_type": "", "cooldown_secs": 0}

    status, err, http_status = await fetch_checkin_status(cred, domain, timeout)
    if status is None:
        out.update(kind="failed", message="签到状态查询失败", error=err,
                   error_type="Unknown", cooldown_secs=0)
        return out
    if status["active"] is False:
        out.update(kind="inactive", message="签到活动未开启",
                   streak_days=status["streak_days"])
        return out
    if status["checked_in"] is True:
        out.update(kind="already_claimed", message="今天已签到",
                   credit=status["credits"], streak_days=status["streak_days"])
        return out

    code, body, text = await _checkin_post(TRAE_CHECKIN_CLAIM_PATH, cred, {},
                                           domain, timeout)
    if code == 0:
        out.update(kind="failed", message="签到请求网络失败", error=text[:200])
        return out
    body = body if isinstance(body, dict) else {}
    biz = read_claim_code(body)
    msg = _read_str(body, "message", "msg")

    if biz == TRAE_CODE_CHECKIN_BUSY:
        out.update(kind="failed", message=msg or "签到人数过多，请稍后再试",
                   error_type="BusinessError", cooldown_secs=TRAE_COOLDOWN_BIZ_ERROR)
        out["upstream_code"] = TRAE_CODE_CHECKIN_BUSY
        return out
    if biz != 0:
        etype, cooldown = classify_checkin_error(code, biz)
        out.update(kind="failed", message=msg or f"签到失败（code={biz}）",
                   error_type=etype, cooldown_secs=cooldown)
        out["upstream_code"] = biz
        return out

    # 领取成功 → **补查状态**取真实积分数（claim 响应里没有）
    after, _err2, _c2 = await fetch_checkin_status(cred, domain, timeout)
    out.update(kind="claimed", message="签到成功",
               credit=(after or {}).get("credits", 0),
               streak_days=(after or {}).get("streak_days", 0))
    return out


# ── JSON 安全读取（供适配器/集成方复用）────────────────

def read_string(source, key: str) -> str:
    """从 dict 安全读字符串（兼容后端把数字返回成 number）。"""
    if not isinstance(source, dict):
        return ""
    return _read_str(source, key)


def read_number(source, key: str) -> Optional[float]:
    """从 dict 安全读数字（兼容字符串形态的数字）；取不到返回 None。"""
    if not isinstance(source, dict) or key not in source:
        return None
    value = source[key]
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return None
    return None


def read_boolean(source, key: str) -> Optional[bool]:
    """从 dict 安全读布尔（缺失 → None，与「false」严格区分）。"""
    if not isinstance(source, dict):
        return None
    return _bool_field(source, key)


# ── 错误归类（供适配器/路由复用）──────────────────────

def classify_error(http_status: int, body: str = "") -> str:
    """按 HTTP 状态码 + 响应体判定错误类别（移植 `trae-errors.ts` 的 `Classify`）。

    ⚠️ **`quota-exceeded`（4008）必须排在 `soft-rate`（4011）之前**：
    两者可能同时出现在一个响应体里（网关把多个错误码拼在 msg 中）。
    `quota-exceeded` 需长冷却、`soft-rate` 只需短冷却 —— 让较轻的类别抢先命中，
    会让一个已耗尽额度的账号在 60 秒后被反复重试，用户看到的却是「稍后再试」。
    """
    text = body or ""
    lower = text.lower()
    if "1005" in text and "plan" in lower:
        return "hard-plan"
    if TRAE_CODE_QUOTA_EXCEEDED.__str__() in text or "quota" in lower or "exceeded the quota" in lower:
        return "quota-exceeded"
    if "4011" in text:
        return "soft-rate"
    if http_status == 401:
        return "session-dead"
    if http_status == 429:
        return "soft-rate"
    if http_status == 404:
        return "not-found"
    if http_status >= 500:
        return "server"
    if http_status >= 400:
        return "client"
    return "none"


def is_terminal_error(kind: str) -> bool:
    """该类别是否终态（重试无意义，只能重新登录）。"""
    return kind == "session-dead"


def should_rotate_account(kind: str) -> bool:
    """该类别是否应触发「换下一个账号」（除 none 外都换）。"""
    return kind != "none"


# ── 过期判定 ─────────────────────────────────────────

def is_expired(cred: dict, now_ms: Optional[int] = None) -> bool:
    """凭据是否已过期；**无法解析过期时间时不判定过期**（宁可试一次）。

    兼容毫秒时间戳字符串 / 秒级时间戳 / ISO 8601 三种形态。
    """
    if not isinstance(cred, dict):
        return False
    raw = cred.get("expires_at")
    expire_ms = 0
    if isinstance(raw, str) and raw.strip():
        s = raw.strip()
        if s.isdigit():
            value = int(s)
            expire_ms = value if value > 1_000_000_000_000 else value * 1000
        else:
            try:
                from datetime import datetime
                dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
                expire_ms = int(dt.timestamp() * 1000)
            except ValueError:
                expire_ms = 0
    elif isinstance(raw, (int, float)) and not isinstance(raw, bool):
        value = float(raw)
        expire_ms = int(value if value > 1_000_000_000_000 else value * 1000)
    if not expire_ms:
        jwt_exp = read_jwt_exp_ms(str(cred.get("access_token") or ""))
        expire_ms = int(jwt_exp or 0)
    if not expire_ms:
        return False
    now = int(now_ms if now_ms is not None else time.time() * 1000)
    return now >= expire_ms


def is_refreshable(cred: dict) -> bool:
    """是否携带可静默续期的 refresh_token。"""
    return bool(isinstance(cred, dict) and str(cred.get("refresh_token") or ""))


__all__ = [
    "TRAE_CN_AGENT_HOST", "TRAE_CN_UG_HOST", "TRAE_CN_OAUTH_HOST", "TRAE_CN_CONSOLE_HOST",
    "TRAE_INTL_AGENT_HOST", "TRAE_INTL_UG_HOST", "TRAE_INTL_OAUTH_HOST",
    "TRAE_INTL_CONSOLE_HOST", "TRAE_CHAT_PATH", "TRAE_MODELS_PATH",
    "TRAE_BATCH_MODELS_PATH", "TRAE_EXCHANGE_PATH", "TRAE_USER_INFO_PATH",
    "TRAE_CHECKIN_STATUS_PATH", "TRAE_CHECKIN_CLAIM_PATH", "TRAE_ENT_USAGE_PATH",
    "TRAE_CALLBACK_PATH", "TRAE_CALLBACK_PORT", "TRAE_DEFAULT_CHANNELS",
    "TRAE_BATCH_FUNCTIONS", "TRAE_ENCRYPTION_NOTE", "SOLO_EVENT_TYPES",
    "is_intl", "product_for", "generate_machine_id", "generate_device_id",
    "machine_trace_id", "build_login_url", "parse_callback", "fix_nickname_mojibake",
    "solo_headers", "oauth_headers", "seeded_stream", "derive_device_id",
    "derive_market_user_id", "derive_session_id", "derive_checkin_headers",
    "checkin_headers", "exchange_body", "parse_exchange_response",
    "parse_user_info_response", "read_jwt_exp_ms", "resolve_expires_at_ms",
    "build_credential", "apply_refresh", "exchange_refresh_token", "fetch_user_info",
    "transform_to_solo_body", "trim_history", "clamp_max_tokens",
    "normalize_tool_calls", "parse_sse_event", "aggregate_sse", "normalize_usage",
    "solo_error_message", "read_consumption_rate", "read_activity_discount",
    "parse_config_entry", "parse_model_list", "parse_batch_model_list",
    "batch_model_body", "fetch_models", "parse_ent_usage", "fetch_credits",
    "read_claim_code", "classify_checkin_error", "parse_checkin_status",
    "fetch_checkin_status", "claim_checkin", "read_string", "read_number",
    "read_boolean", "classify_error", "is_terminal_error", "should_rotate_account",
    "is_expired", "is_refreshable",
]
