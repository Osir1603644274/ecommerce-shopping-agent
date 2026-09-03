from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import sys
import time
from pathlib import Path
from typing import Any


BASE = Path(__file__).resolve().parent
AGENT_ROOT = BASE.parents[1]
SCENARIOS = BASE / "scenarios.jsonl"
MANIFEST = BASE / "manifest.json"
ATTEMPT = BASE / "attempt001"
WORKER_SOURCE = AGENT_ROOT / "app" / "memory_candidate_worker.py"
PARENT = BASE.parent / "shopping_memory_extraction_v16_20260830" / "attempt001"
PARENT_RECEIPT = PARENT / "receipt.json"
PARENT_REPORT = PARENT / "report.json"
CALL_LAYER = (
    "memory_candidate_extraction_only; excludes final_answer, task_manager, "
    "task_state, and react decision calls"
)
SCENARIO_FIELDS = {
    "scenarioId", "split", "message", "expectedGate", "expectedSensitive",
    "expectedRecipient", "expectedModelCall", "expectedOutcome", "expected",
}
OUTCOMES = {
    "accepted", "empty", "rule_not_explicit", "recipient_suppressed",
    "sensitive_suppressed", "ambiguous_input_suppressed",
}
MANIFEST_FIELDS = {
    "schemaVersion", "scope", "scenarioCount", "expectedModelAttemptCount",
    "scenarioSha256", "runnerSha256", "workerSha256", "promptSha256",
    "catalogSha256", "catalogRevision", "model", "providerBaseUrl",
    "openaiSdkVersion", "timeoutSeconds", "maxRetries", "callLayer",
    "parentReceiptSha256", "parentReportSha256",
}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def text_sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def canonical(value: Any) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), allow_nan=False,
    )


def percentile(values: list[float], quantile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, math.ceil(quantile * len(ordered)) - 1))
    return ordered[index]


def triples(
    items: list[dict[str, Any]], *, exact_schema: bool,
) -> list[dict[str, str]]:
    keys = ("preferenceKind", "attributeKey", "normalizedValue")
    required = set(keys)
    result: list[dict[str, str]] = []
    for item in items:
        if type(item) is not dict:
            raise RuntimeError("invalid preference schema")
        if exact_schema and set(item) != required:
            raise RuntimeError("invalid preference schema")
        if not exact_schema and not required.issubset(item):
            raise RuntimeError("invalid preference schema")
        if any(type(item[key]) is not str or not item[key] for key in keys):
            raise RuntimeError("invalid preference value")
        if item["preferenceKind"] not in {"prefer", "avoid", "indifferent"}:
            raise RuntimeError("invalid preference kind")
        result.append({key: item[key] for key in keys})
    if len(result) > 3:
        raise RuntimeError("too many preferences")
    return sorted(result, key=lambda row: tuple(row[key] for key in keys))


def load_manifest() -> dict[str, Any]:
    raw = json.loads(MANIFEST.read_text(encoding="utf-8"))
    if type(raw) is not dict or set(raw) != MANIFEST_FIELDS:
        raise RuntimeError("invalid manifest schema")
    if raw["schemaVersion"] != "shopping-memory-extraction-recovery-manifest-v2":
        raise RuntimeError("invalid manifest version")
    if type(raw["scenarioCount"]) is not int or raw["scenarioCount"] <= 0:
        raise RuntimeError("invalid scenario count")
    if type(raw["expectedModelAttemptCount"]) is not int:
        raise RuntimeError("invalid expected model attempt count")
    return raw


