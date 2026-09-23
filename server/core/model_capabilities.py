"""Model-level capability inference shared by adapters, admin APIs and routing."""
from typing import List, Optional


def infer_reasoning_effort_support(api_type: str, model_id: str) -> bool | None:
    """Return a conservative initial value; admins can override it per model."""
    api = (api_type or "").lower()
    name = (model_id or "").lower()

    if api == "codex_responses":
        return True

    if api == "anthropic":
        if any(tag in name for tag in (
            "claude-3-7", "claude-opus-4", "claude-sonnet-4",
            "claude-haiku-4", "claude-4", "claude-5", "fable",
        )):
            return True
        return False

    # Aggregated OpenAI-compatible endpoints do not expose a capabilities document.
    # Keep this strict so an unsupported upstream does not receive a 400-prone field.
    return any(tag in name for tag in (
        "gpt-5", "o1", "o3", "o4", "deepseek-r", "deepseek-v4-pro",
        "reasoner", "thinking", "reasoning",
    ))


# v4.3 能力感知路由：模型名 → 输入模态的保守推断。
# 定位=**正向提示**（capability_source="inferred"，仅用于排序加分与展示），
# 绝不参与候选硬拦截——见 context_guard.media_known_modalities 的可信来源约束。
_VISION_NAME_TAGS = (
    "-vl", "vision", "4o", "omni", "kimi-latest", "gemini",
    "claude-3", "claude-opus-4", "claude-sonnet-4", "claude-haiku-4",
    "qwen-vl", "glm-4v", "glm-5v",
)


def infer_modalities(model_id: str) -> Optional[list]:
    """按模型名推断输入模态；无把握返回 None（=未知，不是"仅文本"）。"""
    name = (model_id or "").lower()
    mods = ["text"]
    hit = False
    if any(tag in name for tag in _VISION_NAME_TAGS):
        mods.append("image"); hit = True
    if "audio" in name or "omni" in name:
        mods.append("audio"); hit = True
    return mods if hit else None
