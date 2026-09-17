"""Transport mode resolver — wires Live/Record/Replay into the tool call chain.

This module provides a single entry point (get_tool_transport) that returns the
tool-call wrapper dictated by settings.agent_tool_transport_mode:

  - "live"   → direct call_tool (default, backward-compatible)
  - "record" → calls real tool + writes cleansed JSONL
  - "replay" → reads only from recorded JSONL, never touches backend

Usage (drop-in replacement for call_tool):
    transport = get_tool_transport(run_id="run-abc", context_pack_hash="...")
    trace = await transport(tool_name, arguments)

Each call to get_tool_transport creates a fresh transport instance — no global
state is shared across requests.  Live mode imports call_tool fresh so test-time
patches on app.tools.call_tool take effect.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Any

from .settings import settings
from .tool_transport import RecordTransport, ReplayTransport, validate_execution_context

if TYPE_CHECKING:
    from .tool_execution_v2 import ToolExecutionContext

logger = logging.getLogger(__name__)

TOOL_TRANSPORT_MODES = frozenset(
    {"live", "record", "replay", "mcp_in_process_readonly"}
)


class ToolTransportConfigurationError(ValueError):
    """Stable, non-sensitive transport configuration failure."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


_RESOLVER_IDENTITY_KEYS = frozenset({"mode", "source"})
_MCP_IDENTITY_KEYS = frozenset({
    "mode", "source", "protocolVersion", "serverName", "serverVersion",
})
_TRANSPORT_IDENTITY_SOURCES = frozenset({
    "server_resolver", "server_record_shadow",
})


def _publish_transport_identity(
    wrapper: Any,
    identity: Mapping[str, str],
) -> Any:
    """Attach a bounded, immutable identity to the server-created wrapper."""

    wrapper.transport_identity = MappingProxyType(dict(identity))
    return wrapper


def validate_tool_transport_identity(raw: Any) -> dict[str, str]:
    """Validate and copy a bounded identity projection."""

    if not isinstance(raw, Mapping):
        raise ToolTransportConfigurationError("transport_identity_missing")
    identity = dict(raw)
    mode = identity.get("mode")
    source = identity.get("source")
    if not isinstance(mode, str) or not isinstance(source, str):
        raise ToolTransportConfigurationError("transport_identity_shape_invalid")
    expected_keys = (
        _MCP_IDENTITY_KEYS
        if mode == "mcp_in_process_readonly"
        else _RESOLVER_IDENTITY_KEYS
    )
    if set(identity) != set(expected_keys):
        raise ToolTransportConfigurationError("transport_identity_shape_invalid")
    if mode not in TOOL_TRANSPORT_MODES:
        raise ToolTransportConfigurationError("transport_identity_mode_invalid")
    if source not in _TRANSPORT_IDENTITY_SOURCES:
        raise ToolTransportConfigurationError("transport_identity_source_invalid")
    for key, value in identity.items():
        if not isinstance(value, str) or not value.strip() or len(value) > 128:
            raise ToolTransportConfigurationError("transport_identity_shape_invalid")
    if mode == "mcp_in_process_readonly":
        from .mcp_gateway import (
            MCP_PROTOCOL_VERSION,
            MCP_SERVER_NAME,
            MCP_SERVER_VERSION,
        )

        if identity != {
            "mode": mode,
            "source": source,
            "protocolVersion": MCP_PROTOCOL_VERSION,
            "serverName": MCP_SERVER_NAME,
            "serverVersion": MCP_SERVER_VERSION,
        }:
            raise ToolTransportConfigurationError("transport_identity_drift")
    return identity


def get_tool_transport_identity(transport: Any) -> dict[str, str]:
    """Read the identity published by a resolved wrapper.

    Debug consumers must use this server-published projection rather than
    reconstructing identity from settings, tool names, or domain state.
    """

    raw = getattr(transport, "transport_identity", None)
    return validate_tool_transport_identity(raw)


def _transport_mode() -> str:
    mode = getattr(settings, "agent_tool_transport_mode", "live")
    if not isinstance(mode, str):
        raise ToolTransportConfigurationError("unknown_tool_transport_mode")
    normalized = mode.strip().lower()
    if normalized not in TOOL_TRANSPORT_MODES:
        raise ToolTransportConfigurationError("unknown_tool_transport_mode")
    return normalized


def _validate_mcp_opt_in() -> None:
    enabled = getattr(settings, "agent_mcp_readonly_enabled", False)
    mcp_mode = getattr(settings, "agent_mcp_transport_mode", "disabled")
    if type(enabled) is not bool or enabled is not True:
        raise ToolTransportConfigurationError("mcp_readonly_not_enabled")
    if mcp_mode != "in_process_readonly":
        raise ToolTransportConfigurationError("mcp_transport_mode_mismatch")


def reset_transport_cache() -> None:
    """No-op — kept for backward compatibility with tests.

    Transport instances are no longer cached globally; each call to
    get_tool_transport creates a fresh instance.
    """


