"""
OAuth 供应商注册表
每个 provider 的 client_id / token_endpoint / refresh 提前置都在这里注册
（refresh_token 续约走标准 OAuth2 grant_type=refresh_token）。

安全说明：本文件不再硬编码任何真实 OAuth client_id —— 真实 client_id
通过环境变量注入（格式：AIGATE_OAUTH_<CODE>_CLIENT_ID，CODE 为 provider
code 的大写），未配置时使用占位符。这样仓库可以在公开环境下安全分发。

提前置时长参考各 executor 的实际行为：
  - Codex        → 5 天前置刷新
  - Antigravity  → 5 分钟前置刷新
  - Claude Code  → 接近到期才刷
  - GitHub Copilot → OAuth by GitHub App 常规
  - Qoder        → 30 天设备 token（不属 PKCE，单独 adapter）

AIGate 端字段说明：
  code         = provider_name（用作会话标识）
  client_id    = OAuth App Client ID（环境变量注入，见 _env_client_id）
  client_secret= OAuth App Secret（可空，PKCE 时不用）
  authorize_url= 跳浏览器授权的 URL（用户在该 URL 上登录后回调）
  token_url    = AIGate 拿 code 换 token 的 URL
  redirect_uri = AIGate own callback URL（默认 http://localhost:8000/admin/oauth/callback）
  scope        = 权限声明串
  grant_type   = authorization_code / device_code / refresh_token
  refresh_lead_seconds = access_token 到期前多久自动刷新（提前置）
"""
from __future__ import annotations
import os
from dataclasses import dataclass
from typing import Dict, List, Optional


@dataclass
class OAuthProviderConfig:
    code: str
    name: str
    client_id: str
    client_secret: str                       # PKCE 模式时可空字符串
    authorize_url: str
    token_url: str
    refresh_url: str = ""                    # 不指定时复用 token_url
    redirect_uri: str = ""
    scope: str = ""
    use_pkce: bool = True                    # 大多数用 PKCE
    refresh_lead_seconds: int = 600          # 默认 10 分钟提前刷
    extra_params: dict = None
    api_base_url: Optional[str] = None       # 该 provider 实际 LLM API 调用 base_url
    notes: str = ""                          # 描述
    # 自动建服务商（连接成功即入服务商列表）用：
    adapter_api_type: str = "openai_compat"  # AIGate 适配器选择（anthropic/codex_responses/github/qoder…）
    static_models: List[dict] = None         # 在线拉取模型不可用时的兜底种子 [{model_id, display_name}]


# AIGate 默认 callback（运行时由请求 host + port 动态生成也行，这里写死兜底）
_DEFAULT_REDIRECT = "http://localhost:8000/admin/oauth/callback"


def _seed(*pairs):
    """静态模型种子（id, 显示名）→ [{model_id, display_name}]"""
    return [{"model_id": mid, "display_name": name} for mid, name in pairs]


def _env_client_id(code: str, placeholder: str = "CHANGE_ME") -> str:
    """从环境变量读取该 provider 的 OAuth client_id，避免真实凭据硬编码入库。

    环境变量格式：AIGATE_OAUTH_<CODE>_CLIENT_ID（CODE 为 provider code 大写，
    如 AIGATE_OAUTH_CODEX_CLIENT_ID）。未配置时返回占位符，运行时需自行注入
    真实值，否则 OAuth 授权流程将无法通过。
    """
    return os.environ.get(f"AIGATE_OAUTH_{code.upper()}_CLIENT_ID", "") or placeholder


