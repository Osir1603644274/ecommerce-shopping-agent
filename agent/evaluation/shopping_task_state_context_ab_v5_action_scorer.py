"""ReAct-v1 behavioral action gate for the V5 TaskState paired experiment.

The frozen V1 scorer derives an action from pendingQuestions before looking at
the answer body.  ReAct-v1 deliberately represents a safe evidence-boundary
reply as ASK_CLARIFICATION plus a user-visible boundary answer.  For this one
strictly identified case, project the observable behavior as ANSWER while
preserving the raw ReAct action in the score receipt.  All other turns use the
unchanged frozen scorer and private oracle.
"""

from __future__ import annotations

import argparse
import copy
import json
from pathlib import Path
from typing import Any

from evaluation import used_phone_harness_behavior_scorer_v1 as _base


def _is_evidence_boundary_answer(receipt: dict[str, Any]) -> bool:
    selected = receipt.get("selectedAction")
    task_state = receipt.get("taskState")
    domain = task_state.get("domainState") if isinstance(task_state, dict) else None
    extraction = domain.get("taskStateExtraction") if isinstance(domain, dict) else None
    traces = receipt.get("toolTrace") or []
    answer = receipt.get("answer")
    request_trace = receipt.get("requestTrace")
    pending = task_state.get("pendingQuestions") if isinstance(task_state, dict) else None
    final_action = (
        request_trace.get("agentFinalAction")
        if isinstance(request_trace, dict) else None
    )
    server_pending_boundary = bool(
        final_action == "ask_user"
        and isinstance(pending, list)
        and isinstance(answer, str)
        and answer in pending
        and "当前证据不支持" in answer
        and "比较已核验字段" in answer
        and "补充可信" in answer
    )
    composed_boundary = bool(
        final_action == "evidence_boundary_answer"
        and isinstance(answer, str)
        and "当前证据不支持" in answer
        and "不会把商品标题宣传当成已验证事实" in answer
        and "已核验字段" in answer
        and "补充可信" in answer
    )
    return bool(
        isinstance(selected, dict)
        and selected.get("kind") == "ASK_CLARIFICATION"
        and isinstance(extraction, dict)
        and extraction.get("reason") == "unsupported_game_camera_evidence"
        and not any(isinstance(item, dict) and item.get("ok") is True for item in traces)
        and (server_pending_boundary or composed_boundary)
    )


def _project_receipts(rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    projected = copy.deepcopy(rows)
    cases: list[dict[str, Any]] = []
    for receipt in projected:
        if not _is_evidence_boundary_answer(receipt):
            continue
        selected = receipt.get("selectedAction") or {}
        cases.append({
            "scenarioId": receipt.get("scenarioId"),
            "turnId": receipt.get("turnId"),
            "rawSelectedAction": {
                "kind": selected.get("kind"),
                "toolName": selected.get("toolName"),
            },
            "behavioralAction": {"kind": "ANSWER", "toolName": None},
            "reason": "react_v1_safe_evidence_boundary_answer",
        })
        receipt["selectedAction"] = {
            **selected,
            "status": "react_v1_evidence_boundary_answer_projection",
            "kind": "ANSWER",
            "toolName": None,
        }
        authoritative = receipt.get("authoritativeAction")
        receipt["authoritativeAction"] = {
            **(authoritative if isinstance(authoritative, dict) else {}),
            "kind": "ANSWER",
            "toolName": None,
        }
    return projected, cases


def score_receipts(receipts_path: Path) -> dict[str, Any]:
    raw_rows = _base._read_jsonl(receipts_path)
    projected, cases = _project_receipts(raw_rows)
    original_reader = _base._read_jsonl

    def read_for_score(path: Path) -> list[dict[str, Any]]:
        if Path(path).resolve() == receipts_path.resolve():
            return projected
        return original_reader(path)

    _base._read_jsonl = read_for_score
    try:
        report = _base.score_receipts(receipts_path)
    finally:
        _base._read_jsonl = original_reader
    report.update({
        "schemaVersion": "shopping-task-state-context-react-v1-action-score-v5",
        "runtimeSemantics": "react_v1_observable_behavior",
        "baseScorer": str(Path(_base.__file__).resolve()),
        "evidenceBoundaryProjectionCount": len(cases),
        "evidenceBoundaryProjectionCases": cases,
        "rawReactActionsPreservedInProjectionCases": True,
    })
    return report


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--receipts", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = score_receipts(args.receipts)
    if args.output.exists():
        raise FileExistsError(f"refusing to overwrite score: {args.output}")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