def get_tool_transport(
    run_id: str = "",
    context_pack_hash: str | None = None,
    recording_path: Path | None = None,
    replay_path: Path | None = None,
):
    """Return the tool-call wrapper for the current transport mode.

    In live mode each invocation imports call_tool fresh so that test-time
    patches on app.tools.call_tool take effect.

    In record/replay mode a new instance is created each time, scoped to
    the given run_id and context_pack_hash.  No global state is shared
    across concurrent requests.
    """

    mode = _transport_mode()

    if mode == "mcp_in_process_readonly":
        _validate_mcp_opt_in()
        from .mcp_client import MCPReadonlyClient, MCPReadonlyClientError
        from .mcp_gateway import (
            MCP_PROTOCOL_VERSION,
            MCP_SERVER_NAME,
            MCP_SERVER_VERSION,
            READONLY_TOOL_NAMES,
        )

        async def mcp_wrapper(
            tool_name: str,
            arguments: dict[str, Any],
            *,
            execution_context: "ToolExecutionContext | None" = None,
        ):
            if tool_name not in READONLY_TOOL_NAMES:
                raise MCPReadonlyClientError("mcp_tool_not_allowlisted")
            async with MCPReadonlyClient(
                enabled=True,
                transport_mode="in_process_readonly",
            ) as client:
                if execution_context is None:
                    return await client.call_tool(tool_name, arguments)
                return await client.call_tool(
                    tool_name,
                    arguments,
                    execution_context=execution_context,
                )

        return _publish_transport_identity(mcp_wrapper, {
            "mode": mode,
            "source": "server_resolver",
            "protocolVersion": MCP_PROTOCOL_VERSION,
            "serverName": MCP_SERVER_NAME,
            "serverVersion": MCP_SERVER_VERSION,
        })

    # Live mode: never cache — tests patch app.tools.call_tool
    if mode == "live":
        from .tools import call_tool as _live

        async def live_wrapper(
            tool_name: str,
            arguments: dict[str, Any],
            *,
            execution_context: "ToolExecutionContext | None" = None,
        ):
            checked_context = validate_execution_context(
                tool_name,
                arguments,
                execution_context,
            )
            if checked_context is None:
                return await _live(tool_name, arguments)
            return await _live(
                tool_name,
                arguments,
                execution_context=checked_context,
            )
        return _publish_transport_identity(live_wrapper, {
            "mode": mode,
            "source": "server_resolver",
        })

    from .tools import call_tool as _live_call_tool

    if mode == "record":
        rec_path = recording_path or Path(
            getattr(settings, "agent_tool_transport_record_path", "./recordings/tool_calls.jsonl")
        )
        effective_run_id = run_id or getattr(settings, "agent_tool_transport_run_id", "default-run")
        transport = RecordTransport(
            output_path=rec_path,
            run_id=effective_run_id,
            context_pack_hash=context_pack_hash,
        )

        async def record_wrapper(
            tool_name: str,
            arguments: dict[str, Any],
            *,
            execution_context: "ToolExecutionContext | None" = None,
        ):
            return await transport(
                tool_name,
                arguments,
                _live_call_tool,
                execution_context=execution_context,
            )
        record_wrapper.finalize = transport.finalize  # type: ignore[attr-defined]
        return _publish_transport_identity(record_wrapper, {
            "mode": mode,
            "source": "server_resolver",
        })

    if mode == "replay":
        rep_path = replay_path or Path(
            getattr(settings, "agent_tool_transport_replay_path", "./recordings/tool_calls.jsonl")
        )
        strict = getattr(settings, "agent_tool_transport_replay_strict", True)
        transport_obj = ReplayTransport(replay_path=rep_path, strict=strict)

        async def replay_wrapper(
            tool_name: str,
            arguments: dict[str, Any],
            *,
            execution_context: "ToolExecutionContext | None" = None,
        ):
            return await transport_obj(
                tool_name,
                arguments,
                _live_call_tool,
                execution_context=execution_context,
            )
        return _publish_transport_identity(replay_wrapper, {
            "mode": mode,
            "source": "server_resolver",
        })

    raise AssertionError(f"unhandled tool transport mode: {mode}")


def get_record_transport(
    recording_path: Path,
    run_id: str,
    context_pack_hash: str | None = None,
):
    """Return a live tool-call wrapper that ALSO records every call.

    Used by the V2 Record→Replay shadow: the authoritative V1 run calls real
    tools through this wrapper so the resulting recording can be replayed
    strictly into the V2 graph.  Behavior is identical to live mode; recording
    is a pure side effect.
    """
    from .tools import call_tool as _live_call_tool

    transport = RecordTransport(
        output_path=recording_path,
        run_id=run_id,
        context_pack_hash=context_pack_hash,
    )

    async def record_wrapper(
        tool_name: str,
        arguments: dict[str, Any],
        *,
        execution_context: "ToolExecutionContext | None" = None,
    ):
        return await transport(
            tool_name,
            arguments,
            _live_call_tool,
            execution_context=execution_context,
        )

    record_wrapper.finalize = transport.finalize  # type: ignore[attr-defined]
    record_wrapper._record_transport = transport  # type: ignore[attr-defined]
    return _publish_transport_identity(record_wrapper, {
        "mode": "record",
        "source": "server_record_shadow",
    })
