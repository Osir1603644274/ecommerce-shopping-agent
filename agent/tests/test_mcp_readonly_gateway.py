import asyncio
import importlib.metadata
import tomllib
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from mcp import Client
from mcp.server import MCPServer
from mcp.types import ToolAnnotations

from app import tools as app_tools
from app.mcp_client import MCPReadonlyClient, MCPReadonlyClientError
from app.mcp_gateway import (
    MCPGatewayConfigurationError,
    MCP_PROTOCOL_VERSION,
    MCP_SERVER_NAME,
    MCP_SERVER_VERSION,
    READONLY_TOOL_NAMES,
    build_readonly_mcp_server,
    mcp_input_schema_matches,
)
from app.schemas import ToolTrace
from app.settings import Settings


def _enabled_server() -> MCPServer:
    return build_readonly_mcp_server(
        enabled=True,
        transport_mode="in_process_readonly",
    )


def test_official_dependency_and_default_off_surface():
    project = tomllib.loads(
        Path("agent/pyproject.toml").read_text(encoding="utf-8")
    )
    assert "mcp==2.0.0" in project["project"]["dependencies"]
    assert importlib.metadata.version("mcp") == "2.0.0"
    assert Settings().agent_mcp_readonly_enabled is False
    assert Settings().agent_mcp_transport_mode == "disabled"

    async def probe():
        server = build_readonly_mcp_server(
            enabled=False,
            transport_mode="disabled",
        )
        async with Client(server) as client:
            return [tool.name for tool in (await client.list_tools()).tools]

    assert asyncio.run(probe()) == []
    with pytest.raises(MCPReadonlyClientError) as error:
        MCPReadonlyClient(enabled=False, transport_mode="disabled")
    assert error.value.code == "mcp_disabled"


def test_mcp_transport_mode_is_strict_and_fail_closed():
    for mode in ("live", "http", "in_process", ""):
        with pytest.raises(MCPGatewayConfigurationError):
            build_readonly_mcp_server(enabled=True, transport_mode=mode)
        with pytest.raises(ValueError):
            Settings(agent_mcp_transport_mode=mode)


def test_official_in_process_client_discovers_exact_readonly_catalog():
    async def probe():
        async with Client(_enabled_server(), mode="auto") as client:
            result = await client.list_tools()
            return client.protocol_version, client.server_info, result.tools

    protocol, server_info, tools = asyncio.run(probe())
    assert protocol == MCP_PROTOCOL_VERSION
    assert server_info is not None
    assert server_info.name == MCP_SERVER_NAME
    assert server_info.version == MCP_SERVER_VERSION
    assert [tool.name for tool in tools] == list(READONLY_TOOL_NAMES)
    expected_output = ToolTrace.model_json_schema(by_alias=True)
    for tool in tools:
        assert mcp_input_schema_matches(tool.name, tool.input_schema)
        assert tool.output_schema == expected_output
        assert isinstance(tool.annotations, ToolAnnotations)
        assert tool.annotations.read_only_hint is True
        assert tool.annotations.destructive_hint is False
        assert tool.annotations.idempotent_hint is True


def test_gateway_rejects_bad_arguments_before_business_io():
    async def probe():
        with patch(
            "app.tools.call_tool",
            new=AsyncMock(return_value=ToolTrace(tool="search_products", ok=True)),
        ) as live:
            async with Client(_enabled_server()) as client:
                results = [
                    await client.call_tool(
                        "search_products",
                        {"query": "q", "category": "手机", "unexpected": "token"},
                    ),
                    await client.call_tool("search_products", {"query": "q"}),
                    await client.call_tool(
                        "search_products",
                        {"query": "q", "category": "unsupported"},
                    ),
                ]
            return results, live

    results, live = asyncio.run(probe())
    assert live.await_count == 0
    for result in results:
        assert result.is_error is True
        if result.structured_content is not None:
            trace = ToolTrace.model_validate(result.structured_content)
            assert trace.ok is False
            assert trace.detail["code"] == "mcp_invalid_arguments"


