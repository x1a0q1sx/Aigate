"""Model-level capability inference shared by adapters, admin APIs and routing."""


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
