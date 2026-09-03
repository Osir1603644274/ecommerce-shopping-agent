"""One-shot synthetic holdout confirmation of the production-wired MA runtime."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import statistics
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import redis.asyncio as redis

from agent.evaluation.context_multiagent_public_pilot_v2 import (
    DeepSeekToolCompatibleClient,
)
from agent.evaluation.context_multiagent_public_pilot_v3 import (
    FinalAnswerV1,
    FinalClaimV1,
    candidate_scope,
    load_jsonl,
    report_truth,
    score_final,
    score_report,
    tool_trace_for,
)
from app.evidence_research_v1 import ResearchMergeGuardV1
from app.multi_agent_runtime_v2 import (
    deepseek_research_decision_v2,
    run_multi_agent_research_v2,
)
from app.settings import settings


ROOT = Path(__file__).resolve().parents[2]


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def percentile(values: list[float], probability: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    position = (len(ordered) - 1) * probability
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = position - lower
    return ordered[lower] * (1 - fraction) + ordered[upper] * fraction


def canonical_final(result: Any, tool_trace: Any) -> tuple[FinalAnswerV1, dict[str, Any]]:
    report = result.report
    truth = report_truth(result.investigation_set, tool_trace)
    claims = [
        FinalClaimV1(
            candidateId=item.candidate_id,
            evidenceGapKey=item.evidence_gap_key,
            verdict=item.verdict,
            evidenceRefs=(
                item.evidence_refs
                if item.verdict in {"SATISFIED", "VIOLATED", "CONFLICT"}
                else ()
            ),
        )
        for item in report.findings
    ]
    claimed = {(item.candidate_id, item.evidence_gap_key) for item in claims}
    for item in report.unresolved:
        if (item.candidate_id, item.evidence_gap_key) not in claimed:
            claims.append(FinalClaimV1(
                candidateId=item.candidate_id,
                evidenceGapKey=item.evidence_gap_key,
                verdict="UNKNOWN",
                evidenceRefs=(),
            ))
    best = [int(value) for value in result.decision_support["bestVerifiedCandidateIds"]]
    candidates = [
        int(item["candidateId"])
        for item in result.decision_support["candidates"]
    ]
    selected = tuple(dict.fromkeys([*best, *candidates]))[:3]
    all_unknown = sorted(
        gap
        for gap in result.investigation_set.evidence_gap_keys
        if all(
            truth[(candidate_id, gap)][0] == "UNKNOWN"
            for candidate_id in result.investigation_set.candidate_ids
        )
    )
    answer = FinalAnswerV1(
        answer=(
            "只读证据子 Agent 已完成核验；优先项仅依据已验证机况字段。"
            + (
                "以下字段仍无证据，不能作肯定结论：" + "、".join(all_unknown)
                if all_unknown
                else "所有目标字段均按结构化证据回答。"
            )
        ),
        selectedCandidateIds=selected,
        claims=tuple(claims),
        acknowledgedUnknownKeys=tuple(all_unknown),
    )
    return answer, score_final(
        answer,
        report=report,
        truth=truth,
        tool_trace=tool_trace,
        candidate_ids=result.investigation_set.candidate_ids,
    )


async def execute(manifest_path: Path, output: Path) -> int:
    manifest_bytes = manifest_path.read_bytes()
    manifest = json.loads(manifest_bytes.decode("utf-8"))
    if manifest["status"] != "FROZEN_BEFORE_EXECUTION":
        raise RuntimeError("manifest is not frozen")
    for relative, expected in manifest["sourceHashes"].items():
        if sha(ROOT / relative) != expected:
            raise RuntimeError(f"source hash mismatch: {relative}")
    dataset = ROOT / manifest["dataset"]["path"]
    catalog_path = ROOT / manifest["catalog"]["path"]
    if sha(dataset) != manifest["dataset"]["sha256"]:
        raise RuntimeError("dataset hash mismatch")
    if sha(catalog_path) != manifest["catalog"]["sha256"]:
        raise RuntimeError("catalog hash mismatch")
    if not settings.deepseek_api_key or settings.deepseek_model != manifest["model"]:
        raise RuntimeError("provider configuration mismatch")
    output.mkdir(parents=True, exist_ok=False)
    started = {
        "schemaVersion": "multi-agent-runtime-v2-confirmation-start-v1",
        "startedAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "manifestSha256": hashlib.sha256(manifest_bytes).hexdigest(),
    }
    (output / "started.json").write_text(
        json.dumps(started, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    cases = load_jsonl(dataset)
    catalog = {int(row["itemId"]): row for row in load_jsonl(catalog_path)}
    client = DeepSeekToolCompatibleClient(
        api_key=settings.deepseek_api_key,
        base_url=settings.deepseek_base_url,
        timeout=manifest["timeoutSeconds"],
        max_retries=0,
    )
    guard = ResearchMergeGuardV1(
        client=redis.from_url(settings.redis_url, decode_responses=True),
        ttl_seconds=86400,
    )
    rows: list[dict[str, Any]] = []
    try:
        for case in cases:
            began = time.perf_counter()
            responses: list[Any] = []
            tool_calls: list[dict[str, Any]] = []
            scope = candidate_scope({**case, "turns": [{"text": case["currentQuery"]}]})
            observation = tool_trace_for(tuple(scope.ranked_item_ids), catalog)

            class CaptureCompletions:
                async def create(self, **kwargs):
                    response = await client.chat.completions.create(**kwargs)
                    responses.append(response)
                    return response

            capture_client = SimpleNamespace(
                chat=SimpleNamespace(completions=CaptureCompletions())
            )

            async def tool_caller(name: str, arguments: dict[str, Any]):
                tool_calls.append({"name": name, "arguments": arguments})
                return observation

            async def decide(view: dict[str, Any]):
                return await deepseek_research_decision_v2(
                    capture_client,
                    model=manifest["model"],
                    child_view=view,
                    max_tokens=manifest["maxTokens"],
                )

            deadline = datetime.now(timezone.utc) + timedelta(
                seconds=manifest["scenarioDeadlineSeconds"]
            )
            try:
                result = await run_multi_agent_research_v2(
                    candidate_scope=scope,
                    task_revision=1,
                    parent_run_id=f"confirm-{case['scenarioId'].lower()}",
                    parent_context_binding_hash=hashlib.sha256(
                        case["scenarioId"].encode("utf-8")
                    ).hexdigest(),
                    research_goal=case["currentQuery"],
                    hard_unknowns_by_product={},
                    conflicts_by_product={},
                    unknowns_by_product={
                        candidate_id: tuple(case["evidenceGapKeys"])
                        for candidate_id in scope.ranked_item_ids
                    },
                    tool_caller=tool_caller,
                    decide=decide,
                    merge_guard=guard,
                    deadline_at=deadline,
                )
                answer, final_score = canonical_final(result, observation)
                truth = report_truth(result.investigation_set, observation)
                report_score = score_report(result.report, truth)
                serialized_projection = json.dumps(
                    result.parent_projection(), ensure_ascii=False
                )
                response = responses[0]
                usage = getattr(response, "usage", None)
                row = {
                    "scenarioId": case["scenarioId"],
                    "status": "ok",
                    "taskSuccess": bool(
                        final_score["taskSuccess"]
                        and report_score["complete"]
                        and report_score["precision"] == 1.0
                    ),
                    "reportScore": report_score,
                    "finalScore": final_score,
                    "modelCalls": len(responses),
                    "toolCalls": len(tool_calls),
                    "mergeOutcome": result.merge_receipt.outcome,
                    "requestSequence": result.request_envelope.sequence,
                    "replySequence": result.reply_envelope.sequence,
                    "rawChildObservationLeak": (
                        "verifiedToolObservation" in serialized_projection
                        or '"products"' in serialized_projection
                    ),
                    "inputTokens": getattr(usage, "prompt_tokens", None),
                    "outputTokens": getattr(usage, "completion_tokens", None),
                    "tokenStatus": "OBSERVED" if usage is not None else "MISSING",
                    "answer": answer.model_dump(by_alias=True, mode="json"),
                    "durationMs": round((time.perf_counter() - began) * 1000, 3),
                }
            except Exception as exc:
                row = {
                    "scenarioId": case["scenarioId"],
                    "status": "failed",
                    "taskSuccess": False,
                    "errorType": type(exc).__name__,
                    "errorMessage": str(exc)[:240],
                    "modelCalls": len(responses),
                    "toolCalls": len(tool_calls),
                    "durationMs": round((time.perf_counter() - began) * 1000, 3),
                }
            rows.append(row)
            with (output / "traces.jsonl").open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
            print(case["scenarioId"], row["status"], row["taskSuccess"], flush=True)
    finally:
        await client.close()

    durations = [float(row["durationMs"]) for row in rows]
    accepted = all(
        row.get("taskSuccess") is True
        and row.get("modelCalls") == 1
        and row.get("toolCalls") == 1
        and row.get("mergeOutcome") == "ACCEPTED"
        and row.get("requestSequence") == 1
        and row.get("replySequence") == 2
        and row.get("rawChildObservationLeak") is False
        and row.get("tokenStatus") == "OBSERVED"
        for row in rows
    ) and percentile(durations, 0.95) <= manifest["p95LatencyLimitMs"]
    summary = {
        "schemaVersion": "multi-agent-runtime-v2-confirmation-result-v1",
        "status": "BOUNDED_SYNTHETIC_HOLDOUT_ACCEPT" if accepted else "HOLD",
        "scope": "frozen_synthetic_holdout_not_general_superiority_not_production_readiness",
        "scenarioCount": len(rows),
        "taskSuccess": sum(row.get("taskSuccess") is True for row in rows),
        "modelCalls": sum(int(row.get("modelCalls") or 0) for row in rows),
        "toolCalls": sum(int(row.get("toolCalls") or 0) for row in rows),
        "usageComplete": all(row.get("tokenStatus") == "OBSERVED" for row in rows),
        "inputTokens": sum(int(row.get("inputTokens") or 0) for row in rows),
        "outputTokens": sum(int(row.get("outputTokens") or 0) for row in rows),
        "latencyMs": {
            "p50": round(percentile(durations, 0.50), 3),
            "p95": round(percentile(durations, 0.95), 3),
        },
        "productionDefaultsChanged": False,
    }
    (output / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    (output / "RESULT.md").write_text(
        "# Multi-Agent Runtime V2 合成留出确认\n\n"
        f"结论：`{summary['status']}`。\n\n"
        f"- 成功：{summary['taskSuccess']}/{summary['scenarioCount']}\n"
        f"- 子 Agent 模型调用/只读工具调用：{summary['modelCalls']}/{summary['toolCalls']}\n"
        f"- Token：input {summary['inputTokens']}，output {summary['outputTokens']}\n"
        f"- 延迟 P50/P95：{summary['latencyMs']['p50']}/{summary['latencyMs']['p95']} ms\n"
        "- 边界：冻结合成留出确认，不证明普遍优于单 Agent，也不等于生产就绪。\n",
        encoding="utf-8",
    )
    receipt = {
        "schemaVersion": "multi-agent-runtime-v2-confirmation-receipt-v1",
        "completedAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "status": summary["status"],
        "summarySha256": sha(output / "summary.json"),
        "traceSha256": sha(output / "traces.jsonl"),
    }
    (output / "receipt.json").write_text(
        json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    checksum_lines = [
        f"{sha(path)}  {path.name}"
        for path in sorted(output.iterdir())
        if path.name != "SHA256SUMS.txt"
    ]
    (output / "SHA256SUMS.txt").write_text(
        "\n".join(checksum_lines) + "\n", encoding="utf-8"
    )
    return 0 if accepted else 2


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    return asyncio.run(execute(args.manifest, args.output))


if __name__ == "__main__":
    raise SystemExit(main())
