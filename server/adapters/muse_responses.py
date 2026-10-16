"""Meta Muse（Muse Spark）适配器 —— api.meta.ai 的 Responses wire。

为什么复用 Codex 的 Responses 实现：`models.dev` 给 `meta` provider 声明的 npm 是
`@ai-sdk/openai`（AI SDK v5 的 `openai()` 默认打 `/v1/responses`），且官方
opencode muse-code 插件也走 Responses（reasoning 的 encrypted_content 回放只在
该协议上完整 —— 与 codex 同构）。因此 payload 构造 / SSE 解析 / chunk 转换全部
继承 `CodexResponsesAdapter`，本类只覆盖三处**指纹与语义**差异：

  1) 请求头：Meta 不认 Codex 官方客户端指纹（originator / X-Codex-Client /
     OpenAI-Beta），按官方插件只带 `x-api-version`。
  2) 系统提示词：不注入 Codex 的品牌文案（DEFAULT_INSTRUCTIONS=""）。
  3) reasoning effort 档位：Muse 支持 minimal…max（比 Codex 多 minimal/max）。

登录/铸 key 见 `server/core/oauth_client.py` 的 muse 设备码流程：
auth.meta.com OIDC 设备码 → `POST api.meta.ai/muse-code/key` 铸 `LLM|…` key，
该 key 即 Bearer 推理凭证。
"""
from __future__ import annotations

from .codex_responses import CodexResponsesAdapter
from server.core.header_values import outbound_headers
from server.schemas.chat import ChatCompletionRequest

# Muse 没有官方要求的系统提示词（opencode 侧由 meta.txt 注入），这里不注入 ——
# 不把别家品牌文案塞进用户请求。
MUSE_DEFAULT_INSTRUCTIONS = ""

# 离线兜底清单（与注册表种子一致；在线 /v1/models 可用时以在线为准）
MUSE_BUILTIN_MODELS = [
    "muse-spark-1.3",
    "muse-spark-1.2",
    "muse-spark-1.1",
    "muse-wonder-1.0",
    "muse-spark-1.3-contributor",
    "muse-spark-1.2-contributor",
]


class MuseResponsesAdapter(CodexResponsesAdapter):
    """Meta Muse Code（api.meta.ai/v1）的推理适配器。"""

    DEFAULT_INSTRUCTIONS = MUSE_DEFAULT_INSTRUCTIONS
    BUILTIN_MODELS = MUSE_BUILTIN_MODELS
    EFFORT_LEVELS = ["none", "minimal", "low", "medium", "high", "xhigh", "max"]
    API_VERSION = "1.0.0"

    def _get_headers(
        self,
        api_key: str,
        request: ChatCompletionRequest,
        extra_headers: dict = None,
    ) -> dict:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            # 官方插件（opencode-muse-auth）对 Meta 端点的唯一指纹头
            "x-api-version": self.API_VERSION,
        }
        if api_key and str(api_key).strip():
            headers["Authorization"] = f"Bearer {api_key}"
        if extra_headers:
            headers.update(outbound_headers(extra_headers))
        return headers

    def _list_headers(self, api_key: str, extra_headers: dict = None) -> dict:
        headers = {
            "Accept": "application/json",
            "x-api-version": self.API_VERSION,
        }
        if api_key and str(api_key).strip():
            headers["Authorization"] = f"Bearer {api_key}"
        if extra_headers:
            headers.update(outbound_headers(extra_headers))
        return headers
