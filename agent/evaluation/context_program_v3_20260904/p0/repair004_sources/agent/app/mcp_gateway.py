"""Default-off, in-process MCP read-only gateway.

This is an embedding/test seam only.  It deliberately does not create an HTTP
listener and it does not enter the existing Harness or tool transport resolver.
The two published tools reuse ``app.tools.call_tool`` after validating against
the existing ``TOOL_SCHEMAS`` contract; this module owns no second business
implementation.
"""

from __future__ import annotations

import copy
import logging
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import TYPE_CHECKING, Annotated, Any, Iterator

from jsonschema import Draft202012Validator
from mcp.server.mcpserver import Context as MCPContext
from mcp.server import MCPServer
from mcp.types import CallToolResult, TextContent, ToolAnnotations
from pydantic import WithJsonSchema

from .schemas import ToolTrace
from .settings import settings

if TYPE_CHECKING:
    from .tool_execution_v2 import ToolExecutionContext

logger = logging.getLogger(__name__)

MCP_PROTOCOL_VERSION = "2026-07-28"
MCP_SERVER_NAME = "ecommerce-readonly-gateway"
MCP_SERVER_VERSION = "1.0.0"
MCP_TRANSPORT_MODES = frozenset({"disabled", "in_process_readonly"})
READONLY_TOOL_NAMES = ("search_products", "get_product_details")
_RUNTIME_CONTRACT_BINDINGS: dict[str, object] | None = None


@dataclass(slots=True)
class _MCPExecutionDispatchLedger:
    """Private in-process bridge; never serialized into MCP arguments/results."""

    execution_context: "ToolExecutionContext"
    dispatched: bool = False


_mcp_execution_ledger: ContextVar[_MCPExecutionDispatchLedger | None] = ContextVar(
    "mcp_execution_ledger",
    default=None,
)


@contextmanager
def bind_mcp_execution_context(
    execution_context: "ToolExecutionContext | None",
) -> Iterator[_MCPExecutionDispatchLedger | None]:
    """Bind a V2 context only across the in-process SDK/server seam.

    The MCP function schema remains unchanged.  If this private bridge is lost,
    the client observes an unconsumed ledger and rejects UNKNOWN rather than
    claiming any external exactly-once behavior.
    """

    if execution_context is None:
        yield None
        return
    ledger = _MCPExecutionDispatchLedger(execution_context)
    token = _mcp_execution_ledger.set(ledger)
    try:
        yield ledger
    finally:
        _mcp_execution_ledger.reset(token)


def _consume_mcp_execution_context(
    tool_name: str,
    arguments: object,
) -> "ToolExecutionContext | None":
    ledger = _mcp_execution_ledger.get()
    if ledger is None:
        return None
    from .tool_transport import validate_execution_context

    checked = validate_execution_context(
        tool_name,
        arguments,
        ledger.execution_context,
    )
    ledger.dispatched = True
    return checked


