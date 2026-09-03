"""Freeze the complete Phase-A judged pool without rewriting accepted r2.

Reviewer A supplies the 508-item primary lane (plus 48 consistent repeats).
The accepted r2 adjudication replaces the final label for the 124 paired test
items.  The remaining 384 records stay explicitly single-reviewed.
"""
from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from pathlib import Path
from typing import Any

from agent.evaluation.kuaisearch_g2_phase_a_adjudicated_qrels_v1 import (
    PHASE_A_MANIFEST,
    PUBLIC_SAMPLE,
    _load_frozen_public_sample,
    _source,
    _xlsx_sheet_rows,
    canonical,
    normalize_grade,
    reviewer_key,
    sha256,
    validate_bundle as validate_r2_bundle,
)

ROOT = Path(__file__).resolve().parents[2]
R2_DIR = ROOT / "data/annotations/ecommerce/kuaisearch_multicategory_retrieval_g2_phase_a_adjudicated_20260824_r2"
R2_QRELS = R2_DIR / "adjudicated_qrels.jsonl"
R2_MANIFEST = R2_DIR / "manifest.json"
R2_RECEIPT = R2_DIR / "receipt.json"
REVIEWER_A_SNAPSHOT = R2_DIR / "source_evidence/reviewer_a_return.xlsx"
DEFAULT_OUTPUT_DIR = ROOT / "data/annotations/ecommerce/kuaisearch_multicategory_retrieval_g2_phase_a_full_20260824_r3"
GRADE_VALUES = {"0", "1", "2", "3", "U"}
CONFLICT_VALUES = {"是", "否"}


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def build_full_records() -> tuple[list[dict[str, Any]], dict[str, int]]:
    validate_r2_bundle(R2_DIR)
    _, sample_rows = _load_frozen_public_sample(PUBLIC_SAMPLE)
    a_rows = _xlsx_sheet_rows(REVIEWER_A_SNAPSHOT, 2)[1:]
    if len(a_rows) != 556:
        raise ValueError("Reviewer A frozen lane must contain exactly 556 rows")

    a_groups: dict[tuple[str, str, str, str, str], list[dict[str, str]]] = defaultdict(list)
    for row in a_rows:
        a_groups[reviewer_key(row)].append(row)
    if len(a_groups) != 508 or sum(len(rows) - 1 for rows in a_groups.values()) != 48:
        raise ValueError("Reviewer A unique/repeat coverage drift")
    if any(
        len({(normalize_grade(row["G"]), row.get("H", "")) for row in rows}) != 1
        for rows in a_groups.values()
    ):
        raise ValueError("Reviewer A blinded repeat inconsistency")

    sample_by_key = {
        tuple(row[field] for field in ("query", "title", "attr_value", "brand", "seller_name")): row
        for row in sample_rows
    }
    if len(sample_by_key) != 508 or set(sample_by_key) != set(a_groups):
        raise ValueError("Reviewer A does not exactly cover the frozen public sample")

    r2_rows = _read_jsonl(R2_QRELS)
    r2_by_pair = {(row["queryId"], row["blindCandidateId"]): row for row in r2_rows}
    if len(r2_rows) != 124 or len(r2_by_pair) != 124:
        raise ValueError("accepted r2 paired coverage drift")

    records: list[dict[str, Any]] = []
    for key, sample in sorted(sample_by_key.items(), key=lambda item: (item[1]["queryId"], item[1]["blindCandidateId"])):
        a_group = a_groups[key]
        a = a_group[0]
        a_grade = normalize_grade(a["G"])
        a_conflict = a.get("H", "")
        if a_conflict not in CONFLICT_VALUES:
            raise ValueError("Reviewer A hard-constraint conflict domain drift")
        pair = (sample["queryId"], sample["blindCandidateId"])
        paired = r2_by_pair.get(pair)
        if paired is None:
            final_grade = a_grade
            final_conflict = a_conflict
            source = "REVIEWER_A_PRIMARY_ONLY"
        else:
            if paired["reviewerAGrade"] != a_grade or paired["reviewerAConflict"] != a_conflict:
                raise ValueError("r2 paired row is not bound to Reviewer A primary judgment")
            final_grade = str(paired["relevance"])
            final_conflict = paired["hardConstraintConflict"]
            source = "R2_PAIRED_ADJUDICATION"
        if final_grade not in GRADE_VALUES or final_conflict not in CONFLICT_VALUES:
            raise ValueError("final qrel grade/conflict domain drift")
        records.append(
            {
                "schemaVersion": "kuaisearch-g2-phase-a-full-qrel-v1",
                "queryId": sample["queryId"],
                "blindCandidateId": sample["blindCandidateId"],
                "split": sample["split"],
                "stratum": sample["stratum"],
                "relevance": final_grade,
                "hardConstraintConflict": final_conflict,
                "resolutionSource": source,
                "reviewerAItemIds": [row["A"] for row in a_group],
                "reviewerAGrade": a_grade,
                "reviewerAConflict": a_conflict,
                "secondReviewItemId": paired.get("secondReviewItemId") if paired else None,
                "reviewerBGrade": paired.get("reviewerBGrade") if paired else None,
                "reviewerBConflict": paired.get("reviewerBConflict") if paired else None,
            }
        )

    pairs = {(row["queryId"], row["blindCandidateId"]) for row in records}
    if len(records) != 508 or len(pairs) != 508 or set(r2_by_pair) - pairs:
        raise ValueError("full qrel identity coverage drift")
    counts = {
        "rows": 508,
        "queries": len({row["queryId"] for row in records}),
        "pairedAdjudicatedRows": sum(row["resolutionSource"] == "R2_PAIRED_ADJUDICATION" for row in records),
        "primaryOnlyRows": sum(row["resolutionSource"] == "REVIEWER_A_PRIMARY_ONLY" for row in records),
        "reviewerARepeatRows": 48,
        "unknownRows": sum(row["relevance"] == "U" for row in records),
    }
    if counts != {
        "rows": 508,
        "queries": 48,
        "pairedAdjudicatedRows": 124,
        "primaryOnlyRows": 384,
        "reviewerARepeatRows": 48,
        "unknownRows": 83,
    }:
        raise ValueError(f"full qrel frozen counts drift: {counts}")
    return records, counts


