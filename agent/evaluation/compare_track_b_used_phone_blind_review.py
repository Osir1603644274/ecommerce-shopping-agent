"""Compare a frozen blind review with sealed Track B used-phone suggestions.

This module is a post-blind gate, not a gold/qrel freezer.  Exact comparison is
limited to relevance grade, eligibility, and expected action.  Reviewer
constraints and evidence are emitted side by side with their sealed/formal
counterparts for later AI adjudication; no string equality is treated as
semantic agreement.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

from evaluation.build_track_b_complex_intent_design import (
    canonical_json,
    sha256_file,
    write_json,
    write_jsonl,
    write_utf8_lf,
)


GATE_ID = "kuaisearch-track-b-used-phone-post-blind-comparison-v01"
STATUS = "pending_ai_adjudication"
EXPECTED_REVIEW_ROWS = 83
EXPECTED_ACTION_ONLY_ROWS = 3
SUMMARY_SCHEMA_VERSION = "track-b-used-phone-post-blind-summary-v2"
DISAGREEMENT_SCHEMA_VERSION = "track-b-used-phone-post-blind-disagreement-v2"
STRUCTURED_SCHEMA_VERSION = "track-b-used-phone-post-blind-structured-review-v1"
ACTION_SCHEMA_VERSION = "track-b-used-phone-post-blind-action-comparison-v2"
ACTION_CANONICAL_MAP_VERSION = "track-b-used-phone-action-canonical-map-v1"
ACTION_CANONICAL_MAP = {
    "CLARIFY": "clarify",
    "COMPARE_WITH_FIELD_EVIDENCE": "compare",
    "ABSTAIN_OR_EXPLAIN": "abstain",
}
PROVENANCE = {
    "humanApproved": False,
    "aiOnly": True,
    "automaticGoldOrQrelFreeze": False,
}


def read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    with path.open(encoding="utf-8", newline="") as stream:
        for line_number, line in enumerate(stream, 1):
            if not line.strip():
                raise ValueError(f"blank JSONL row at {path}:{line_number}")
            value = json.loads(line)
            if not isinstance(value, dict):
                raise ValueError(f"expected JSON object at {path}:{line_number}")
            records.append(value)
    return records


def require_bijection(forward: Any, reverse: Any, label: str) -> tuple[dict[str, str], dict[str, str]]:
    if not isinstance(forward, dict) or not isinstance(reverse, dict):
        raise ValueError(f"{label} mapping must contain forward and reverse objects")
    if len(forward) != len(reverse):
        raise ValueError(f"{label} mapping cardinality differs")
    for original, opaque in forward.items():
        if reverse.get(opaque) != original:
            raise ValueError(f"{label} mapping is not bijective")
    return forward, reverse


def input_descriptor(role: str, path: Path) -> dict[str, Any]:
    return {
        "role": role,
        "path": path.name,
        "bytes": path.stat().st_size,
        "sha256": sha256_file(path),
    }


def review_key(record: dict[str, Any]) -> tuple[str, str | None]:
    return str(record["reviewCaseId"]), record.get("candidateDisplayId")


def validate_nonblank_review(record: dict[str, Any], *, action_only: bool) -> None:
    if not isinstance(record.get("expectedAction"), str) or not record["expectedAction"].strip():
        raise ValueError(f"blank expectedAction for {review_key(record)}")
    if not isinstance(record.get("reason"), str) or not record["reason"].strip():
        raise ValueError(f"blank reason for {review_key(record)}")
    if action_only:
        if record.get("relevanceGrade") is not None or record.get("eligible") is not None:
            raise ValueError(f"action-only row must leave grade/eligible null: {review_key(record)}")
        return
    if not record.get("independentlyExtractedConstraints"):
        raise ValueError(f"blank independentlyExtractedConstraints for {review_key(record)}")
    if not record.get("evidenceCitations"):
        raise ValueError(f"blank evidenceCitations for {review_key(record)}")
    eligible = record.get("eligible")
    grade = record.get("relevanceGrade")
    if eligible is None:
        raise ValueError(f"blank eligible for candidate row {review_key(record)}")
    if grade is None and not (eligible == "unknown" and record.get("hardUnknowns")):
        raise ValueError(f"null grade requires eligible=unknown and hardUnknowns: {review_key(record)}")


def exact_difference(field: str, reviewer_value: Any, sealed_value: Any) -> dict[str, Any] | None:
    if reviewer_value == sealed_value:
        return None
    return {"field": field, "reviewerValue": reviewer_value, "sealedValue": sealed_value}


def comparison_counter() -> dict[str, int]:
    return {"compared": 0, "agreed": 0, "disagreed": 0, "notApplicable": 0}


def update_counter(counter: dict[str, int], applicable: bool, exact_match: bool = False) -> None:
    if not applicable:
        counter["notApplicable"] += 1
    else:
        counter["compared"] += 1
        counter["agreed" if exact_match else "disagreed"] += 1


def compare(
    reviewer_path: Path,
    reviewer_schema_path: Path,
    sealed_mapping_path: Path,
    sealed_suggestions_path: Path,
    formal_cases_path: Path,
    output_dir: Path,
    output_schema_dir: Path,
) -> dict[str, Any]:
    reviewer_schema = read_json(reviewer_schema_path)
    Draft202012Validator.check_schema(reviewer_schema)
    validator = Draft202012Validator(reviewer_schema)
    reviews = read_jsonl(reviewer_path)
    if len(reviews) != EXPECTED_REVIEW_ROWS:
        raise ValueError(f"expected exactly {EXPECTED_REVIEW_ROWS} reviewer rows, got {len(reviews)}")
    for index, record in enumerate(reviews, 1):
        errors = sorted(validator.iter_errors(record), key=lambda error: list(error.path))
        if errors:
            raise ValueError(f"reviewer schema validation failed at row {index}: {errors[0].message}")

    mapping = read_json(sealed_mapping_path)
    case_forward, case_reverse = require_bijection(
        mapping.get("caseIdToReviewCaseId"), mapping.get("reviewCaseIdToCaseId"), "case ID"
    )
    candidate_forward, candidate_reverse = require_bijection(
        mapping.get("candidateDisplayIdToBlindCandidateDisplayId"),
        mapping.get("blindCandidateDisplayIdToCandidateDisplayId"),
        "candidate ID",
    )
    formal_cases = read_jsonl(formal_cases_path)
    sealed_suggestions = read_jsonl(sealed_suggestions_path)
    formal_by_id = {str(case["caseId"]): case for case in formal_cases}
    if len(formal_by_id) != len(formal_cases) or set(formal_by_id) != set(case_forward):
        raise ValueError("formal case IDs do not uniquely match sealed case mapping")

    sealed_by_key: dict[tuple[str, str], dict[str, Any]] = {}
    for suggestion in sealed_suggestions:
        key = (str(suggestion["caseId"]), str(suggestion["candidateDisplayId"]))
        if key in sealed_by_key:
            raise ValueError(f"duplicate sealed suggestion key: {key}")
        if key[0] not in formal_by_id or key[1] not in candidate_forward:
            raise ValueError(f"sealed suggestion references unknown ID: {key}")
        sealed_by_key[key] = suggestion

    expected_keys: set[tuple[str, str | None]] = set()
    action_only_case_ids: set[str] = set()
    for case_id, case in formal_by_id.items():
        opaque_case_id = case_forward[case_id]
        if case["ordinaryProductJudgments"]:
            candidates = [
                suggestion for (suggestion_case_id, _), suggestion in sealed_by_key.items()
                if suggestion_case_id == case_id and suggestion["candidateRole"] == "judgment_candidate"
            ]
            if not candidates:
                raise ValueError(f"ordinary case has no judgment candidates: {case_id}")
            expected_keys.update(
                (opaque_case_id, candidate_forward[str(suggestion["candidateDisplayId"])])
                for suggestion in candidates
            )
        else:
            expected_keys.add((opaque_case_id, None))
            action_only_case_ids.add(case_id)
    if len(action_only_case_ids) != EXPECTED_ACTION_ONLY_ROWS:
        raise ValueError(f"expected {EXPECTED_ACTION_ONLY_ROWS} action-only cases, got {len(action_only_case_ids)}")
    if len(expected_keys) != EXPECTED_REVIEW_ROWS:
        raise ValueError(f"derived template coverage must contain {EXPECTED_REVIEW_ROWS} keys, got {len(expected_keys)}")

    review_by_key: dict[tuple[str, str | None], dict[str, Any]] = {}
    ordered_review_keys = [review_key(record) for record in reviews]
    if ordered_review_keys != sorted(ordered_review_keys, key=lambda key: (key[0], key[1] or "")):
        raise ValueError("reviewer rows do not follow the deterministic template key order")
    for record in reviews:
        key = review_key(record)
        if key in review_by_key:
            raise ValueError(f"duplicate reviewer key: {key}")
        if key not in expected_keys:
            raise ValueError(f"unknown reviewer ID/template key: {key}")
        review_by_key[key] = record
    missing = sorted(expected_keys - set(review_by_key), key=lambda key: (key[0], key[1] or ""))
    if missing:
        raise ValueError(f"missing reviewer template keys: {missing[:3]}")

    counters = {field: comparison_counter() for field in ("relevanceGrade", "eligible", "expectedAction")}
    disagreements: list[dict[str, Any]] = []
    structured_rows: list[dict[str, Any]] = []
    action_rows: list[dict[str, Any]] = []
    exact_all_count = 0

    for opaque_key in sorted(expected_keys, key=lambda key: (key[0], key[1] or "")):
        record = review_by_key[opaque_key]
        original_case_id = case_reverse[opaque_key[0]]
        formal_case = formal_by_id[original_case_id]
        action_only = opaque_key[1] is None
        validate_nonblank_review(record, action_only=action_only)
        original_candidate_id = None if action_only else candidate_reverse[str(opaque_key[1])]
        sealed = None if action_only else sealed_by_key[(original_case_id, original_candidate_id)]

        exact_differences: list[dict[str, Any]] = []
        if action_only:
            formal_action = formal_case["expectedAction"]
            if formal_action not in ACTION_CANONICAL_MAP:
                raise ValueError(f"action-only formal action is absent from {ACTION_CANONICAL_MAP_VERSION}: {formal_action}")
            canonical_reviewer_action = ACTION_CANONICAL_MAP[formal_action]
            action_match = record["expectedAction"] == canonical_reviewer_action
            update_counter(counters["expectedAction"], True, action_match)
            if not action_match:
                exact_differences.append({
                    "field": "expectedAction",
                    "reviewerValue": record["expectedAction"],
                    "sealedValue": canonical_reviewer_action,
                })
            update_counter(counters["relevanceGrade"], False)
            update_counter(counters["eligible"], False)
        else:
            action_match = False
            update_counter(counters["expectedAction"], False)
            automatic = sealed["automaticSuggestion"]
            grade_diff = exact_difference("relevanceGrade", record["relevanceGrade"], automatic["relevanceGrade"])
            eligible_diff = exact_difference("eligible", record["eligible"], automatic["eligible"])
            update_counter(counters["relevanceGrade"], True, grade_diff is None)
            update_counter(counters["eligible"], True, eligible_diff is None)
            exact_differences.extend(item for item in (grade_diff, eligible_diff) if item is not None)

        structured = {
            "schemaVersion": STRUCTURED_SCHEMA_VERSION,
            "gateId": GATE_ID,
            "status": STATUS,
            "reviewCaseId": opaque_key[0],
            "candidateDisplayId": opaque_key[1],
            "originalCaseId": original_case_id,
            "originalCandidateDisplayId": original_candidate_id,
            "reviewerExtractedConstraints": record["independentlyExtractedConstraints"],
            "formalConstraintAtoms": formal_case["constraintContract"]["atoms"],
            "reviewerEvidenceCitations": record["evidenceCitations"],
            "sealedAutomaticEvidence": [] if action_only else sealed["automaticSuggestion"].get("requirementEvidence", []),
            "comparisonPolicy": "structured_side_by_side_only_no_automatic_semantic_equivalence",
            "semanticAgreement": None,
            "requiresAiAdjudication": True,
            "provenance": PROVENANCE,
        }
        structured_rows.append(structured)

        if action_only:
            action_rows.append({
                "schemaVersion": ACTION_SCHEMA_VERSION,
                "gateId": GATE_ID,
                "status": STATUS,
                "reviewCaseId": opaque_key[0],
                "originalCaseId": original_case_id,
                "reviewerExpectedAction": record["expectedAction"],
                "formalExpectedAction": formal_case["expectedAction"],
                "canonicalReviewerExpectedAction": canonical_reviewer_action,
                "actionCanonicalMapVersion": ACTION_CANONICAL_MAP_VERSION,
                "comparisonPolicy": "versioned_canonical_pair_map_not_raw_string_equality",
                "canonicalActionMatch": action_match,
                "structuredComparisonRef": {"reviewCaseId": opaque_key[0], "candidateDisplayId": None},
                "provenance": PROVENANCE,
            })
        if exact_differences:
            disagreements.append({
                "schemaVersion": DISAGREEMENT_SCHEMA_VERSION,
                "gateId": GATE_ID,
                "status": STATUS,
                "reviewCaseId": opaque_key[0],
                "candidateDisplayId": opaque_key[1],
                "originalCaseId": original_case_id,
                "originalCandidateDisplayId": original_candidate_id,
                "exactFieldDifferences": exact_differences,
                "structuredComparisonRef": {"reviewCaseId": opaque_key[0], "candidateDisplayId": opaque_key[1]},
                "provenance": PROVENANCE,
            })
        else:
            exact_all_count += 1

    input_evidence = [
        input_descriptor("blind_reviewer_jsonl", reviewer_path),
        input_descriptor("blind_reviewer_schema", reviewer_schema_path),
        input_descriptor("sealed_opaque_mapping", sealed_mapping_path),
        input_descriptor("sealed_automatic_suggestions", sealed_suggestions_path),
        input_descriptor("formal_case_contract", formal_cases_path),
    ]
    summary = {
        "schemaVersion": SUMMARY_SCHEMA_VERSION,
        "gateId": GATE_ID,
        "status": STATUS,
        "counts": {
            "reviewRows": len(reviews),
            "candidateRows": len(reviews) - len(action_rows),
            "actionOnlyRows": len(action_rows),
            "rowsWithAllApplicableExactFieldsAgreed": exact_all_count,
            "disagreementQueueRows": len(disagreements),
            "structuredReviewRowsPendingAiAdjudication": len(structured_rows),
        },
        "exactAgreement": counters,
        "exactComparisonScope": {
            "candidateRows": ["relevanceGrade", "eligible"],
            "actionOnlyRows": ["expectedAction"],
        },
        "actionComparisonPolicy": {
            "scope": "candidate_display_id_null_only",
            "candidateRowTreatment": "notApplicable",
            "comparisonMethod": "versioned_canonical_pair_map_not_raw_string_equality",
            "canonicalMapVersion": ACTION_CANONICAL_MAP_VERSION,
            "canonicalPairs": [
                {"formalExpectedAction": formal, "reviewerExpectedAction": reviewer}
                for formal, reviewer in ACTION_CANONICAL_MAP.items()
            ],
        },
        "constraintEvidencePolicy": "structured_side_by_side_only_no_automatic_semantic_equivalence",
        "inputs": input_evidence,
        "provenance": PROVENANCE,
    }

    output_dir.mkdir(parents=True, exist_ok=True)
    write_json(output_dir / "agreement_summary.json", summary)
    write_jsonl(output_dir / "disagreement_queue.jsonl", disagreements)
    write_jsonl(output_dir / "structured_constraint_evidence_review.jsonl", structured_rows)
    write_jsonl(output_dir / "action_only_comparison.jsonl", action_rows)
    schema_names = (
        "track_b_used_phone_post_blind_summary_v2.schema.json",
        "track_b_used_phone_post_blind_disagreement_v2.schema.json",
        "track_b_used_phone_post_blind_structured_review_v1.schema.json",
        "track_b_used_phone_post_blind_action_comparison_v2.schema.json",
    )
    for schema_name in schema_names:
        write_utf8_lf(output_dir / schema_name, (output_schema_dir / schema_name).read_text(encoding="utf-8"))
    for legacy_schema_name in (
        "track_b_used_phone_post_blind_summary_v1.schema.json",
        "track_b_used_phone_post_blind_disagreement_v1.schema.json",
        "track_b_used_phone_post_blind_action_comparison_v1.schema.json",
    ):
        legacy_schema_path = output_dir / legacy_schema_name
        if legacy_schema_path.exists():
            legacy_schema_path.unlink()

    artifact_names = sorted((
        "agreement_summary.json",
        "disagreement_queue.jsonl",
        "structured_constraint_evidence_review.jsonl",
        "action_only_comparison.jsonl",
        *schema_names,
    ))
    manifest = {
        "gateId": GATE_ID,
        "status": STATUS,
        "inputs": input_evidence,
        "counts": summary["counts"],
        "networkUsed": False,
        "formalGoldWritten": False,
        "formalQrelWritten": False,
        "provenance": PROVENANCE,
        "artifacts": [
            {"path": name, "bytes": (output_dir / name).stat().st_size, "sha256": sha256_file(output_dir / name)}
            for name in artifact_names
        ],
    }
    write_json(output_dir / "comparison_manifest.json", manifest)
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--reviewer", type=Path, required=True)
    parser.add_argument("--reviewer-schema", type=Path, required=True)
    parser.add_argument("--sealed-mapping", type=Path, required=True)
    parser.add_argument("--sealed-suggestions", type=Path, required=True)
    parser.add_argument("--formal-cases", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--output-schema-dir", type=Path, default=Path(__file__).resolve().parent / "schemas")
    args = parser.parse_args()
    manifest = compare(
        args.reviewer.resolve(),
        args.reviewer_schema.resolve(),
        args.sealed_mapping.resolve(),
        args.sealed_suggestions.resolve(),
        args.formal_cases.resolve(),
        args.output.resolve(),
        args.output_schema_dir.resolve(),
    )
    print(canonical_json({"counts": manifest["counts"], "status": manifest["status"]}))


if __name__ == "__main__":
    main()
