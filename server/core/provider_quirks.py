"""上游服务商「请求方言」档案（实现方式对齐 9router executors 的实测结论）。

部分 OpenAI 兼容网关有非标行为，通用 openai_compat adapter 需要按域名分发处理：

  CodeBuddy CN (copilot.tencent.com)
    - 拒绝非流式请求（400 code 11101 "Non-stream chat request is currently
      not supported"）→ 强制 stream=true，非流式客户端由网关侧聚合 SSE 回包。
    - 内容审查会把 Claude Code 等 CLI agent 的 system prompt 判为敏感内容整包
      拒绝 → 超长或命中 agent 身份特征的 system 消息替换为中性 prompt。
    - reasoning 仅在带 reasoning_effort + reasoning_summary:"auto" 时透出；
      网关无 "none" 档 → none/off 直接删除 effort，不携带 summary。

  CodeBuddy International (www.codebuddy.ai)
    - 同样流式专属；请求体要求 IDE 插件形态：前置固定 system
      "You are CodeBuddy Code."，user 的 string content 转 typed blocks
      （[{type:"text",text}]），否则 11101 invalid request。

  Cline (api.cline.bot)
    - OAuth access token 是 WorkOS JWT，Authorization 头必须带 "workos:" 前缀
      （Bearer workos.<jwt>，官方插件行为）；非 JWT（如 API key）原样。
    - 非流式响应被包在 {"success":true,"data":{...}} 信封里 → 解包。

只读匹配 + 纯函数转换，不发起网络请求；adapter 在出站前调用。
"""
from __future__ import annotations
import re
from dataclasses import dataclass, field
from typing import Optional

# agent system prompt 特征（对齐 9router CodeBuddyExecutor 的正则集）
AGENT_SYSTEM_PATTERN = re.compile(
    r"you are claude code|claude.?code.+official.+cli|anthropic.+official.+cli"
    r"|anxthxropic.+official.+cli|you are (?:cursor|windsurf|cline|aider|continue|copilot|cody)"
    r"|you are an? (?:ai )?(?:coding |code )?agent"
    r"|cc_entrypoint\s*=\s*(?:cli|vscode|jetbrains|gui)"
    r"|claude.?code.+issues|give feedback.+claude.?code"
    r"|you are .{0,30}(?:powerful )?ai agent|orchestration capabilities"
    r"|OhMyOpenCode|<agent-identity>|<Role>|<Behavior_Instructions>",
    re.IGNORECASE,
)
NEUTRAL_SYSTEM_PROMPT = (
    "You are a helpful AI assistant that helps with software engineering tasks."
)
CODEBUDDY_IDE_SYSTEM_PROMPT = "You are CodeBuddy Code."


@dataclass
class ProviderQuirks:
    name: str
    force_stream: bool = False          # 上游只收流式：非流式请求强制 stream=true 并由网关聚合
    sanitize_system_prompt: bool = False  # 命中 agent 特征/超长的 system 换中性 prompt
    ide_message_shape: bool = False     # 前置 CodeBuddy system + user string → typed blocks
    workos_token: bool = False          # JWT token 出站前补 "workos:" 前缀
    unwrap_envelope: bool = False       # 非流式响应 {success,data} 解包
    default_headers: dict = field(default_factory=dict)


_CODEBUDDY_CN = ProviderQuirks(
    name="codebuddy_cn",
    force_stream=True,
    sanitize_system_prompt=True,
    default_headers={
        "User-Agent": "CLI/2.108.1 CodeBuddy/2.108.1",
        "X-Product": "SaaS",
        "X-IDE-Type": "CLI",
        "X-IDE-Name": "CLI",
        "x-requested-with": "XMLHttpRequest",
        "x-codebuddy-request": "1",
    },
)

_CODEBUDDY_INTL = ProviderQuirks(
    name="codebuddy_intl",
    force_stream=True,
    ide_message_shape=True,
    default_headers={
        "User-Agent": "IDE/2.108.1 CodeBuddy/2.108.1",
        "X-Product": "SaaS",
        "X-IDE-Type": "IDE",
        "X-IDE-Name": "IDE",
        "x-requested-with": "XMLHttpRequest",
        "x-codebuddy-request": "1",
    },
)

_CLINE = ProviderQuirks(
    name="cline",
    workos_token=True,
    unwrap_envelope=True,
    default_headers={
        "HTTP-Referer": "https://cline.bot",
        "X-Title": "Cline",
    },
)