def _manifest_for(output_dir: Path, records: list[dict[str, Any]], counts: dict[str, int]) -> dict[str, Any]:
    qrels_path = output_dir / "full_qrels.jsonl"
    manifest: dict[str, Any] = {
        "schemaVersion": "kuaisearch-g2-phase-a-full-qrels-manifest-v1",
        "version": "kuaisearch-g2-phase-a-full-20260824-r3",
        "status": "PHASE_A_FULL_QRELS_READY_DIAGNOSTIC_ANALYSIS_PENDING",
        "coverageScope": "FULL_PHASE_A_JUDGED_POOL_MIXED_REVIEW_DEPTH",
        "counts": counts,
        "reviewDepthPolicy": {
            "paired": "124 rows use accepted r2 adjudication",
            "primaryOnly": "384 rows retain Reviewer A primary judgment and are not called double-reviewed",
            "unknown": "U remains unknown and is never coerced to relevance 0",
        },
        "sourceBindings": {
            "phaseAManifest": _source(PHASE_A_MANIFEST, role="FROZEN_PHASE_A_MANIFEST"),
            "publicReviewSample": _source(PUBLIC_SAMPLE, role="PUBLIC_REVIEW_SAMPLE", rows=508),
            "r2Manifest": _source(R2_MANIFEST, role="ACCEPTED_R2_MANIFEST"),
            "r2Receipt": _source(R2_RECEIPT, role="ACCEPTED_R2_RECEIPT"),
            "r2Qrels": _source(R2_QRELS, role="ACCEPTED_R2_ADJUDICATED_QRELS", rows=124),
            "reviewerA": _source(REVIEWER_A_SNAPSHOT, role="FROZEN_REVIEWER_A_RETURN", rows=556),
            "producer": _source(Path(__file__), role="PRODUCER"),
        },
        "artifacts": {"fullQrels": _source(qrels_path, role="FULL_PHASE_A_QRELS", rows=len(records))},
        "claimBoundary": "NO_RETRIEVAL_WINNER_COMPLETE_GOLD_OR_AGENT_BENEFIT_DECLARED",
    }
    manifest["canonicalDigest"] = hashlib.sha256(canonical(manifest).encode("utf-8")).hexdigest()
    return manifest