class MCPGatewayError(RuntimeError):
    """Stable, non-sensitive gateway failure."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


class MCPGatewayConfigurationError(MCPGatewayError):
    pass


def _runtime_contract_registry() -> dict[str, object]:
    """Return the live registry after checking its server-owned bindings.

    The values in ``_RUNTIME_CONTRACT_BINDINGS`` are references to the
    existing registry objects, not copied contract declarations.  A removal
    or replacement therefore fails closed instead of leaving a ghost MCP
    capability behind.
    """

    global _RUNTIME_CONTRACT_BINDINGS
    try:
        # Importing the domains package first preserves the repository's
        # existing control/domain initialization order without moving a
        # RuntimeToolContract definition into this MCP seam.
        from . import domains as _domains  # noqa: F401
        from .control.validation_contracts import (
            RUNTIME_TOOL_CONTRACTS,
            RuntimeToolContract,
        )
    except Exception:
        raise MCPGatewayConfigurationError(
            "mcp_runtime_contract_unavailable"
        ) from None

    if _RUNTIME_CONTRACT_BINDINGS is None:
        bindings = {
            name: RUNTIME_TOOL_CONTRACTS.get(name)
            for name in READONLY_TOOL_NAMES
        }
        if any(
            not isinstance(value, RuntimeToolContract)
            or not value.expected_outputs
            for value in bindings.values()
        ):
            raise MCPGatewayConfigurationError("mcp_runtime_contract_missing")
        _RUNTIME_CONTRACT_BINDINGS = bindings

    for name, bound in _RUNTIME_CONTRACT_BINDINGS.items():
        current = RUNTIME_TOOL_CONTRACTS.get(name)
        if current is None:
            raise MCPGatewayConfigurationError("mcp_runtime_contract_missing")
        if current is not bound or not isinstance(current, RuntimeToolContract):
            raise MCPGatewayConfigurationError("mcp_runtime_contract_drift")
        if not current.expected_outputs:
            raise MCPGatewayConfigurationError("mcp_runtime_contract_invalid")
    return RUNTIME_TOOL_CONTRACTS


def validate_readonly_runtime_contracts() -> tuple[str, ...]:
    """Validate the complete schema/runtime/output binding before exposure."""

    _runtime_contract_registry()
    for name in READONLY_TOOL_NAMES:
        _readonly_tool_definition(name)
    if not ToolTrace.model_json_schema(by_alias=True):
        raise MCPGatewayConfigurationError("mcp_tool_trace_schema_invalid")
    return READONLY_TOOL_NAMES


def _configured_mode(
    *,
    enabled: bool | None = None,
    transport_mode: str | None = None,
) -> tuple[bool, str]:
    active = (
        settings.agent_mcp_readonly_enabled
        if enabled is None
        else enabled
    )
    mode = (
        settings.agent_mcp_transport_mode
        if transport_mode is None
        else transport_mode
    )
    if type(active) is not bool:
        raise MCPGatewayConfigurationError("mcp_invalid_enabled_flag")
    if not isinstance(mode, str) or mode.strip().lower() not in MCP_TRANSPORT_MODES:
        raise MCPGatewayConfigurationError("mcp_invalid_transport_mode")
    return active, mode.strip().lower()


def mcp_gateway_is_enabled(
    *,
    enabled: bool | None = None,
    transport_mode: str | None = None,
) -> bool:
    active, mode = _configured_mode(
        enabled=enabled,
        transport_mode=transport_mode,
    )
    return active and mode == "in_process_readonly"


def _readonly_tool_definition(tool_name: str) -> dict[str, Any]:
    from . import tools as app_tools

    matches = [
        item
        for item in app_tools.TOOL_SCHEMAS
        if item.get("function", {}).get("name") == tool_name
    ]
    if len(matches) != 1:
        raise MCPGatewayConfigurationError("mcp_tool_schema_missing")
    function = matches[0].get("function")
    if not isinstance(function, dict):
        raise MCPGatewayConfigurationError("mcp_tool_schema_invalid")
    parameters = function.get("parameters")
    if not isinstance(parameters, dict):
        raise MCPGatewayConfigurationError("mcp_tool_schema_invalid")
    return copy.deepcopy(function)


def readonly_tool_schema(tool_name: str) -> dict[str, Any]:
    """Return a detached existing OpenAI-style function schema projection."""

    if tool_name not in READONLY_TOOL_NAMES:
        raise MCPGatewayError("mcp_tool_not_allowlisted")
    return _readonly_tool_definition(tool_name)["parameters"]


def _normalize_schema_for_mcp_comparison(value: object) -> object:
    """Ignore SDK presentation metadata while preserving contract semantics."""

    if isinstance(value, dict):
        normalized = {
            key: _normalize_schema_for_mcp_comparison(item)
            for key, item in value.items()
            if key not in {"title", "default", "description"}
        }
        if normalized.get("type") == "object":
            normalized.setdefault("additionalProperties", False)
        return normalized
    if isinstance(value, list):
        return [_normalize_schema_for_mcp_comparison(item) for item in value]
    return value


def mcp_input_schema_matches(tool_name: str, advertised: object) -> bool:
    """Compare public SDK output with the existing input contract."""

    return _normalize_schema_for_mcp_comparison(advertised) == (
        _normalize_schema_for_mcp_comparison(readonly_tool_schema(tool_name))
    )


def _input_field_annotation(tool_name: str, field_name: str) -> object:
    schema = readonly_tool_schema(tool_name)
    properties = schema.get("properties")
    if not isinstance(properties, dict) or not isinstance(
        properties.get(field_name), dict
    ):
        raise MCPGatewayConfigurationError("mcp_tool_schema_invalid")
    # The gateway still validates the complete raw argument object against
    # TOOL_SCHEMAS.  WithJsonSchema makes the public add_tool-generated
    # catalog advertise the same field contract without using SDK internals.
    return Annotated[
        Any,
        WithJsonSchema(copy.deepcopy(properties[field_name]), mode="validation"),
    ]


_SEARCH_QUERY = _input_field_annotation("search_products", "query")
_SEARCH_CATEGORY = _input_field_annotation("search_products", "category")
_SEARCH_BRAND = _input_field_annotation("search_products", "brand")
_SEARCH_MIN_PRICE = _input_field_annotation("search_products", "minPriceMinor")
_SEARCH_MAX_PRICE = _input_field_annotation("search_products", "maxPriceMinor")
_SEARCH_LIMIT = _input_field_annotation("search_products", "limit")
_SEARCH_REQUIREMENTS = _input_field_annotation("search_products", "requirements")
_DETAIL_PRODUCT_IDS = _input_field_annotation(
    "get_product_details", "productIds"
)


def _invalid_arguments_trace(tool_name: str, schema: dict[str, Any], arguments: object) -> ToolTrace:
    if not isinstance(arguments, dict):
        return ToolTrace(
            tool=tool_name,
            ok=False,
            detail={"code": "mcp_invalid_arguments", "reason": "object_required"},
        )
    errors = sorted(
        Draft202012Validator(schema).iter_errors(arguments),
        key=lambda error: (list(error.path), error.validator or ""),
    )
    if not errors:
        return ToolTrace(tool=tool_name, ok=True)
    error = errors[0]
    return ToolTrace(
        tool=tool_name,
        ok=False,
        detail={
            "code": "mcp_invalid_arguments",
            "keyword": error.validator or "schema",
            "path": [str(item) for item in error.path],
        },
    )


def _request_arguments(context: MCPContext, fallback: dict[str, Any]) -> dict[str, Any]:
    request_context = getattr(context, "request_context", None)
    params = getattr(request_context, "params", None)
    arguments = (
        params.get("arguments")
        if isinstance(params, dict)
        else getattr(params, "arguments", None)
    )
    return arguments if isinstance(arguments, dict) else fallback


async def _dispatch_readonly_tool(
    tool_name: str,
    arguments: object,
) -> ToolTrace | CallToolResult:
    schema = readonly_tool_schema(tool_name)
    invalid = _invalid_arguments_trace(tool_name, schema, arguments)
    if not invalid.ok:
        return _failed_tool_result(tool_name, "mcp_invalid_arguments")
    from . import tools as app_tools

    try:
        execution_context = _consume_mcp_execution_context(tool_name, arguments)
        if execution_context is None:
            trace = await app_tools.call_tool(tool_name, arguments)
        else:
            trace = await app_tools.call_tool(
                tool_name,
                arguments,
                execution_context=execution_context,
            )
    except Exception:
        # Do not let a backend exception expose its message, query, token, or
        # credential through MCP content or logs.
        logger.warning("mcp_gateway_tool_failure tool=%s code=tool_execution_error", tool_name)
        return _failed_tool_result(tool_name, "mcp_tool_execution_error")
    if not isinstance(trace, ToolTrace) or trace.tool != tool_name:
        raise MCPGatewayError("mcp_tool_trace_invalid")
    if not trace.ok:
        logger.info("mcp_gateway_tool_failure tool=%s outcome=tool_failed", tool_name)
        return _failed_tool_result(tool_name, "mcp_tool_failed")
    return trace


def _failed_tool_result(tool_name: str, code: str) -> CallToolResult:
    """Build a public MCP error result with only a bounded stable trace."""

    trace = ToolTrace(tool=tool_name, ok=False, detail={"code": code})
    return CallToolResult(
        content=[TextContent(type="text", text="mcp_tool_failed")],
        structuredContent=trace.model_dump(by_alias=True),
        isError=True,
    )


async def _search_products(
    query: _SEARCH_QUERY,
    category: _SEARCH_CATEGORY,
    brand: _SEARCH_BRAND = None,
    minPriceMinor: _SEARCH_MIN_PRICE = None,
    maxPriceMinor: _SEARCH_MAX_PRICE = None,
    limit: _SEARCH_LIMIT = None,
    requirements: _SEARCH_REQUIREMENTS = None,
    ctx: MCPContext = None,
) -> ToolTrace:
    fallback = {
        "query": query,
        "category": category,
        "brand": brand,
        "minPriceMinor": minPriceMinor,
        "maxPriceMinor": maxPriceMinor,
        "limit": limit,
        "requirements": requirements,
    }
    fallback = {key: value for key, value in fallback.items() if value is not None}
    return await _dispatch_readonly_tool(
        "search_products",
        _request_arguments(ctx, fallback),
    )


async def _get_product_details(
    productIds: _DETAIL_PRODUCT_IDS,
    ctx: MCPContext = None,
) -> ToolTrace:
    fallback = {"productIds": productIds}
    return await _dispatch_readonly_tool(
        "get_product_details",
        _request_arguments(ctx, fallback),
    )


def _register_tool(server: MCPServer, tool_name: str) -> None:
    function = _readonly_tool_definition(tool_name)
    handlers = {
        "search_products": _search_products,
        "get_product_details": _get_product_details,
    }
    handler = handlers[tool_name]
    annotations = ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=True,
    )
    # MCPServer.add_tool and the public CallToolResult/ToolTrace types are the
    # SDK seam.  The private Tool model was previously used only to overwrite
    # generated parameters and is intentionally not part of this contract.
    server.add_tool(
        handler,
        name=tool_name,
        description=function["description"],
        annotations=annotations,
        structured_output=True,
    )


def build_readonly_mcp_server(
    *,
    enabled: bool | None = None,
    transport_mode: str | None = None,
) -> MCPServer:
    """Build an in-process server; default configuration publishes no tools."""

    active, mode = _configured_mode(
        enabled=enabled,
        transport_mode=transport_mode,
    )
    server = MCPServer(
        name=MCP_SERVER_NAME,
        version=MCP_SERVER_VERSION,
        instructions="Read-only ecommerce catalog gateway; no transactions or previews.",
    )
    if active and mode == "in_process_readonly":
        validate_readonly_runtime_contracts()
        for name in READONLY_TOOL_NAMES:
            _register_tool(server, name)
    return server


# Capture the server-owned contract object identities as soon as the module is
# importable.  If repository initialization is still in a cycle, the first
# enabled build retries this check; no capability is published in the interim.
try:
    _runtime_contract_registry()
except MCPGatewayConfigurationError:
    pass


__all__ = [
    "MCPGatewayConfigurationError",
    "MCPGatewayError",
    "MCP_PROTOCOL_VERSION",
    "MCP_SERVER_NAME",
    "MCP_SERVER_VERSION",
    "MCP_TRANSPORT_MODES",
    "READONLY_TOOL_NAMES",
    "build_readonly_mcp_server",
    "mcp_gateway_is_enabled",
    "mcp_input_schema_matches",
    "readonly_tool_schema",
    "validate_readonly_runtime_contracts",
]
