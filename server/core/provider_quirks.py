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

# u1s1 竞品客户端指纹：2026-09-22 实测，u1s1 会扫描请求体全文，按**大小写精确**匹配
# 竞品客户端名，命中即 403「检测到请求来自非 u1s1 客户端」（赠送额度限官方客户端）。
# 实测边界（同一轮 A/B）：
#   拦截 → "Claude Code" / "Windsurf" / "Gemini CLI" / "Codex CLI"
#   放行 → "claude code"(全小写) / "CLAUDE CODE"(全大写) / "codex"(单词) /
#          "Cody" / "copilot" / "CodeBuddy" / "ZCode" / "Cursor" / "aider" / "Continue"
# 下游 IDE/CLI（Claude Code、Windsurf、Gemini CLI… 以及任何把上游客户端身份
# 写进 system prompt 的工具）原样透传会被拦 → 出站前做**语义等价改写**规避精确串。
_U1S1_COMPETITOR_TOKENS = (
    "Claude Code",
    "Windsurf",
    "Gemini CLI",
    "Codex CLI",
)


def neutralize_u1s1_fingerprint(text: str) -> str:
    """把命中 u1s1 黑名单的精确串改写成语义等价、不触发拦截的形式。

    只做**同义变体替换**（保持可读、不删内容、不改语义）：
      "Claude Code" → "Claude  Code"   （词组内补一个空格，人眼等价）
      "Windsurf"    → "WindSurf"       （压缩词补驼峰，语义不变）
      "Gemini CLI"  → "Gemini  CLI"
      "Codex CLI"   → "Codex  CLI"
    """
    if not text:
        return text
    out = text
    for tok in _U1S1_COMPETITOR_TOKENS:
        if tok in out:
            if " " in tok:
                repl = tok.replace(" ", "  ")
            else:
                # 单token：在首个音节后插入驼峰边界（Windsurf → WindSurf）
                mid = max(1, len(tok) // 2)
                repl = tok[:mid] + tok[mid].upper() + tok[mid + 1:]
                if repl == tok:
                    repl = tok[:-1] + tok[-1].upper()
            out = out.replace(tok, repl)
    return out


# u1s1 工具名黑名单（2026-09-23 生产 A/B 实证）：出站请求体 tools[].function.name
# 精确等于下表左侧名字即 403「检测到请求来自非 u1s1 客户端」（Codex CLI 特征工具）。
# 只扫 tools 字段的名字：描述/参数 schema 任意改都仍拦；改名即放行
# （applyPatch / apply_patch_2 / plan_update 实测 200），消息正文与历史 tool_calls 不拦。
# 规避 = 出站等价改名 + 响应回写原名（客户端按名分发工具结果，必须还原）。
_U1S1_TOOL_NAME_ALIASES = {
    "apply_patch": "applyPatch",
    "update_plan": "updatePlan",
}


@dataclass
class ProviderQuirks:
    name: str
    force_stream: bool = False          # 上游只收流式：非流式请求强制 stream=true 并由网关聚合
    sanitize_system_prompt: bool = False  # 命中 agent 特征/超长的 system 换中性 prompt
    ide_message_shape: bool = False     # 前置 CodeBuddy system + user string → typed blocks
    workos_token: bool = False          # JWT token 出站前补 "workos:" 前缀
    unwrap_envelope: bool = False       # 非流式响应 {success,data} 解包
    neutralize_competitor_tokens: bool = False  # 出站前等价改写竞品客户端名（u1s1 指纹黑名单）
    blocked_tool_names: dict = field(default_factory=dict)  # 上游按工具名拦截：原名→出站别名
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
    # 竞品客户端名消毒：下游 IDE/CLI 的 system prompt 常含 "Claude Code"/"Windsurf"
    # 等精确串，u1s1 扫描 body 命中即 403（赠送额度限官方客户端）；出站前等价改写规避
    neutralize_competitor_tokens=True,
    # 工具名黑名单改写（apply_patch/update_plan → 别名；响应侧还原）。
    # 2026-09-23 生产实测：这是 combo 里 u1s1 候选持续 403 的真正主因。
    blocked_tool_names=dict(_U1S1_TOOL_NAME_ALIASES),
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


def _neutralize_payload_tokens(payload: dict) -> dict:
    """递归遍历出站请求体，对所有字符串做竞品客户端名等价改写。

    覆盖 messages（含 content 数组/ typed blocks）、tools 描述、system 等任意嵌套位置——
    u1s1 按请求体全文精确匹配，漏一处即 403。只改写字符串，不增删字段。
    """
    def walk(node):
        if isinstance(node, str):
            return neutralize_u1s1_fingerprint(node)
        if isinstance(node, list):
            return [walk(x) for x in node]
        if isinstance(node, dict):
            return {k: walk(v) for k, v in node.items()}
        return node
    return walk(payload)


def _rename_tool_names_in_request(payload: dict, aliases: dict) -> dict:
    """把 tools[].function.name / tool_choice 里命中上游黑名单的工具名改为别名。

    只动结构字段（name/tool_choice），不改消息正文——实测上游正文里的
    apply_patch 字样不触发拦截（只有 tools 字段的名字会）。
    """
    p = dict(payload)
    tools = p.get("tools")
    if isinstance(tools, list):
        new_tools = []
        for t in tools:
            if isinstance(t, dict) and isinstance(t.get("function"), dict) \
                    and t["function"].get("name") in aliases:
                t = dict(t)
                fn = dict(t["function"])
                fn["name"] = aliases[fn["name"]]
                t["function"] = fn
            new_tools.append(t)
        p["tools"] = new_tools
    tc = p.get("tool_choice")
    if isinstance(tc, dict) and isinstance(tc.get("function"), dict) \
            and tc["function"].get("name") in aliases:
        tc = dict(tc)
        fn = dict(tc["function"])
        fn["name"] = aliases[fn["name"]]
        tc["function"] = fn
        p["tool_choice"] = tc
    return p


def restore_tool_names_in_response(data, aliases: dict):
    """把响应里被改名的工具调用还原为客户端侧的原名（含流式 delta 与非流式 message）。

    别名表反查；不命中的名字原样返回。data 为 OpenAI chat.completion / chunk dict。
    """
    if not aliases or not isinstance(data, dict):
        return data
    rev = {v: k for k, v in aliases.items()}
    for ch in (data.get("choices") or []):
        if not isinstance(ch, dict):
            continue
        for key in ("message", "delta"):
            node = ch.get(key)
            if not isinstance(node, dict):
                continue
            calls = node.get("tool_calls")
            if not isinstance(calls, list):
                continue
            new_calls = []
            changed = False
            for c in calls:
                if isinstance(c, dict) and isinstance(c.get("function"), dict) \
                        and c["function"].get("name") in rev:
                    c = dict(c)
                    fn = dict(c["function"])
                    fn["name"] = rev[fn["name"]]
                    c["function"] = fn
                    changed = True
                new_calls.append(c)
            if changed:
                node["tool_calls"] = new_calls
    return data


def transform_payload(payload: dict, q: Optional[ProviderQuirks],
                      tool_guard_enabled: bool = True) -> dict:
    """按档案改写出站请求体（就地语义由返回新 dict 承载）。

    tool_guard_enabled=False（服务商面板关掉「指纹过滤」）时跳过 u1s1 的
    竞品客户端名消毒与工具名黑名单改写；协议保真（UA/DPoP/归因头）不受影响。
    """
    if not q:
        return payload
    p = dict(payload)
    if q.force_stream:
        p["stream"] = True
    # 工具名黑名单改写（u1s1）：必须在 messages 早退之前——只动 tools/tool_choice 结构字段
    if q.blocked_tool_names and tool_guard_enabled:
        p = _rename_tool_names_in_request(p, q.blocked_tool_names)
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
    # 竞品客户端名消毒（u1s1）：扫描**整份 body 的文本**做等价改写。
    # 放这里而不是只改 system：下游可能把身份声明写进 user/tool 消息或 tools 描述里，
    # 上游是按全文精确匹配的（实测 2026-09-22）。与工具名改写同受「指纹过滤」开关控制。
    if q.neutralize_competitor_tokens and tool_guard_enabled:
        p = _neutralize_payload_tokens(p)
        messages = p.get("messages")
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
