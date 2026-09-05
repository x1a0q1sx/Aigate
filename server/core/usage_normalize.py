"""usage 统一归一化（P1-4）。

上游返回的 usage 至少有三种方言，字段含义还有口径差异：

1. OpenAI Chat Completions
   prompt_tokens / completion_tokens（completion 已含 reasoning）
   prompt_tokens_details.cached_tokens = 缓存读（prompt 的子集）
   completion_tokens_details.reasoning_tokens = 思考 token（completion 的子集）
2. OpenAI Responses API
   input_tokens / output_tokens
   input_tokens_details.cached_tokens；cache_creation_details.total_tokens
   output_tokens_details.reasoning_tokens
3. Anthropic Messages
   input_tokens（不含缓存！）+ cache_read_input_tokens + cache_creation_input_tokens
   （独立于 input_tokens；新版可能是 usage.cache_creation.{ephemeral_5m/1h_input_tokens}）

归一化口径（与 OpenAI chat 对齐，全网关统计一致）：
- prompt_tokens      = 输入总量，**包含**缓存读/写
- completion_tokens  = 输出总量，包含 reasoning
- cache_read_tokens  = prompt 的子集
- cache_write_tokens = prompt 的子集
- reasoning_tokens   = completion 的子集
- source             = 命中的方言（openai_chat / openai_responses / anthropic / unknown）

这样 cache_hit_rate = cache_read / prompt_tokens 在所有上游下口径一致。
"""
from dataclasses import dataclass, field


@dataclass
class NormalizedUsage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    reasoning_tokens: int = 0
    source: str = "unknown"

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    @property
    def cache_hit_rate(self) -> float:
        """缓存命中率 = 缓存读 / 输入总量（prompt 含缓存读，口径统一）。"""
        return round(self.cache_read_tokens / self.prompt_tokens * 100, 1) if self.prompt_tokens > 0 else 0.0

    def to_openai_chat(self) -> dict:
        """转成 OpenAI chat 形态（网关内部标准形态，供日志/统计/下游客户端）。"""
        d = {
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
        }
        if self.cache_read_tokens or self.cache_write_tokens:
            d["prompt_tokens_details"] = {
                "cached_tokens": self.cache_read_tokens,
                "cache_write_tokens": self.cache_write_tokens,
            }
        if self.reasoning_tokens:
            d["completion_tokens_details"] = {"reasoning_tokens": self.reasoning_tokens}
        return d


def _to_int(v) -> int:
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


def _sub(d, *keys) -> dict:
    """安全取嵌套 dict：_sub(u, 'prompt_tokens_details') / _sub(u, 'a', 'b')。"""
    cur = d
    for k in keys:
        if not isinstance(cur, dict):
            return {}
        cur = cur.get(k)
        if cur is None:
            return {}
    return cur if isinstance(cur, dict) else {}


def normalize_usage(usage) -> NormalizedUsage:
    """把任意方言的 usage dict 归一化。非 dict / 空输入返回全零（source=unknown）。

    按字段特征提取而非硬性分支：prompt 取 prompt_tokens|input_tokens，
    completion 取 completion_tokens|output_tokens，缓存/思考按各家字段名逐一尝试。
    Anthropic 的 input_tokens 不含缓存 → 命中 Anthropic 缓存字段时把缓存并入 prompt。
    """
    if not isinstance(usage, dict) or not usage:
        return NormalizedUsage()
    nu = NormalizedUsage()

    nu.prompt_tokens = _to_int(usage.get("prompt_tokens")) or _to_int(usage.get("input_tokens"))
    nu.completion_tokens = _to_int(usage.get("completion_tokens")) or _to_int(usage.get("output_tokens"))

    # ── 缓存读：OpenAI chat → Responses → Anthropic 命名 ──
    nu.cache_read_tokens = (
        _to_int(_sub(usage, "prompt_tokens_details").get("cached_tokens"))
        or _to_int(_sub(usage, "input_tokens_details").get("cached_tokens"))
        or _to_int(usage.get("cache_read_tokens"))
        or _to_int(usage.get("cache_read_input_tokens"))
    )
    # ── 缓存写：chat details → Responses details/details → Anthropic 命名/新版结构 ──
    nu.cache_write_tokens = (
        _to_int(_sub(usage, "prompt_tokens_details").get("cache_creation_tokens"))
        or _to_int(_sub(usage, "prompt_tokens_details").get("cache_write_tokens"))
        or _to_int(_sub(usage, "input_tokens_details").get("cache_write_tokens"))
        or _to_int(_sub(usage, "input_tokens_details").get("cache_creation_tokens"))
        or _to_int(_sub(usage, "cache_creation_details").get("total_tokens"))
        or _to_int(usage.get("cache_creation_input_tokens"))
    )
    if not nu.cache_write_tokens:
        cc = _sub(usage, "cache_creation")
        nu.cache_write_tokens = _to_int(cc.get("ephemeral_5m_input_tokens")) + _to_int(cc.get("ephemeral_1h_input_tokens"))
    # ── 思考：chat details → Responses details → 顶层（DeepSeek 等） ──
    nu.reasoning_tokens = (
        _to_int(_sub(usage, "completion_tokens_details").get("reasoning_tokens"))
        or _to_int(_sub(usage, "output_tokens_details").get("reasoning_tokens"))
        or _to_int(usage.get("reasoning_tokens"))
    )

    # ── 方言标记 + Anthropic 口径修正（input_tokens 不含缓存 → 并入 prompt） ──
    if "cache_read_input_tokens" in usage or "cache_creation_input_tokens" in usage or "cache_creation" in usage:
        nu.source = "anthropic"
        nu.prompt_tokens = nu.prompt_tokens + nu.cache_read_tokens + nu.cache_write_tokens
    elif "input_tokens_details" in usage or "cache_creation_details" in usage or "input_tokens" in usage:
        nu.source = "openai_responses"
    elif "prompt_tokens" in usage or "completion_tokens" in usage:
        nu.source = "openai_chat"
    return nu
