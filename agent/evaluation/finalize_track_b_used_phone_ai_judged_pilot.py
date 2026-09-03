"""Atomically finalize and verify the Track B used-phone AI-judged pilot.

This module deliberately produces an AI-only methodology-pilot freeze.  It
must never be represented as human gold or as a final closed test.
"""

from __future__ import annotations

import argparse
import os
import tempfile
from collections import Counter
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

from evaluation.build_track_b_used_phone_ai_judged_freeze import (
    ACTION_SEMANTIC_MAP,
    EXPECTED_FINAL_ELIGIBLE,
    EXPECTED_FINAL_GRADE,
    FROZEN_INPUT_SHA256,
    STATUS,
    build,
    eligible_key,
    grade_key,
    sha256_file,
    strict_json,
    strict_jsonl,
    strict_text,
)


BASE_RELATIVE_PATH = Path(
    "data/processed/ecommerce/kuaisearch_synthetic_evidence_track_b_v01/"
    "09807c773ce67360ed8df30842e372182fcf7ad9"
)
OUTPUT_DIR_NAME = "used_phone_agent_pilot_ai_judged_freeze_v1"
INPUT_RELATIVE_PATHS = {
    "blindReviewer": Path("used_phone_agent_pilot_v1/independent_ai_blind_review_v1.jsonl"),
    "blindCaseActionReview": Path("used_phone_agent_pilot_v1/independent_ai_blind_case_action_review_v1.jsonl"),
    "independentAiAdjudication": Path(
        "used_phone_agent_pilot_post_blind_comparison_v1/independent_ai_adjudication_v1.jsonl"
    ),
    "independentAiAdjudicationSummary": Path(
        "used_phone_agent_pilot_post_blind_comparison_v1/independent_ai_adjudication_summary_v1.json"
    ),
    "comparisonManifest": Path(
        "used_phone_agent_pilot_post_blind_comparison_v1/comparison_manifest.json"
    ),
    "formalCases": Path("used_phone_agent_pilot_v1/used_phone_agent_cases_pending.jsonl"),
    "sealedMapping": Path("used_phone_agent_pilot_v1/blind_id_mapping_sealed_not_gold.json"),
    "sealedSuggestions": Path(
        "used_phone_agent_pilot_v1/automatic_judgment_suggestions_sealed_not_gold.jsonl"
    ),
}
SCHEMA_NAMES = {
    "candidate": "track_b_used_phone_ai_judged_candidate_qrel_v1.schema.json",
    "action": "track_b_used_phone_ai_judged_case_action_v1.schema.json",
    "constraints": "track_b_used_phone_ai_judged_query_constraints_v1.schema.json",
    "audit": "track_b_used_phone_ai_judged_freeze_audit_v1.schema.json",
    "manifest": "track_b_used_phone_ai_judged_freeze_manifest_v1.schema.json",
}
DATA_FILES = {
    "candidate": ("ai_judged_candidate_qrel_v1.jsonl", 80),
    "action": ("ai_judged_case_action_v1.jsonl", 8),
    "constraints": ("ai_judged_query_constraints_v1.jsonl", 8),
}
EXPECTED_OUTPUT_FILES = {
    *(name for name, _ in DATA_FILES.values()),
    "freeze_audit.json",
    "freeze_report.md",
    "manifest.json",
    *SCHEMA_NAMES.values(),
}


def default_base_dir() -> Path:
    return Path(__file__).resolve().parents[2] / BASE_RELATIVE_PATH


def input_paths(base_dir: Path) -> dict[str, Path]:
    return {role: base_dir / relative for role, relative in INPUT_RELATIVE_PATHS.items()}


def _load_validator(schema_path: Path) -> Draft202012Validator:
    schema = strict_json(schema_path)
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


def _require_unique(records: list[dict[str, Any]], fields: tuple[str, ...], label: str) -> None:
    keys = [tuple(record[field] for field in fields) for record in records]
    if len(keys) != len(set(keys)):
        raise ValueError(f"duplicate {label} key in finalized bundle")