def test_client_rejects_bad_arguments_before_mcp_call():
    async def probe():
        with patch(
            "app.tools.call_tool",
            new=AsyncMock(return_value=ToolTrace(tool="search_products", ok=True)),
        ) as live:
            async with MCPReadonlyClient(
                enabled=True,
                transport_mode="in_process_readonly",
            ) as client:
                for arguments in (
                    {"query": "q", "category": "手机", "token": "secret"},
                    {"query": "q"},
                ):
                    with pytest.raises(MCPReadonlyClientError) as error:
                        await client.call_tool("search_products", arguments)
                    assert error.value.code == "mcp_invalid_arguments"
            return live

    live = asyncio.run(probe())
    assert live.await_count == 0


def test_client_revalidates_tool_trace_and_rejects_tool_failure():
    async def probe():
        async def fake_live(tool_name, arguments):
            return ToolTrace(
                tool=tool_name,
                ok=tool_name == "search_products",
                detail={"public": True},
            )

        with patch("app.tools.call_tool", new=fake_live) as live:
            async with MCPReadonlyClient(
                enabled=True,
                transport_mode="in_process_readonly",
            ) as client:
                success = await client.call_tool(
                    "search_products",
                    {"query": "q", "category": "手机"},
                )
                with pytest.raises(MCPReadonlyClientError) as error:
                    await client.call_tool(
                        "get_product_details",
                        {"productIds": [101]},
                    )
            return success, error.value.code

    success, failure_code = asyncio.run(probe())
    assert success.ok is True
    assert failure_code == "mcp_tool_failed"


def test_runtime_contract_removal_and_replacement_fail_closed_before_exposure():
    from app.control.validation_contracts import (
        RUNTIME_TOOL_CONTRACTS,
        RuntimeToolContract,
    )

    original = RUNTIME_TOOL_CONTRACTS["search_products"]
    with patch("app.tools.call_tool", new=AsyncMock()) as live:
        try:
            del RUNTIME_TOOL_CONTRACTS["search_products"]
            with pytest.raises(MCPGatewayConfigurationError) as removed:
                _enabled_server()
            assert removed.value.code == "mcp_runtime_contract_missing"
        finally:
            RUNTIME_TOOL_CONTRACTS["search_products"] = original
    assert live.await_count == 0

    with patch.dict(
        RUNTIME_TOOL_CONTRACTS,
        {
            "search_products": RuntimeToolContract(
                frozenset({"unsupportedOutput"}),
            )
        },
        clear=False,
    ):
        with pytest.raises(MCPGatewayConfigurationError) as replaced:
            _enabled_server()
    assert replaced.value.code == "mcp_runtime_contract_drift"


def test_tooltrace_failure_maps_to_raw_mcp_error_and_adapter_failure():
    async def probe():
        failure = ToolTrace(
            tool="get_product_details",
            ok=False,
            detail={"secret": "must_not_cross"},
        )
        with patch("app.tools.call_tool", new=AsyncMock(return_value=failure)):
            async with Client(_enabled_server()) as raw_client:
                raw = await raw_client.call_tool(
                    "get_product_details",
                    {"productIds": [101]},
                )
            async with MCPReadonlyClient(
                enabled=True,
                transport_mode="in_process_readonly",
            ) as adapter:
                with pytest.raises(MCPReadonlyClientError) as error:
                    await adapter.call_tool(
                        "get_product_details",
                        {"productIds": [101]},
                    )
                return raw, error.value.code, adapter.observations

    raw, error_code, observations = asyncio.run(probe())
    assert raw.is_error is True
    assert ToolTrace.model_validate(raw.structured_content).ok is False
    assert "must_not_cross" not in repr(raw)
    assert error_code == "mcp_tool_failed"
    assert observations[-1]["outcome"] == "tool_failed"


