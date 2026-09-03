from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from retrieval_judgment_pool_mcp.server import RESPONSE_SCHEMA_VERSION, SERVER_INSTRUCTIONS, build_server


ROOT = Path(__file__).resolve().parents[2]
PUBLIC_FIXTURE = ROOT / "evaluation" / "retrieval-judgment-pool-v1-20260901" / "fixtures" / "public_tiny_v1"
TOOL_NAMES = {
    "create_pool_run",
    "submit_retrieval_run",
    "build_pool",
    "export_blind_packet",
    "get_run_status",
    "verify_run",
}


def _workspace(tmp_path: Path) -> tuple[Path, Path]:
    data_root = tmp_path / "public-data"
    shutil.copytree(PUBLIC_FIXTURE, data_root / "public_tiny_v1")
    return data_root, tmp_path / "runs"


@asynccontextmanager
async def _session(data_root: Path, run_root: Path) -> AsyncIterator[ClientSession]:
    environment = {
        **os.environ,
        "PYTHONPATH": str(ROOT),
        "PYTHONUTF8": "1",
        "RJP_DATA_ROOT": str(data_root),
        "RJP_RUN_ROOT": str(run_root),
        "RJP_TEST_SECRET": "must-never-appear-in-results",
    }
    parameters = StdioServerParameters(
        command=sys.executable,
        args=["-m", "retrieval_judgment_pool_mcp.server"],
        cwd=str(ROOT),
        env=environment,
    )
    async with stdio_client(parameters) as (reader, writer):
        async with ClientSession(reader, writer) as session:
            yield session


async def _call(session: ClientSession, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    result = await session.call_tool(name, arguments)
    assert result.is_error is False
    assert result.structured_content is not None
    return result.structured_content


def test_contract_is_discoverable_and_strict(tmp_path: Path) -> None:
    data_root, run_root = _workspace(tmp_path)

    async def scenario() -> None:
        server = build_server(data_root, run_root)
        tools = await server.list_tools()
        assert {tool.name for tool in tools} == TOOL_NAMES
        for tool in tools:
            assert tool.description and len(tool.description) >= 100
            assert tool.input_schema["additionalProperties"] is False
            assert tool.output_schema is not None
            assert tool.output_schema["additionalProperties"] is False
            assert tool.annotations is not None
            assert tool.annotations.destructive_hint is False
            assert tool.annotations.idempotent_hint is True
            assert tool.annotations.open_world_hint is False
        by_name = {tool.name: tool for tool in tools}
        for name in {"export_blind_packet", "get_run_status", "verify_run"}:
            assert by_name[name].annotations.read_only_hint is True
        for name in TOOL_NAMES - {"export_blind_packet", "get_run_status", "verify_run"}:
            assert by_name[name].annotations.read_only_hint is False

    asyncio.run(scenario())
    assert "UNJUDGED" in SERVER_INSTRUCTIONS
    assert "qrels" in SERVER_INSTRUCTIONS
    assert "production release" in SERVER_INSTRUCTIONS


def test_real_stdio_protocol_resume_build_verify_and_export(tmp_path: Path) -> None:
    data_root, run_root = _workspace(tmp_path)

    async def create_on_first_connection() -> str:
        async with _session(data_root, run_root) as session:
            initialized = await session.initialize()
            assert initialized.server_info.name == "retrieval-judgment-pool"
            assert initialized.instructions == SERVER_INSTRUCTIONS
            listed = await session.list_tools()
            assert {tool.name for tool in listed.tools} == TOOL_NAMES
            created = await _call(session, "create_pool_run", {"dataset_ref": "public_tiny_v1"})
            assert created["schema_version"] == RESPONSE_SCHEMA_VERSION
            assert created["ok"] is True
            assert created["data"]["phase"] == "NORMALIZE"
            assert created["data"]["idempotent_reuse"] is False
            return created["data"]["run_id"]

    run_id = asyncio.run(create_on_first_connection())

    async def resume_on_second_connection() -> None:
        async with _session(data_root, run_root) as session:
            await session.initialize()
            before = await _call(session, "get_run_status", {"run_id": run_id})
            assert before["data"]["phase"] == "NORMALIZE"
            built = await _call(session, "build_pool", {"run_id": run_id})
            assert built["ok"] is True
            assert built["data"]["status"] == "READY"
            assert built["data"]["verification"]["candidate_pair_count"] == 30
            assert built["data"]["verification"]["all_candidates_unjudged"] is True
            assert built["data"]["verification"]["qrels_read"] is False
            assert built["data"]["verification"]["sealed_or_hidden_data_read"] is False
            assert built["data"]["verification"]["production_release_allowed"] is False
            verified = await _call(session, "verify_run", {"run_id": run_id})
            exported = await _call(session, "export_blind_packet", {"run_id": run_id})
            assert verified["data"] == built["data"]["verification"]
            assert exported["data"]["label_status"] == "UNJUDGED"
            assert exported["data"]["candidate_pair_count"] == 30
            repeated_create = await _call(session, "create_pool_run", {"dataset_ref": "public_tiny_v1"})
            repeated_build = await _call(session, "build_pool", {"run_id": run_id})
            assert repeated_create["data"]["run_id"] == run_id
            assert repeated_create["data"]["idempotent_reuse"] is True
            assert repeated_build["data"]["idempotent_reuse"] is True

    asyncio.run(resume_on_second_connection())


def test_domain_errors_are_stable_and_non_sensitive(tmp_path: Path) -> None:
    data_root, run_root = _workspace(tmp_path)

    async def scenario() -> None:
        async with _session(data_root, run_root) as session:
            await session.initialize()
            qrels = await _call(session, "create_pool_run", {"dataset_ref": "qrels-private"})
            traversal = await _call(session, "create_pool_run", {"dataset_ref": "../escape"})
            unknown = await _call(session, "get_run_status", {"run_id": "rjp-00000000000000000000"})
            created = await _call(session, "create_pool_run", {"dataset_ref": "public_tiny_v1"})
            run_id = created["data"]["run_id"]
            undeclared = await _call(
                session,
                "submit_retrieval_run",
                {"run_id": run_id, "retriever_id": "bm25f-title-body-v1", "submission_ref": "external.jsonl"},
            )
            lock = run_root / run_id / ".mutate.lock"
            lock.write_text("test", encoding="ascii")
            try:
                busy = await _call(session, "build_pool", {"run_id": run_id})
            finally:
                lock.unlink()
            results = [qrels, traversal, unknown, undeclared, busy]
            assert [item["error"]["code"] for item in results] == [
                "SENSITIVE_INPUT_FORBIDDEN",
                "PATH_OUT_OF_SCOPE",
                "RUN_NOT_FOUND",
                "EXTERNAL_RETRIEVER_NOT_DECLARED",
                "RUN_BUSY",
            ]
            rendered = json.dumps(results, ensure_ascii=False)
            assert str(tmp_path) not in rendered
            assert "must-never-appear-in-results" not in rendered
            assert "Traceback" not in rendered
            invalid = await session.call_tool(
                "create_pool_run",
                {"dataset_ref": "public_tiny_v1", "undeclared_argument": "rejected"},
            )
            assert invalid.is_error is True

    asyncio.run(scenario())
