"""Fail-closed adapter for the Day4 in-process MCP read-only gateway."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from jsonschema import Draft202012Validator
from mcp import Client as MCPClient
from mcp.server import MCPServer
from mcp.types import ToolAnnotations

from .mcp_gateway import (
    MCP_PROTOCOL_VERSION,
    MCP_SERVER_NAME,
    MCP_SERVER_VERSION,
    MCPGatewayError,
    READONLY_TOOL_NAMES,
    build_readonly_mcp_server,
    mcp_input_schema_matches,
    mcp_gateway_is_enabled,
    readonly_tool_schema,
    validate_readonly_runtime_contracts,
)
from .schemas import ToolTrace

if TYPE_CHECKING:
    from .tool_execution_v2 import ToolExecutionContext

logger = logging.getLogger(__name__)

MCP_CALL_OUTCOMES = Literal[
    "ok",
    "tool_failed",
    "invalid_arguments",
    "unknown_tool",
    "protocol_error",
    "timeout",
    "cancelled",
    "close_failed",
]


@dataclass(frozen=True, slots=True)
class MCPObservation:
    event: Literal["discover", "tools_list", "tool_call", "close"]
    protocol_version: str
    server_name: str
    server_version: str
    tool_name: str | None = None
    tool_names: tuple[str, ...] = ()
    outcome: str | None = None

    def as_dict(self) -> dict[str, str]:
        value = {
            "event": self.event,
            "protocolVersion": self.protocol_version,
            "serverName": self.server_name,
            "serverVersion": self.server_version,
        }
        if self.tool_name is not None:
            value["toolName"] = self.tool_name
        if self.tool_names:
            value["toolNames"] = ",".join(self.tool_names)
        if self.outcome is not None:
            value["outcome"] = self.outcome
        return value


class MCPReadonlyClientError(MCPGatewayError):
    pass


class MCPReadonlyClient:
    """One-use official SDK client with catalog and result revalidation."""

    def __init__(
        self,
        server: MCPServer | None = None,
        *,
        enabled: bool | None = None,
        transport_mode: str | None = None,
        timeout_seconds: float = 5.0,
        max_observations: int = 32,
    ) -> None:
        if not mcp_gateway_is_enabled(
            enabled=enabled,
            transport_mode=transport_mode,
        ):
            raise MCPReadonlyClientError("mcp_disabled")
        if type(timeout_seconds) not in {int, float} or timeout_seconds <= 0:
            raise MCPReadonlyClientError("mcp_invalid_timeout")
        if type(max_observations) is not int or max_observations < 1:
            raise MCPReadonlyClientError("mcp_invalid_observation_limit")
        self._server = server or build_readonly_mcp_server(
            enabled=True,
            transport_mode="in_process_readonly",
        )
        self._timeout_seconds = float(timeout_seconds)
        self._max_observations = max_observations
        self._client: MCPClient | None = None
        self._observations: list[MCPObservation] = []

    @property
    def observations(self) -> tuple[dict[str, str], ...]:
        return tuple(item.as_dict() for item in self._observations)

    def _record(
        self,
        event: Literal["discover", "tools_list", "tool_call", "close"],
        *,
        tool_name: str | None = None,
        tool_names: tuple[str, ...] = (),
        outcome: str | None = None,
    ) -> None:
        self._observations.append(
            MCPObservation(
                event=event,
                protocol_version=MCP_PROTOCOL_VERSION,
                server_name=MCP_SERVER_NAME,
                server_version=MCP_SERVER_VERSION,
                tool_name=tool_name,
                tool_names=tool_names,
                outcome=outcome,
            )
        )
        del self._observations[:-self._max_observations]

    async def _close_sdk_client(self, client: MCPClient) -> bool:
        """Close without letting SDK errors rewrite the caller's exception."""

        try:
            await client.__aexit__(None, None, None)
        except Exception:
            self._record("close", outcome="close_failed")
            logger.warning("mcp_client_close_failed code=mcp_close_failed")
            return False
        return True

    async def __aenter__(self) -> "MCPReadonlyClient":
        if self._client is not None:
            raise MCPReadonlyClientError("mcp_client_reuse_forbidden")
        client = MCPClient(
            self._server,
            mode="auto",
            read_timeout_seconds=self._timeout_seconds,
        )
        try:
            await client.__aenter__()
            if client.protocol_version != MCP_PROTOCOL_VERSION:
                raise MCPReadonlyClientError("mcp_protocol_version_mismatch")
            server_info = client.server_info
            if (
                server_info is None
                or server_info.name != MCP_SERVER_NAME
                or server_info.version != MCP_SERVER_VERSION
            ):
                raise MCPReadonlyClientError("mcp_server_identity_mismatch")
            self._client = client
            self._record("discover", outcome="ok")
            await self._validate_catalog()
            return self
        except asyncio.CancelledError:
            self._client = None
            await self._close_sdk_client(client)
            raise MCPReadonlyClientError("mcp_discover_cancelled") from None
        except MCPReadonlyClientError:
            self._client = None
            await self._close_sdk_client(client)
            raise
        except Exception:
            self._client = None
            await self._close_sdk_client(client)
            raise MCPReadonlyClientError("mcp_discover_failed") from None

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
        client = self._client
        self._client = None
        if client is None:
            return False
        close_succeeded = await self._close_sdk_client(client)
        if not close_succeeded and exc_type is None and exc is None:
            raise MCPReadonlyClientError("mcp_close_failed") from None
        # The body owns its exception.  The SDK must never receive it because
        # its task group may wrap it in an ExceptionGroup.
        return False

    async def _validate_catalog(self) -> None:
        assert self._client is not None
        try:
            expected_names = validate_readonly_runtime_contracts()
        except MCPGatewayError as exc:
            raise MCPReadonlyClientError(exc.code) from None
        result = await self._client.list_tools()
        tools = list(result.tools)
        if sorted(tool.name for tool in tools) != sorted(expected_names):
            raise MCPReadonlyClientError("mcp_tool_catalog_mismatch")
        expected_output_schema = ToolTrace.model_json_schema(by_alias=True)
        for tool in tools:
            if not mcp_input_schema_matches(tool.name, tool.input_schema):
                raise MCPReadonlyClientError("mcp_tool_input_schema_mismatch")
            if tool.output_schema != expected_output_schema:
                raise MCPReadonlyClientError("mcp_tool_output_schema_mismatch")
            annotations = tool.annotations
            if not isinstance(annotations, ToolAnnotations):
                raise MCPReadonlyClientError("mcp_tool_annotations_missing")
            if (
                annotations.read_only_hint is not True
                or annotations.destructive_hint is not False
                or annotations.idempotent_hint is not True
            ):
                raise MCPReadonlyClientError("mcp_tool_annotations_invalid")
        self._record(
            "tools_list",
            tool_names=tuple(sorted(expected_names)),
            outcome="allowlist_verified",
        )

    @staticmethod
    def _invalid_argument_code(tool_name: str, arguments: object) -> str | None:
        if not isinstance(arguments, dict):
            return "mcp_invalid_arguments"
        errors = list(
            Draft202012Validator(readonly_tool_schema(tool_name)).iter_errors(arguments)
        )
        return "mcp_invalid_arguments" if errors else None

    async def call_tool(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        execution_context: "ToolExecutionContext | None" = None,
    ) -> ToolTrace:
        from .tool_transport import (
            ToolExecutionContextError,
            validate_execution_context,
        )

        try:
            checked_context = validate_execution_context(
                tool_name,
                arguments,
                execution_context,
            )
        except ToolExecutionContextError as exc:
            self._record("tool_call", tool_name=tool_name, outcome="protocol_error")
            raise MCPReadonlyClientError(exc.code) from None
        try:
            validate_readonly_runtime_contracts()
        except MCPGatewayError as exc:
            self._record("tool_call", tool_name=tool_name, outcome="protocol_error")
            raise MCPReadonlyClientError(exc.code) from None
        if tool_name not in READONLY_TOOL_NAMES:
            self._record("tool_call", outcome="unknown_tool")
            raise MCPReadonlyClientError("mcp_tool_not_allowlisted")
        invalid_code = self._invalid_argument_code(tool_name, arguments)
        if invalid_code is not None:
            self._record(
                "tool_call",
                tool_name=tool_name,
                outcome="invalid_arguments",
            )
            raise MCPReadonlyClientError(invalid_code)
        client = self._client
        if client is None:
            raise MCPReadonlyClientError("mcp_client_not_connected")
        from .mcp_gateway import bind_mcp_execution_context

        ledger = None
        try:
            with bind_mcp_execution_context(checked_context) as ledger:
                result = await asyncio.wait_for(
                    client.call_tool(tool_name, arguments),
                    timeout=self._timeout_seconds,
                )
        except asyncio.TimeoutError:
            self._record("tool_call", tool_name=tool_name, outcome="timeout")
            if checked_context is not None and (ledger is None or not ledger.dispatched):
                raise MCPReadonlyClientError("mcp_execution_context_unknown") from None
            raise MCPReadonlyClientError("mcp_call_timeout") from None
        except asyncio.CancelledError:
            self._record("tool_call", tool_name=tool_name, outcome="cancelled")
            if checked_context is not None and (ledger is None or not ledger.dispatched):
                raise MCPReadonlyClientError("mcp_execution_context_unknown") from None
            raise MCPReadonlyClientError("mcp_call_cancelled") from None
        except Exception:
            self._record("tool_call", tool_name=tool_name, outcome="protocol_error")
            if checked_context is not None and (ledger is None or not ledger.dispatched):
                raise MCPReadonlyClientError("mcp_execution_context_unknown") from None
            raise MCPReadonlyClientError("mcp_call_failed") from None
        if checked_context is not None and (ledger is None or not ledger.dispatched):
            self._record("tool_call", tool_name=tool_name, outcome="protocol_error")
            raise MCPReadonlyClientError("mcp_execution_context_unknown")
        structured = result.structured_content
        if result.is_error:
            if isinstance(structured, dict):
                try:
                    failed_trace = ToolTrace.model_validate(structured)
                except Exception:
                    failed_trace = None
                if (
                    failed_trace is not None
                    and failed_trace.tool == tool_name
                    and failed_trace.ok is False
                ):
                    self._record(
                        "tool_call",
                        tool_name=tool_name,
                        outcome="tool_failed",
                    )
                    raise MCPReadonlyClientError("mcp_tool_failed")
                if (
                    failed_trace is not None
                    and failed_trace.tool == tool_name
                    and failed_trace.ok is True
                ):
                    self._record(
                        "tool_call",
                        tool_name=tool_name,
                        outcome="protocol_error",
                    )
                    raise MCPReadonlyClientError("mcp_result_ok_mismatch")
            self._record("tool_call", tool_name=tool_name, outcome="protocol_error")
            raise MCPReadonlyClientError("mcp_result_error")
        if not isinstance(structured, dict):
            self._record("tool_call", tool_name=tool_name, outcome="protocol_error")
            raise MCPReadonlyClientError("mcp_structured_result_missing")
        try:
            trace = ToolTrace.model_validate(structured)
        except Exception:
            self._record("tool_call", tool_name=tool_name, outcome="protocol_error")
            raise MCPReadonlyClientError("mcp_tool_trace_invalid") from None
        if trace.tool != tool_name:
            self._record("tool_call", tool_name=tool_name, outcome="protocol_error")
            raise MCPReadonlyClientError("mcp_tool_trace_identity_mismatch")
        if trace.ok is False:
            self._record("tool_call", tool_name=tool_name, outcome="protocol_error")
            raise MCPReadonlyClientError("mcp_result_ok_mismatch")
        self._record(
            "tool_call",
            tool_name=tool_name,
            outcome="ok",
        )
        return trace


__all__ = [
    "MCPObservation",
    "MCPReadonlyClient",
    "MCPReadonlyClientError",
]
