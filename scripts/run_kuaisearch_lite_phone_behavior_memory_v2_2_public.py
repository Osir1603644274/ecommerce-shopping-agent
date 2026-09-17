#!/usr/bin/env python3
"""Public calibration for an untouched KuaiSearch single-click cohort.

The runner reads hash-verified local files only.  It excludes all 501 users
from the previous V1/V2 split, partitions every remaining user by a frozen
SHA-256 rule before JSON decoding, and decodes only the public half.  The
authority half is counted and hashed as raw bytes but never decoded here.
"""
from __future__ import annotations

import argparse
import collections
import hashlib
import json
import re
import sys
import time
from pathlib import Path
from typing import Any, Callable

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from analyze_kuaisearch_lite_phone_behavior_v1 import (  # noqa: E402
    REVISION,
    classify_category,
    utc_now,
)
from run_kuaisearch_lite_phone_behavior_memory_v2 import (  # noqa: E402
    normalize_text,
    sha256_file,
    title_tokens,
)
from run_kuaisearch_lite_phone_behavior_memory_v2_1 import (  # noqa: E402
    evaluate,
)


ROUTE_PATTERN_TEXT = r'^\{"user_id":\s*([0-9]+),'
ROUTE_PATTERN = re.compile(ROUTE_PATTERN_TEXT.encode("ascii"))


def partition_bucket(user_id: int, salt: str) -> int:
    digest = hashlib.sha256(f"{salt}:{user_id}".encode("ascii")).digest()
    return int.from_bytes(digest[:8], "big") % 100


def route_recall_line(
    raw: bytes,
    consumed_users: set[int],
    *,
    salt: str,
    public_bucket_upper_exclusive: int,
    loads: Callable[[bytes], Any] = json.loads,
) -> tuple[str, int, dict[str, Any] | None]:
    match = ROUTE_PATTERN.match(raw)
    if match is None:
        raise ValueError("recall row does not match the frozen user_id-first envelope")
    user_id = int(match.group(1))
    if user_id in consumed_users:
        return "consumed", user_id, None
    if partition_bucket(user_id, salt) < public_bucket_upper_exclusive:
        row = loads(raw)
        if not isinstance(row, dict) or int(row.get("user_id", -1)) != user_id:
            raise ValueError("decoded user_id differs from the routed envelope")
        return "public", user_id, row
    return "authority", user_id, None


def stream_local_jsonl(
    path: Path,
    *,
    expected_bytes: int,
    expected_sha256: str,
    consume: Callable[[dict[str, Any], int], None],
) -> dict[str, Any]:
    started = time.perf_counter()
    digest = hashlib.sha256()
    byte_count = row_count = 0
    buffer = b""
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(4 * 1024 * 1024), b""):
            digest.update(chunk)
            byte_count += len(chunk)
            buffer += chunk
            lines = buffer.split(b"\n")
            buffer = lines.pop()
            for raw in lines:
                if not raw:
                    continue
                row_count += 1
                consume(json.loads(raw), row_count)
        if buffer:
            row_count += 1
            consume(json.loads(buffer), row_count)
    actual_hash = digest.hexdigest()
    if byte_count != expected_bytes or actual_hash != expected_sha256:
        raise RuntimeError(
            f"local source verification failed for {path}: "
            f"bytes={byte_count} sha256={actual_hash}"
        )
    return {
        "path": str(path),
        "readOnly": not path.stat().st_mode & 0o200,
        "expectedBytes": expected_bytes,
        "actualBytes": byte_count,
        "expectedSha256": expected_sha256,
        "actualSha256": actual_hash,
        "rows": row_count,
        "wallClockSeconds": round(time.perf_counter() - started, 3),
        "verification": "PASS",
    }


