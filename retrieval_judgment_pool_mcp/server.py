"""STDIO MCP server exposing the shared label-free candidate-pool service."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Callable, Literal, TypeVar

from mcp.server import MCPServer
from mcp.types import ToolAnnotations
from pydantic import BaseModel, ConfigDict

from retrieval_judgment_pool_core import PoolError, PoolService


SERVER_VERSION = "1.0.0"
RESPONSE_SCHEMA_VERSION = "retrieval-judgment-pool-mcp-response-v1"
SERVER_INSTRUCTIONS = (
    "Build deterministic public pre-label retrieval judgment pools only. All candidates remain UNJUDGED. "
    "Never provide qrels, relevance labels, sealed, hidden, private, or production evaluation data. "
    "Call create_pool_run, submit any explicitly declared external runs, build_pool, then verify_run and "
    "export_blind_packet. Use get_run_status to resume. Tools share the same immutable core as the CLI and "
    "repository Skill; MCP does not judge relevance or authorize production release."
)


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ErrorDetail(StrictModel):
    code: str
    message: str


class VerificationData(StrictModel):
    run_id: str
    status: Literal["READY"]
    checksum_entry_count: int
    query_count: int
    document_count: int
    candidate_pair_count: int
    all_candidates_unjudged: bool
    blind_provenance_hidden: bool
    qrels_read: bool
    sealed_or_hidden_data_read: bool
    production_release_allowed: bool


class CreateData(StrictModel):
    run_id: str
    status: str
    phase: str
    idempotent_reuse: bool


class SubmitData(StrictModel):
    run_id: str
    retriever_id: str
    status: Literal["ACCEPTED"]
    idempotent_reuse: bool


class BuildData(StrictModel):
    run_id: str
    status: Literal["READY"]
    idempotent_reuse: bool
    verification: VerificationData


class StatusData(StrictModel):
    run_id: str
    status: str
    phase: str
    completed_stages: list[str]
    pending_external_retrievers: list[str]


class ExportData(StrictModel):
    run_id: str
    status: Literal["READY"]
    artifact: Literal["blind_packet.jsonl"]
    sha256: str
    query_count: int
    candidate_pair_count: int
    label_status: Literal["UNJUDGED"]


class CreateResponse(StrictModel):
    schema_version: Literal["retrieval-judgment-pool-mcp-response-v1"] = RESPONSE_SCHEMA_VERSION
    ok: bool
    data: CreateData | None
    error: ErrorDetail | None


class SubmitResponse(StrictModel):
    schema_version: Literal["retrieval-judgment-pool-mcp-response-v1"] = RESPONSE_SCHEMA_VERSION
    ok: bool
    data: SubmitData | None
    error: ErrorDetail | None


class BuildResponse(StrictModel):
    schema_version: Literal["retrieval-judgment-pool-mcp-response-v1"] = RESPONSE_SCHEMA_VERSION
    ok: bool
    data: BuildData | None
    error: ErrorDetail | None


class StatusResponse(StrictModel):
    schema_version: Literal["retrieval-judgment-pool-mcp-response-v1"] = RESPONSE_SCHEMA_VERSION
    ok: bool
    data: StatusData | None
    error: ErrorDetail | None


class ExportResponse(StrictModel):
    schema_version: Literal["retrieval-judgment-pool-mcp-response-v1"] = RESPONSE_SCHEMA_VERSION
    ok: bool
    data: ExportData | None
    error: ErrorDetail | None


class VerifyResponse(StrictModel):
    schema_version: Literal["retrieval-judgment-pool-mcp-response-v1"] = RESPONSE_SCHEMA_VERSION
    ok: bool
    data: VerificationData | None
    error: ErrorDetail | None


ResponseT = TypeVar("ResponseT", bound=StrictModel)


def _call(response_type: type[ResponseT], operation: Callable[[], dict[str, Any]]) -> ResponseT:
    try:
        return response_type(ok=True, data=operation(), error=None)
    except PoolError as exc:
        return response_type(ok=False, data=None, error=ErrorDetail(**exc.as_dict()))
    except Exception:
        return response_type(
            ok=False,
            data=None,
            error=ErrorDetail(code="INTERNAL_ERROR", message="candidate-pool operation failed"),
        )


def _enforce_strict_tool_inputs(server: MCPServer) -> None:
    """Make the SDK-generated flat argument models reject undeclared keys."""

    for registered in server._tool_manager._tools.values():
        argument_model = registered.fn_metadata.arg_model
        argument_model.model_config["extra"] = "forbid"
        argument_model.model_rebuild(force=True)
        registered.parameters = argument_model.model_json_schema()


def build_server(data_root: Path | str, run_root: Path | str) -> MCPServer:
    """Build a local-only server bound to explicit public data and run roots."""

    service = PoolService(data_root, run_root)
    server = MCPServer(
        name="retrieval-judgment-pool",
        title="Retrieval Judgment Pool",
        description="Deterministic, label-free candidate-pool workflow for human relevance judgment.",
        instructions=SERVER_INSTRUCTIONS,
        version=SERVER_VERSION,
    )
    mutating = ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    )
    reading = ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    )

    @server.tool(
        name="create_pool_run",
        title="Create candidate-pool run",
        description=(
            "Create or resume an immutable public, label-free pool run from a dataset reference under the configured data root. "
            "Rejects qrels, relevance labels, sealed/hidden references, path traversal, and input drift."
        ),
        annotations=mutating,
        structured_output=True,
    )
    def create_pool_run(dataset_ref: str) -> CreateResponse:
        def operation() -> dict[str, Any]:
            created = service.create_run(dataset_ref)
            status = service.get_run_status(created["run_id"])
            return {**created, "phase": status["phase"]}

        return _call(CreateResponse, operation)

    @server.tool(
        name="submit_retrieval_run",
        title="Submit declared external retrieval run",
        description=(
            "Attach a deterministic JSONL result to a retriever explicitly declared as kind=external in the run config. "
            "Submission references stay inside the public dataset directory and become immutable after acceptance."
        ),
        annotations=mutating,
        structured_output=True,
    )
    def submit_retrieval_run(run_id: str, retriever_id: str, submission_ref: str) -> SubmitResponse:
        return _call(
            SubmitResponse,
            lambda: service.submit_retrieval_run(run_id, retriever_id, submission_ref),
        )

    @server.tool(
        name="build_pool",
        title="Build deterministic candidate pool",
        description=(
            "Run retrieval, overlap/disagreement analysis, deterministic pair scoring, selection, packaging, and verification. "
            "The operation is resumable and idempotent; every selected candidate remains UNJUDGED."
        ),
        annotations=mutating,
        structured_output=True,
    )
    def build_pool(run_id: str) -> BuildResponse:
        return _call(BuildResponse, lambda: service.build_pool(run_id))

    @server.tool(
        name="export_blind_packet",
        title="Describe verified blind packet",
        description=(
            "Return the content hash and counts for a READY blind packet without exposing its filesystem path, retriever provenance, "
            "ranking scores, selection reasons, or labels other than UNJUDGED."
        ),
        annotations=reading,
        structured_output=True,
    )
    def export_blind_packet(run_id: str) -> ExportResponse:
        return _call(ExportResponse, lambda: service.export_blind_packet(run_id))

    @server.tool(
        name="get_run_status",
        title="Get resumable run status",
        description=(
            "Inspect the current phase, completed deterministic stages, and pending declared external retrievers for a run. "
            "Use this after interruption or from a newly connected MCP client."
        ),
        annotations=reading,
        structured_output=True,
    )
    def get_run_status(run_id: str) -> StatusResponse:
        return _call(StatusResponse, lambda: service.get_run_status(run_id))

    @server.tool(
        name="verify_run",
        title="Verify run identity and artifacts",
        description=(
            "Fail closed unless a READY run has matching source identity, stage artifacts, receipt, checksums, all-UNJUDGED candidates, "
            "and a provenance-hidden blind packet. Verification never authorizes production release."
        ),
        annotations=reading,
        structured_output=True,
    )
    def verify_run(run_id: str) -> VerifyResponse:
        return _call(VerifyResponse, lambda: service.verify_run(run_id))

    _enforce_strict_tool_inputs(server)
    return server


def main() -> int:
    data_root = os.environ.get("RJP_DATA_ROOT")
    run_root = os.environ.get("RJP_RUN_ROOT")
    if not data_root or not run_root:
        raise SystemExit("RJP_DATA_ROOT and RJP_RUN_ROOT are required")
    build_server(data_root, run_root).run(transport="stdio")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
