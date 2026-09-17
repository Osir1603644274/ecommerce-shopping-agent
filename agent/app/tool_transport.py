"""Injectable tool-call transport with live / record / replay modes.

Provides three transports:
  - live:    Calls the real tool and returns its actual output.
  - record:  Calls the real tool AND records cleansed input/output for later replay.
  - replay:  Reads only from recorded data; never touches Java, ES, Qdrant, Redis,
             or any business service.

Recording format is intentionally flat JSONL so that diffs are human-readable.
Each recorded call is individually deserialisable back to a valid ToolTrace.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict, Field

from .schemas import ToolTrace

if TYPE_CHECKING:
    from .tool_execution_v2 import ToolExecutionContext

logger = logging.getLogger(__name__)

# ── Recording schema ─────────────────────────────────────────────────────────


class RecordedCall(BaseModel):
    """One cleansed tool-call record. NEVER stores tokens, passwords, or raw queries."""

    model_config = ConfigDict(populate_by_name=True)

    run_id: str = Field(alias="runId")
    step_index: int = Field(default=0, alias="stepIndex")
    tool_name: str = Field(alias="toolName")
    arguments_hash: str = Field(alias="argumentsHash")
    arguments_keys: list[str] = Field(default_factory=list, alias="argumentsKeys")
    arguments: dict[str, Any] | None = None
    ok: bool
    duration_ms: float | None = Field(default=None, alias="durationMs")
    output_hash: str = Field(alias="outputHash")
    output: Any = None  # The cleansed, serialisable tool result
    execution_identity: dict[str, str | int] | None = Field(
        default=None, alias="executionIdentity"
    )


class RecordSession(BaseModel):
    """A recording session header for a single run."""

    model_config = ConfigDict(populate_by_name=True)

    run_id: str = Field(alias="runId")
    context_pack_hash: str | None = Field(default=None, alias="contextPackHash")
    recorded_at: str = Field(alias="recordedAt")
    tool_call_count: int = 0


# ── Transport interface ──────────────────────────────────────────────────────


ToolCallFn = Any  # matches ToolCaller from executor.py

_DURABLE_READ_ONLY_TOOL_NAMES = frozenset({
    "search_products",
    "get_product_details",
    "compare_products",
    "search_product_evidence",
    "rerank_products_in_scope",
})


class ToolExecutionContextError(ValueError):
    """Stable fail-closed rejection at the production dispatch boundary."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def validate_execution_context(
    tool_name: str,
    arguments: object,
    execution_context: "ToolExecutionContext | None",
) -> "ToolExecutionContext | None":
    """Validate the server-owned V2 identity without changing tool arguments."""

    if execution_context is None:
        return None
    from .graph.tool_inbox_v2 import ToolInboxSlot, sha256
    from .tool_execution_v2 import ToolExecutionContext

    if type(execution_context) is not ToolExecutionContext:
        raise ToolExecutionContextError("execution_context_invalid")
    if tool_name not in _DURABLE_READ_ONLY_TOOL_NAMES:
        raise ToolExecutionContextError("execution_context_tool_not_readonly")
    if not isinstance(arguments, dict):
        raise ToolExecutionContextError("execution_context_arguments_invalid")
    try:
        checked = ToolExecutionContext(
            task_id=execution_context.task_id,
            run_id=execution_context.run_id,
            thread_id=execution_context.thread_id,
            plan_id=execution_context.plan_id,
            step_id=execution_context.step_id,
            state_revision=execution_context.state_revision,
            session_owner_hash=execution_context.session_owner_hash,
            execution_id=execution_context.execution_id,
            logical_slot_key=execution_context.logical_slot_key,
            fence=execution_context.fence,
            canonical_args_sha256=execution_context.canonical_args_sha256,
        )
    except (TypeError, ValueError) as exc:
        raise ToolExecutionContextError("execution_context_invalid") from exc
    if sha256(arguments) != checked.canonical_args_sha256:
        raise ToolExecutionContextError("execution_context_arguments_mismatch")
    try:
        slot = ToolInboxSlot.create(
            task_id=checked.task_id,
            plan_id=checked.plan_id,
            step_id=checked.step_id,
            state_revision=checked.state_revision,
            tool_name=tool_name,
            canonical_args_sha256=checked.canonical_args_sha256,
        )
    except (PermissionError, TypeError, ValueError) as exc:
        raise ToolExecutionContextError("execution_context_tool_invalid") from exc
    if slot.logical_slot_key() != checked.logical_slot_key:
        raise ToolExecutionContextError("execution_context_tool_mismatch")
    if (
        slot.execution_id(run_id=checked.run_id, thread_id=checked.thread_id)
        != checked.execution_id
    ):
        raise ToolExecutionContextError("execution_context_execution_mismatch")
    return checked


