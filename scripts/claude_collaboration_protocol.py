from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
from pathlib import Path
from typing import Any


class ProtocolError(RuntimeError):
    pass


REQUIRED_REPORT_HEADINGS = (
    "### Status",
    "### Implementation model",
    "### Files changed",
    "### Commands and results",
    "### Failure evidence and artifacts",
    "### SHA-256",
    "### Remaining risks",
    "### EXPERIENCE_CANDIDATE",
    "### Truthful claims",
    "### Unsupported claims",
    "### Stop",
)

REQUIRED_EXPERIENCE_LABELS = (
    "Problem:",
    "Root cause:",
    "Fix:",
    "Evidence:",
    "Contribution boundaries:",
    "Interview-safe statement:",
    "Unsupported statement:",
)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_state(path: Path) -> dict[str, Any]:
    try:
        state = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ProtocolError(f"cannot read collaboration state: {exc}") from exc
    if not isinstance(state, dict):
        raise ProtocolError("collaboration state must be a JSON object")
    return state


def _required_string(state: dict[str, Any], key: str) -> str:
    value = state.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ProtocolError(f"state.{key} must be a non-empty string")
    return value


def validate_state_paths(state: dict[str, Any], workspace: Path, document: Path) -> None:
    configured_workspace = Path(_required_string(state, "workspace")).resolve()
    configured_document = Path(_required_string(state, "interactionDocument")).resolve()
    if configured_workspace != workspace.resolve():
        raise ProtocolError(f"workspace mismatch: {configured_workspace}")
    if configured_document != document.resolve():
        raise ProtocolError(f"interaction document mismatch: {configured_document}")
    allowed_paths = state.get("allowedPaths")
    if not isinstance(allowed_paths, list) or not allowed_paths:
        raise ProtocolError("state.allowedPaths must be a non-empty list")
    normalized = []
    for value in allowed_paths:
        if not isinstance(value, str) or not value.strip():
            raise ProtocolError("allowed path must be a non-empty string")
        relative = Path(value)
        if relative.is_absolute() or ".." in relative.parts:
            raise ProtocolError(f"allowed path must stay workspace-relative: {value}")
        normalized.append(relative.as_posix())
    if len(normalized) != len(set(normalized)):
        raise ProtocolError("state.allowedPaths contains duplicates")


def validate_source_hashes(state: dict[str, Any], workspace: Path) -> None:
    hashes = state.get("sourceSha256AtOpen")
    if not isinstance(hashes, dict) or not hashes:
        raise ProtocolError("state.sourceSha256AtOpen must be a non-empty object")
    for relative, expected in hashes.items():
        if not isinstance(relative, str) or not isinstance(expected, str):
            raise ProtocolError("source hash entries must be strings")
        path = workspace / relative
        if not path.is_file():
            raise ProtocolError(f"pinned source is missing: {relative}")
        actual = sha256_file(path)
        if actual.lower() != expected.lower():
            raise ProtocolError(
                f"source changed since task opened: {relative}; expected={expected}; actual={actual}"
            )


def task_heading(task_id: str) -> str:
    return f"## TASK_PACKET — {task_id}"


def report_heading_prefix(task_id: str) -> str:
    return f"## CLAUDE_REPORT — {task_id} — Attempt "


def review_heading(task_id: str) -> str:
    return f"## CODEX_REVIEW — {task_id}"


def validate_preflight(state: dict[str, Any], workspace: Path, document: Path) -> None:
    validate_state_paths(state, workspace, document)
    if state.get("status") != "OPEN":
        raise ProtocolError(f"task is not executable: status={state.get('status')}")
    task_id = _required_string(state, "taskId")
    text = document.read_text(encoding="utf-8-sig")
    if text.count(task_heading(task_id)) != 1:
        raise ProtocolError("active TASK_PACKET must appear exactly once")
    if report_heading_prefix(task_id) in text:
        raise ProtocolError("active task already has a CLAUDE_REPORT")
    if review_heading(task_id) in text:
        raise ProtocolError("active task already has a CODEX_REVIEW")
    expected_document_hash = _required_string(state, "interactionDocumentSha256AtOpen")
    actual_document_hash = sha256_file(document)
    if actual_document_hash.lower() != expected_document_hash.lower():
        raise ProtocolError(
            "interaction document changed since task opened; issue a new task state instead of reusing it"
        )
    validate_source_hashes(state, workspace)