def stream_partitioned_recall(
    path: Path,
    *,
    expected_bytes: int,
    expected_sha256: str,
    consumed_users: set[int],
    salt: str,
    public_bucket_upper_exclusive: int,
    consume_public: Callable[[dict[str, Any], int], None],
) -> dict[str, Any]:
    started = time.perf_counter()
    full_digest = hashlib.sha256()
    public_digest = hashlib.sha256()
    authority_digest = hashlib.sha256()
    byte_count = row_count = 0
    counts: collections.Counter[str] = collections.Counter()
    buffer = b""

    def consume_raw(raw: bytes) -> None:
        nonlocal row_count
        if not raw:
            return
        row_count += 1
        route, _, row = route_recall_line(
            raw,
            consumed_users,
            salt=salt,
            public_bucket_upper_exclusive=public_bucket_upper_exclusive,
        )
        counts[route] += 1
        if route == "public":
            public_digest.update(raw)
            public_digest.update(b"\n")
            assert row is not None
            consume_public(row, row_count)
        elif route == "authority":
            authority_digest.update(raw)
            authority_digest.update(b"\n")

    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(4 * 1024 * 1024), b""):
            full_digest.update(chunk)
            byte_count += len(chunk)
            buffer += chunk
            lines = buffer.split(b"\n")
            buffer = lines.pop()
            for raw in lines:
                consume_raw(raw)
        if buffer:
            consume_raw(buffer)
    actual_hash = full_digest.hexdigest()
    if byte_count != expected_bytes or actual_hash != expected_sha256:
        raise RuntimeError(
            f"local source verification failed for {path}: "
            f"bytes={byte_count} sha256={actual_hash}"
        )
    return {
        "path": str(path),
        "expectedBytes": expected_bytes,
        "actualBytes": byte_count,
        "expectedSha256": expected_sha256,
        "actualSha256": actual_hash,
        "rows": row_count,
        "publicDecodedRows": counts["public"],
        "authorityRawRowsSkippedBeforeJsonDecode": counts["authority"],
        "consumedRawRowsSkippedBeforeJsonDecode": counts["consumed"],
        "publicRawShardSha256": public_digest.hexdigest(),
        "authorityRawShardSha256": authority_digest.hexdigest(),
        "routing": {
            "version": "sha256-user-partition-v1",
            "anchoredPattern": ROUTE_PATTERN_TEXT,
            "salt": salt,
            "publicBucketRange": [0, public_bucket_upper_exclusive - 1],
            "authorityBucketRange": [public_bucket_upper_exclusive, 99],
            "jsonDecodedRoutes": ["public"],
            "jsonNotDecodedRoutes": ["authority", "consumed"],
        },
        "wallClockSeconds": round(time.perf_counter() - started, 3),
        "verification": "PASS",
    }


def build_records(
    raw_sessions: dict[int, list[dict[str, Any]]],
    *,
    minimum_history: int,
) -> tuple[dict[int, list[dict[str, Any]]], list[int]]:
    records_by_user: dict[int, list[dict[str, Any]]] = collections.defaultdict(list)
    for user_id, sessions in raw_sessions.items():
        grouped: dict[int, list[dict[str, Any]]] = collections.defaultdict(list)
        for session in sessions:
            grouped[session["timeIndex"]].append(session)
        history: list[int] = []
        for time_index in sorted(grouped):
            current = sorted(grouped[time_index], key=lambda row: row["sessionId"])
            if len(history) >= minimum_history:
                for session in current:
                    if session["candidates"]:
                        records_by_user[user_id].append({
                            "sessionId": session["sessionId"],
                            "timeIndex": time_index,
                            "history": list(history),
                            "candidates": list(session["candidates"]),
                            "futureStrictClicks": set(session["clicked"]),
                        })
            for session in current:
                history.extend(session["clicked"])
    eligible_users = sorted(
        user_id for user_id, records in records_by_user.items() if records
    )
    return records_by_user, eligible_users


