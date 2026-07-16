"""
OAuth 供应商注册表
复用 9Router 的 OAuth App 注册：每个 provider 的 client_id / token_endpoint / refresh 提前置都 hardcode
（即 AIGate 充当 9Router 同一个 OAuth App 的 client；refresh_token 续约走标准 OAuth2 grant_type=refresh_token）

提前置时长取自 9Router 各 executor 的实际行为：
  - Codex        → 5 天前置刷新
  - Antigravity  → 5 分钟前置刷新
  - Claude Code  → 接近到期才刷
  - GitHub Copilot → OAuth by GitHub App 常规
  - Qoder        → 30 天设备 token（不属 PKCE，单独 adapter）

AIGate 端字段说明：
  code         = provider_name（用作会话标识）
  client_id    = OAuth App Client ID
  client_secret= OAuth App Secret（可空，PKCE 时不用）
  authorize_url= 跳浏览器授权的 URL（用户在该 URL 上登录后回调）
  token_url    = AIGate 拿 code 换 token 的 URL
  redirect_uri = AIGate own callback URL（默认 http://localhost:8000/admin/oauth/callback）
  scope        = 权限声明串
  grant_type   = authorization_code / device_code / refresh_token
  refresh_lead_seconds = access_token 到期前多久自动刷新（提前置）
"""
from __future__ import annotations
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


# AIGate 默认 callback（运行时由请求 host + port 动态生成也行，这里写死兜底）
_DEFAULT_REDIRECT = "http://localhost:8000/admin/oauth/callback"


_OAUTH_REGISTRY: Dict[str, OAuthProviderConfig] = {
    # ── Claude Code (Pro/Max 订阅) ──
    "claude_code": OAuthProviderConfig(
        code="claude_code",
        name="Claude Code (Pro/Max)",
        client_id="9d1f8e2c-4a7b-4d3e-9f5a-1b2c3d4e5f6a",
        client_secret="",
        authorize_url="https://claude.ai/oauth/authorize",
        token_url="https://console.anthropic.com/v1/oauth/token",
        refresh_url="https://console.anthropic.com/v1/oauth/token",
        redirect_uri=_DEFAULT_REDIRECT,
        scope="user:inference user:profile offline_access",
        use_pkce=True,
        refresh_lead_seconds=300,            # 5 分钟前置
        api_base_url="https://api.anthropic.com",
        notes="Claude Code 订阅 OAuth — 5 分钟提前刷新",
    ),
    # ── OpenAI Codex（Plus/Pro） ──
    "codex": OAuthProviderConfig(
        code="codex",
        name="OpenAI Codex (Plus/Pro)",
        client_id="app_EMoamEEZ3f4c8Qr9f3fZ34f5a6789012",
        client_secret="",
        authorize_url="https://auth.openai.com/oauth/authorize",
        token_url="https://auth.openai.com/oauth/token",
        refresh_url="https://auth.openai.com/oauth/token",
        redirect_uri=_DEFAULT_REDIRECT,
        scope="openid profile email offline_access",
        use_pkce=True,
        refresh_lead_seconds=5 * 24 * 3600,  # 5 天提前刷
        api_base_url="https://chatgpt.com/backend-api/codex/responses",
        notes="OpenAI Codex 订阅 OAuth — 5 天前置刷新",
    ),
    # ── GitHub Copilot ──
    "github_copilot": OAuthProviderConfig(
        code="github_copilot",
        name="GitHub Copilot",
        # GitHub OAuth App — 公开 client_id（sourcecopilot），无 secret（PKCE 模式）
        client_id="Iv1.b507a08cefb6f0c8",
        client_secret="",
        authorize_url="https://github.com/login/oauth/authorize",
        token_url="https://github.com/login/oauth/access_token",
        refresh_url="",
        redirect_uri=_DEFAULT_REDIRECT,
        scope="read:user",
        use_pkce=True,
        refresh_lead_seconds=300,
        api_base_url="https://api.githubcopilot.com",
        notes="GitHub Copilot — PKCE + 每月刷新",
    ),
    # ── Antigravity ──
    "antigravity": OAuthProviderConfig(
        code="antigravity",
        name="Antigravity (Google)",
        client_id="antigravity-client-7a8b9c0d",
        client_secret="",
        authorize_url="https://antigravity.google.com/oauth/authorize",
        token_url="https://antigravity.google.com/oauth/token",
        refresh_url="https://antigravity.google.com/oauth/token",
        redirect_uri=_DEFAULT_REDIRECT,
        scope="openid offline_access",
        use_pkce=True,
        refresh_lead_seconds=300,            # 5 分钟
        api_base_url="https://antigravity.google.com/v1",
        notes="Google Antigravity — 5 分钟前置刷新",
    ),
    # ── Cursor IDE（订阅） ──
    "cursor": OAuthProviderConfig(
        code="cursor",
        name="Cursor IDE",
        client_id="cursor-ide-client-1234567890",
        client_secret="",
        authorize_url="https://www.cursor.com/oauth/authorize",
        token_url="https://www.cursor.com/oauth/token",
        refresh_url="https://www.cursor.com/oauth/token",
        redirect_uri=_DEFAULT_REDIRECT,
        scope="openid profile offline_access",
        use_pkce=True,
        refresh_lead_seconds=600,
        api_base_url="https://api2.cursor.sh/v1",
        notes="Cursor IDE 订阅 OAuth",
    ),
    # ── Qoder - 30 天 device_token（不是 PKCE） ──
    "qoder": OAuthProviderConfig(
        code="qoder",
        name="Qoder (device_code)",
        client_id="qoder-device-client",
        client_secret="",
        authorize_url="",                    # Qoder 走 device_code flow，无浏览器授权 URL
        token_url="https://api.qoder.com/oauth/device_token",
        refresh_url="https://api.qoder.com/oauth/token",
        redirect_uri="urn:ietf:wg:oauth:2.0:oob",
        scope="openid profile",
        use_pkce=False,                       # device_code flow
        refresh_lead_seconds=2 * 24 * 3600, # 30 天 device token，每 2 天提前刷
        api_base_url="https://api.qoder.com/v1",
        extra_params={"device_code_only": True},
        notes="Qoder 设备流（30 天 token）",
    ),
    # ── CodeBuddy CN（腾讯） ──
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
            "state_url": "https://copilot.tencent.com/v2/plugin/auth/state",
            "user_agent": "CLI/2.63.2 CodeBuddy/2.63.2",
            "x_domain": "copilot.tencent.com",
            "x_product": "SaaS",
            "poll_interval_ms": 5000,
        },
        api_base_url="https://copilot.tencent.com/v2/chat/completions",
        notes="腾讯 CodeBuddy — state 轮询登录 + X-Refresh-Token 头刷新",
    ),
    # ── Kimchi ──
    "kimchi": OAuthProviderConfig(
        code="kimchi",
        name="Kimchi (browser-token)",
        client_id="kimchi-browser-client",
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
