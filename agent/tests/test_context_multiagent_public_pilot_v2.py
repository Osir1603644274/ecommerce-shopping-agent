from __future__ import annotations

import asyncio
from typing import Any

import pytest

from evaluation.context_multiagent_public_pilot_v2 import _CompletionsProxy


class _FakeCompletions:
    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> dict[str, Any]:
        self.requests.append(kwargs)
        return {"ok": True}


def test_adapter_disables_thinking_only_when_tools_are_present() -> None:
    fake = _FakeCompletions()
    proxy = _CompletionsProxy(fake)
    asyncio.run(proxy.create(model="deepseek-v4-flash", tools=[{"type": "function"}]))
    asyncio.run(proxy.create(model="deepseek-v4-flash", messages=[]))
    assert fake.requests[0]["extra_body"] == {"thinking": {"type": "disabled"}}
    assert "extra_body" not in fake.requests[1]


def test_adapter_preserves_forced_tool_choice_and_other_request_fields() -> None:
    fake = _FakeCompletions()
    proxy = _CompletionsProxy(fake)
    choice = {"type": "function", "function": {"name": "submit"}}
    asyncio.run(
        proxy.create(
            model="deepseek-v4-flash",
            tools=[{"type": "function"}],
            tool_choice=choice,
            temperature=0,
            max_tokens=2200,
        )
    )
    request = fake.requests[0]
    assert request["tool_choice"] == choice
    assert request["temperature"] == 0
    assert request["max_tokens"] == 2200


def test_adapter_rejects_conflicting_explicit_thinking_mode() -> None:
    fake = _FakeCompletions()
    proxy = _CompletionsProxy(fake)
    with pytest.raises(RuntimeError, match="conflicting thinking mode"):
        asyncio.run(
            proxy.create(
                tools=[{"type": "function"}],
                extra_body={"thinking": {"type": "enabled"}},
            )
        )
    assert fake.requests == []