def validate_output_bundle(output_dir: Path) -> dict[str, Any]:
    """Validate schemas, hashes, counts, provenance, and semantic freeze gates."""
    if not output_dir.is_dir():
        raise ValueError(f"freeze output directory is missing: {output_dir}")
    actual_files = {path.name for path in output_dir.iterdir() if path.is_file()}
    if actual_files != EXPECTED_OUTPUT_FILES:
        missing = sorted(EXPECTED_OUTPUT_FILES - actual_files)
        extra = sorted(actual_files - EXPECTED_OUTPUT_FILES)
        raise ValueError(f"freeze output file set drift; missing={missing}, extra={extra}")
    if any(path.is_dir() for path in output_dir.iterdir()):
        raise ValueError("freeze output must not contain subdirectories")

    # Every artifact, including copied schemas and the report, obeys the exact
    # UTF-8/no-BOM/LF/terminal-LF serialization contract.
    for name in sorted(EXPECTED_OUTPUT_FILES):
        strict_text(output_dir / name)

    validators = {
        role: _load_validator(output_dir / schema_name)
        for role, schema_name in SCHEMA_NAMES.items()
    }
    records: dict[str, list[dict[str, Any]]] = {}
    for role, (name, expected_rows) in DATA_FILES.items():
        rows = strict_jsonl(output_dir / name, expected_rows)
        for row in rows:
            validators[role].validate(row)
        records[role] = rows
    audit = strict_json(output_dir / "freeze_audit.json")
    manifest = strict_json(output_dir / "manifest.json")
    validators["audit"].validate(audit)
    validators["manifest"].validate(manifest)

    candidates = records["candidate"]
    _require_unique(candidates, ("reviewCaseId", "candidateDisplayId"), "candidate qrel")
    _require_unique(candidates, ("originalCaseId", "originalCandidateDisplayId"), "original candidate qrel")
    if Counter(grade_key(row["relevanceGrade"]) for row in candidates) != Counter(EXPECTED_FINAL_GRADE):
        raise ValueError("finalized grade distribution drift")
    if Counter(eligible_key(row["eligible"]) for row in candidates) != Counter(EXPECTED_FINAL_ELIGIBLE):
        raise ValueError("finalized eligible distribution drift")
    if Counter(row["finalBasis"] for row in candidates) != {
        "reviewer_sealed_exact_agreement": 70,
        "independent_ai_adjudication": 10,
    }:
        raise ValueError("final judgment basis distribution drift")
    if any(
        row["humanApproved"] is not False
        or row["aiOnly"] is not True
        or row["labelKind"] != "ai_judged_not_human_gold"
        for row in candidates
    ):
        raise ValueError("candidate provenance drift")

    actions = records["action"]
    _require_unique(actions, ("reviewCaseId",), "case action")
    expected_pairs = {
        review_case_id: pair for review_case_id, pair in ACTION_SEMANTIC_MAP.items()
    }
    actual_pairs = {
        row["reviewCaseId"]: (row["blindInferredCaseActionCode"], row["formalExpectedAction"])
        for row in actions
    }
    if actual_pairs != expected_pairs or not all(row["semanticAgreement"] is True for row in actions):
        raise ValueError("8/8 case-action semantic-pair contract drift")
    action_by_case = {row["reviewCaseId"]: row for row in actions}
    if action_by_case["BLIND-CASE-005"]["comparisonSupport"]["recommendedCandidateHasLowerBatteryBandThanA"] is not True:
        raise ValueError("case 005 comparison tradeoff support drift")
    for case_id in ("BLIND-CASE-004", "BLIND-CASE-005", "BLIND-CASE-007"):
        if action_by_case[case_id]["supportingActionOnlyReview"] is None:
            raise ValueError(f"{case_id} lacks its independent action-only support")

    constraints = records["constraints"]
    _require_unique(constraints, ("reviewCaseId",), "query constraints")
    if {row["reviewCaseId"] for row in constraints} != set(ACTION_SEMANTIC_MAP):
        raise ValueError("query-constraint case coverage drift")
    if any(row["stableVariantCount"] != 1 for row in constraints):
        raise ValueError("query constraints no longer collapse to one stable variant per case")
    if sum(row["sourceReviewRowCount"] for row in constraints) != 83:
        raise ValueError("query-constraint source review coverage drift")

    if manifest["status"] != STATUS or audit["status"] != STATUS:
        raise ValueError("freeze status drift")
    if manifest["inputs"] != [
        next(item for item in manifest["inputs"] if item["role"] == role)
        for role in INPUT_RELATIVE_PATHS
    ]:
        raise ValueError("manifest input roles/order drift")
    manifest_input_hashes = {item["role"]: item["sha256"] for item in manifest["inputs"]}
    if manifest_input_hashes != FROZEN_INPUT_SHA256:
        raise ValueError("manifest frozen input hashes drift")

    artifact_names = EXPECTED_OUTPUT_FILES - {"manifest.json"}
    descriptors = {item["path"]: item for item in manifest["artifacts"]}
    if set(descriptors) != artifact_names:
        raise ValueError("manifest artifact set drift")
    for name in sorted(artifact_names):
        path = output_dir / name
        descriptor = descriptors[name]
        if descriptor["bytes"] != path.stat().st_size or descriptor["sha256"] != sha256_file(path):
            raise ValueError(f"manifest artifact hash/size drift: {name}")

    if audit["networkUsed"] is not False or manifest["networkUsed"] is not False:
        raise ValueError("offline freeze contract drift")
    if audit["structuredEvidenceAuditTrail"]["humanFieldAdjudicationClaimed"] is not False:
        raise ValueError("structured evidence audit trail cannot claim human field adjudication")
    report = strict_text(output_dir / "freeze_report.md")
    required_report_phrases = (
        "8-case methodology pilot",
        "not human gold",
        "not a final closed test",
        "cannot establish production or cross-category generalization",
        "not individually human-adjudicated",
    )
    if any(phrase not in report for phrase in required_report_phrases):
        raise ValueError("freeze report limitation language drift")
    return manifest