def test_transaction_scope_and_preview_tools_are_not_discoverable_or_callable():
    forbidden = (
        "compare_products",
        "rerank_products_in_scope",
        "preview_order",
        "preview_cancel_order",
        "preview_payment",
        "create_order",
        "cancel_order",
        "create_payment",
        "search_places",
    )

    async def probe():
        with patch("app.tools.call_tool", new=AsyncMock()) as live:
            async with Client(_enabled_server()) as client:
                listed = [tool.name for tool in (await client.list_tools()).tools]
                results = [
                    await client.call_tool(name, {})
                    for name in forbidden
                ]
            return listed, results, live

    listed, results, live = asyncio.run(probe())
    assert listed == list(READONLY_TOOL_NAMES)
    assert all(result.is_error is True for result in results)
    assert live.await_count == 0

    async def adapter_probe():
        async with MCPReadonlyClient(
            enabled=True,
            transport_mode="in_process_readonly",
        ) as client:
            with pytest.raises(MCPReadonlyClientError) as error:
                await client.call_tool("create_payment", {})
            return error.value.code

    assert asyncio.run(adapter_probe()) == "mcp_tool_not_allowlisted"


def test_schema_drift_is_rejected_before_adapter_use():
    server = _enabled_server()

    async def extra_tool() -> ToolTrace:
        return ToolTrace(tool="compare_products", ok=False)

    server.add_tool(extra_tool, name="compare_products", structured_output=True)

    async def probe():
        with pytest.raises(MCPReadonlyClientError) as error:
            async with MCPReadonlyClient(
                server=server,
                enabled=True,
                transport_mode="in_process_readonly",
            ):
                pass
        return error.value.code

    assert asyncio.run(probe()) == "mcp_tool_catalog_mismatch"

    input_schema_drift = MCPServer(
        name=MCP_SERVER_NAME,
        version=MCP_SERVER_VERSION,
    )

    async def wrong_search(query: str) -> ToolTrace:
        return ToolTrace(tool="search_products", ok=True)

    async def valid_details(productIds: list[int]) -> ToolTrace:
        return ToolTrace(tool="get_product_details", ok=True)

    annotations = ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=True,
    )
    input_schema_drift.add_tool(
        wrong_search,
        name="search_products",
        annotations=annotations,
        structured_output=True,
    )
    input_schema_drift.add_tool(
        valid_details,
        name="get_product_details",
        annotations=annotations,
        structured_output=True,
    )

    async def input_schema_probe():
        with pytest.raises(MCPReadonlyClientError) as error:
            async with MCPReadonlyClient(
                server=input_schema_drift,
                enabled=True,
                transport_mode="in_process_readonly",
            ):
                pass
        return error.value.code

    assert asyncio.run(input_schema_probe()) == "mcp_tool_input_schema_mismatch"


def test_structured_result_drift_and_error_flag_fail_closed():
    async def structural_probe():
        with patch("app.tools.call_tool", new=AsyncMock(return_value={"ok": True})):
            async with MCPReadonlyClient(
                enabled=True,
                transport_mode="in_process_readonly",
            ) as client:
                with pytest.raises(MCPReadonlyClientError) as error:
                    await client.call_tool(
                        "search_products",
                        {"query": "q", "category": "手机"},
                    )
                return error.value.code

    assert asyncio.run(structural_probe()) == "mcp_result_error"

    async def error_flag_probe():
        client = MCPReadonlyClient(
            enabled=True,
            transport_mode="in_process_readonly",
        )
        fake = SimpleNamespace(
            is_error=True,
            structured_content={"tool": "search_products", "ok": True},
        )
        client._client = SimpleNamespace(
            call_tool=AsyncMock(return_value=fake),
        )
        with pytest.raises(MCPReadonlyClientError) as error:
            await client.call_tool(
                "search_products",
                {"query": "q", "category": "手机"},
            )
        return error.value.code

    assert asyncio.run(error_flag_probe()) == "mcp_result_ok_mismatch"

    async def truth_table_probe(is_error, ok):
        client = MCPReadonlyClient(
            enabled=True,
            transport_mode="in_process_readonly",
        )
        fake = SimpleNamespace(
            is_error=is_error,
            structured_content={"tool": "search_products", "ok": ok},
        )
        client._client = SimpleNamespace(
            call_tool=AsyncMock(return_value=fake),
        )
        if is_error is False and ok is True:
            return (await client.call_tool(
                "search_products",
                {"query": "q", "category": "手机"},
            )).ok
        with pytest.raises(MCPReadonlyClientError) as error:
            await client.call_tool(
                "search_products",
                {"query": "q", "category": "手机"},
            )
        return error.value.code

    assert asyncio.run(truth_table_probe(False, True)) is True
    assert asyncio.run(truth_table_probe(False, False)) == "mcp_result_ok_mismatch"
    assert asyncio.run(truth_table_probe(True, True)) == "mcp_result_ok_mismatch"
    assert asyncio.run(truth_table_probe(True, False)) == "mcp_tool_failed"