def execution_audit_identity(
    execution_context: "ToolExecutionContext | None",
) -> dict[str, str | int] | None:
    """Return only the minimum server-owned identity safe for audit."""

    if execution_context is None:
        return None
    return {
        "executionId": execution_context.execution_id,
        "logicalSlotKey": execution_context.logical_slot_key,
        "fence": execution_context.fence,
        "inputHash": execution_context.canonical_args_sha256,
    }


async def _dispatch_with_optional_context(
    call_tool: ToolCallFn,
    tool_name: str,
    arguments: dict[str, Any],
    execution_context: "ToolExecutionContext | None",
) -> Any:
    """Keep pre-V2 two-argument test and extension callers compatible."""

    if execution_context is None:
        return await call_tool(tool_name, arguments)
    return await call_tool(
        tool_name,
        arguments,
        execution_context=execution_context,
    )


def _tool_trace_to_serialisable(trace: Any) -> dict[str, Any]:
    """Convert a ToolTrace (pydantic or dict) to a cleansed JSON-safe dict."""
    if isinstance(trace, ToolTrace):
        return trace.model_dump(by_alias=True, mode="json")
    if isinstance(trace, dict):
        return trace
    if hasattr(trace, "model_dump"):
        return trace.model_dump(by_alias=True, mode="json")
    return {"raw": str(trace)}


def _deserialise_to_tool_trace(data: dict[str, Any]) -> ToolTrace:
    """Reconstruct a ToolTrace from a recorded dict."""
    return ToolTrace.model_validate(data)


SENSITIVE_KEYS = {
    "rawQuery", "bearerToken", "apiKey", "authHeader",
    "token", "secret", "password", "accessKey", "privateKey",
}


def _recursive_cleanse(obj: Any) -> Any:
    """Recursively redact sensitive keys from any nested dict/list structure."""
    if isinstance(obj, dict):
        return {
            k: "**REDACTED**" if k in SENSITIVE_KEYS else _recursive_cleanse(v)
            for k, v in obj.items()
        }
    if isinstance(obj, list):
        return [_recursive_cleanse(item) for item in obj]
    return obj


def _cleanse_tool_output(tool_name: str, output: Any) -> dict[str, Any]:
    """Strip auth tokens, full raw queries, and PII before recording.

    Accepts ToolTrace, dict, or raw object. Always returns a JSON-safe dict.
    Recursively redacts sensitive keys at any nesting depth.
    """
    cleansed = _tool_trace_to_serialisable(output)
    return _recursive_cleanse(cleansed)


def _stable_args_hash(arguments: dict[str, Any]) -> str:
    import hashlib
    payload = json.dumps(arguments, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


def _stable_output_hash(output: Any) -> str:
    import hashlib
    payload = json.dumps(output, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


# ── Live transport ───────────────────────────────────────────────────────────


async def live_transport(
    tool_name: str,
    arguments: dict[str, Any],
    call_tool: ToolCallFn,
    *,
    execution_context: "ToolExecutionContext | None" = None,
) -> Any:
    """Call the real tool directly — no recording, no replay."""
    checked = validate_execution_context(tool_name, arguments, execution_context)
    return await _dispatch_with_optional_context(
        call_tool,
        tool_name,
        arguments,
        checked,
    )


# ── Record transport ─────────────────────────────────────────────────────────


class RecordTransport:
    """Live calls + cleansed recording to a JSONL file.

    Writes a session header on first call, then one RecordedCall per invocation.
    The real ToolTrace is returned to the caller unchanged; the recording is a
    side-effect.  Every call is appended in order (same tool+same args may appear
    multiple times).
    """

    def __init__(self, output_path: Path, run_id: str, context_pack_hash: str | None = None) -> None:
        self._path = output_path
        self._run_id = run_id
        self._context_pack_hash = context_pack_hash
        self._step = 0
        self._call_count = 0
        self._started = False

    @property
    def call_count(self) -> int:
        """Number of tool calls recorded so far (excluding the header)."""
        return self._call_count

    def _ensure_session_header(self) -> None:
        if self._started:
            return
        session = RecordSession(
            runId=self._run_id,
            contextPackHash=self._context_pack_hash,
            recordedAt=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        )
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with open(self._path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"type": "session", **session.model_dump(by_alias=True)}, ensure_ascii=False) + "\n")
        self._started = True

    async def finalize(self, trace: Any) -> None:
        """Append the deterministic Harness baseline used by offline replay.

        ContextView payload hashes are validated while each phase runs.  The
        recording stores the externally comparable control-flow signature:
        Harness phase order and final action.
        """
        self._ensure_session_header()
        if hasattr(trace, "model_dump"):
            payload = trace.model_dump(by_alias=True, mode="json")
        elif isinstance(trace, dict):
            payload = trace
        else:
            return
        phases = payload.get("phases", [])
        phase_order = [
            str(item.get("phase"))
            for item in phases
            if isinstance(item, dict) and item.get("phase") != "final_answer"
        ]
        baseline = {
            "type": "trace_baseline",
            "runId": self._run_id,
            "harnessPhaseOrder": phase_order,
            "contextViewTypes": [
                str(item.get("type"))
                for item in payload.get("contextViews", [])
                if isinstance(item, dict)
            ],
            "finalAction": payload.get("finalAction"),
        }
        with open(self._path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(baseline, ensure_ascii=False) + "\n")

    async def __call__(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        call_tool: ToolCallFn,
        *,
        execution_context: "ToolExecutionContext | None" = None,
    ) -> Any:
        checked = validate_execution_context(tool_name, arguments, execution_context)
        self._ensure_session_header()
        result = await _dispatch_with_optional_context(
            call_tool,
            tool_name,
            arguments,
            checked,
        )
        arg_hash = _stable_args_hash(arguments)
        arg_keys = sorted(arguments.keys())
        cleansed = _cleanse_tool_output(tool_name, result)
        out_hash = _stable_output_hash(cleansed)
        record = RecordedCall(
            runId=self._run_id,
            stepIndex=self._step,
            toolName=tool_name,
            argumentsHash=arg_hash,
            argumentsKeys=arg_keys,
            arguments=_recursive_cleanse(arguments),
            ok=bool(getattr(result, "ok", True)),
            durationMs=getattr(result, "duration_ms", None) if hasattr(result, "duration_ms") else None,
            outputHash=out_hash,
            output=cleansed,
            executionIdentity=execution_audit_identity(checked),
        )
        with open(self._path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record.model_dump(by_alias=True), ensure_ascii=False) + "\n")
        self._step += 1
        self._call_count += 1
        return result