def finalize(
    base_dir: Path,
    output_dir: Path,
    schema_dir: Path | None = None,
    *,
    expected_hashes: dict[str, str] = FROZEN_INPUT_SHA256,
) -> dict[str, Any]:
    """Build into a staging directory, validate, then publish without overwrite."""
    base_dir = base_dir.resolve()
    output_dir = output_dir.resolve()
    schema_dir = (
        schema_dir.resolve()
        if schema_dir is not None
        else Path(__file__).resolve().parent.joinpath("schemas")
    )
    if os.path.lexists(output_dir):
        raise FileExistsError(f"refusing to overwrite existing freeze output: {output_dir}")
    paths = input_paths(base_dir)
    missing = [str(path) for path in paths.values() if not path.is_file()]
    if missing:
        raise FileNotFoundError(f"required frozen inputs are missing: {missing}")
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=f".{OUTPUT_DIR_NAME}-", dir=output_dir.parent) as temporary:
        staging_dir = Path(temporary) / "bundle"
        build(
            paths["blindReviewer"],
            paths["blindCaseActionReview"],
            paths["independentAiAdjudication"],
            paths["independentAiAdjudicationSummary"],
            paths["comparisonManifest"],
            paths["formalCases"],
            paths["sealedMapping"],
            paths["sealedSuggestions"],
            staging_dir,
            schema_dir,
            expected_hashes=expected_hashes,
        )
        manifest = validate_output_bundle(staging_dir)
        staging_dir.rename(output_dir)
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-dir", type=Path, default=default_base_dir())
    parser.add_argument("--output", type=Path)
    parser.add_argument("--schema-dir", type=Path)
    args = parser.parse_args()
    base_dir = args.base_dir.resolve()
    output_dir = args.output.resolve() if args.output else base_dir / OUTPUT_DIR_NAME
    manifest = finalize(base_dir, output_dir, args.schema_dir)
    print(
        f"{manifest['status']}: "
        f"{manifest['counts']['candidateQrelRows']} qrels, "
        f"{manifest['counts']['caseActionRows']} actions, "
        f"{manifest['counts']['queryConstraintRows']} constraint rows"
    )


if __name__ == "__main__":
    main()
