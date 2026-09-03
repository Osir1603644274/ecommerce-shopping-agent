"""Independent, read-only recomputation for the frozen V4 formal attempt."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[3]
PACKAGE_DIR = Path(__file__).resolve().parent
ATTEMPT_DIR = PACKAGE_DIR / "attempt001"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def percentile(values: list[float], probability: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * probability
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return float(ordered[lower])
    fraction = position - lower
    return float(ordered[lower] + (ordered[upper] - ordered[lower]) * fraction)


def aggregate(rows: list[dict[str, Any]], arm: str) -> dict[str, Any]:
    arm_rows = [row for row in rows if row["arm"] == arm]
    succeeded = [row for row in arm_rows if row["status"] == "SUCCEEDED"]
    observed = [
        row
        for row in succeeded
        if all(row["usage"].get(field) is not None for field in (
            "promptTokens", "completionTokens", "totalTokens"
        ))
    ]
    latency = [float(row["durationMs"]) for row in succeeded]
    prompt = [float(row["usage"]["promptTokens"]) for row in observed]
    completion = [float(row["usage"]["completionTokens"]) for row in observed]
    total = [float(row["usage"]["totalTokens"]) for row in observed]
    cached = [
        int(row["usage"]["cachedInputTokens"])
        for row in observed
        if row["usage"].get("cachedInputTokens") is not None
    ]
    field_names = tuple(next(row for row in succeeded)["expected"])
    return {
        "caseCount": len(arm_rows),
        "succeeded": len(succeeded),
        "failed": len(arm_rows) - len(succeeded),
        "exactFidelity": sum(row.get("actual") == row.get("expected") for row in arm_rows),
        "fieldFidelity": {
            field: sum(
                row.get("actual", {}).get(field) == row.get("expected", {}).get(field)
                and field in row.get("actual", {})
                for row in arm_rows
            )
            for field in field_names
        },
        "usageObserved": len(observed),
        "promptTokensSum": int(sum(prompt)),
        "promptTokensP50": percentile(prompt, 0.5),
        "promptTokensP95": percentile(prompt, 0.95),
        "completionTokensSum": int(sum(completion)),
        "totalTokensSum": int(sum(total)),
        "totalTokensP50": percentile(total, 0.5),
        "totalTokensP95": percentile(total, 0.95),
        "cachedInputTokensSum": sum(cached) if cached else None,
        "latencyP50Ms": percentile(latency, 0.5),
        "latencyP95Ms": percentile(latency, 0.95),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--write", action="store_true")
    args = parser.parse_args()
    manifest_path = PACKAGE_DIR / "manifest.json"
    started_path = ATTEMPT_DIR / "started.json"
    traces_path = ATTEMPT_DIR / "traces.jsonl"
    result_path = ATTEMPT_DIR / "result.json"
    receipt_path = ATTEMPT_DIR / "receipt.json"
    checksum_path = ATTEMPT_DIR / "SHA256SUMS.txt"
    smoke_path = PACKAGE_DIR / "compatibility-smoke001/smoke.json"
    manifest = load_json(manifest_path)
    started = load_json(started_path)
    traces = load_jsonl(traces_path)
    result = load_json(result_path)
    receipt = load_json(receipt_path)
    smoke = load_json(smoke_path)

    source_hashes = {
        relative: sha256(ROOT / relative)
        for relative in manifest["sourceFreeze"]
    }
    checksum_rows: dict[str, str] = {}
    for line in checksum_path.read_text(encoding="utf-8").splitlines():
        digest, name = line.split(maxsplit=1)
        checksum_rows[name.strip()] = digest
    checksum_actual = {
        name: sha256(ATTEMPT_DIR / name)
        for name in checksum_rows
    }
    recomputed = {arm: aggregate(traces, arm) for arm in ("CTX1a", "CTX1b")}
    case_count = manifest["dataset"]["turnCaseCount"]
    prompt_reduction = (
        (recomputed["CTX1a"]["promptTokensSum"] - recomputed["CTX1b"]["promptTokensSum"])
        / recomputed["CTX1a"]["promptTokensSum"]
    )
    total_reduction = (
        (recomputed["CTX1a"]["totalTokensSum"] - recomputed["CTX1b"]["totalTokensSum"])
        / recomputed["CTX1a"]["totalTokensSum"]
    )
    p95_ratio = (
        recomputed["CTX1b"]["latencyP95Ms"]
        / recomputed["CTX1a"]["latencyP95Ms"]
    )
    gates = {
        "deterministicPreflight": result["preflight"]["status"] == "PASS",
        "allCallsSucceeded": all(recomputed[arm]["succeeded"] == case_count for arm in recomputed),
        "allUsageObserved": all(recomputed[arm]["usageObserved"] == case_count for arm in recomputed),
        "allExactFidelity": all(recomputed[arm]["exactFidelity"] == case_count for arm in recomputed),
        "allFieldsExact": all(
            count == case_count
            for arm in recomputed
            for count in recomputed[arm]["fieldFidelity"].values()
        ),
        "treatmentPromptTokenReductionAtLeast3Pct": prompt_reduction >= 0.03,
        "treatmentTotalTokenReductionAtLeast2Pct": total_reduction >= 0.02,
        "treatmentP95LatencyWithin20Pct": p95_ratio <= 1.20,
        "zeroRetries": True,
    }
    expected_verdict = (
        "BOUNDED_CONTEXT_PROVIDER_ACCEPT"
        if all(gates.values())
        else "HOLD_CONTEXT_PROVIDER_PAIR"
    )
    checks = {
        "manifestFrozen": manifest["status"] == "FROZEN_BEFORE_EXECUTION",
        "datasetHash": sha256(ROOT / manifest["dataset"]["path"]) == manifest["dataset"]["sha256"],
        "lineageHash": sha256(ROOT / manifest["dataset"]["lineagePath"]) == manifest["dataset"]["lineageSha256"],
        "sourceFreeze": source_hashes == manifest["sourceFreeze"],
        "smokePassedUnscored": smoke.get("status") == "PASS" and smoke.get("scored") is False,
        "startedManifestBinding": started["manifestSha256"] == sha256(manifest_path),
        "startedRunnerBinding": started["runnerSha256"] == sha256(PACKAGE_DIR / "runner.py"),
        "startedCompilerBinding": started["compilerSha256"] == sha256(ROOT / "agent/app/context_compiler_v1.py"),
        "startedReferenceResolverBinding": started["a2ReferenceResolverSha256"] == sha256(ROOT / "agent/app/reference_context.py"),
        "traceCount": len(traces) == manifest["expectedProviderCalls"],
        "providerCallOrdinals": [row["providerCallOrdinal"] for row in traces] == list(range(1, len(traces) + 1)),
        "pairCompleteness": all(
            {row["arm"] for row in traces if row["caseId"] == case_id} == {"CTX1a", "CTX1b"}
            for case_id in {row["caseId"] for row in traces}
        ),
        "aggregateExact": recomputed == result["aggregate"],
        "effectsExact": result["pairedEffects"] == {
            "promptTokenReductionFraction": prompt_reduction,
            "totalTokenReductionFraction": total_reduction,
            "treatmentToControlP95LatencyRatio": p95_ratio,
        },
        "gatesExact": gates == result["gates"],
        "verdictExact": result["verdict"] == expected_verdict == receipt["verdict"],
        "receiptBindings": all((
            receipt["manifestSha256"] == sha256(manifest_path),
            receipt["startedSha256"] == sha256(started_path),
            receipt["tracesSha256"] == sha256(traces_path),
            receipt["resultSha256"] == sha256(result_path),
        )),
        "attemptChecksums": checksum_rows == checksum_actual,
    }
    verification = {
        "schemaVersion": "context-compiler-provider-independent-verification-v4",
        "status": "PASS" if all(checks.values()) else "FAIL",
        "formalVerdict": result["verdict"],
        "checks": checks,
        "recomputedAggregate": recomputed,
        "recomputedEffects": {
            "promptTokenReductionFraction": prompt_reduction,
            "totalTokenReductionFraction": total_reduction,
            "treatmentToControlP95LatencyRatio": p95_ratio,
        },
        "recomputedGates": gates,
        "failedFormalGates": [name for name, passed in gates.items() if not passed],
    }
    if args.write:
        (PACKAGE_DIR / "verification.json").write_text(
            json.dumps(verification, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
            newline="\n",
        )
    print(json.dumps(verification, ensure_ascii=False, indent=2))
    return 0 if verification["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())