def test_timeout_cancellation_and_concurrent_calls_are_fail_closed_and_isolated():
    async def slow_live(tool_name, arguments):
        await asyncio.sleep(1)
        return ToolTrace(tool=tool_name, ok=True)

    async def timeout_probe():
        with patch("app.tools.call_tool", new=slow_live):
            async with MCPReadonlyClient(
                enabled=True,
                transport_mode="in_process_readonly",
                timeout_seconds=0.01,
            ) as client:
                with pytest.raises(MCPReadonlyClientError) as error:
                    await client.call_tool(
                        "search_products",
                        {"query": "q", "category": "手机"},
                    )
                return error.value.code

    assert asyncio.run(timeout_probe()) == "mcp_call_timeout"

    async def cancellation_probe():
        with patch("app.tools.call_tool", new=slow_live):
            async with MCPReadonlyClient(
                enabled=True,
                transport_mode="in_process_readonly",
                timeout_seconds=1,
            ) as client:
                task = asyncio.create_task(
                    client.call_tool(
                        "search_products",
                        {"query": "q", "category": "手机"},
                    )
                )
                await asyncio.sleep(0.01)
                task.cancel()
                with pytest.raises(MCPReadonlyClientError) as error:
                    await task
                return error.value.code

    assert asyncio.run(cancellation_probe()) == "mcp_call_cancelled"

    async def concurrent_probe():
        async def concurrent_live(tool_name, arguments):
            await asyncio.sleep(0.01 if tool_name == "search_products" else 0.02)
            return ToolTrace(tool=tool_name, ok=True, detail={"tool": tool_name})

        with patch("app.tools.call_tool", new=concurrent_live):
            async with MCPReadonlyClient(
                enabled=True,
                transport_mode="in_process_readonly",
            ) as client:
                return await asyncio.gather(
                    client.call_tool(
                        "search_products",
                        {"query": "q1", "category": "手机"},
                    ),
                    client.call_tool(
                        "get_product_details",
                        {"productIds": [101]},
                    ),
                )

    results = asyncio.run(concurrent_probe())
    assert [result.tool for result in results] == [
        "search_products",
        "get_product_details",
    ]


def test_exception_and_observability_are_bounded_and_non_sensitive(caplog):
    async def probe():
        async def exploding_live(_tool_name, _arguments):
            raise RuntimeError("query=SECRET_QUERY token=SECRET_TOKEN")

        with patch("app.tools.call_tool", new=exploding_live):
            async with MCPReadonlyClient(
                enabled=True,
                transport_mode="in_process_readonly",
                max_observations=3,
            ) as client:
                with pytest.raises(MCPReadonlyClientError) as error:
                    await client.call_tool(
                        "search_products",
                        {"query": "SECRET_QUERY", "category": "手机"},
                    )
                assert error.value.code == "mcp_tool_failed"
                with pytest.raises(MCPReadonlyClientError):
                    await client.call_tool("create_payment", {})
                return client.observations

    with caplog.at_level("WARNING"):
        observations = asyncio.run(probe())
    rendered = repr(observations) + caplog.text
    assert "SECRET_QUERY" not in rendered
    assert "SECRET_TOKEN" not in rendered
    assert len(observations) <= 3
    assert any(item.get("outcome") == "tool_failed" for item in observations)
    assert all(set(item) <= {
        "event", "protocolVersion", "serverName", "serverVersion",
        "toolName", "toolNames", "outcome",
    } for item in observations)