# ── Replay transport ─────────────────────────────────────────────────────────


class ReplayTransport:
    """Serve tool calls from a previously recorded JSONL file.

    - Supports duplicate (tool_name, args_hash) pairs: replays in recorded order.
    - In strict mode, raises if the requested call doesn't match the next in the
      recording (tool name, argument hash).
    - In non-strict mode, looks up by (tool_name, args_hash) key, falling back to
      a synthetic error trace on miss (never silently calls live).
    - Returns a real ToolTrace object — the caller must never know it's replayed.
    """

    def __init__(self, replay_path: Path, *, strict: bool = True) -> None:
        self._sequence: list[dict[str, Any]] = []
        self._index = 0
        self._strict = strict
        self._session_header: dict[str, Any] | None = None
        self._load(replay_path)

    def _load(self, path: Path) -> None:
        if not path.exists():
            raise FileNotFoundError(f"Replay file not found: {path}")
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                record = json.loads(line)
                if record.get("type") == "session":
                    self._session_header = record
                    continue
                if record.get("type") == "trace_baseline":
                    # Baseline markers are evidence, not tool calls — skip them
                    # so a single recording file replays cleanly.
                    continue
                self._sequence.append(record)

    @property
    def session_header(self) -> dict[str, Any] | None:
        return self._session_header

    @property
    def tool_call_count(self) -> int:
        return len(self._sequence)

    @property
    def consumed_count(self) -> int:
        return self._index

    async def __call__(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        _call_tool: ToolCallFn,
        *,
        execution_context: "ToolExecutionContext | None" = None,
    ) -> Any:
        checked = validate_execution_context(tool_name, arguments, execution_context)
        arg_hash = _stable_args_hash(arguments)
        if self._strict:
            if self._index >= len(self._sequence):
                raise IndexError(
                    f"Replay exhausted: {self._index} calls consumed, "
                    f"requested tool={tool_name} args_hash={arg_hash}"
                )
            next_call = self._sequence[self._index]
            if next_call["toolName"] != tool_name:
                raise KeyError(
                    f"Replay mismatch at step {self._index}: "
                    f"expected tool={next_call['toolName']}, got {tool_name}"
                )
            if next_call["argumentsHash"] != arg_hash:
                raise KeyError(
                    f"Replay argument hash mismatch at step {self._index}: "
                    f"expected {next_call['argumentsHash']}, got {arg_hash}"
                )
            if execution_audit_identity(checked) != next_call.get("executionIdentity"):
                raise KeyError("Replay execution identity mismatch")
            output = next_call.get("output", {})
            self._index += 1
            return _deserialise_to_tool_trace(output)

        # Non-strict: scan for matching call
        for i in range(self._index, len(self._sequence)):
            candidate = self._sequence[i]
            if candidate["toolName"] == tool_name and candidate["argumentsHash"] == arg_hash:
                if execution_audit_identity(checked) != candidate.get("executionIdentity"):
                    continue
                self._index = i + 1
                return _deserialise_to_tool_trace(candidate.get("output", {}))
        # Replay miss — never fall back to live
        return ToolTrace(
            tool=tool_name,
            ok=False,
            durationMs=0.0,
            detail={
                "code": "replay_miss",
                "message": f"No recorded output for {tool_name} (seq={self._index})",
            },
        )
