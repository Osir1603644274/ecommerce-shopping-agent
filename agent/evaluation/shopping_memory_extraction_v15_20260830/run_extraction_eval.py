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

SCENARIO_FIELDS = {
    "scenarioId",
    "split",
    "message",
    "expectedGate",
    "expectedSensitive",
    "expectedRecipient",
    "expectedModelCall",
    "expectedOutcome",
    "expected",
}
EXPECTED_OUTCOMES = {
    "accepted",
    "empty",
    "rule_not_explicit",
    "recipient_suppressed",
    "sensitive_suppressed",
}
MANIFEST_FIELDS = {
    "schemaVersion",
    "scope",
    "scenarioCount",
    "scenarioSha256",
    "runnerSha256",
    "workerSha256",
    "promptSha256",
    "catalogSha256",
    "catalogRevision",
    "model",
    "providerBaseUrl",
    "openaiSdkVersion",
    "timeoutSeconds",
    "maxRetries",
    "callLayer",
}
CALL_LAYER = (
    "memory_candidate_extraction_only; excludes final_answer, task_manager, "
    "task_state, and react decision calls"
)


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
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def percentile(values: list[float], quantile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, math.ceil(quantile * len(ordered)) - 1))
    return ordered[index]


def _preference_triples(
    items: list[dict[str, Any]], *, exact_schema: bool = True,
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
        raise RuntimeError("too many expected preferences")
    return sorted(result, key=lambda row: tuple(row[key] for key in keys))


def load_scenarios(expected_count: int) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line in SCENARIOS.read_text(encoding="utf-8").splitlines():
        row = json.loads(line)
        if type(row) is not dict or set(row) != SCENARIO_FIELDS:
            raise RuntimeError("invalid scenario schema")
        if type(row["scenarioId"]) is not str or not row["scenarioId"]:
            raise RuntimeError("invalid scenario id")
        if row["split"] not in {"development", "validation"}:
            raise RuntimeError("invalid split")
        if type(row["message"]) is not str or not 1 <= len(row["message"]) <= 2000:
            raise RuntimeError("invalid message")
        for name in ("expectedGate", "expectedSensitive", "expectedModelCall"):
            if type(row[name]) is not bool:
                raise RuntimeError(f"invalid {name}")
        if row["expectedRecipient"] not in {"self", "other", None}:
            raise RuntimeError("invalid expected recipient")
        if row["expectedOutcome"] not in EXPECTED_OUTCOMES:
            raise RuntimeError("invalid expected outcome")
        if type(row["expected"]) is not list:
            raise RuntimeError("invalid expected preferences")
        expected = _preference_triples(row["expected"])
        model_outcome = row["expectedOutcome"] in {"accepted", "empty"}
        if row["expectedModelCall"] is not model_outcome:
            raise RuntimeError("expected model layering mismatch")
        if bool(expected) is not (row["expectedOutcome"] == "accepted"):
            raise RuntimeError("expected preference/outcome mismatch")
        if row["expectedSensitive"] is not (
            row["expectedOutcome"] == "sensitive_suppressed"
        ):
            raise RuntimeError("expected sensitive/outcome mismatch")
        rows.append(row)
    ids = [row["scenarioId"] for row in rows]
    if len(rows) != expected_count or len(ids) != len(set(ids)):
        raise RuntimeError("scenario identity/count mismatch")
    if sum(row["split"] == "development" for row in rows) != expected_count // 2:
        raise RuntimeError("development split mismatch")
    if sum(row["split"] == "validation" for row in rows) != expected_count // 2:
        raise RuntimeError("validation split mismatch")
    return rows


def load_manifest() -> dict[str, Any]:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    if type(manifest) is not dict or set(manifest) != MANIFEST_FIELDS:
        raise RuntimeError("invalid manifest schema")
    if manifest["schemaVersion"] != "shopping-memory-extraction-manifest-v2":
        raise RuntimeError("invalid manifest version")
    if type(manifest["scenarioCount"]) is not int or manifest["scenarioCount"] <= 0:
        raise RuntimeError("invalid manifest scenario count")
    return manifest


def split_accuracy(
    traces: list[dict[str, Any]], split: str, field: str, *, positive: bool,
) -> float:
    selected = [
        row for row in traces
        if row["split"] == split and bool(row["expected"]) is positive
    ]
    return sum(bool(row[field]) for row in selected) / len(selected) if selected else 0.0


def positive_exact_match(
    expected: list[dict[str, str]],
    actual: list[dict[str, str]],
    outcome: str,
    error: str | None,
) -> bool:
    return bool(expected) and actual == expected and outcome == "accepted" and error is None


def negative_semantic_match(
    expected: list[dict[str, str]],
    actual: list[dict[str, str]],
    outcome: str,
    expected_outcome: str,
    error: str | None,
) -> bool:
    return (
        not expected
        and not actual
        and outcome == expected_outcome
        and error is None
    )


async def run() -> int:
    if ATTEMPT.exists():
        raise RuntimeError("attempt001 already exists; this frozen evaluation cannot rerun")
    manifest = load_manifest()
    scenarios = load_scenarios(manifest["scenarioCount"])
    if sha256(SCENARIOS) != manifest["scenarioSha256"]:
        raise RuntimeError("scenario hash mismatch")
    if sha256(Path(__file__)) != manifest["runnerSha256"]:
        raise RuntimeError("runner hash mismatch")
    if sha256(WORKER_SOURCE) != manifest["workerSha256"]:
        raise RuntimeError("production worker hash mismatch")

    sys.path.insert(0, str(AGENT_ROOT))
    import openai
    from app import memory_candidate_worker as worker
    from app.settings import settings

    catalog_path = Path(settings.memory_catalog_values_path)
    runtime_contract = {
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
    for key, actual in runtime_contract.items():
        if manifest[key] != actual:
            raise RuntimeError(f"runtime contract mismatch: {key}")
    if not settings.deepseek_api_key:
        raise RuntimeError("model identity unavailable")

    allowed = {
        (row["attributeKey"], row["normalizedValue"])
        for row in worker._catalog()
    }
    for scenario in scenarios:
        expected = _preference_triples(scenario["expected"])
        if any(
            (item["attributeKey"], item["normalizedValue"]) not in allowed
            for item in expected
        ):
            raise RuntimeError("expected preference escapes catalog")

    os.mkdir(ATTEMPT)
    started = {
        "schemaVersion": "shopping-memory-extraction-started-v2",
        "startedAtEpochMs": int(time.time() * 1000),
        "manifestSha256": sha256(MANIFEST),
        "scenarioSha256": sha256(SCENARIOS),
        "runnerSha256": sha256(Path(__file__)),
        "workerSha256": sha256(WORKER_SOURCE),
        **runtime_contract,
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
            "modelCalled": False,
            "model": None,
            "outcome": "runner_not_observed",
            "durationMs": 0.0,
            "usageObserved": False,
            "promptTokens": 0,
            "completionTokens": 0,
            "totalTokens": 0,
        }
        try:
            extracted, observed = await worker._extract_observed(
                scenario["message"], "phone", recipient or ""
            )
            observation = observed.plain()
            actual = _preference_triples(extracted, exact_schema=False)
        except worker.MemoryExtractionFailure as exc:
            observation = exc.observation.plain()
            error = exc.cause_type
        except Exception as exc:
            error = type(exc).__name__
            observation = {**observation, "outcome": "runner_exception"}

        expected = _preference_triples(scenario["expected"])
        catalog_bound = all(
            (item["attributeKey"], item["normalizedValue"]) in allowed
            for item in actual
        )
        positive_exact = positive_exact_match(
            expected, actual, observation["outcome"], error
        )
        negative_semantic = negative_semantic_match(
            expected,
            actual,
            observation["outcome"],
            scenario["expectedOutcome"],
            error,
        )
        traces.append({
            "scenarioId": scenario["scenarioId"],
            "split": scenario["split"],
            "message": scenario["message"],
            "expected": expected,
            "actual": actual,
            "expectedGate": scenario["expectedGate"],
            "actualGate": gate,
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
            "modelCallMatch": (
                observation["modelCalled"] is scenario["expectedModelCall"]
            ),
            "outcomeMatch": observation["outcome"] == scenario["expectedOutcome"],
            "catalogBound": catalog_bound,
            "positiveExactMatch": positive_exact,
            "negativeSemanticMatch": negative_semantic,
            "exactMatch": positive_exact or negative_semantic,
            "error": error,
        })

    (ATTEMPT / "trace.jsonl").write_text(
        "".join(canonical(row) + "\n" for row in traces),
        encoding="utf-8",
    )
    positive_rows = [row for row in traces if row["expected"]]
    negative_rows = [row for row in traces if not row["expected"]]
    attempted_rows = [row for row in traces if row["observation"]["modelCalled"]]
    usage_unavailable = [
        row["scenarioId"]
        for row in attempted_rows
        if not row["observation"]["usageObserved"]
    ]
    latencies = [float(row["observation"]["durationMs"]) for row in attempted_rows]
    false_positives = [row["scenarioId"] for row in negative_rows if row["actual"]]
    positive_accuracy = (
        sum(row["positiveExactMatch"] for row in positive_rows) / len(positive_rows)
    )
    negative_accuracy = (
        sum(row["negativeSemanticMatch"] for row in negative_rows) / len(negative_rows)
    )
    validation_positive_accuracy = split_accuracy(
        traces, "validation", "positiveExactMatch", positive=True
    )
    gates = {
        "gateAccuracyOne": all(row["gateMatch"] for row in traces),
        "sensitiveAccuracyOne": all(row["sensitiveMatch"] for row in traces),
        "recipientAccuracyOne": all(row["recipientMatch"] for row in traces),
        "modelCallLayeringExact": all(row["modelCallMatch"] for row in traces),
        "outcomeAccuracyOne": all(row["outcomeMatch"] for row in traces),
        "catalogEscapeCountZero": all(row["catalogBound"] for row in traces),
        "falsePositiveCountZero": not false_positives,
        "exceptionCountZero": all(row["error"] is None for row in traces),
        "tokenAccountingComplete": not usage_unavailable,
        "positiveExactAccuracyOne": positive_accuracy == 1.0,
        "validationPositiveExactAccuracyOne": validation_positive_accuracy == 1.0,
        "negativeSemanticAccuracyOne": negative_accuracy == 1.0,
    }
    decision = (
        "BOUNDED_LLM_EXTRACTION_ACCEPT"
        if all(gates.values())
        else "HOLD_LLM_EXTRACTION"
    )
    report = {
        "schemaVersion": "shopping-memory-extraction-report-v2",
        "decision": decision,
        "scope": manifest["scope"],
        "callLayer": CALL_LAYER,
        "scenarioCount": len(traces),
        "developmentCount": sum(row["split"] == "development" for row in traces),
        "validationCount": sum(row["split"] == "validation" for row in traces),
        "positiveScenarioCount": len(positive_rows),
        "negativeScenarioCount": len(negative_rows),
        "positiveExactMatchCount": sum(row["positiveExactMatch"] for row in positive_rows),
        "positiveExactAccuracy": positive_accuracy,
        "developmentPositiveExactAccuracy": split_accuracy(
            traces, "development", "positiveExactMatch", positive=True
        ),
        "validationPositiveExactAccuracy": validation_positive_accuracy,
        "negativeSemanticMatchCount": sum(
            row["negativeSemanticMatch"] for row in negative_rows
        ),
        "negativeSemanticAccuracy": negative_accuracy,
        "falsePositiveScenarioIds": false_positives,
        "modelAttemptCount": len(attempted_rows),
        "usageObservedAttemptCount": len(attempted_rows) - len(usage_unavailable),
        "usageUnavailableScenarioIds": usage_unavailable,
        "tokenAccountingStatus": "complete" if not usage_unavailable else "partial",
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
        "schemaVersion": "shopping-memory-extraction-receipt-v2",
        "decision": decision,
        "model": settings.deepseek_model,
        "providerBaseUrl": settings.deepseek_base_url,
        "openaiSdkVersion": openai.__version__,
        "timeoutSeconds": worker._EXTRACTION_TIMEOUT_SECONDS,
        "maxRetries": worker._EXTRACTION_MAX_RETRIES,
        "callLayer": CALL_LAYER,
        "scenarioSha256": sha256(SCENARIOS),
        "runnerSha256": sha256(Path(__file__)),
        "workerSha256": sha256(WORKER_SOURCE),
        "promptSha256": text_sha256(worker._EXTRACTION_SYSTEM_PROMPT),
        "manifestSha256": sha256(MANIFEST),
        "catalogSha256": sha256(catalog_path),
        "traceSha256": sha256(ATTEMPT / "trace.jsonl"),
        "reportSha256": sha256(ATTEMPT / "report.json"),
    }
    (ATTEMPT / "receipt.json").write_text(
        canonical(receipt) + "\n", encoding="utf-8"
    )
    files = ["started.json", "trace.jsonl", "report.json", "receipt.json"]
    (ATTEMPT / "SHA256SUMS.txt").write_text(
        "".join(f"{sha256(ATTEMPT / name)}  {name}\n" for name in files),
        encoding="ascii",
    )
    print(canonical(report))
    return 0 if decision == "BOUNDED_LLM_EXTRACTION_ACCEPT" else 2


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run()))
