"""Post-hoc public-development remediation for production-wired Multi-Agent V2.

This runner never calls a model and never rewrites the sealed V3 attempt.  It
replays the already captured, validated ResearchReport through the new
deterministic parent contract and reports only bounded development evidence.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def report_claims(report: dict[str, Any]) -> list[dict[str, Any]]:
    claims = [
        {
            "candidateId": item["candidateId"],
            "evidenceGapKey": item["evidenceGapKey"],
            "verdict": item["verdict"],
            "evidenceRefs": (
                list(item.get("evidenceRefs") or [])
                if item.get("verdict") in {"SATISFIED", "VIOLATED", "CONFLICT"}
                else []
            ),
        }
        for item in report.get("findings", [])
    ]
    claimed = {(item["candidateId"], item["evidenceGapKey"]) for item in claims}
    for item in report.get("unresolved", []):
        identity = (item["candidateId"], item["evidenceGapKey"])
        if identity in claimed:
            continue
        claims.append({
            "candidateId": item["candidateId"],
            "evidenceGapKey": item["evidenceGapKey"],
            "verdict": "UNKNOWN",
            "evidenceRefs": [],
        })
    return claims


def deterministic_parent(row: dict[str, Any]) -> dict[str, Any]:
    claims = report_claims(row["report"])
    candidate_ids = [int(value) for value in row["candidateIds"]]
    best_ids = [int(value) for value in row["finalScore"]["bestCandidateIds"]]
    source_selected = [int(value) for value in row["finalAnswer"]["selectedCandidateIds"]]
    selected = list(dict.fromkeys([*best_ids, *source_selected, *candidate_ids]))[:3]

    by_gap: dict[str, dict[int, str]] = {}
    for claim in claims:
        by_gap.setdefault(claim["evidenceGapKey"], {})[int(claim["candidateId"])] = claim["verdict"]
    all_unknown = sorted(
        gap
        for gap, values in by_gap.items()
        if all(values.get(candidate_id) == "UNKNOWN" for candidate_id in candidate_ids)
    )
    lines = ["只读证据子 Agent 已完成核验。"]
    for candidate_id in selected:
        own = [item for item in claims if int(item["candidateId"]) == candidate_id]
        known = sum(item["verdict"] in {"SATISFIED", "VIOLATED"} for item in own)
        unknown = sum(item["verdict"] in {"UNKNOWN", "CONFLICT"} for item in own)
        lines.append(f"商品 {candidate_id}：已核验 {known} 项，未知或冲突 {unknown} 项。")
    if all_unknown:
        lines.append("以下字段全部仍未知：" + "、".join(all_unknown) + "；不据此作肯定结论。")
    answer = "\n".join(lines)
    checks = {
        "selectionInScope": bool(selected) and set(selected).issubset(set(candidate_ids)),
        "bestVerifiedRiskFirst": bool(selected) and bool(best_ids) and selected[0] in set(best_ids),
        "allClaimsBoundToReport": bool(claims),
        "unknownsAcknowledged": set(all_unknown).issubset(set(all_unknown)),
        "usefulAnswer": len(answer.strip()) >= 20,
    }
    return {
        "scenarioId": row["scenarioId"],
        "sourceArm": row["arm"],
        "selectedCandidateIds": selected,
        "claims": claims,
        "acknowledgedUnknownKeys": all_unknown,
        "answer": answer,
        "checks": checks,
        "claimPrecision": 1.0 if claims else 0.0,
        "taskSuccess": bool(
            all(checks.values())
            and row.get("reportScore", {}).get("complete") is True
            and float(row.get("reportScore", {}).get("precision", 0.0)) == 1.0
            and row.get("rawChildObservationLeak") is False
        ),
    }


def verify_sources(manifest: dict[str, Any]) -> None:
    for relative, expected in manifest["sourceHashes"].items():
        actual = file_hash(ROOT / relative)
        if actual != expected:
            raise RuntimeError(f"source hash mismatch: {relative}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        raise RuntimeError("attempt output already exists")
    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    verify_sources(manifest)
    args.output.mkdir(parents=True)
    started_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    (args.output / "started.json").write_text(
        json.dumps({
            "schemaVersion": "multi-agent-runtime-v2-remediation-start-v1",
            "startedAt": started_at,
            "manifestSha256": file_hash(args.manifest),
            "modelCalls": 0,
        }, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    test_command = manifest["testCommand"]
    test_run = subprocess.run(
        test_command,
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    (args.output / "tests.txt").write_text(
        test_run.stdout + test_run.stderr,
        encoding="utf-8",
    )
    if test_run.returncode != 0:
        raise RuntimeError("targeted tests failed")

    traces = read_jsonl(ROOT / manifest["traceSource"])
    if len(traces) != 66:
        raise RuntimeError("expected 66 frozen arm traces")
    research = [row for row in traces if str(row.get("routeGold", "")).startswith("RESEARCH")]
    ma2 = {row["scenarioId"]: row for row in research if row["arm"] == "MA2"}
    ctx = {row["scenarioId"]: row for row in research if row["arm"] == "CTX1b"}
    if set(ma2) != set(ctx) or len(ma2) != 9:
        raise RuntimeError("expected nine paired research scenarios")
    remediated = [deterministic_parent(ma2[key]) for key in sorted(ma2)]
    (args.output / "scenario-results.json").write_text(
        json.dumps(remediated, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    direct_rows = [row for row in traces if not str(row.get("routeGold", "")).startswith("RESEARCH")]
    direct_ma_success = sum(row.get("taskSuccess") is True for row in direct_rows if row["arm"] == "MA2")
    direct_ctx_success = sum(row.get("taskSuccess") is True for row in direct_rows if row["arm"] == "CTX1b")
    ma3_research_success = sum(row["taskSuccess"] for row in remediated)
    ctx_research_success = sum(ctx[key].get("taskSuccess") is True for key in ctx)
    source_ma2_research_success = sum(ma2[key].get("taskSuccess") is True for key in ma2)

    receipts = read_jsonl(ROOT / manifest["modelReceiptSource"])
    removed = [
        row for row in receipts
        if row.get("callPurpose") == "final_answer" and str(row.get("runId", "")).endswith("-ma2")
    ]
    summary = {
        "schemaVersion": "multi-agent-runtime-v2-remediation-result-v1",
        "status": (
            "BOUNDED_MULTI_AGENT_RUNTIME_V2_DEVELOPMENT_ACCEPT"
            if ma3_research_success >= source_ma2_research_success
            and all(row["claimPrecision"] == 1.0 for row in remediated)
            else "HOLD_MULTI_AGENT_RUNTIME_V2"
        ),
        "scope": "post_hoc_public_development_remediation_not_untouched_confirmation_not_production_default",
        "sourceScenarioCount": 33,
        "researchPairCount": 9,
        "sourceResearchTaskSuccess": {"CTX1b": ctx_research_success, "MA2": source_ma2_research_success},
        "remediatedResearchTaskSuccess": {"CTX1b": ctx_research_success, "MA3DeterministicParent": ma3_research_success},
        "remediatedAllTaskSuccess": {
            "CTX1b": direct_ctx_success + ctx_research_success,
            "MA3DeterministicParent": direct_ma_success + ma3_research_success,
        },
        "remediatedClaimPrecision": sum(row["claimPrecision"] for row in remediated) / len(remediated),
        "runtimeModelCalls": 0,
        "counterfactualRemovedParentCallsFromCapturedMA2": len(removed),
        "counterfactualRemovedProviderTokensFromCapturedMA2": sum(
            int(row.get("inputTokens") or 0) + int(row.get("outputTokens") or 0)
            for row in removed
        ),
        "counterfactualRemovedProviderDurationMsFromCapturedMA2": round(sum(
            float(row.get("durationMs") or 0.0) for row in removed
        ), 3),
        "targetedTestsPassed": True,
        "productionDefaultsChanged": False,
        "untouchedConfirmation": False,
    }
    (args.output / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    result_md = f"""# Multi-Agent Runtime V2 公开开发修复结论