def public_gate(result: dict[str, Any], gates: dict[str, Any]) -> dict[str, Any]:
    conditional_ci = result["conditionalCMinusABootstrap95User"]
    demonstrated_ci = result["demonstratedCMinusABootstrap95AllRequestedUsers"]
    checks = {
        "minimumEligibleUsers": result["requestedUsers"] >= gates["minimumEligibleUsers"],
        "minimumCandidateAtLeast2Coverage": (
            result["candidateAtLeast2Coverage"] >= gates["minimumCandidateAtLeast2Coverage"]
        ),
        "minimumFuturePositiveSessionCoverage": (
            result["futurePositiveSessionCoverage"] >= gates["minimumFuturePositiveSessionCoverage"]
        ),
        "minimumPositiveLabelUserCoverage": (
            result["evaluableUserCoverage"] >= gates["minimumPositiveLabelUserCoverage"]
        ),
        "conditionalNdcgLowerPositive": (
            conditional_ci["ndcgAt10"] is not None
            and conditional_ci["ndcgAt10"][0] > gates["conditionalNdcgLowerGreaterThan"]
        ),
        "conditionalHitNonInferior": (
            conditional_ci["hitAt3"] is not None
            and conditional_ci["hitAt3"][0] >= gates["conditionalHitLowerAtLeast"]
        ),
        "conditionalMrrNonInferior": (
            conditional_ci["mrr"] is not None
            and conditional_ci["mrr"][0] >= gates["conditionalMrrLowerAtLeast"]
        ),
        "demonstratedNdcgLowerPositive": (
            demonstrated_ci["ndcgAt10"] is not None
            and demonstrated_ci["ndcgAt10"][0] > gates["demonstratedNdcgLowerGreaterThan"]
        ),
        "demonstratedHitNonInferior": (
            demonstrated_ci["hitAt3"] is not None
            and demonstrated_ci["hitAt3"][0] >= gates["demonstratedHitLowerAtLeast"]
        ),
        "demonstratedMrrNonInferior": (
            demonstrated_ci["mrr"] is not None
            and demonstrated_ci["mrr"][0] >= gates["demonstratedMrrLowerAtLeast"]
        ),
        "candidateMultisetPreserved": result["candidateSetFailures"] == 0,
        "inputCandidatesUnique": result["inputDuplicateCandidateSessions"] == 0,
        "hardFilteringZero": result["hardFilteredCandidates"] == 0,
        "bAndCDiffer": result["bCDivergenceSessions"] > 0,
        "minimumChangeRate": result["changeRate"] >= gates["minimumChangeRate"],
        "maximumChangeRate": result["changeRate"] <= gates["maximumChangeRate"],
        "latencyP95": result["rerankLatencyMs"]["p95"] <= gates["maximumRerankP95Ms"],
        "profileP95Bytes": result["profileBytes"]["p95"] <= gates["maximumProfileP95Bytes"],
    }
    return {"checks": checks, "decision": "ACCEPT" if all(checks.values()) else "HOLD"}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    if args.output_dir.exists():
        raise FileExistsError(f"refusing to overwrite {args.output_dir}")
    started_at = utc_now()
    manifest_raw = args.manifest.read_bytes()
    manifest = json.loads(manifest_raw)
    if manifest.get("schemaVersion") != "kuaisearch-lite-phone-behavior-memory-v2.2-public-preregistration":
        raise ValueError("manifest version mismatch")
    if manifest["dataset"]["revision"] != REVISION:
        raise ValueError("dataset revision mismatch")
    if sha256_file(Path(__file__)) != manifest["code"]["runnerSha256"]:
        raise ValueError("runner hash mismatch")
    helper_path = SCRIPT_DIR / "run_kuaisearch_lite_phone_behavior_memory_v2_1.py"
    if sha256_file(helper_path) != manifest["code"]["v2_1HelperSha256"]:
        raise ValueError("V2.1 helper hash mismatch")

    split_path = Path(manifest["consumedUsers"]["splitPath"])
    if sha256_file(split_path) != manifest["consumedUsers"]["splitSha256"]:
        raise ValueError("consumed split hash mismatch")
    split = json.loads(split_path.read_text(encoding="utf-8"))["splits"]
    consumed_users = set(map(int, split["development"] + split["validation"] + split["sealed-test"]))
    if len(consumed_users) != manifest["consumedUsers"]["count"]:
        raise ValueError("consumed user count mismatch")

    items_source = manifest["dataset"]["localSources"]["itemsLite"]
    recall_source = manifest["dataset"]["localSources"]["recallLite"]
    flags = bytearray(1)
    item_meta: dict[int, dict[str, Any]] = {}

    def consume_item(row: dict[str, Any], _: int) -> None:
        nonlocal flags
        item_id = int(row["item_id"])
        if item_id >= len(flags):
            flags.extend(b"\0" * (item_id + 1 - len(flags)))
        names = tuple(str(row.get(key) or "") for key in (
            "category_level1_name", "category_level2_name", "category_level3_name",
        ))
        strict, _ = classify_category(names)
        flags[item_id] = int(strict)
        if strict:
            item_meta[item_id] = {
                "brand": normalize_text(row.get("brand_name")),
                "seller": normalize_text(row.get("seller_name")),
                "titleTokens": title_tokens(row.get("item_title")),
            }

    item_receipt = stream_local_jsonl(
        Path(items_source["path"]),
        expected_bytes=items_source["bytes"],
        expected_sha256=items_source["sha256"],
        consume=consume_item,
    )
    if len(item_meta) != manifest["dataset"]["strictPhoneItems"]:
        raise RuntimeError("strict phone catalog count drift")

    raw_sessions: dict[int, list[dict[str, Any]]] = collections.defaultdict(list)

    def consume_public_recall(row: dict[str, Any], _: int) -> None:
        user_id = int(row["user_id"])
        candidates = [
            int(item_id) for item_id in row.get("impressed_item_ids", [])
            if 0 <= int(item_id) < len(flags) and flags[int(item_id)]
        ]
        clicked = [
            int(item_id) for item_id in row.get("clicked_item_ids", [])
            if 0 <= int(item_id) < len(flags) and flags[int(item_id)]
        ]
        if candidates or clicked:
            raw_sessions[user_id].append({
                "sessionId": int(row["session_id"]),
                "timeIndex": int(row["time_index"]),
                "candidates": candidates,
                "clicked": clicked,
            })

    recall_receipt = stream_partitioned_recall(
        Path(recall_source["path"]),
        expected_bytes=recall_source["bytes"],
        expected_sha256=recall_source["sha256"],
        consumed_users=consumed_users,
        salt=manifest["partition"]["salt"],
        public_bucket_upper_exclusive=manifest["partition"]["publicBucketUpperExclusive"],
        consume_public=consume_public_recall,
    )
    records_by_user, eligible_users = build_records(
        raw_sessions,
        minimum_history=manifest["eligibility"]["minimumStrictEarlierClicks"],
    )
    result = evaluate(
        eligible_users,
        records_by_user,
        item_meta,
        manifest["frozenConfig"],
        bootstrap_seed=manifest["bootstrap"]["seed"],
        bootstrap_samples=manifest["bootstrap"]["samples"],
        include_per_user=True,
    )
    gate = public_gate(result, manifest["publicCalibrationGates"])

    args.output_dir.mkdir(parents=True, exist_ok=False)
    per_user_path = args.output_dir / "public-per-user.jsonl"
    per_user_rows = result.pop("perUser")
    with per_user_path.open("w", encoding="utf-8", newline="\n") as stream:
        for row in per_user_rows:
            stream.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    report = {
        "schemaVersion": "kuaisearch-lite-phone-behavior-memory-v2.2-public-report",
        "decision": (
            "PUBLIC_CALIBRATION_ACCEPT_AUTHORITY_ELIGIBLE"
            if gate["decision"] == "ACCEPT" else "HOLD_PUBLIC_CALIBRATION"
        ),
        "startedAt": started_at,
        "completedAt": utc_now(),
        "dataset": {"id": "benchen4395/KuaiSearch", "revision": REVISION},
        "sourceReceipts": {"itemsLite": item_receipt, "recallLite": recall_receipt},
        "consumedUsersExcluded": len(consumed_users),
        "strictPhoneItems": len(item_meta),
        "eligiblePublicUsers": len(eligible_users),
        "frozenConfig": manifest["frozenConfig"],
        "result": result,
        "gate": gate,
        "authority": {
            "status": "NOT_DECODED_OR_RUN_BY_PUBLIC_PROCESS",
            "rawRowsSkippedBeforeJsonDecode": recall_receipt[
                "authorityRawRowsSkippedBeforeJsonDecode"
            ],
            "rawShardSha256": recall_receipt["authorityRawShardSha256"],
            "mayRunOnlyAfterIndependentAudit": gate["decision"] == "ACCEPT",
        },
        "boundaries": {
            "minimumHistoryChangedFromV2_1": "3_to_1",
            "noDevelopmentTuning": True,
            "eligibilityUsesFutureClicks": False,
            "behaviorAuthority": "low",
            "productionSwitchAuthority": False,
        },
    }
    report_path = args.output_dir / "report.json"
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    receipt_path = args.output_dir / "receipt.json"
    receipt = {
        "schemaVersion": "kuaisearch-lite-phone-behavior-memory-v2.2-public-receipt",
        "decision": report["decision"],
        "manifestSha256": hashlib.sha256(manifest_raw).hexdigest(),
        "runnerSha256": sha256_file(Path(__file__)),
        "v2_1HelperSha256": sha256_file(helper_path),
        "consumedSplitSha256": sha256_file(split_path),
        "perUserSha256": sha256_file(per_user_path),
        "reportSha256": sha256_file(report_path),
        "authorityJsonDecodedRows": 0,
        "authorityExecuted": False,
    }
    receipt_path.write_text(
        json.dumps(receipt, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    artifacts = [per_user_path, report_path, receipt_path]
    (args.output_dir / "SHA256SUMS.txt").write_text(
        "".join(f"{sha256_file(path)}  {path.name}\n" for path in artifacts),
        encoding="ascii",
    )
    print(json.dumps({
        "decision": report["decision"],
        "eligiblePublicUsers": len(eligible_users),
        "gate": gate,
        "output": str(report_path),
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
