#!/usr/bin/env python3
"""Non-confirmatory resource replay of the frozen attempt001 validation arm.

This script does not select parameters, evaluate gates, or run sealed-test.
"""

from __future__ import annotations

import argparse
import collections
import json
import sys
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from analyze_kuaisearch_lite_phone_behavior_v1 import SOURCES, classify_category, stream_jsonl, utc_now  # noqa: E402
from run_kuaisearch_lite_phone_brand_profile_v1 import evaluate, normalize_brand, sha256_file  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--attempt001", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    decision_receipt_path = args.attempt001 / "receipt.json"
    split_path = args.attempt001 / "split_assignments.json"
    decision = json.loads(decision_receipt_path.read_text(encoding="utf-8"))
    split = json.loads(split_path.read_text(encoding="utf-8"))
    if decision["status"] != "HOLD" or decision["sealedTest"]["status"] != "NOT_RUN_VALIDATION_HOLD":
        raise RuntimeError("attempt001 is not the expected HOLD with sealed-test unrun")
    if decision["manifest"]["sha256"] != sha256_file(args.manifest):
        raise RuntimeError("attempt001 manifest binding mismatch")
    validation_users = [int(value) for value in split["splits"]["validation"]]
    selected_config = dict(decision["development"]["selected"]["config"])
    started_at = utc_now()

    flags = bytearray(1)
    item_meta: dict[int, dict[str, Any]] = {}

    def consume_item(row: dict[str, Any], _: int) -> None:
        nonlocal flags
        item_id = int(row["item_id"])
        if item_id >= len(flags):
            flags.extend(b"\0" * (item_id + 1 - len(flags)))
        names = (
            str(row.get("category_level1_name") or ""),
            str(row.get("category_level2_name") or ""),
            str(row.get("category_level3_name") or ""),
        )
        strict, _ = classify_category(names)
        flags[item_id] = 1 if strict else 0
        if strict:
            item_meta[item_id] = {"brandNormalized": normalize_brand(row.get("brand_name"))}

    item_source = stream_jsonl(SOURCES["items_lite"], consume_item)
    validation_set = set(validation_users)
    raw_sessions: dict[int, list[dict[str, Any]]] = collections.defaultdict(list)

    def consume_recall(row: dict[str, Any], _: int) -> None:
        user_id = int(row["user_id"])
        if user_id not in validation_set:
            return
        candidates = [int(value) for value in row.get("impressed_item_ids", []) if 0 <= int(value) < len(flags) and flags[int(value)]]
        clicked = [int(value) for value in row.get("clicked_item_ids", []) if 0 <= int(value) < len(flags) and flags[int(value)]]
        if candidates or clicked:
            raw_sessions[user_id].append({
                "sessionId": int(row["session_id"]),
                "timeIndex": int(row["time_index"]),
                "candidates": candidates,
                "clicked": clicked,
            })

    recall_source = stream_jsonl(SOURCES["recall_lite"], consume_recall)
    records_by_user: dict[int, list[dict[str, Any]]] = collections.defaultdict(list)
    minimum_history = int(manifest["eligibility"]["minimumStrictEarlierClicks"])
    for user_id in validation_users:
        grouped: dict[int, list[dict[str, Any]]] = collections.defaultdict(list)
        for session in raw_sessions.get(user_id, []):
            grouped[session["timeIndex"]].append(session)
        history: list[int] = []
        for time_index in sorted(grouped):
            sessions = sorted(grouped[time_index], key=lambda row: row["sessionId"])
            if len(history) >= minimum_history:
                for session in sessions:
                    if session["candidates"]:
                        records_by_user[user_id].append({
                            "sessionId": session["sessionId"],
                            "timeIndex": time_index,
                            "history": list(history),
                            "candidates": list(session["candidates"]),
                            "positives": set(session["clicked"]) & set(session["candidates"]),
                        })
            for session in sessions:
                history.extend(session["clicked"])

    replay = evaluate(
        validation_users, records_by_user, item_meta, selected_config,
        int(manifest["bootstrap"]["seed"]) + 100, 0,
    )
    original = decision["validation"]["result"]
    stable_fields = [
        "eligibleUsers", "evaluableUsers", "opportunitySessions", "candidateAtLeast2Sessions",
        "candidateAtLeast2Coverage", "noPositiveSessionsExcluded", "evaluableSessions",
        "positiveCandidates", "unjudgedCandidates", "baselineMacroUser", "treatmentMacroUser",
        "deltaMacroUser", "changedSessions", "changeRate", "top1ChangeRate",
        "profileNonemptyCoverage", "candidateSetPreservationRate", "crossCategoryContaminationCount",
        "hardFilteredCandidates", "purchaseSignalsUsed",
    ]
    comparison = {field: replay[field] == original[field] for field in stable_fields}
    payload = {
        "schemaVersion": "kuaisearch-phone-brand-profile-non-confirmatory-resource-replay-v1",
        "role": "NON_CONFIRMATORY_RESOURCE_REPLAY",
        "startedAt": started_at,
        "finishedAt": utc_now(),
        "attempt001": {
            "path": str(args.attempt001.resolve()),
            "receiptSha256": sha256_file(decision_receipt_path),
            "authoritativeDecision": "HOLD",
        },
        "selectedConfigFixedFromAttempt001": selected_config,
        "developmentSelectionExecuted": False,
        "validationGateExecuted": False,
        "sealedTestExecuted": False,
        "sealedTestStatusPreserved": "NOT_RUN_VALIDATION_HOLD",
        "sourceReceipts": {"itemsLite": item_source, "recallLite": recall_source},
        "replayValidationDescriptiveMetrics": replay,
        "stableFieldComparison": comparison,
        "allStableFieldsMatch": all(comparison.values()),
        "decisionChanged": False,
        "productionSwitchChanged": False,
    }
    args.output_dir.mkdir(parents=True, exist_ok=False)
    output = args.output_dir / "receipt.json"
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({
        "role": payload["role"],
        "allStableFieldsMatch": payload["allStableFieldsMatch"],
        "decisionChanged": False,
        "sealedTestExecuted": False,
        "output": str(output),
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