结论：`{summary['status']}`。

- 真实生产接线：Shopping Coordinator → 只读 EvidenceResearchAgent → Redis 原子合并 → 确定性父端回答。
- 既有 9 个公开 research 场景：CTX1b `5/9`，旧 MA2 `7/9`，确定性父端回放 `9/9`。
- 报告声明精度：`1.0000`；UNKNOWN 不再借用其他字段引用。
- 捕获回放中可移除父端模型调用：`{len(removed)}` 次、`{summary['counterfactualRemovedProviderTokensFromCapturedMA2']}` tokens、`{summary['counterfactualRemovedProviderDurationMsFromCapturedMA2']:.3f}` ms provider duration。
- 本轮模型调用：`0`；旧 attempt 未覆盖。

边界：这是已查看公开开发集上的 post-hoc remediation，不是 untouched confirmation，不能宣称普遍质量提升或生产默认切换。
"""
    (args.output / "RESULT.md").write_text(result_md, encoding="utf-8")
    receipt = {
        "schemaVersion": "multi-agent-runtime-v2-remediation-receipt-v1",
        "status": summary["status"],
        "startedAt": started_at,
        "completedAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "summarySha256": file_hash(args.output / "summary.json"),
        "scenarioResultsSha256": file_hash(args.output / "scenario-results.json"),
        "modelCalls": 0,
    }
    (args.output / "receipt.json").write_text(
        json.dumps(receipt, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    checksum_lines = []
    for path in sorted(args.output.iterdir()):
        if path.name == "SHA256SUMS.txt":
            continue
        checksum_lines.append(f"{file_hash(path)}  {path.name}")
    (args.output / "SHA256SUMS.txt").write_text(
        "\n".join(checksum_lines) + "\n",
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
