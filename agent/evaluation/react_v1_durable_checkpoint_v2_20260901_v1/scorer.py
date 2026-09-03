"""Mechanical scorer for the frozen deterministic durability layer."""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
PACKAGE = Path(__file__).resolve().parent


def _canonical(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=True, sort_keys=True, separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _sha(value: object) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _read(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _percentile(values: list[float], quantile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, math.ceil(quantile * len(ordered)))
    return round(float(ordered[rank - 1]), 3)


def _result(process: dict[str, Any]) -> dict[str, Any]:
    value = process.get("result")
    return value if isinstance(value, dict) else {}


def score_attempt(out_dir: Path) -> dict[str, Any]:
    errors: list[str] = []
    manifest = _read(out_dir / "manifest.json")
    observations = _read(out_dir / "observations.json")
    sources = _read(out_dir / "source-hashes.json")
    if manifest.get("identity") != observations.get("identity"):
        errors.append("identity_mismatch")
    if manifest.get("controlPolicy") != "react_v1":
        errors.append("control_policy_not_react_v1")
    if manifest.get("observationsSha256") != _sha(observations):
        errors.append("observations_hash_mismatch")
    if manifest.get("sourceHashesSha256") != _sha(sources):
        errors.append("source_hash_manifest_mismatch")
    for name, digest in sources.items():
        path = ROOT / name
        if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            errors.append(f"source_drift:{name}")
    if manifest.get("preregistrationSha256") != hashlib.sha256(
        (PACKAGE / "preregistration.json").read_bytes()
    ).hexdigest():
        errors.append("preregistration_drift")
    if manifest.get("scenarioMatrixSha256") != hashlib.sha256(
        (PACKAGE / "scenario-matrix.json").read_bytes()
    ).hexdigest():
        errors.append("scenario_matrix_drift")
    if manifest.get("implementationFreezeSha256") != hashlib.sha256(
        (PACKAGE / "IMPLEMENTATION_FREEZE_RECEIPT.json").read_bytes()
    ).hexdigest():
        errors.append("implementation_freeze_drift")

    families = observations.get("families") or {}
    expected_counts = {
        "baseline": 10,
        "react_action_commit": 10,
        "inflight_before_effect": 10,
        "effect_before_inbox_complete": 10,
        "inbox_before_projection": 10,
        "projection_before_checkpoint": 10,
        "validator_before_checkpoint": 10,
        "exact_replay": 10,
        "state_diverged": 10,
        "clarification": 10,
        "operator_pause": 10,
        "claim_takeover_fence": 20,
    }
    for family, count in expected_counts.items():
        if len(families.get(family) or []) != count:
            errors.append(f"sample_count:{family}")

    rto: list[float] = []
    expected_cases = 0
    passed_cases = 0
    duplicate_effects = 0
    missing_effects = 0
    stale_overwrites = 0
    unexpected_model_calls = 0
    replay_revision_deltas = 0

    for family in (
        "baseline", "react_action_commit", "inflight_before_effect",
        "effect_before_inbox_complete", "inbox_before_projection",
        "projection_before_checkpoint", "validator_before_checkpoint",
    ):
        for row in families.get(family) or []:
            expected_cases += 1
            first = row["first"]
            restarts = row.get("restarts") or []
            ledger_count = len(row.get("ledger") or [])
            unexpected_model_calls += len(row.get("modelCalls") or [])
            for item in restarts[:1]:
                rto.append(float(_result(item).get("graphElapsedMs") or 0))
            ok = True
            if family == "baseline":
                ok &= first.get("returnCode") == 0
                ok &= _result(first).get("boundary") == "task_completed"
                ok &= len(restarts) == 3
                ok &= all(
                    item.get("returnCode") == 0
                    and _result(item).get("boundary") == "task_completed"
                    for item in restarts
                )
                revisions = [_result(first).get("revision")] + [
                    _result(item).get("revision") for item in restarts
                ]
                if len(set(revisions)) != 1:
                    replay_revision_deltas += 1
            else:
                expected_exit = row.get("expectedFaultExit")
                ok &= first.get("returnCode") == expected_exit
                ok &= len(restarts) == 1 and restarts[0].get("returnCode") == 0
            expected_ledger = 0 if family == "inflight_before_effect" else 1
            if ledger_count > expected_ledger:
                duplicate_effects += ledger_count - expected_ledger
            if ledger_count < expected_ledger:
                missing_effects += expected_ledger - ledger_count
            ok &= ledger_count == expected_ledger
            if restarts and family in {
                "inflight_before_effect", "effect_before_inbox_complete",
            }:
                ok &= _result(restarts[0]).get("boundary") in {
                    "stop_turn", "task_completed",
                }
                receipt = (row.get("finalState") or {}).get("reactV1OutcomeReceipt") or {}
                ok &= (receipt.get("outcome") or {}).get("errorCode") == "tool_inbox_unknown"
            elif restarts and family != "baseline":
                ok &= _result(restarts[0]).get("boundary") == "task_completed"
            if ok:
                passed_cases += 1
            else:
                errors.append(f"case_failed:{family}:{row.get('index')}")

    for row in families.get("state_diverged") or []:
        expected_cases += 1
        unexpected_model_calls += len(row.get("modelCalls") or [])
        restart = (row.get("restarts") or [{}])[0]
        ok = (
            row.get("first", {}).get("returnCode") == 81
            and restart.get("returnCode") == 0
            and _result(restart).get("boundary") == "state_diverged"
            and len(row.get("ledger") or []) == 0
            and (row.get("finalState") or {}).get("revision")
            == row.get("driftRevision")
        )
        stale_overwrites += 0 if ok else 1
        if ok:
            passed_cases += 1
        else:
            errors.append(f"case_failed:state_diverged:{row.get('index')}")

    final_publication_replay_deltas = 0
    for row in families.get("exact_replay") or []:
        expected_cases += 1
        ok = (
            row.get("publicationCount") == 1
            and row.get("replayHits") == [True, True, True]
            and row.get("publishedRevision") == row.get("finalRevision")
        )
        if row.get("publishedRevision") != row.get("finalRevision"):
            final_publication_replay_deltas += 1
        if ok:
            passed_cases += 1
        else:
            errors.append(f"case_failed:exact_replay:{row.get('index')}")

    for row in families.get("clarification") or []:
        expected_cases += 1
        unexpected_model_calls += len(row.get("modelCalls") or [])
        before = row.get("beforeReplayState") or {}
        after = row.get("finalState") or {}
        if before.get("revision") != after.get("revision"):
            replay_revision_deltas += 1
        ok = (
            _result(row.get("parked") or {}).get("boundary") == "clarification"
            and _result(row.get("resumed") or {}).get("boundary") == "task_completed"
            and all(
                item.get("returnCode") == 0
                and _result(item).get("mode") == "idempotent_replay"
                for item in row.get("replays") or []
            )
            and len(row.get("ledger") or []) == 1
            and before.get("revision") == after.get("revision")
        )
        if ok:
            passed_cases += 1
        else:
            errors.append(f"case_failed:clarification:{row.get('index')}")

    for row in families.get("operator_pause") or []:
        expected_cases += 1
        unexpected_model_calls += len(row.get("modelCalls") or [])
        ok = (
            _result(row.get("paused") or {}).get("boundary") == "operator_paused"
            and _result(row.get("resumed") or {}).get("boundary") == "task_completed"
            and len(row.get("ledger") or []) == 1
        )
        if ok:
            passed_cases += 1
        else:
            errors.append(f"case_failed:operator_pause:{row.get('index')}")

    fence_rows = families.get("claim_takeover_fence") or []
    for row in fence_rows:
        expected_cases += 1
        if row.get("pass") is True:
            passed_cases += 1
        else:
            stale_overwrites += 1
            errors.append(f"case_failed:claim_takeover_fence:{row.get('index')}")

    redis_audit = observations.get("redisAudit") or {}
    tamper_rows = observations.get("tamper") or []
    tamper_accepted = sum(bool(row.get("accepted")) for row in tamper_rows)
    if len(tamper_rows) != 12:
        errors.append("sample_count:tamper")
    if {row.get("id") for row in tamper_rows} != {
        f"T{index:02d}" for index in range(1, 13)
    }:
        errors.append("tamper_case_identity")
    if tamper_accepted:
        errors.append("tamper_accepted")
    pending_write_case = next(
        (row for row in tamper_rows if row.get("id") == "T02"), {}
    )
    if int(pending_write_case.get("tamperedWrites") or 0) < 1:
        errors.append("tamper_pending_write_not_exercised")

    real_rows = observations.get("realModel") or []
    real_contract_violations = 0
    real_provider_failures = 0
    real_model_calls_per_run: list[int] = []
    real_model_latency_ms: list[float] = []
    real_model_tokens: list[int] = []
    if len(real_rows) != 3 or {row.get("id") for row in real_rows} != {
        "B01", "B02", "B03"
    }:
        errors.append("sample_count:real_model")
    for row in real_rows:
        process = row.get("process") or {}
        result = _result(process)
        events = result.get("modelEvents") or []
        real_model_calls_per_run.append(len(events))
        for event in events:
            real_provider_failures += int(bool(event.get("failed")))
            real_model_latency_ms.append(float(event.get("durationMs") or 0))
            if event.get("totalTokens") is not None:
                real_model_tokens.append(int(event["totalTokens"]))
            if not event.get("modelCallId") or not event.get("contextBindingHash"):
                real_contract_violations += 1
        if (
            process.get("returnCode") != 0
            or len(events) < 1
            or len(events) > 2
            or result.get("boundary") not in {
                "task_completed", "stop_turn", "clarification", "state_diverged"
            }
        ):
            real_contract_violations += 1
    if any(value > 2 for value in real_model_calls_per_run):
        errors.append("real_model_call_limit")
    if real_contract_violations:
        errors.append("real_model_contract_violation")
    sensitive_hits = len(redis_audit.get("sensitiveHits") or [])
    missing_ttl_count = len(redis_audit.get("missingTtl") or [])
    rto_p50 = _percentile(rto, 0.50)
    rto_p95 = _percentile(rto, 0.95)
    gates = {
        "expectedOutcomeRate": passed_cases / expected_cases if expected_cases else 0,
        "duplicateToolSideEffects": duplicate_effects,
        "missingExpectedToolSideEffects": missing_effects,
        "staleWorkerOverwriteCount": stale_overwrites,
        "unexpectedModelCalls": unexpected_model_calls,
        "replayRevisionDeltaBundles": replay_revision_deltas,
        "finalPublicationReplayDeltaBundles": final_publication_replay_deltas,
        "rtoP50Ms": rto_p50,
        "rtoP95Ms": rto_p95,
        "sensitiveArtifactHitCount": sensitive_hits,
        "ownedExpiringRedisKeyMissingTtlCount": missing_ttl_count,
        "tamperAcceptedCount": tamper_accepted,
        "realModelCallsPerRun": real_model_calls_per_run,
        "realModelContractViolationCount": real_contract_violations,
        "realModelProviderFailureCount": real_provider_failures,
        "realModelLatencyMs": real_model_latency_ms,
        "realModelTotalTokens": sum(real_model_tokens),
    }
    if passed_cases != expected_cases:
        errors.append("expected_outcome_rate")
    if duplicate_effects:
        errors.append("duplicate_tool_side_effects")
    if missing_effects:
        errors.append("missing_tool_side_effects")
    if stale_overwrites:
        errors.append("stale_worker_overwrite")
    if unexpected_model_calls:
        errors.append("layer_a_model_call")
    if replay_revision_deltas:
        errors.append("replay_revision_delta")
    if final_publication_replay_deltas:
        errors.append("final_publication_replay_delta")
    if rto_p50 is None or rto_p50 > 5000:
        errors.append("rto_p50")
    if rto_p95 is None or rto_p95 > 10000:
        errors.append("rto_p95")
    if sensitive_hits:
        errors.append("sensitive_artifact_hit")
    if missing_ttl_count:
        errors.append("missing_owned_key_ttl")
    return {
        "schemaVersion": 1,
        "identity": manifest.get("identity"),
        "status": "ACCEPT" if not errors else "FAIL",
        "expectedCases": expected_cases,
        "passedCases": passed_cases,
        "gates": gates,
        "errors": sorted(set(errors)),
        "scope": (
            "bounded react_v1 durability: deterministic real-Redis/process "
            "matrix plus exactly three configured real-model runs"
        ),
    }


__all__ = ["score_attempt"]
