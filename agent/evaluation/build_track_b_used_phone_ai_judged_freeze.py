"""Build the frozen AI-judged Track B used-phone methodology pilot.

The output is intentionally not human gold and not a final closed test.  The
builder accepts candidate decisions only through exact reviewer/sealed
agreement or an explicit independent-AI adjudication row.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from evaluation.build_track_b_complex_intent_design import (
    canonical_json,
    sha256_file,
    write_json,
    write_jsonl,
    write_utf8_lf,
)


FREEZE_ID = "kuaisearch-track-b-used-phone-ai-judged-freeze-v01"
STATUS = "FROZEN_AI_JUDGED_PILOT_NOT_HUMAN_GOLD"
LABEL_KIND = "ai_judged_not_human_gold"
QREL_SCHEMA_VERSION = "track-b-used-phone-ai-judged-candidate-qrel-v1"
ACTION_SCHEMA_VERSION = "track-b-used-phone-ai-judged-case-action-v1"
CONSTRAINT_SCHEMA_VERSION = "track-b-used-phone-ai-judged-query-constraints-v1"
AUDIT_SCHEMA_VERSION = "track-b-used-phone-ai-judged-freeze-audit-v1"
MANIFEST_SCHEMA_VERSION = "track-b-used-phone-ai-judged-freeze-manifest-v1"
EXPECTED_REVIEW_ROWS = 83
EXPECTED_CANDIDATE_ROWS = 80
EXPECTED_ACTION_REVIEW_ROWS = 8
EXPECTED_ADJUDICATION_ROWS = 10
EXPECTED_CASE_ROWS = 8
EXPECTED_FINAL_GRADE = {"3": 20, "2": 20, "1": 29, "null": 11}
EXPECTED_FINAL_ELIGIBLE = {"true": 40, "false": 29, "unknown": 11}
ACTION_SEMANTIC_MAP_VERSION = "track-b-used-phone-case-action-semantic-map-v1"
ACTION_SEMANTIC_MAP = {
    "BLIND-CASE-001": ("retrieve_rank_with_tradeoff_explanation", "RETRIEVE_FILTER_AND_EXPLAIN_TRADEOFF"),
    "BLIND-CASE-002": ("retrieve_rank_with_priority_tradeoff", "RETRIEVE_FILTER_AND_EXPLAIN_TRADEOFF"),
    "BLIND-CASE-003": ("retrieve_filter_and_rank", "RETRIEVE_FILTER_AND_RANK"),
    "BLIND-CASE-004": ("clarify_condition_threshold_before_retrieval", "CLARIFY"),
    "BLIND-CASE-005": ("compare_visible_candidates", "COMPARE_WITH_FIELD_EVIDENCE"),
    "BLIND-CASE-006": ("exclude_prior_and_retrieve_replacement", "RETRIEVE_SUBSTITUTES_RETAINING_CONSTRAINTS"),
    "BLIND-CASE-007": ("abstain_from_unverifiable_guarantees_and_clarify_proxies", "ABSTAIN_OR_EXPLAIN"),
    "BLIND-CASE-008": ("update_constraints_then_retrieve", "UPDATE_STATE_THEN_RETRIEVE"),
}
FROZEN_INPUT_SHA256 = {
    "blindReviewer": "9d009811821b5d52f9c5bebe3d5c97422021a37a9257fa8ebd1e4546ff4a331b",
    "blindCaseActionReview": "d7c3beb33b2c5a584583ff343f7634652d87319c5692fe6a1dbb9195588fb4d5",
    "independentAiAdjudication": "bb5f70cdb64220492b0e6561618f17a7d059ef11812cdf19a66bdcc782d3a150",
    "independentAiAdjudicationSummary": "eb38b137e4a0b66345079d970e3daa6d1bd1d2e7d7cbd22910f214d61f253c49",
    "comparisonManifest": "9c800d15f682bfa65b45c85c925e872357c17cb13aac096d8b8ee935829a5110",
    "formalCases": "136c7a585c4b77d6c582abc7820daa2fb2a78d145a8636a06d1ee31593a75289",
    "sealedMapping": "f0935e0ffd3b38b915755c945aa713936239deac96766292992f6beff418ba84",
    "sealedSuggestions": "48c7830fdc91742b85201991ee15e38446200837e328e51be6159e14047425fa",
}
PROVENANCE = {
    "humanApproved": False,
    "aiOnly": True,
    "labelKind": LABEL_KIND,
    "humanGold": False,
    "finalClosedTest": False,
}


def strict_text(path: Path) -> str:
    raw = path.read_bytes()
    if raw.startswith(b"\xef\xbb\xbf"):
        raise ValueError(f"UTF-8 BOM is forbidden: {path}")
    if b"\r" in raw:
        raise ValueError(f"CR/CRLF is forbidden: {path}")
    if not raw.endswith(b"\n"):
        raise ValueError(f"terminal LF is required: {path}")
    return raw.decode("utf-8")


def strict_jsonl(path: Path, expected_rows: int) -> list[dict[str, Any]]:
    text = strict_text(path)
    lines = text.splitlines()
    if len(lines) != expected_rows:
        raise ValueError(f"expected {expected_rows} JSONL rows in {path.name}, got {len(lines)}")
    records = []
    for line_number, line in enumerate(lines, 1):
        if not line.strip():
            raise ValueError(f"blank JSONL row at {path}:{line_number}")
        value = json.loads(line)
        if not isinstance(value, dict):
            raise ValueError(f"expected object at {path}:{line_number}")
        records.append(value)
    return records


def strict_json(path: Path) -> dict[str, Any]:
    value = json.loads(strict_text(path))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def require_hashes(paths: dict[str, Path], expected_hashes: dict[str, str]) -> list[dict[str, Any]]:
    if set(paths) != set(expected_hashes):
        raise ValueError("input hash contract roles do not match supplied paths")
    descriptors = []
    for role in paths:
        path = paths[role]
        actual = sha256_file(path)
        if actual != expected_hashes[role]:
            raise ValueError(f"SHA-256 drift for {role}: expected {expected_hashes[role]}, got {actual}")
        descriptors.append({"role": role, "path": path.name, "bytes": path.stat().st_size, "sha256": actual})
    return descriptors


def unique_by(records: list[dict[str, Any]], key_fn, label: str) -> dict[Any, dict[str, Any]]:
    result = {}
    for record in records:
        key = key_fn(record)
        if key in result:
            raise ValueError(f"duplicate {label} key: {key}")
        result[key] = record
    return result


def require_ai_only(record: dict[str, Any], label: str) -> None:
    if record.get("aiOnly") is not True or record.get("humanApproved") is not False:
        raise ValueError(f"{label} must be AI-only with humanApproved=false")


def mapping_contract(mapping: dict[str, Any]) -> tuple[dict[str, str], dict[str, str], dict[str, str], dict[str, str]]:
    case_forward = mapping["caseIdToReviewCaseId"]
    case_reverse = mapping["reviewCaseIdToCaseId"]
    candidate_forward = mapping["candidateDisplayIdToBlindCandidateDisplayId"]
    candidate_reverse = mapping["blindCandidateDisplayIdToCandidateDisplayId"]
    if len(case_forward) != len(case_reverse) or any(case_reverse.get(value) != key for key, value in case_forward.items()):
        raise ValueError("case ID mapping is not bijective")
    if len(candidate_forward) != len(candidate_reverse) or any(candidate_reverse.get(value) != key for key, value in candidate_forward.items()):
        raise ValueError("candidate ID mapping is not bijective")
    return case_forward, case_reverse, candidate_forward, candidate_reverse


def validate_comparison_manifest(path: Path, manifest: dict[str, Any]) -> dict[str, Path]:
    if manifest.get("status") != "pending_ai_adjudication":
        raise ValueError("comparison manifest has an unexpected status")
    if manifest.get("formalGoldWritten") or manifest.get("formalQrelWritten"):
        raise ValueError("comparison manifest claims a formal gold/qrel write")
    artifact_paths = {}
    for descriptor in manifest.get("artifacts", []):
        artifact = path.parent / descriptor["path"]
        if not artifact.is_file() or artifact.stat().st_size != descriptor["bytes"] or sha256_file(artifact) != descriptor["sha256"]:
            raise ValueError(f"comparison artifact drift: {descriptor['path']}")
        artifact_paths[descriptor["path"]] = artifact
    required = {"agreement_summary.json", "disagreement_queue.jsonl", "structured_constraint_evidence_review.jsonl"}
    if not required.issubset(artifact_paths):
        raise ValueError("comparison manifest lacks required audit artifacts")
    return artifact_paths


def grade_key(value: Any) -> str:
    return "null" if value is None else str(value)


def eligible_key(value: Any) -> str:
    if value is True:
        return "true"
    if value is False:
        return "false"
    if value == "unknown":
        return "unknown"
    raise ValueError(f"invalid eligible value: {value!r}")


def validate_grade_eligibility(grade: Any, eligible: Any) -> None:
    expected = {3: True, 2: True, 1: False, None: "unknown"}
    if grade not in expected or eligible != expected[grade]:
        raise ValueError(f"invalid final grade/eligible pair: {grade!r}/{eligible!r}")


def build(
    reviewer_path: Path,
    case_action_review_path: Path,
    adjudication_path: Path,
    adjudication_summary_path: Path,
    comparison_manifest_path: Path,
    formal_cases_path: Path,
    sealed_mapping_path: Path,
    sealed_suggestions_path: Path,
    output_dir: Path,
    schema_dir: Path,
    *,
    expected_hashes: dict[str, str] = FROZEN_INPUT_SHA256,
    expected_grade_distribution: dict[str, int] = EXPECTED_FINAL_GRADE,
    expected_eligible_distribution: dict[str, int] = EXPECTED_FINAL_ELIGIBLE,
) -> dict[str, Any]:
    paths = {
        "blindReviewer": reviewer_path,
        "blindCaseActionReview": case_action_review_path,
        "independentAiAdjudication": adjudication_path,
        "independentAiAdjudicationSummary": adjudication_summary_path,
        "comparisonManifest": comparison_manifest_path,
        "formalCases": formal_cases_path,
        "sealedMapping": sealed_mapping_path,
        "sealedSuggestions": sealed_suggestions_path,
    }
    input_descriptors = require_hashes(paths, expected_hashes)
    reviews = strict_jsonl(reviewer_path, EXPECTED_REVIEW_ROWS)
    case_action_reviews = strict_jsonl(case_action_review_path, EXPECTED_ACTION_REVIEW_ROWS)
    adjudications = strict_jsonl(adjudication_path, EXPECTED_ADJUDICATION_ROWS)
    formal_cases = strict_jsonl(formal_cases_path, EXPECTED_CASE_ROWS)
    sealed_suggestions = strict_jsonl(sealed_suggestions_path, EXPECTED_REVIEW_ROWS)
    adjudication_summary = strict_json(adjudication_summary_path)
    comparison_manifest = strict_json(comparison_manifest_path)
    mapping = strict_json(sealed_mapping_path)
    comparison_artifacts = validate_comparison_manifest(comparison_manifest_path, comparison_manifest)

    agreement_summary = strict_json(comparison_artifacts["agreement_summary.json"])
    if agreement_summary["exactAgreement"]["expectedAction"] != {"compared": 3, "agreed": 3, "disagreed": 0, "notApplicable": 80}:
        raise ValueError("comparison action scope/policy is not the corrected 3-action-only contract")
    if agreement_summary["counts"]["disagreementQueueRows"] != EXPECTED_ADJUDICATION_ROWS:
        raise ValueError("comparison disagreement count does not match adjudication contract")
    structured_rows = strict_jsonl(comparison_artifacts["structured_constraint_evidence_review.jsonl"], EXPECTED_REVIEW_ROWS)
    if any(row.get("semanticAgreement") is not None or row.get("requiresAiAdjudication") is not True for row in structured_rows):
        raise ValueError("structured evidence audit trail improperly claims semantic adjudication")
    disagreement_rows = strict_jsonl(comparison_artifacts["disagreement_queue.jsonl"], EXPECTED_ADJUDICATION_ROWS)
    for row in disagreement_rows:
        if {item["field"] for item in row["exactFieldDifferences"]} != {"relevanceGrade", "eligible"}:
            raise ValueError("freeze only accepts paired grade+eligible disagreement rows")

    case_forward, case_reverse, candidate_forward, candidate_reverse = mapping_contract(mapping)
    formal_by_original = unique_by(formal_cases, lambda row: row["caseId"], "formal case")
    if set(formal_by_original) != set(case_forward) or len(case_forward) != EXPECTED_CASE_ROWS:
        raise ValueError("formal cases do not match the sealed case mapping")
    review_by_key = unique_by(reviews, lambda row: (row["reviewCaseId"], row["candidateDisplayId"]), "review")
    if list(review_by_key) != sorted(review_by_key, key=lambda key: (key[0], key[1] or "")):
        raise ValueError("review rows are not in stable template order")
    for row in reviews:
        require_ai_only(row, "blind reviewer row")
    candidate_reviews = {key: row for key, row in review_by_key.items() if key[1] is not None}
    action_only_reviews = {key[0]: row for key, row in review_by_key.items() if key[1] is None}
    if len(candidate_reviews) != EXPECTED_CANDIDATE_ROWS or set(action_only_reviews) != {"BLIND-CASE-004", "BLIND-CASE-005", "BLIND-CASE-007"}:
        raise ValueError("reviewer candidate/action-only partition drift")

    sealed_by_key = unique_by(sealed_suggestions, lambda row: (row["caseId"], row["candidateDisplayId"]), "sealed suggestion")
    judgment_suggestions = {key: row for key, row in sealed_by_key.items() if row["candidateRole"] == "judgment_candidate"}
    expected_candidate_keys = {
        (case_forward[case_id], candidate_forward[candidate_id])
        for case_id, candidate_id in judgment_suggestions
    }
    if len(judgment_suggestions) != EXPECTED_CANDIDATE_ROWS or set(candidate_reviews) != expected_candidate_keys:
        raise ValueError("reviewer candidate keys do not exactly cover the 80 judgment-candidate pool")

    disagreement_keys = {(row["reviewCaseId"], row["candidateDisplayId"]) for row in disagreement_rows}
    adjudication_by_key = unique_by(adjudications, lambda row: (row["reviewCaseId"], row["candidateDisplayId"]), "adjudication")
    if set(adjudication_by_key) != disagreement_keys:
        raise ValueError("adjudication keys must exactly cover the comparison disagreement queue")
    if adjudication_summary.get("scope", {}).get("targetRows") != EXPECTED_ADJUDICATION_ROWS or adjudication_summary.get("scope", {}).get("adjudicatedRows") != EXPECTED_ADJUDICATION_ROWS:
        raise ValueError("adjudication summary row contract drift")

    candidate_rows = []
    for opaque_key in sorted(candidate_reviews):
        review = candidate_reviews[opaque_key]
        original_case_id = case_reverse[opaque_key[0]]
        original_candidate_id = candidate_reverse[opaque_key[1]]
        sealed = judgment_suggestions[(original_case_id, original_candidate_id)]
        reviewer_value = {"relevanceGrade": review["relevanceGrade"], "eligible": review["eligible"]}
        automatic = sealed["automaticSuggestion"]
        sealed_value = {"relevanceGrade": automatic["relevanceGrade"], "eligible": automatic["eligible"]}
        adjudication = adjudication_by_key.get(opaque_key)
        if adjudication is None:
            if reviewer_value != sealed_value:
                raise ValueError(f"unadjudicated reviewer/sealed disagreement: {opaque_key}")
            final_value = reviewer_value
            final_basis = "reviewer_sealed_exact_agreement"
            adjudication_details = None
        else:
            require_ai_only(adjudication, "adjudication row")
            if adjudication["reviewerValue"] != reviewer_value or adjudication["sealedValue"] != sealed_value:
                raise ValueError(f"adjudication input values drift: {opaque_key}")
            if adjudication["originalCaseId"] != original_case_id or adjudication["originalCandidateDisplayId"] != original_candidate_id:
                raise ValueError(f"adjudication original ID mapping drift: {opaque_key}")
            final_value = adjudication["finalAiJudgment"]
            final_basis = "independent_ai_adjudication"
            adjudication_details = {
                "selectedBasis": adjudication["selectedBasis"],
                "reason": adjudication["reason"],
                "confidence": adjudication["confidence"],
                "evidenceCitations": adjudication["evidenceCitations"],
                "ruleFollowup": adjudication["ruleFollowup"],
            }
        grade, eligible = final_value["relevanceGrade"], final_value["eligible"]
        validate_grade_eligibility(grade, eligible)
        candidate_rows.append({
            "schemaVersion": QREL_SCHEMA_VERSION,
            "freezeId": FREEZE_ID,
            "status": STATUS,
            "reviewCaseId": opaque_key[0],
            "candidateDisplayId": opaque_key[1],
            "originalCaseId": original_case_id,
            "originalCandidateDisplayId": original_candidate_id,
            "relevanceGrade": grade,
            "eligible": eligible,
            "hardViolations": review["hardViolations"],
            "hardUnknowns": review["hardUnknowns"],
            "softAssessment": review["softAssessment"],
            "evidenceCitations": review["evidenceCitations"],
            "reviewerValue": reviewer_value,
            "sealedAutomaticValue": sealed_value,
            "finalBasis": final_basis,
            "adjudicationDetails": adjudication_details,
            "assessmentDetailPolicy": "reviewer_structured_details_preserved; adjudication may override only final grade/eligible",
            "labelKind": LABEL_KIND,
            "humanApproved": False,
            "aiOnly": True,
        })

    grade_distribution = Counter(grade_key(row["relevanceGrade"]) for row in candidate_rows)
    eligible_distribution = Counter(eligible_key(row["eligible"]) for row in candidate_rows)
    if dict(grade_distribution) != expected_grade_distribution:
        raise ValueError(f"final grade distribution drift: {dict(grade_distribution)}")
    if dict(eligible_distribution) != expected_eligible_distribution:
        raise ValueError(f"final eligible distribution drift: {dict(eligible_distribution)}")

    case_action_by_id = unique_by(case_action_reviews, lambda row: row["reviewCaseId"], "case action review")
    if set(case_action_by_id) != set(ACTION_SEMANTIC_MAP):
        raise ValueError("case action review must cover exactly BLIND-CASE-001..008")
    case_action_rows = []
    for review_case_id in sorted(ACTION_SEMANTIC_MAP):
        action_review = case_action_by_id[review_case_id]
        require_ai_only(action_review, "case action review")
        original_case_id = case_reverse[review_case_id]
        formal_case = formal_by_original[original_case_id]
        blind_action, formal_action = ACTION_SEMANTIC_MAP[review_case_id]
        if action_review["inferredCaseActionCode"] != blind_action or formal_case["expectedAction"] != formal_action:
            raise ValueError(f"case action semantic pair mismatch: {review_case_id}")
        supporting_action = action_only_reviews.get(review_case_id)
        comparison_support = None
        if review_case_id == "BLIND-CASE-005":
            if supporting_action is None or supporting_action["expectedAction"] != "compare":
                raise ValueError("BLIND-CASE-005 lacks its action-only comparison review")
            soft = supporting_action["softAssessment"]
            formal_ids = formal_case["comparisonContract"]["candidateDisplayIds"]
            opaque_a = candidate_forward[formal_ids["candidate_A"]]
            opaque_b = candidate_forward[formal_ids["candidate_B"]]
            citations = {item["candidateDisplayId"]: item for item in supporting_action["evidenceCitations"]}
            if set(citations) != {opaque_a, opaque_b} or soft.get("recommendation") != "candidate B":
                raise ValueError("BLIND-CASE-005 comparison recommendation/evidence IDs drift")
            raw_a = citations[opaque_a]["rawValue"]
            raw_b = citations[opaque_b]["rawValue"]
            if "90%+" not in raw_a or "80%-90%" not in raw_b or not soft.get("tradeoff"):
                raise ValueError("BLIND-CASE-005 battery-band tradeoff evidence drift")
            comparison_support = {
                "recommendedCandidateDisplayId": opaque_b,
                "candidateABatteryBand": "90%+",
                "candidateBBatteryBand": "80%-90%",
                "recommendedCandidateHasLowerBatteryBandThanA": True,
                "reviewerSoftAssessment": soft,
                "reviewerEvidenceCitations": supporting_action["evidenceCitations"],
                "reviewerReason": supporting_action["reason"],
            }
        case_action_rows.append({
            "schemaVersion": ACTION_SCHEMA_VERSION,
            "freezeId": FREEZE_ID,
            "status": STATUS,
            "reviewCaseId": review_case_id,
            "originalCaseId": original_case_id,
            "blindInferredCaseActionCode": blind_action,
            "formalExpectedAction": formal_action,
            "semanticPairMapVersion": ACTION_SEMANTIC_MAP_VERSION,
            "semanticAgreement": True,
            "flags": {
                "shouldRetrieve": action_review["shouldRetrieve"],
                "shouldClarify": action_review["shouldClarify"],
                "shouldCompare": action_review["shouldCompare"],
                "shouldAbstain": action_review["shouldAbstain"],
            },
            "actionSteps": action_review["actionSteps"],
            "stateUpdate": action_review["stateUpdate"],
            "constraintRetention": action_review["constraintRetention"],
            "evidencePolicy": action_review["evidencePolicy"],
            "reason": action_review["reason"],
            "supportingActionOnlyReview": None if supporting_action is None else {
                "expectedAction": supporting_action["expectedAction"],
                "independentlyExtractedConstraints": supporting_action["independentlyExtractedConstraints"],
                "softAssessment": supporting_action["softAssessment"],
                "evidenceCitations": supporting_action["evidenceCitations"],
                "reason": supporting_action["reason"],
            },
            "comparisonSupport": comparison_support,
            "labelKind": LABEL_KIND,
            "humanApproved": False,
            "aiOnly": True,
        })

    constraints_by_case: dict[str, dict[str, str]] = defaultdict(dict)
    source_counts = Counter()
    for review in reviews:
        encoded = canonical_json(review["independentlyExtractedConstraints"])
        constraints_by_case[review["reviewCaseId"]][encoded] = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
        source_counts[review["reviewCaseId"]] += 1
    if set(constraints_by_case) != set(ACTION_SEMANTIC_MAP) or any(len(variants) != 1 for variants in constraints_by_case.values()):
        raise ValueError("each case must collapse to exactly one reviewer constraint variant")
    constraint_rows = []
    for review_case_id in sorted(constraints_by_case):
        encoded, variant_sha = next(iter(constraints_by_case[review_case_id].items()))
        original_case_id = case_reverse[review_case_id]
        formal_case = formal_by_original[original_case_id]
        constraint_rows.append({
            "schemaVersion": CONSTRAINT_SCHEMA_VERSION,
            "freezeId": FREEZE_ID,
            "status": STATUS,
            "reviewCaseId": review_case_id,
            "originalCaseId": original_case_id,
            "independentlyExtractedConstraints": json.loads(encoded),
            "constraintVariantSha256": variant_sha,
            "sourceReviewRowCount": source_counts[review_case_id],
            "stableVariantCount": 1,
            "formalContracts": {
                "queryText": formal_case["queryText"],
                "constraintContract": formal_case["constraintContract"],
                "expectedAction": formal_case["expectedAction"],
                "actionContract": formal_case.get("actionContract"),
                "comparisonContract": formal_case.get("comparisonContract"),
                "substituteContract": formal_case.get("substituteContract"),
                "multiTurnContract": formal_case.get("multiTurnContract"),
            },
            "semanticSupport": "independent_ai_blind_review_stable_across_all_case_rows",
            "labelKind": LABEL_KIND,
            "humanApproved": False,
            "aiOnly": True,
        })

    output_dir.mkdir(parents=True, exist_ok=True)
    write_jsonl(output_dir / "ai_judged_candidate_qrel_v1.jsonl", candidate_rows)
    write_jsonl(output_dir / "ai_judged_case_action_v1.jsonl", case_action_rows)
    write_jsonl(output_dir / "ai_judged_query_constraints_v1.jsonl", constraint_rows)
    audit = {
        "schemaVersion": AUDIT_SCHEMA_VERSION,
        "freezeId": FREEZE_ID,
        "status": STATUS,
        "gates": {
            "inputHashesPinned": True,
            "reviewRows": len(reviews),
            "candidateRows": len(candidate_rows),
            "actionOnlyReviewRows": len(action_only_reviews),
            "caseActionRows": len(case_action_rows),
            "adjudicationRows": len(adjudications),
            "reviewerSealedExactAgreementRows": sum(row["finalBasis"] == "reviewer_sealed_exact_agreement" for row in candidate_rows),
            "independentAiAdjudicationRows": sum(row["finalBasis"] == "independent_ai_adjudication" for row in candidate_rows),
            "queryConstraintStableVariantsPerCase": {case_id: 1 for case_id in sorted(constraints_by_case)},
            "caseActionSemanticAgreement": {"agreed": 8, "compared": 8, "mapVersion": ACTION_SEMANTIC_MAP_VERSION},
            "finalGradeDistribution": dict(grade_distribution),
            "finalEligibleDistribution": dict(eligible_distribution),
        },
        "candidatePoolBoundary": {
            "sourceEvidenceCategory": "46/133/185",
            "sourceEvidenceProducts": 252,
            "judgedCandidatePoolRows": 80,
            "excludedActionOnlyRows": 3,
            "comparisonCandidatesNotConvertedToQrel": 2,
            "substituteAnchorNotConvertedToQrel": 1,
            "unpooledOrUnjudgedProductsAreNotAssumedNegative": True,
        },
        "structuredEvidenceAuditTrail": {
            "rows": len(structured_rows),
            "sourcePath": comparison_artifacts["structured_constraint_evidence_review.jsonl"].name,
            "sha256": sha256_file(comparison_artifacts["structured_constraint_evidence_review.jsonl"]),
            "semanticAgreementClaimed": False,
            "humanFieldAdjudicationClaimed": False,
        },
        "limitations": [
            "8-case methodology pilot, not a final closed test",
            "AI-only judgments, not human gold",
            "cannot support claims of production or category-general performance",
            "candidate qrel covers only the fixed 80-row judged pool",
        ],
        "networkUsed": False,
        "businessApiModified": False,
        "formalHumanGoldWritten": False,
        "formalClosedTestWritten": False,
        "provenance": PROVENANCE,
    }
    write_json(output_dir / "freeze_audit.json", audit)

    schema_names = (
        "track_b_used_phone_ai_judged_candidate_qrel_v1.schema.json",
        "track_b_used_phone_ai_judged_case_action_v1.schema.json",
        "track_b_used_phone_ai_judged_query_constraints_v1.schema.json",
        "track_b_used_phone_ai_judged_freeze_audit_v1.schema.json",
        "track_b_used_phone_ai_judged_freeze_manifest_v1.schema.json",
    )
    for schema_name in schema_names:
        write_utf8_lf(output_dir / schema_name, (schema_dir / schema_name).read_text(encoding="utf-8"))
    report = f"""# Track B used-phone AI-judged methodology pilot freeze\n\nStatus: `{STATUS}`\n\n- 8-case methodology pilot; not human gold and not a final closed test.\n- Candidate judgments: 80 (70 reviewer/sealed exact agreements + 10 independent-AI adjudications).\n- Final grade distribution: 3/2/1/null = 20/20/29/11.\n- Final eligibility distribution: true/false/unknown = 40/29/11.\n- Case actions: 8/8 semantic agreement under `{ACTION_SEMANTIC_MAP_VERSION}`.\n- Query constraints: one stable independent-review variant per case (8/8).\n- The 83 structured constraint/evidence comparison rows remain an audit trail; they were not individually human-adjudicated.\n- Candidate pool is fixed to 80 judged rows. Unpooled products are not negatives.\n- This pilot cannot establish production or cross-category generalization.\n"""
    write_utf8_lf(output_dir / "freeze_report.md", report)

    artifact_names = sorted((
        "ai_judged_candidate_qrel_v1.jsonl",
        "ai_judged_case_action_v1.jsonl",
        "ai_judged_query_constraints_v1.jsonl",
        "freeze_audit.json",
        "freeze_report.md",
        *schema_names,
    ))
    manifest = {
        "schemaVersion": MANIFEST_SCHEMA_VERSION,
        "freezeId": FREEZE_ID,
        "status": STATUS,
        "inputs": input_descriptors,
        "counts": {"candidateQrelRows": 80, "caseActionRows": 8, "queryConstraintRows": 8, "adjudicationRows": 10},
        "networkUsed": False,
        "humanGoldWritten": False,
        "finalClosedTestWritten": False,
        "provenance": PROVENANCE,
        "artifacts": [
            {"path": name, "bytes": (output_dir / name).stat().st_size, "sha256": sha256_file(output_dir / name)}
            for name in artifact_names
        ],
    }
    write_json(output_dir / "manifest.json", manifest)
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--reviewer", type=Path, required=True)
    parser.add_argument("--case-action-review", type=Path, required=True)
    parser.add_argument("--adjudication", type=Path, required=True)
    parser.add_argument("--adjudication-summary", type=Path, required=True)
    parser.add_argument("--comparison-manifest", type=Path, required=True)
    parser.add_argument("--formal-cases", type=Path, required=True)
    parser.add_argument("--sealed-mapping", type=Path, required=True)
    parser.add_argument("--sealed-suggestions", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--schema-dir", type=Path, default=Path(__file__).resolve().parent / "schemas")
    args = parser.parse_args()
    manifest = build(
        args.reviewer.resolve(), args.case_action_review.resolve(), args.adjudication.resolve(),
        args.adjudication_summary.resolve(), args.comparison_manifest.resolve(), args.formal_cases.resolve(),
        args.sealed_mapping.resolve(), args.sealed_suggestions.resolve(), args.output.resolve(), args.schema_dir.resolve(),
    )
    print(canonical_json({"counts": manifest["counts"], "status": manifest["status"]}))


if __name__ == "__main__":
    main()