_OAUTH_REGISTRY: Dict[str, OAuthProviderConfig] = {
    # ── Claude Code (Pro/Max 订阅) ──
    "claude_code": OAuthProviderConfig(
        code="claude_code",
        name="Claude Code (Pro/Max)",
        client_id=_env_client_id("claude_code"),
        client_secret="",
        authorize_url="https://claude.ai/oauth/authorize",
        token_url="https://console.anthropic.com/v1/oauth/token",
        refresh_url="https://console.anthropic.com/v1/oauth/token",
        redirect_uri=_DEFAULT_REDIRECT,
        scope="user:inference user:profile offline_access",
        use_pkce=True,
        refresh_lead_seconds=300,            # 5 分钟前置
        api_base_url="https://api.anthropic.com",
        adapter_api_type="anthropic",
        static_models=_seed(
            ("claude-opus-5", "Claude Opus 5"),
            ("claude-fable-5-1", "Claude Fable 5.1"),
            ("claude-fable-5", "Claude Fable 5"),
            ("claude-sonnet-5", "Claude Sonnet 5"),
            ("claude-haiku-4-5-20251001", "Claude 4.5 Haiku"),
        ),
        notes="Claude Code 订阅 OAuth — 5 分钟提前刷新",
    ),
    # ── OpenAI Codex（Plus/Pro） ──
    "codex": OAuthProviderConfig(
        code="codex",
        name="OpenAI Codex (Plus/Pro)",
        client_id=_env_client_id("codex"),
        client_secret="",
        authorize_url="https://auth.openai.com/oauth/authorize",
        token_url="https://auth.openai.com/oauth/token",
        refresh_url="https://auth.openai.com/oauth/token",
        redirect_uri=_DEFAULT_REDIRECT,
        scope="openid profile email offline_access",
        use_pkce=True,
        refresh_lead_seconds=5 * 24 * 3600,  # 5 天提前刷
        api_base_url="https://chatgpt.com/backend-api/codex",
        adapter_api_type="codex_responses",
        static_models=_seed(
            ("gpt-6-astra", "GPT 6.0 Astra"),
            ("gpt-5.6-sol", "GPT 5.6 Sol"),
            ("gpt-5.6-terra", "GPT 5.6 Terra"),
            ("gpt-5.6-luna", "GPT 5.6 Luna"),
            ("gpt-5.5", "GPT 5.5"),
            ("gpt-5.4", "GPT 5.4"),
            ("gpt-5.4-mini", "GPT 5.4 Mini"),
            ("gpt-5.3-codex-spark", "GPT 5.3 Codex Spark"),
        ),
        notes="OpenAI Codex 订阅 OAuth — 5 天前置刷新",
    ),
    # ── GitHub Copilot ──
    "github_copilot": OAuthProviderConfig(
        code="github_copilot",
        name="GitHub Copilot",
        # GitHub OAuth App — 无 secret（PKCE 模式），client_id 经环境变量注入
        client_id=_env_client_id("github_copilot"),
        client_secret="",
        authorize_url="https://github.com/login/oauth/authorize",
        token_url="https://github.com/login/oauth/access_token",
        refresh_url="",
        redirect_uri=_DEFAULT_REDIRECT,
        scope="read:user",
        use_pkce=True,
        refresh_lead_seconds=300,
        api_base_url="https://api.githubcopilot.com",
        adapter_api_type="github",
        static_models=_seed(
            ("gpt-5.2", "GPT-5.2"),
            ("gpt-5.2-codex", "GPT-5.2 Codex"),
            ("gpt-5.3-codex", "GPT-5.3 Codex"),
            ("gpt-5.4", "GPT-5.4"),
            ("gpt-5.4-mini", "GPT-5.4 Mini"),
            ("claude-haiku-4.5", "Claude Haiku 4.5"),
            ("claude-sonnet-4.6", "Claude Sonnet 4.6"),
            ("claude-opus-4.6", "Claude Opus 4.6"),
            ("claude-opus-4.7", "Claude Opus 4.7"),
            ("gemini-2.5-pro", "Gemini 2.5 Pro"),
            ("gemini-3.1-pro-preview", "Gemini 3.1 Pro"),
        ),
        notes="GitHub Copilot — PKCE + 每月刷新",
    ),
    # ── Antigravity ──
    "antigravity": OAuthProviderConfig(
        code="antigravity",
        name="Antigravity (Google)",
        client_id=_env_client_id("antigravity"),
        client_secret="",
        authorize_url="https://antigravity.google.com/oauth/authorize",
        token_url="https://antigravity.google.com/oauth/token",
        refresh_url="https://antigravity.google.com/oauth/token",
        redirect_uri=_DEFAULT_REDIRECT,
        scope="openid offline_access",
        use_pkce=True,
        refresh_lead_seconds=300,            # 5 分钟
        api_base_url="https://antigravity.google.com/v1",
        adapter_api_type="openai_compat",
        static_models=_seed(
            ("gemini-3.8-flash-high", "Gemini 3.8 Flash (High)"),
            ("gemini-3.8-flash-medium", "Gemini 3.8 Flash (Medium)"),
            ("gemini-3.8-flash-low", "Gemini 3.8 Flash (Low)"),
            ("gemini-3.7-flash-high", "Gemini 3.7 Flash (High)"),
            ("gemini-3.7-flash-medium", "Gemini 3.7 Flash (Medium)"),
            ("gemini-3.5-flash-low", "Gemini 3.5 Flash (Low)"),
            ("gemini-pro-agent", "Gemini Pro Agent"),
            ("gemini-3.1-pro-low", "Gemini 3.1 Pro (Low)"),
            ("claude-sonnet-4-6", "Claude Sonnet 4.6"),
            ("claude-opus-4-6-thinking", "Claude Opus 4.6 Thinking"),
            ("gpt-oss-120b-medium", "GPT-OSS 120B"),
            ("gemini-3.1-flash-image", "Gemini 3.1 Flash Image"),
        ),
        notes="Google Antigravity — 5 分钟前置刷新",
    ),
    # ── Cursor IDE（订阅） ──
    "cursor": OAuthProviderConfig(
        code="cursor",
        name="Cursor IDE",
        client_id=_env_client_id("cursor"),
        client_secret="",
        authorize_url="https://www.cursor.com/oauth/authorize",
        token_url="https://www.cursor.com/oauth/token",
        refresh_url="https://www.cursor.com/oauth/token",
        redirect_uri=_DEFAULT_REDIRECT,
        scope="openid profile offline_access",
        use_pkce=True,
        refresh_lead_seconds=600,
        api_base_url="https://api2.cursor.sh/v1",
        adapter_api_type="openai_compat",
        static_models=_seed(
            ("default", "Auto (Server Picks)"),
            ("claude-4.6-opus-max", "Claude 4.6 Opus Max"),
            ("claude-4.6-sonnet-medium-thinking", "Claude 4.6 Sonnet Thinking"),
            ("claude-4.5-opus-high", "Claude 4.5 Opus High"),
            ("claude-4.5-sonnet", "Claude 4.5 Sonnet"),
            ("claude-4.5-haiku", "Claude 4.5 Haiku"),
            ("gpt-5.2-codex", "GPT 5.2 Codex"),
            ("gpt-5.3-codex", "GPT 5.3 Codex"),
            ("gpt-5.2", "GPT 5.2"),
            ("kimi-k2.5", "Kimi K2.5"),
            ("gemini-3-flash-preview", "Gemini 3 Flash Preview"),
        ),
        notes="Cursor IDE 订阅 OAuth",
    ),
    # ── Qoder — 官方设备流 + COSY 签名推理代理（端口自 9router qoder 全栈） ──
    # 流程：本地生成 PKCE(S256) + nonce + machine_id → 浏览器打开
    #   https://qoder.com/device/selectAccounts?challenge=..&nonce=..&machine_id=..
    #   → 轮询 openapi.qoder.sh/api/v1/deviceToken/poll?nonce&verifier（202/404=pending，
    #   200 带 dt- token）→ userinfo 补 uid/email → scope 列存 COSY 签名所需元数据。
    # device token 约 30 天；upstream refresh 对本流返回 403 → refresh_style=none。
    "qoder": OAuthProviderConfig(
        code="qoder",
        name="Qoder",
        client_id="",
        client_secret="",
        authorize_url="",
        token_url="https://openapi.qoder.sh/api/v1/deviceToken/poll",
        refresh_url="",
        redirect_uri="",
        scope="",
        use_pkce=False,
        refresh_lead_seconds=24 * 3600,      # 每天看一眼（实际不刷，仅标记过期临近提醒重登）
        api_base_url="https://api3.qoder.sh/algo/api/v2",
        adapter_api_type="qoder",
        extra_params={
            "auth_mode": "qoder_device",
            "refresh_style": "none",
            "login_url": "https://qoder.com/device/selectAccounts",
            "userinfo_url": "https://openapi.qoder.sh/api/v1/userinfo",
            "quota_usage_url": "https://openapi.qoder.sh/api/v2/quota/usage",
        },
        static_models=_seed(
            ("auto", "Auto"), ("ultimate", "Ultimate"), ("performance", "Performance"),
            ("efficient", "Efficient"), ("lite", "Lite"),
            ("qmodel_38max", "Qwen3.8-Max"), ("qmodel_latest", "Qwen3.7-Max"),
            ("qmodel", "Qwen3.7-Plus"), ("qfmodel", "Qwen3.8-Flash"),
            ("kmodel_latest", "Kimi-K3"), ("kmodel", "Kimi-K2.7-Code"),
            ("gmodel", "GLM-5.3"), ("gfmodel", "GLM-5.3-Flash"),
            ("dmodel", "DeepSeek-V4-Pro"), ("dfmodel", "DeepSeek-V4-Flash"),
            ("mmodel", "MiniMax-M3"),
        ),
        notes="Qoder 设备流（30 天 token）+ COSY 签名推理（api3.qoder.sh）",
    ),
    # ── CodeBuddy CN（腾讯 copilot.tencent.com） ──
    # 流程（对齐 9router codebuddy-cn provider 实测）：
    #   1) POST {state_url}?platform=CLI body={}（带 X-No-* 匿名头）→ {code:0,data:{state,authUrl}}
    #   2) 浏览器打开 authUrl 登录
    #   3) GET {token_url}?state=... 轮询，code 11217=pending，code 0 带 accessToken/refreshToken
    #   4) 刷新 POST {refresh_url} 头 X-Refresh-Token + 空 JSON body
    "codebuddy_cn": OAuthProviderConfig(
        code="codebuddy_cn",
        name="CodeBuddy CN (腾讯)",
        client_id="",                               # 腾讯协议没有 client_id
        client_secret="",
        authorize_url="",                           # 不走标准 authorize，走 device poll
        token_url="https://copilot.tencent.com/v2/plugin/auth/token",
        refresh_url="https://copilot.tencent.com/v2/plugin/auth/token/refresh",
        redirect_uri="",
        scope="",
        use_pkce=False,
        refresh_lead_seconds=300,
        extra_params={
            "auth_mode": "device_poll",
            "refresh_style": "codebuddy",
            "state_url": "https://copilot.tencent.com/v2/plugin/auth/state",
            "platform": "CLI",
            "user_agent": "CLI/2.63.2 CodeBuddy/2.63.2",
            "x_domain": "copilot.tencent.com",
            "x_product": "SaaS",
            "poll_interval_ms": 5000,
        },
        api_base_url="https://copilot.tencent.com/v2/chat/completions",
        # 种子表在「在线列表不可用」时使用（copilot.tencent.com 无公开 list 端点，只能兜底）。
        # 2026-09-23 用真实账号逐模型探测校正（探测法：stream 请求 + system 首条，
        # 11102 service info not found = 已下架，须移出）。
        static_models=_seed(
            ("glm-5.2", "GLM-5.2"), ("glm-5.1", "GLM-5.1"), ("glm-5.3", "GLM-5.3"),
            ("glm-5.3-flash", "GLM-5.3-Flash"), ("glm-5v-turbo", "GLM-5v-Turbo"),
            ("minimax-m3", "MiniMax-M3"), ("minimax-m2.7", "MiniMax-M2.7"),
            ("kimi-k2.8-preview", "Kimi-K2.8-Preview"),
            ("kimi-k2.7", "Kimi-K2.7-Code"),
            ("kimi-k2.6", "Kimi-K2.6"), ("kimi-k2.5", "Kimi-K2.5"),
            ("kimi-k3", "Kimi-K3"), ("kimi-k3-1", "Kimi-K3"),
            ("hy3", "Hy3"), ("hy3-preview", "Hy3 Preview"),
            ("hy3-preview-agent", "Hy3 Preview Agent"),
            ("hy4-preview", "Hy4-Preview"), ("hy4-preview-f", "Hy4-Preview-F"),
            ("deepseek-v4-pro", "DeepSeek-V4-Pro"),
            ("deepseek-v4.1-flash", "DeepSeek-V4.1-Flash"),
            ("deepseek-v4-flash", "DeepSeek-V4-Flash"),
            ("deepseek-v3-2-volc", "DeepSeek-V3.2"),
            ("minimax-m3-pay", "MiniMax-M3-Pay"),
            ("fast-model", "Fast Model"), ("balanced-model", "Balanced Model"),
            ("deep-model", "Deep Model"), ("auto", "Auto"),
        ),
        notes="腾讯 CodeBuddy — state 轮询登录 + X-Refresh-Token 头刷新",
    ),
    # ── CodeBuddy International（www.codebuddy.ai）──
    # 与 CN 同构，仅域名 / X-Domain / platform=ide（CN 用 CLI）不同。
    "codebuddy_intl": OAuthProviderConfig(
        code="codebuddy_intl",
        name="CodeBuddy (International)",
        client_id="",
        client_secret="",
        authorize_url="",
        token_url="https://www.codebuddy.ai/v2/plugin/auth/token",
        refresh_url="https://www.codebuddy.ai/v2/plugin/auth/token/refresh",
        redirect_uri="",
        scope="",
        use_pkce=False,
        refresh_lead_seconds=300,
        extra_params={
            "auth_mode": "device_poll",
            "refresh_style": "codebuddy",
            "state_url": "https://www.codebuddy.ai/v2/plugin/auth/state",
            "platform": "ide",
            "user_agent": "IDE/2.63.2 CodeBuddy/2.63.2",
            "x_domain": "www.codebuddy.ai",
            "x_product": "SaaS",
            "poll_interval_ms": 5000,
        },
        api_base_url="https://www.codebuddy.ai/v2/chat/completions",
        # 2026-09-23 实测：workbuddy.ai 与 codebuddy.ai 是同一后端（同 token 同结果），
        # 国际版专属模型（gpt-* / gemini-3.5-flash）只在授权账号可见。
        # 旧种子（deepseek-v4-flash / deepseek-v4-pro / deepseek-v3-2-volc / glm-4.7 /
        # minimax-m2.7 / kimi-k3-1 / hy3-preview / glm-5.0）已全部 11102 下架，已剔除。
        static_models=_seed(
            ("glm-5.2", "GLM-5.2"), ("glm-5.1", "GLM-5.1"),
            ("glm-5.3", "GLM-5.3"), ("glm-5.3-flash", "GLM-5.3-Flash"),
            ("glm-5v-turbo", "GLM-5v-Turbo"),
            ("minimax-m3", "MiniMax-M3"),
            ("kimi-k3", "Kimi-K3"), ("kimi-k2.8-preview", "Kimi-K2.8-Preview"),
            ("kimi-k2.7", "Kimi-K2.7-Code"), ("kimi-k2.6", "Kimi-K2.6"),
            ("kimi-k2.5", "Kimi-K2.5"),
            ("hy3", "Hy3"), ("hy4-preview", "Hy4-Preview"), ("hy4-preview-f", "Hy4-Preview-F"),
            ("deepseek-v4.1-flash", "DeepSeek-V4.1-Flash"),
            ("gpt-6-astra", "GPT-6 Astra"),
            ("gpt-5.6-sol", "GPT-5.6 Sol"), ("gpt-5.6-terra", "GPT-5.6 Terra"),
            ("gpt-5.6-luna", "GPT-5.6 Luna"),
            ("gpt-5.5", "GPT-5.5"), ("gpt-5.4", "GPT-5.4"),
            ("gpt-5.3-codex", "GPT-5.3 Codex"),
            ("gemini-3.5-flash", "Gemini-3.5-Flash"),
            ("default-model", "Default Model"), ("fast-model", "Fast Model"),
            ("balanced-model", "Balanced Model"), ("primary-model", "Primary Model"),
            ("deep-model", "Deep Model"), ("auto", "Auto"),
        ),
        notes="CodeBuddy 国际服（workbuddy.ai 同后端）— 与 CN 同协议（state 轮询 + X-Refresh-Token 刷新）",
    ),
    # ── Cline（api.cline.bot 订阅网关）──
    # authorization_code 变体：/auth/authorize?client_type=extension&callback_url=&redirect_uri=
    #   回调的 code 本身是 base64(JSON token)，直接解码即得 token（解析失败回退
    #   POST token_url 标准交换）。刷新为 JSON {refreshToken,grantType,clientType}。
    #   注意：ClinePass（clp_ API key）共用域名但只认 API key，OAuth token 会被 401。
    "cline": OAuthProviderConfig(
        code="cline",
        name="Cline (cline.bot)",
        client_id="",                                # authorize 无 client_id
        client_secret="",
        authorize_url="https://api.cline.bot/api/v1/auth/authorize",
        token_url="https://api.cline.bot/api/v1/auth/token",
        refresh_url="https://api.cline.bot/api/v1/auth/refresh",
        redirect_uri=_DEFAULT_REDIRECT,
        scope="",
        use_pkce=False,
        refresh_lead_seconds=300,
        extra_params={
            "auth_mode": "cline",
            "refresh_style": "cline",
            "token_in_code": True,
            "client_type": "extension",
        },
        api_base_url="https://api.cline.bot/api/v1/chat/completions",
        static_models=_seed(
            ("anthropic/claude-opus-4.7", "Claude Opus 4.7"),
            ("anthropic/claude-sonnet-4.6", "Claude Sonnet 4.6"),
            ("anthropic/claude-opus-4.6", "Claude Opus 4.6"),
            ("openai/gpt-5.3-codex", "GPT-5.3 Codex"),
            ("openai/gpt-5.4", "GPT-5.4"),
            ("google/gemini-3.1-pro-preview", "Gemini 3.1 Pro Preview"),
            ("google/gemini-3.1-flash-lite-preview", "Gemini 3.1 Flash Lite"),
            ("kwaipilot/kat-coder-pro", "KAT Coder Pro"),
        ),
        notes="Cline 订阅 — code 即 base64 token（自解码）+ JSON 刷新 + workos: 前缀",
    ),
    # ── u1s1（有一说一）额度平台 ──
    # 设备登录流（复刻官方 CLI login.js，免客户端）：
    #   1) 本地生成 EC P-256 密钥对
    #   2) POST {origin}/auth/device/start {public_jwk, device_name, client_version}
    #      → {verify_url, poll_secret, interval, expires_in}
    #   3) 浏览器打开 verify_url 登录并「批准设备」
    #   4) POST {origin}/auth/device/poll {poll_secret} → status ok 时带
    #      {api_key: u1s1-…, device_token: u1s1d-…}
    # api_key 即可直接 Bearer 调 https://api.u1s1.io/v1/*（OpenAI 兼容，实测
    # chat/completions 与 models 均按普通 key 鉴权，DPoP 只是官方客户端的
    # sender-constrained 增强）。api_key 长期有效，无标准 refresh——到期需
    # 重新登录（refresh_style=none）。
    "u1s1": OAuthProviderConfig(
        code="u1s1",
        name="u1s1 (有一说一)",
        client_id="",
        client_secret="",
        authorize_url="",
        token_url="",
        refresh_url="",
        redirect_uri="",
        scope="",
        use_pkce=False,
        refresh_lead_seconds=86400,
        extra_params={
            "auth_mode": "u1s1_device",
            "refresh_style": "none",
            "device_start_url": "https://api.u1s1.io/auth/device/start",
            "device_poll_url": "https://api.u1s1.io/auth/device/poll",
            "device_name": "AIGate Gateway",
            "client_version": "1.11.2",
        },
        api_base_url="https://api.u1s1.io/v1",
        notes="u1s1 额度平台 — 浏览器批准设备后自动收 api_key（OpenAI 兼容直连）",
    ),
    # ── Kimchi ──
    "kimchi": OAuthProviderConfig(
        code="kimchi",
        name="Kimchi (browser-token)",
        client_id=_env_client_id("kimchi"),
        client_secret="",
        authorize_url="https://kimchi.ai/oauth/authorize",
        token_url="https://kimchi.ai/oauth/token",
        redirect_uri=_DEFAULT_REDIRECT,
        scope="openid",
        use_pkce=True,
        refresh_lead_seconds=600,
        api_base_url="https://gateway.kimchi.ai/v1",
        notes="Kimchi — browser-token OAuth + OpenAI 兼容网关",
    ),
}


def get_all_oauth_providers() -> List[OAuthProviderConfig]:
    """读取全部已注册 OAuth provider（供前端可选）"""
    return list(_OAUTH_REGISTRY.values())


def get_oauth_provider(code: str) -> Optional[OAuthProviderConfig]:
    return _OAUTH_REGISTRY.get(code)


def list_provider_codes() -> List[str]:
    return list(_OAUTH_REGISTRY.keys())