def load_scenarios(expected_count: int, expected_attempts: int) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line in SCENARIOS.read_text(encoding="utf-8").splitlines():
        row = json.loads(line)
        if type(row) is not dict or set(row) != SCENARIO_FIELDS:
            raise RuntimeError("invalid scenario schema")
        if type(row["scenarioId"]) is not str or not row["scenarioId"]:
            raise RuntimeError("invalid scenario id")
        if row["split"] not in {"development", "confirmation"}:
            raise RuntimeError("invalid split")
        if type(row["message"]) is not str or not 1 <= len(row["message"]) <= 2000:
            raise RuntimeError("invalid message")
        for name in ("expectedGate", "expectedSensitive", "expectedModelCall"):
            if type(row[name]) is not bool:
                raise RuntimeError(f"invalid {name}")
        if row["expectedRecipient"] not in {"self", "other", None}:
            raise RuntimeError("invalid expected recipient")
        if row["expectedOutcome"] not in OUTCOMES:
            raise RuntimeError("invalid expected outcome")
        if type(row["expected"]) is not list:
            raise RuntimeError("invalid expected")
        expected = triples(row["expected"], exact_schema=True)
        if row["expectedModelCall"] is not (
            row["expectedOutcome"] in {"accepted", "empty"}
        ):
            raise RuntimeError("invalid expected call layering")
        if bool(expected) is not (row["expectedOutcome"] == "accepted"):
            raise RuntimeError("invalid expected preference/outcome")
        if row["expectedSensitive"] is not (
            row["expectedOutcome"] == "sensitive_suppressed"
        ):
            raise RuntimeError("invalid sensitive/outcome")
        rows.append(row)
    ids = [row["scenarioId"] for row in rows]
    if len(rows) != expected_count or len(ids) != len(set(ids)):
        raise RuntimeError("scenario identity/count mismatch")
    if sum(row["split"] == "development" for row in rows) != expected_count // 2:
        raise RuntimeError("development count mismatch")
    if sum(row["split"] == "confirmation" for row in rows) != expected_count // 2:
        raise RuntimeError("confirmation count mismatch")
    if sum(row["expectedModelCall"] for row in rows) != expected_attempts:
        raise RuntimeError("expected model attempt mismatch")
    return rows


def accuracy(rows: list[dict[str, Any]], field: str) -> float:
    return sum(bool(row[field]) for row in rows) / len(rows) if rows else 0.0