def test_context_manager_preserves_tool_failure_for_outer_caller():
    async def probe():
        async def failing_live(_tool_name, _arguments):
            return ToolTrace(
                tool="get_product_details",
                ok=False,
                detail={"code": "backend_failed"},
            )

        with patch("app.tools.call_tool", new=failing_live):
            with pytest.raises(MCPReadonlyClientError) as error:
                async with MCPReadonlyClient(
                    enabled=True,
                    transport_mode="in_process_readonly",
                ) as client:
                    await client.call_tool(
                        "get_product_details",
                        {"productIds": [101]},
                    )
            return error.value.code

    assert asyncio.run(probe()) == "mcp_tool_failed"


def test_context_manager_preserves_outer_body_value_error():
    async def probe():
        with pytest.raises(ValueError, match="^body_error$"):
            async with MCPReadonlyClient(
                enabled=True,
                transport_mode="in_process_readonly",
            ):
                raise ValueError("body_error")

    asyncio.run(probe())


@pytest.mark.parametrize(
    ("protocol_version", "server_name", "tools", "expected_code"),
    (
        ("wrong-version", MCP_SERVER_NAME, (), "mcp_protocol_version_mismatch"),
        (MCP_PROTOCOL_VERSION, "foreign-server", (), "mcp_server_identity_mismatch"),
        (MCP_PROTOCOL_VERSION, MCP_SERVER_NAME, (), "mcp_tool_catalog_mismatch"),
    ),
)
def test_enter_failure_resets_client_and_closes_once(
    protocol_version,
    server_name,
    tools,
    expected_code,
):
    class FakeSDKClient:
        def __init__(self):
            self.protocol_version = protocol_version
            self.server_info = SimpleNamespace(
                name=server_name,
                version=MCP_SERVER_VERSION,
            )
            self.tools = tools
            self.close_args = []

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            self.close_args.append(args)

        async def list_tools(self):
            return SimpleNamespace(tools=self.tools)

    async def probe():
        fake = FakeSDKClient()
        client = MCPReadonlyClient(
            enabled=True,
            transport_mode="in_process_readonly",
        )
        with patch("app.mcp_client.MCPClient", return_value=fake):
            with pytest.raises(MCPReadonlyClientError) as error:
                await client.__aenter__()
        return client, fake, error.value.code

    client, fake, code = asyncio.run(probe())
    assert code == expected_code
    assert client._client is None
    assert fake.close_args == [(None, None, None)]


def test_close_failure_is_stable_and_never_replaces_body_error():
    class CloseFailure:
        def __init__(self):
            self.close_args = []

        async def __aexit__(self, *args):
            self.close_args.append(args)
            raise RuntimeError("sdk_close_secret")

    async def probe():
        client = MCPReadonlyClient(
            enabled=True,
            transport_mode="in_process_readonly",
        )
        no_body_close = CloseFailure()
        client._client = no_body_close
        with pytest.raises(MCPReadonlyClientError) as close_error:
            await client.__aexit__(None, None, None)
        assert close_error.value.code == "mcp_close_failed"
        assert "sdk_close_secret" not in str(close_error.value)
        assert client._client is None
        assert no_body_close.close_args == [(None, None, None)]
        assert client.observations[-1]["outcome"] == "close_failed"

        body_close = CloseFailure()
        client._client = body_close
        body_error = ValueError("body_error")
        with pytest.raises(ValueError, match="^body_error$"):
            try:
                raise body_error
            except ValueError as caught:
                assert await client.__aexit__(
                    ValueError,
                    caught,
                    caught.__traceback__,
                ) is False
                raise
        assert client._client is None
        assert body_close.close_args == [(None, None, None)]
        assert "sdk_close_secret" not in repr(client.observations)

    asyncio.run(probe())
