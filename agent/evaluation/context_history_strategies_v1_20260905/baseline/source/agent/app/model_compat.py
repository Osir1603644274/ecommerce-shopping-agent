from __future__ import annotations

from typing import Any


def supports_tool_choice(model: str, *, thinking_enabled: bool | None = None) -> bool:
    """Return whether the model accepts the OpenAI ``tool_choice`` field.

    DeepSeek V4 thinking models support tools but reject the entire
    ``tool_choice`` request parameter. Explicit non-thinking callers may force
    a named tool; callers that omit the mode keep the conservative default.
    Omitting it leaves tool selection in
    the provider's default auto mode; callers must still validate the returned
    tool name and arguments before executing or persisting anything.
    """

    return thinking_enabled is False or not model.strip().lower().startswith("deepseek-v4-")


def tool_choice_kwargs(model: str, choice: Any, *, thinking_enabled: bool | None = None) -> dict[str, Any]:
    """Build provider-compatible kwargs for an optional tool choice."""

    if not supports_tool_choice(model, thinking_enabled=thinking_enabled):
        return {}
    return {"tool_choice": choice}