def materialize(output_dir: Path = DEFAULT_OUTPUT_DIR) -> Path:
    output_dir.mkdir(parents=True, exist_ok=False)
    records, counts = build_full_records()
    qrels_path = output_dir / "full_qrels.jsonl"
    qrels_path.write_text("".join(canonical(row) + "\n" for row in records), encoding="utf-8")
    manifest = _manifest_for(output_dir, records, counts)
    manifest_path = output_dir / "manifest.json"
    manifest_path.write_text(canonical(manifest) + "\n", encoding="utf-8")
    _validate_core(output_dir)
    receipt = {
        "schemaVersion": "kuaisearch-g2-phase-a-full-qrels-receipt-v1",
        "status": manifest["status"],
        "counts": counts,
        "checks": {
            "primaryCoverage": "PASS",
            "repeatConsistency": "PASS",
            "pairedR2Replacement": "PASS",
            "unknownPreserved": "PASS",
            "sourceBinding": "PASS",
        },
        "manifest": _source(manifest_path, role="MANIFEST"),
        "fullQrels": _source(qrels_path, role="FULL_PHASE_A_QRELS", rows=508),
    }
    (output_dir / "receipt.json").write_text(canonical(receipt) + "\n", encoding="utf-8")
    validate_bundle(output_dir)
    return output_dir


def _validate_core(output_dir: Path) -> None:
    records = _read_jsonl(output_dir / "full_qrels.jsonl")
    manifest = _read_json(output_dir / "manifest.json")
    rebuilt, counts = build_full_records()
    if records != rebuilt or manifest.get("counts") != counts:
        raise ValueError("full qrel replay mismatch")
    if manifest.get("coverageScope") != "FULL_PHASE_A_JUDGED_POOL_MIXED_REVIEW_DEPTH":
        raise ValueError("full qrel coverage scope drift")
    expected_sources = {
        "phaseAManifest": PHASE_A_MANIFEST,
        "publicReviewSample": PUBLIC_SAMPLE,
        "r2Manifest": R2_MANIFEST,
        "r2Receipt": R2_RECEIPT,
        "r2Qrels": R2_QRELS,
        "reviewerA": REVIEWER_A_SNAPSHOT,
        "producer": Path(__file__),
    }
    for name, path in expected_sources.items():
        binding = manifest["sourceBindings"][name]
        if binding["sha256"] != sha256(path) or binding["bytes"] != path.stat().st_size:
            raise ValueError(f"full qrel source binding drift: {name}")
        if Path(str(binding["path"])).is_absolute():
            raise ValueError(f"full qrel source path is absolute: {name}")
    qrels_path = output_dir / "full_qrels.jsonl"
    if manifest["artifacts"]["fullQrels"]["sha256"] != sha256(qrels_path):
        raise ValueError("full qrel artifact binding drift")
    expected = dict(manifest)
    digest = expected.pop("canonicalDigest", None)
    if digest != hashlib.sha256(canonical(expected).encode("utf-8")).hexdigest():
        raise ValueError("full qrel manifest digest mismatch")


def validate_bundle(output_dir: Path = DEFAULT_OUTPUT_DIR) -> None:
    _validate_core(output_dir)
    expected_files = {"full_qrels.jsonl", "manifest.json", "receipt.json"}
    if {path.name for path in output_dir.iterdir() if path.is_file()} != expected_files:
        raise ValueError("full qrel bundle file set drift")
    receipt = _read_json(output_dir / "receipt.json")
    if (
        receipt["manifest"]["sha256"] != sha256(output_dir / "manifest.json")
        or receipt["fullQrels"]["sha256"] != sha256(output_dir / "full_qrels.jsonl")
        or receipt["checks"]
        != {
            "primaryCoverage": "PASS",
            "repeatConsistency": "PASS",
            "pairedR2Replacement": "PASS",
            "unknownPreserved": "PASS",
            "sourceBinding": "PASS",
        }
    ):
        raise ValueError("full qrel receipt binding drift")


if __name__ == "__main__":
    materialize()