# u1s1（有一说一）：OpenAI 兼容。推理「客户端信号」= RFC9449 DPoP（官方 CLI device-auth.js
# 同协议）：Authorization: DPoP <u1s1d-…> + dpop proof 逐请求现签，签名材料来自设备登录时
# 持久化的密钥对（credential_resolver 注入 __dpop 内部标记，openai_compat 出站前签发）。
# 客户端完整性审查（client_integrity_review 403）实测判据 = user-agent: u1s1-cli
#（官方 tools.js 同源）；缺 DPoP → 401/缺 UA → 403，二者皆必需，缺一仍被拦。
# 归因头（x-u1s1-*）对齐官方 CLI 形态，随域名方言统一附加。
_U1S1 = ProviderQuirks(
    name="u1s1",
    default_headers={
        "user-agent": "u1s1-cli",
        "x-u1s1-client": "terminal",
        "x-u1s1-version": "1.11.2",
        "x-u1s1-platform": "linux-x64",
    },
)

# 域名 → 档案（子串匹配，大小写不敏感；codebuddy.ai 兜住 www./裸域两形态）
_DOMAIN_RULES = (
    ("copilot.tencent.com", _CODEBUDDY_CN),
    ("codebuddy.ai", _CODEBUDDY_INTL),
    ("api.cline.bot", _CLINE),
    ("api.u1s1.io", _U1S1),
)


def quirks_for(base_url: str) -> Optional[ProviderQuirks]:
    url = (base_url or "").lower()
    if not url:
        return None
    for domain, q in _DOMAIN_RULES:
        if domain in url:
            return q
    return None


# Cline OAuth access token 是 WorkOS JWT（base64url eyJ… 开头）。
# 非 JWT 形态（ClinePass 的 clp_ API key 等）必须原样发送，加前缀会被 401 拒绝。
_WORKOS_JWT = re.compile(r"^eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+")


def auth_token_for(q: Optional[ProviderQuirks], api_key: str) -> str:
    token = (api_key or "").strip()
    if not q or not token or not q.workos_token:
        return token
    if token.startswith("workos:") or not _WORKOS_JWT.match(token):
        return token
    return f"workos:{token}"


def _flatten_content(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            b.get("text", "") for b in content if isinstance(b, dict) and isinstance(b.get("text"), str)
        )
    return ""


def transform_payload(payload: dict, q: Optional[ProviderQuirks]) -> dict:
    """按档案改写出站请求体（就地语义由返回新 dict 承载）。"""
    if not q:
        return payload
    p = dict(payload)
    if q.force_stream:
        p["stream"] = True
    # reasoning_effort 镜像（两家 CodeBuddy 行为一致）：none/off 网关不认，直接删；
    # 有 effort 时补 reasoning_summary 才会透出思考内容
    if q.name.startswith("codebuddy"):
        eff = p.get("reasoning_effort")
        if eff in ("none", "off"):
            p.pop("reasoning_effort", None)
        elif eff:
            p["reasoning_summary"] = "auto"
    messages = p.get("messages")
    if not isinstance(messages, list):
        return p
    if q.sanitize_system_prompt:
        fixed = []
        for m in messages:
            if isinstance(m, dict) and m.get("role") == "system":
                text = _flatten_content(m.get("content"))
                if text and (len(text) > 2000 or AGENT_SYSTEM_PATTERN.search(text)):
                    m = dict(m)
                    if isinstance(m.get("content"), list):
                        m["content"] = [{"type": "text", "text": NEUTRAL_SYSTEM_PROMPT}]
                    else:
                        m["content"] = NEUTRAL_SYSTEM_PROMPT
            fixed.append(m)
        p["messages"] = fixed
        messages = fixed
    if q.ide_message_shape:
        shaped = [{"role": "system", "content": CODEBUDDY_IDE_SYSTEM_PROMPT}]
        for m in messages:
            if not isinstance(m, dict):
                continue
            if m.get("role") in ("system", "developer"):
                continue  # intl 形态固定自家 system，丢弃上游 system/developer
            if m.get("role") == "user" and isinstance(m.get("content"), str):
                m = dict(m)
                m["content"] = [{"type": "text", "text": m["content"]}]
            shaped.append(m)
        p["messages"] = shaped
    return p


def unwrap_response(data, q: Optional[ProviderQuirks]):
    """Cline 非流式信封 {"success":true,"data":{...}} → 取内层；错误信封原样透传。"""
    if not q or not q.unwrap_envelope or not isinstance(data, dict):
        return data
    if data.get("success") is True and isinstance(data.get("data"), dict):
        return data["data"]
    return data
