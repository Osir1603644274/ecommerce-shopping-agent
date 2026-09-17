from __future__ import annotations

from typing import Any


def supports_tool_choice(model: str) -> bool:
    """Return whether the model accepts the OpenAI ``tool_choice`` field.

    DeepSeek V4 thinking models support tools but reject the entire
    ``tool_choice`` request parameter. Omitting it leaves tool selection in
    the provider's default auto mode; callers must still validate the returned
    tool name and arguments before executing or persisting anything.
    """

    return not model.strip().lower().startswith("deepseek-v4-")


def tool_choice_kwargs(model: str, choice: Any) -> dict[str, Any]:
    """Build provider-compatible kwargs for an optional tool choice."""

    if not supports_tool_choice(model):
        return {}
    return {"tool_choice": choice}