def _current_report(text: str, task_id: str) -> str:
    prefix = report_heading_prefix(task_id)
    starts = [index for index in range(len(text)) if text.startswith(prefix, index)]
    if len(starts) != 1:
        raise ProtocolError("exactly one CLAUDE_REPORT is required")
    start = starts[0]
    next_record = text.find("\n## ", start + len(prefix))
    return text[start:] if next_record == -1 else text[start:next_record]


def _section(report: str, heading: str) -> str:
    marker = f"{heading}\n"
    if marker not in report:
        raise ProtocolError(f"missing report section: {heading}")
    body = report.split(marker, 1)[1]
    next_heading = body.find("\n### ")
    return body if next_heading == -1 else body[:next_heading]


def validate_postflight(state: dict[str, Any], workspace: Path, document: Path) -> None:
    validate_state_paths(state, workspace, document)
    if state.get("status") != "CLAUDE_RUNNING":
        raise ProtocolError(f"postflight requires CLAUDE_RUNNING, got {state.get('status')}")
    task_id = _required_string(state, "taskId")
    text = document.read_text(encoding="utf-8-sig")
    report = _current_report(text, task_id)
    for heading in REQUIRED_REPORT_HEADINGS:
        if report.count(heading) != 1:
            raise ProtocolError(f"report must contain heading exactly once: {heading}")
    status = _section(report, "### Status")
    if "READY_FOR_CODEX_REVIEW" not in status and "BLOCKED" not in status:
        raise ProtocolError("Claude report status must be READY_FOR_CODEX_REVIEW or BLOCKED")
    if "ACCEPT" in status or "HOLD" in status:
        raise ProtocolError("Claude cannot assign Codex review status")
    if "deepseek-v4-flash" not in _section(report, "### Implementation model"):
        raise ProtocolError("report must identify implementation model deepseek-v4-flash")
    if review_heading(task_id) in text or "independently accepted" in report.lower():
        raise ProtocolError("Claude cannot write or claim CODEX_REVIEW acceptance")
    experience = _section(report, "### EXPERIENCE_CANDIDATE")
    for label in REQUIRED_EXPERIENCE_LABELS:
        if label not in experience:
            raise ProtocolError(f"experience candidate is missing label: {label}")
    if not re.search(r"\b[0-9a-fA-F]{64}\b", _section(report, "### SHA-256")):
        raise ProtocolError("SHA-256 section must contain at least one full hash")
    stop_section = report.split("### Stop", 1)[1]
    if "Codex" not in stop_section or "验收" not in stop_section:
        raise ProtocolError("Stop section must explicitly wait for Codex review")


def git_paths(workspace: Path, *args: str) -> list[str]:
    result = subprocess.run(
        ["git", "-c", "core.quotepath=false", "-C", str(workspace), *args],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if result.returncode != 0:
        raise ProtocolError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return [line.replace("\\", "/") for line in result.stdout.splitlines() if line]


def out_of_scope_snapshot(workspace: Path, allowed_paths: list[str]) -> dict[str, Any]:
    allowed = {Path(value).as_posix() for value in allowed_paths}
    entries: list[dict[str, str]] = []
    all_paths = sorted(
        set(git_paths(workspace, "ls-files"))
        | set(git_paths(workspace, "ls-files", "--others", "--exclude-standard"))
    )
    for relative in all_paths:
        if relative in allowed:
            continue
        path = workspace / relative
        entries.append(
            {
                "path": relative,
                "sha256": sha256_file(path) if path.is_file() else "<missing>",
            }
        )
    payload = json.dumps(entries, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return {
        "fingerprint": hashlib.sha256(payload.encode("utf-8")).hexdigest(),
        "fileCount": len(entries),
        "entries": entries,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("preflight", "postflight", "snapshot"):
        subparser = subparsers.add_parser(command)
        subparser.add_argument("--workspace", type=Path, required=True)
        subparser.add_argument("--state", type=Path, required=True)
        subparser.add_argument("--document", type=Path, required=True)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    state = load_state(args.state)
    workspace = args.workspace.resolve()
    document = args.document.resolve()
    if args.command == "preflight":
        validate_preflight(state, workspace, document)
        result = {"ok": True, "taskId": state["taskId"], "status": state["status"]}
    elif args.command == "postflight":
        validate_postflight(state, workspace, document)
        result = {"ok": True, "taskId": state["taskId"], "status": state["status"]}
    else:
        validate_state_paths(state, workspace, document)
        result = out_of_scope_snapshot(workspace, state["allowedPaths"])
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":"), sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ProtocolError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        raise SystemExit(2) from exc