async def run() -> int:
    if ATTEMPT.exists():
        raise RuntimeError("attempt001 already exists; recovery evaluation cannot rerun")
    manifest = load_manifest()
    scenarios = load_scenarios(
        manifest["scenarioCount"], manifest["expectedModelAttemptCount"]
    )
    static_hashes = {
        "scenarioSha256": sha256(SCENARIOS),
        "runnerSha256": sha256(Path(__file__)),
        "workerSha256": sha256(WORKER_SOURCE),
        "parentReceiptSha256": sha256(PARENT_RECEIPT),
        "parentReportSha256": sha256(PARENT_REPORT),
    }
    for key, actual in static_hashes.items():
        if manifest[key] != actual:
            raise RuntimeError(f"hash mismatch: {key}")
    parent_report = json.loads(PARENT_REPORT.read_text(encoding="utf-8"))
    if parent_report.get("decision") != "HOLD_LLM_EXTRACTION_RECOVERY":
        raise RuntimeError("parent is not the consumed HOLD evidence")

    sys.path.insert(0, str(AGENT_ROOT))
    import openai
    from app import memory_candidate_worker as worker
    from app.settings import settings

    catalog_path = Path(settings.memory_catalog_values_path)
    runtime = {
        "promptSha256": text_sha256(worker._EXTRACTION_SYSTEM_PROMPT),
        "catalogSha256": sha256(catalog_path),
        "catalogRevision": settings.memory_active_catalog_revision,
        "model": settings.deepseek_model,
        "providerBaseUrl": settings.deepseek_base_url,
        "openaiSdkVersion": openai.__version__,
        "timeoutSeconds": worker._EXTRACTION_TIMEOUT_SECONDS,
        "maxRetries": worker._EXTRACTION_MAX_RETRIES,
        "callLayer": CALL_LAYER,
    }
    for key, actual in runtime.items():
        if manifest[key] != actual:
            raise RuntimeError(f"runtime mismatch: {key}")
    if not settings.deepseek_api_key:
        raise RuntimeError("model identity unavailable")
    allowed = {
        (row["attributeKey"], row["normalizedValue"])
        for row in worker._catalog()
    }
    for scenario in scenarios:
        for item in triples(scenario["expected"], exact_schema=True):
            if (item["attributeKey"], item["normalizedValue"]) not in allowed:
                raise RuntimeError("expected preference escapes catalog")

    os.mkdir(ATTEMPT)
    started = {
        "schemaVersion": "shopping-memory-extraction-recovery-started-v2",
        "startedAtEpochMs": int(time.time() * 1000),
        "manifestSha256": sha256(MANIFEST),
        **static_hashes,
        **runtime,
    }
    (ATTEMPT / "started.json").write_text(
        canonical(started) + "\n", encoding="utf-8"
    )

    traces: list[dict[str, Any]] = []
    for scenario in scenarios:
        gate = worker.is_explicit_memory_request(scenario["message"])
        sensitive = worker.is_sensitive_memory_request(scenario["message"])
        recipient = worker.current_request_recipient_scope(scenario["message"])
        actual: list[dict[str, str]] = []
        error: str | None = None
        observation = {
            "modelCalled": False, "model": None,
            "outcome": "runner_not_observed", "durationMs": 0.0,
            "usageObserved": False, "promptTokens": 0,
            "completionTokens": 0, "totalTokens": 0,
        }
        try:
            extracted, observed = await worker._extract_observed(
                scenario["message"], "phone", recipient or ""
            )
            observation = observed.plain()
            actual = triples(extracted, exact_schema=False)
        except worker.MemoryExtractionFailure as exc:
            observation = exc.observation.plain()
            error = exc.cause_type
        except Exception as exc:
            error = type(exc).__name__
            observation = {**observation, "outcome": "runner_exception"}
        expected = triples(scenario["expected"], exact_schema=True)
        positive = bool(expected) and (
            actual == expected and observation["outcome"] == "accepted"
            and error is None
        )
        negative = not expected and (
            not actual and observation["outcome"] == scenario["expectedOutcome"]
            and error is None
        )
        traces.append({
            "scenarioId": scenario["scenarioId"], "split": scenario["split"],
            "message": scenario["message"], "expected": expected, "actual": actual,
            "expectedGate": scenario["expectedGate"], "actualGate": gate,
            "expectedSensitive": scenario["expectedSensitive"],
            "actualSensitive": sensitive,
            "expectedRecipient": scenario["expectedRecipient"],
            "actualRecipient": recipient,
            "expectedModelCall": scenario["expectedModelCall"],
            "expectedOutcome": scenario["expectedOutcome"],
            "observation": observation,
            "gateMatch": gate is scenario["expectedGate"],
            "sensitiveMatch": sensitive is scenario["expectedSensitive"],
            "recipientMatch": recipient == scenario["expectedRecipient"],
            "modelCallMatch": observation["modelCalled"] is scenario["expectedModelCall"],
            "outcomeMatch": observation["outcome"] == scenario["expectedOutcome"],
            "catalogBound": all(
                (item["attributeKey"], item["normalizedValue"]) in allowed
                for item in actual
            ),
            "positiveExactMatch": positive,
            "negativeSemanticMatch": negative,
            "error": error,
        })

    (ATTEMPT / "trace.jsonl").write_text(
        "".join(canonical(row) + "\n" for row in traces), encoding="utf-8"
    )
    positives = [row for row in traces if row["expected"]]
    negatives = [row for row in traces if not row["expected"]]
    confirmation_positives = [
        row for row in positives if row["split"] == "confirmation"
    ]
    attempts = [row for row in traces if row["observation"]["modelCalled"]]
    usage_missing = [
        row["scenarioId"] for row in attempts
        if not row["observation"]["usageObserved"]
    ]
    false_positives = [row["scenarioId"] for row in negatives if row["actual"]]
    latencies = [float(row["observation"]["durationMs"]) for row in attempts]
    gates = {
        "gateAccuracyOne": all(row["gateMatch"] for row in traces),
        "sensitiveAccuracyOne": all(row["sensitiveMatch"] for row in traces),
        "recipientAccuracyOne": all(row["recipientMatch"] for row in traces),
        "modelCallLayeringExact": all(row["modelCallMatch"] for row in traces),
        "modelAttemptCountExact": len(attempts) == manifest["expectedModelAttemptCount"],
        "outcomeAccuracyOne": all(row["outcomeMatch"] for row in traces),
        "catalogEscapeCountZero": all(row["catalogBound"] for row in traces),
        "falsePositiveCountZero": not false_positives,
        "exceptionCountZero": all(row["error"] is None for row in traces),
        "tokenAccountingComplete": not usage_missing,
        "positiveExactAccuracyOne": accuracy(positives, "positiveExactMatch") == 1.0,
        "confirmationPositiveExactAccuracyOne": (
            accuracy(confirmation_positives, "positiveExactMatch") == 1.0
        ),
        "negativeSemanticAccuracyOne": accuracy(negatives, "negativeSemanticMatch") == 1.0,
        "ambiguousInputsSuppressedBeforeModel": all(
            row["observation"]["outcome"] == "ambiguous_input_suppressed"
            and not row["observation"]["modelCalled"]
            for row in traces
            if row["expectedOutcome"] == "ambiguous_input_suppressed"
        ),
    }
    decision = (
        "BOUNDED_LLM_EXTRACTION_RECOVERY_V2_ACCEPT"
        if all(gates.values()) else "HOLD_LLM_EXTRACTION_RECOVERY_V2"
    )
    report = {
        "schemaVersion": "shopping-memory-extraction-recovery-report-v2",
        "decision": decision, "scope": manifest["scope"],
        "parentDecision": parent_report["decision"], "callLayer": CALL_LAYER,
        "scenarioCount": len(traces),
        "developmentCount": sum(row["split"] == "development" for row in traces),
        "confirmationCount": sum(row["split"] == "confirmation" for row in traces),
        "positiveScenarioCount": len(positives),
        "positiveExactAccuracy": accuracy(positives, "positiveExactMatch"),
        "confirmationPositiveExactAccuracy": accuracy(
            confirmation_positives, "positiveExactMatch"
        ),
        "negativeScenarioCount": len(negatives),
        "negativeSemanticAccuracy": accuracy(negatives, "negativeSemanticMatch"),
        "falsePositiveScenarioIds": false_positives,
        "modelAttemptCount": len(attempts),
        "usageObservedAttemptCount": len(attempts) - len(usage_missing),
        "usageUnavailableScenarioIds": usage_missing,
        "tokenAccountingStatus": "complete" if not usage_missing else "partial",
        "promptTokens": sum(row["observation"]["promptTokens"] for row in traces),
        "completionTokens": sum(
            row["observation"]["completionTokens"] for row in traces
        ),
        "totalTokens": sum(row["observation"]["totalTokens"] for row in traces),
        "latencyP50Ms": percentile(latencies, 0.50),
        "latencyP95Ms": percentile(latencies, 0.95),
        "gates": gates,
    }
    (ATTEMPT / "report.json").write_text(
        canonical(report) + "\n", encoding="utf-8"
    )
    receipt = {
        "schemaVersion": "shopping-memory-extraction-recovery-receipt-v2",
        "decision": decision, "manifestSha256": sha256(MANIFEST),
        **static_hashes, **runtime,
        "traceSha256": sha256(ATTEMPT / "trace.jsonl"),
        "reportSha256": sha256(ATTEMPT / "report.json"),
    }
    (ATTEMPT / "receipt.json").write_text(
        canonical(receipt) + "\n", encoding="utf-8"
    )
    names = ["started.json", "trace.jsonl", "report.json", "receipt.json"]
    (ATTEMPT / "SHA256SUMS.txt").write_text(
        "".join(f"{sha256(ATTEMPT / name)}  {name}\n" for name in names),
        encoding="ascii",
    )
    print(canonical(report))
    return 0 if decision == "BOUNDED_LLM_EXTRACTION_RECOVERY_V2_ACCEPT" else 2


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run()))
