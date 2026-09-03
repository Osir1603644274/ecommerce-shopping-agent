"""Freeze and evaluate the bounded shopping-memory A/B/C experiment.

A = no long-term memory
B = naive prompt concatenation of every recalled row
C = governed MemorySnapshot -> ApplicabilityResolver -> effective projection

The experiment is deterministic, storage-free, and does not enable production
memory.  It uses the frozen 439-item used-phone catalog only for ranking effects.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import statistics
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable

from app.memory import (
    MemoryApplicationContext,
    MemorySnapshot,
    ScopedMemoryRecord,
    ShoppingPreference,
    resolve_effective_preferences,
)


ROOT = Path(__file__).resolve().parents[2]
CATALOG_PATH = ROOT / "data/derived/ecommerce/used_phone_catalog_expansion_kuaisearch_09807c_20260823_r3/catalog.jsonl"
DEFAULT_ASSET_DIR = ROOT / "agent/evaluation/assets/shopping_memory_abc_v1_20260829"
DEFAULT_OUTPUT_DIR = ROOT / "agent/evaluation/runs/shopping_memory_abc_v1_20260829_attempt001"
NOW = datetime(2026, 8, 29, tzinfo=UTC)
SCHEMA_VERSION = "shopping-memory-abc-v1"


def canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")


def write_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(canonical(row) + "\n" for row in rows), encoding="utf-8")


def pref(key: str, value: str) -> dict[str, str]:
    return {"semanticKey": key, "value": value}


def row(entry_id: str, key: str, value: str, **overrides: Any) -> dict[str, Any]:
    result: dict[str, Any] = {
        "entryId": entry_id,
        "ownerUserId": "user-1",
        "productCategory": "phone",
        "recipientScope": "self",
        "preference": pref(key, value),
        "source": "explicit_user",
        "status": "active",
        "version": 1,
        "supersedes": None,
        "expiresOffsetDays": 30,
    }
    result.update(overrides)
    return result


def case(
    case_id: str,
    family: str,
    records: list[dict[str, Any]],
    expected: list[str],
    **context: Any,
) -> dict[str, Any]:
    base_context = {
        "authenticatedOwnerUserId": "user-1",
        "productCategory": "phone",
        "recipientScope": "self",
        "memoryEnabled": True,
        "currentTurn": [],
        "taskState": [],
    }
    base_context.update(context)
    return {
        "schemaVersion": SCHEMA_VERSION,
        "caseId": case_id,
        "family": family,
        "records": records,
        "context": base_context,
        "expectedAppliedEntryIds": expected,
    }


def scenario_definitions() -> list[dict[str, Any]]:
    scenarios = [
        case("memory-01", "correct_application", [row("r01", "os", "android")], ["r01"]),
        case("memory-02", "correct_application", [row("r02", "battery_health", "90_plus")], ["r02"]),
        case("memory-03", "current_turn_override", [row("r03", "os", "android")], [], currentTurn=[pref("os", "ios")]),
        case("memory-04", "current_turn_override", [row("r04", "scratch_level", "none")], [], currentTurn=[pref("scratch_level", "light")]),
        case("memory-05", "task_state_override", [row("r05", "screen_originality", "original")], [], taskState=[pref("screen_originality", "non_original")]),
        case("memory-06", "task_state_override", [row("r06", "motherboard_repair", "not_repaired")], [], taskState=[pref("motherboard_repair", "repaired")]),
        case("memory-07", "recipient_isolation", [row("r07", "os", "android")], [], recipientScope="other"),
        case("memory-08", "recipient_isolation", [row("r08", "battery_health", "90_plus")], [], recipientScope="unknown"),
        case("memory-09", "cross_user_isolation", [row("r09", "os", "android")], [], authenticatedOwnerUserId="user-2"),
        case("memory-10", "cross_user_isolation", [row("r10", "scratch_level", "none", ownerUserId="user-2")], []),
        case("memory-11", "cross_category_isolation", [row("r11", "os", "android", productCategory="laptop")], []),
        case("memory-12", "cross_category_isolation", [row("r12", "battery_health", "90_plus")], [], productCategory="headphones"),
        case("memory-13", "expiry", [row("r13", "os", "android", expiresOffsetDays=0)], []),
        case("memory-14", "expiry", [row("r14", "screen_originality", "original", expiresOffsetDays=-1)], []),
        case("memory-15", "revocation_delete", [row("r15", "os", "android", status="revoked")], []),
        case("memory-16", "revocation_delete", [row("r16", "scratch_level", "none", status="disabled")], []),
        case("memory-17", "supersede", [row("r17a", "os", "android"), row("r17b", "os", "ios", version=2, supersedes="r17a")], ["r17b"]),
        case("memory-18", "supersede", [row("r18a", "battery_health", "80_90"), row("r18b", "battery_health", "90_plus", version=2, supersedes="r18a")], ["r18b"]),
        case("memory-19", "memory_disabled", [row("r19", "os", "android")], [], memoryEnabled=False),
        case("memory-20", "memory_disabled", [row("r20", "shell_condition", "normal")], [], memoryEnabled=False),
        case("memory-21", "invalid_version_chain", [row("r21a", "os", "android"), row("r21c", "os", "ios", version=3, supersedes="r21a")], []),
        case("memory-22", "invalid_version_chain", [row("r22a", "scratch_level", "light"), row("r22b", "scratch_level", "none", version=2, supersedes="wrong-id")], []),
        case("memory-23", "multiple_valid", [row("r23a", "os", "android"), row("r23b", "screen_originality", "original")], ["r23a", "r23b"]),
        case("memory-24", "multiple_valid", [row("r24a", "battery_health", "90_plus"), row("r24b", "motherboard_repair", "not_repaired"), row("r24c", "shell_condition", "normal")], ["r24a", "r24b", "r24c"]),
    ]
    if len(scenarios) != 24 or len({item["caseId"] for item in scenarios}) != 24:
        raise ValueError("scenario identity contract mismatch")
    families = {item["family"] for item in scenarios}
    if len(families) != 12 or any(sum(item["family"] == family for item in scenarios) != 2 for family in families):
        raise ValueError("expected 12 behavior families with two cases each")
    return scenarios


def freeze(asset_dir: Path) -> dict[str, Any]:
    scenarios_path = asset_dir / "scenarios.jsonl"
    write_jsonl(scenarios_path, scenario_definitions())
    manifest = {
        "schemaVersion": "shopping-memory-abc-prereg-v1",
        "status": "FROZEN_BEFORE_EXECUTION",
        "createdAt": "2026-08-29T00:00:00Z",
        "arms": {
            "A": "no_long_term_memory",
            "B": "naive_recall_prompt_concatenation",
            "C": "governed_scope_override_revoke_projection",
        },
        "catalog": {"path": str(CATALOG_PATH.relative_to(ROOT)), "rows": 439, "sha256": sha256(CATALOG_PATH)},
        "scenarios": {"path": "scenarios.jsonl", "rows": 24, "families": 12, "sha256": sha256(scenarios_path)},
        "source": {"path": str(Path(__file__).resolve().relative_to(ROOT)), "sha256": sha256(Path(__file__).resolve())},
        "safetyGate": {
            "cFalseApplicationCount": 0,
            "cMissedApplicationCount": 0,
            "cCrossUserRecipientCategoryContaminationCount": 0,
            "cOverrideFailureCount": 0,
            "cRevocationExpiryLeakCount": 0,
            "taskStateMutationCount": 0,
        },
        "decisionRule": "ACCEPT_CONTROLLED_SLICE_ONLY_IF_ALL_C_SAFETY_GATES_PASS",
        "productionActivationAuthorized": False,
    }
    write_json(asset_dir / "manifest.json", manifest)
    return manifest


def preference(raw: dict[str, str]) -> ShoppingPreference:
    return ShoppingPreference("shopping_preference", raw["semanticKey"], raw["value"])


def make_record(raw: dict[str, Any]) -> ScopedMemoryRecord:
    return ScopedMemoryRecord(
        entry_id=raw["entryId"],
        owner_user_id=raw["ownerUserId"],
        product_category=raw["productCategory"],
        recipient_scope=raw["recipientScope"],
        preference=preference(raw["preference"]),
        source=raw["source"],
        status=raw["status"],
        version=raw["version"],
        created_at=NOW - timedelta(days=30),
        updated_at=NOW - timedelta(days=1),
        expires_at=NOW + timedelta(days=raw["expiresOffsetDays"]),
        supersedes=raw["supersedes"],
    )


def make_inputs(scenario: dict[str, Any]) -> tuple[MemorySnapshot, MemoryApplicationContext, list[ScopedMemoryRecord]]:
    records = [make_record(item) for item in scenario["records"]]
    context = scenario["context"]
    snapshot = MemorySnapshot("user-1", 1, tuple(records))
    application = MemoryApplicationContext(
        authenticated_owner_user_id=context["authenticatedOwnerUserId"],
        product_category=context["productCategory"],
        recipient_scope=context["recipientScope"],
        now=NOW,
        current_turn=tuple(preference(item) for item in context["currentTurn"]),
        task_state=tuple(preference(item) for item in context["taskState"]),
        memory_enabled=context["memoryEnabled"],
    )
    return snapshot, application, records


def canonical_brand(value: object) -> str:
    text = str(value or "").casefold()
    aliases = {
        "apple": ("apple", "苹果"), "huawei": ("huawei", "华为"),
        "xiaomi": ("xiaomi", "小米", "redmi", "红米"), "oppo": ("oppo",),
        "vivo": ("vivo",), "honor": ("honor", "荣耀"), "samsung": ("samsung", "三星"),
    }
    return next((key for key, tokens in aliases.items() if any(token in text for token in tokens)), text)


def product_value(product: dict[str, Any], key: str) -> str | None:
    if key == "brand":
        return canonical_brand(product.get("brand"))
    attributes = product.get("attributes")
    field = attributes.get(key) if isinstance(attributes, dict) else None
    if not isinstance(field, dict) or field.get("status") != "known":
        return None
    value = field.get("value")
    return value if isinstance(value, str) else None


def preference_match(product: dict[str, Any], item: ShoppingPreference) -> bool:
    if item.semantic_key == "avoid_brand":
        return canonical_brand(product.get("brand")) != item.value
    return product_value(product, item.semantic_key) == item.value


def layered_preferences(
    current: tuple[ShoppingPreference, ...],
    task: tuple[ShoppingPreference, ...],
    memory: Iterable[ShoppingPreference],
) -> tuple[ShoppingPreference, ...]:
    output: list[ShoppingPreference] = []
    occupied: set[tuple[str, str]] = set()
    for layer in (current, task, tuple(memory)):
        for item in layer:
            key = (item.category, item.semantic_key)
            if key not in occupied:
                output.append(item)
                occupied.add(key)
    return tuple(output)


def rank_catalog(catalog: list[dict[str, Any]], preferences: tuple[ShoppingPreference, ...]) -> list[int]:
    def score(product: dict[str, Any]) -> tuple[float, float, int]:
        matches = sum(preference_match(product, item) for item in preferences)
        attributes = product.get("attributes") if isinstance(product.get("attributes"), dict) else {}
        known = sum(isinstance(value, dict) and value.get("status") == "known" for value in attributes.values())
        return (-float(matches), -float(known), int(product["itemId"]))
    return [int(product["itemId"]) for product in sorted(catalog, key=score)]


def percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    position = (len(ordered) - 1) * fraction
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def score_arm(rows: list[dict[str, Any]], arm: str) -> dict[str, Any]:
    tp = fp = fn = 0
    family_rows: dict[str, list[dict[str, Any]]] = {}
    context_bytes: list[int] = []
    top1_success: list[int] = []
    top3_overlap: list[float] = []
    for result in rows:
        expected = set(result["expectedAppliedEntryIds"])
        predicted = set(result["arms"][arm]["appliedEntryIds"])
        tp += len(expected & predicted)
        fp += len(predicted - expected)
        fn += len(expected - predicted)
        family_rows.setdefault(result["family"], []).append(result)
        context_bytes.append(result["arms"][arm]["contextBytes"])
        oracle_top = result["oracleTop3"]
        arm_top = result["arms"][arm]["top3"]
        top1_success.append(int(arm_top[0] == oracle_top[0]))
        top3_overlap.append(len(set(arm_top) & set(oracle_top)) / 3)
    precision = tp / (tp + fp) if tp + fp else 1.0
    recall = tp / (tp + fn) if tp + fn else 1.0
    negative_families = {"recipient_isolation", "cross_user_isolation", "cross_category_isolation", "expiry", "revocation_delete", "memory_disabled", "invalid_version_chain"}
    negative_cases = [item for family in negative_families for item in family_rows[family]]
    false_influence = sum(bool(item["arms"][arm]["appliedEntryIds"]) for item in negative_cases) / len(negative_cases)
    def success(families: set[str]) -> float:
        selected = [item for family in families for item in family_rows[family]]
        return sum(set(item["arms"][arm]["appliedEntryIds"]) == set(item["expectedAppliedEntryIds"]) for item in selected) / len(selected)
    return {
        "applicationPrecision": precision,
        "applicationRecall": recall,
        "falseInfluenceRate": false_influence,
        "overrideCorrectness": success({"current_turn_override", "task_state_override"}),
        "revocationExpirySuccessRate": success({"expiry", "revocation_delete"}),
        "crossUserRecipientCategoryIsolationRate": success({"recipient_isolation", "cross_user_isolation", "cross_category_isolation"}),
        "top1OracleAgreementMacro": statistics.fmean(top1_success),
        "top3OracleOverlapMacro": statistics.fmean(top3_overlap),
        "contextBytesMean": statistics.fmean(context_bytes),
        "contextEstimatedTokensCeilMean": statistics.fmean(math.ceil(value / 4) for value in context_bytes),
        "trueApplicationCount": tp,
        "falseApplicationCount": fp,
        "missedApplicationCount": fn,
    }


def evaluate(asset_dir: Path, output_dir: Path) -> dict[str, Any]:
    manifest = json.loads((asset_dir / "manifest.json").read_text(encoding="utf-8"))
    scenarios_path = asset_dir / manifest["scenarios"]["path"]
    if sha256(scenarios_path) != manifest["scenarios"]["sha256"]:
        raise ValueError("scenario freeze hash mismatch")
    if sha256(CATALOG_PATH) != manifest["catalog"]["sha256"]:
        raise ValueError("catalog freeze hash mismatch")
    if sha256(Path(__file__).resolve()) != manifest["source"]["sha256"]:
        raise ValueError("evaluator source hash mismatch")
    scenarios = read_jsonl(scenarios_path)
    catalog = read_jsonl(CATALOG_PATH)
    if len(scenarios) != 24 or len(catalog) != 439:
        raise ValueError("frozen input cardinality mismatch")

    traces: list[dict[str, Any]] = []
    latencies: list[float] = []
    task_state_mutations = 0
    for scenario in scenarios:
        snapshot, application, records = make_inputs(scenario)
        before = canonical({
            "currentTurn": [item.plain() for item in application.current_turn],
            "taskState": [item.plain() for item in application.task_state],
        })
        started = time.perf_counter_ns()
        effective = resolve_effective_preferences(snapshot, application)
        latencies.append((time.perf_counter_ns() - started) / 1_000_000)
        after = canonical({
            "currentTurn": [item.plain() for item in application.current_turn],
            "taskState": [item.plain() for item in application.task_state],
        })
        task_state_mutations += int(before != after)
        by_id = {item.entry_id: item for item in records}
        expected_ids = scenario["expectedAppliedEntryIds"]
        c_ids = [item.entry_id for item in effective.decisions if item.outcome == "applied"]
        predictions = {
            "A": [],
            "B": [item.entry_id for item in records],
            "C": c_ids,
        }
        oracle_preferences = layered_preferences(
            application.current_turn,
            application.task_state,
            [by_id[item].preference for item in expected_ids],
        )
        oracle_top3 = rank_catalog(catalog, oracle_preferences)[:3]
        arms: dict[str, Any] = {}
        for arm, ids in predictions.items():
            memory_preferences = [by_id[item].preference for item in ids]
            active = layered_preferences(application.current_turn, application.task_state, memory_preferences)
            payload = [item.plain() for item in memory_preferences]
            arms[arm] = {
                "appliedEntryIds": ids,
                "effectivePreferences": [item.plain() for item in active],
                "top3": rank_catalog(catalog, active)[:3],
                "contextBytes": len(canonical(payload).encode("utf-8")),
            }
        traces.append({
            "schemaVersion": "shopping-memory-abc-trace-v1",
            "caseId": scenario["caseId"],
            "family": scenario["family"],
            "expectedAppliedEntryIds": expected_ids,
            "oracleTop3": oracle_top3,
            "resolverSnapshotHash": effective.snapshot_hash,
            "resolverDecisions": [
                {"entryId": item.entry_id, "outcome": item.outcome, "reason": item.reason}
                for item in effective.decisions
            ],
            "arms": arms,
        })

    metrics = {arm: score_arm(traces, arm) for arm in ("A", "B", "C")}
    c = metrics["C"]
    contamination_count = sum(
        bool(row["arms"]["C"]["appliedEntryIds"])
        for row in traces
        if row["family"] in {"recipient_isolation", "cross_user_isolation", "cross_category_isolation"}
    )
    override_failures = sum(
        set(row["arms"]["C"]["appliedEntryIds"]) != set(row["expectedAppliedEntryIds"])
        for row in traces
        if row["family"] in {"current_turn_override", "task_state_override"}
    )
    leak_count = sum(
        bool(row["arms"]["C"]["appliedEntryIds"])
        for row in traces
        if row["family"] in {"expiry", "revocation_delete"}
    )
    gates = {
        "cFalseApplicationCount": c["falseApplicationCount"],
        "cMissedApplicationCount": c["missedApplicationCount"],
        "cCrossUserRecipientCategoryContaminationCount": contamination_count,
        "cOverrideFailureCount": override_failures,
        "cRevocationExpiryLeakCount": leak_count,
        "taskStateMutationCount": task_state_mutations,
    }
    accepted = all(gates[key] == expected for key, expected in manifest["safetyGate"].items())
    write_jsonl(output_dir / "traces.jsonl", traces)
    report = {
        "schemaVersion": "shopping-memory-abc-report-v1",
        "verdict": "ACCEPT_CONTROLLED_MEMORY_SLICE" if accepted else "HOLD_MEMORY_SAFETY_GATE_FAILED",
        "productionActivation": "HOLD_DEFAULT_OFF",
        "catalogRows": len(catalog),
        "scenarioCount": len(scenarios),
        "behaviorFamilyCount": len({item["family"] for item in scenarios}),
        "metrics": metrics,
        "safetyGates": gates,
        "resolverLatencyMs": {
            "p50": percentile(latencies, 0.50),
            "p95": percentile(latencies, 0.95),
            "samples": len(latencies),
        },
        "providerTokenCountsAvailable": False,
        "inputHashes": {
            "catalogSha256": sha256(CATALOG_PATH),
            "scenariosSha256": sha256(scenarios_path),
            "sourceSha256": sha256(Path(__file__).resolve()),
        },
    }
    write_json(output_dir / "score.json", report)
    receipt = {
        "schemaVersion": "shopping-memory-abc-receipt-v1",
        "scoreSha256": sha256(output_dir / "score.json"),
        "tracesSha256": sha256(output_dir / "traces.jsonl"),
        "manifestSha256": sha256(asset_dir / "manifest.json"),
    }
    write_json(output_dir / "receipt.json", receipt)
    return report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--freeze", action="store_true")
    parser.add_argument("--evaluate", action="store_true")
    parser.add_argument("--asset-dir", type=Path, default=DEFAULT_ASSET_DIR)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    args = parser.parse_args()
    if args.freeze == args.evaluate:
        parser.error("choose exactly one of --freeze or --evaluate")
    result = freeze(args.asset_dir) if args.freeze else evaluate(args.asset_dir, args.output_dir)
    print(canonical(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
