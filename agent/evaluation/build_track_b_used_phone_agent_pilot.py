"""Build the AI-only, blind-audit-pending used-phone Agent pilot.

No retrieval system is run.  Products are selected directly from the pinned
evidence category using deterministic evidence strata.  Automatic judgments
are sealed in a separate not-gold file; blind candidates contain no retrieval
rank, score, system, or suggested answer.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterable

from evaluation.build_track_b_complex_category_capability_audit import (
    CATALOG,
    GroupRule,
    observe,
    token_matches,
)
from evaluation.build_track_b_complex_intent_design import (
    canonical_json,
    sha256_file,
    stable_repository_relative_path,
    write_json,
    write_jsonl,
    write_utf8_lf,
)


PILOT_ID = "kuaisearch-track-b-used-phone-complex-agent-pilot-v01"
PILOT_VERSION = "used-phone-one-category-eight-case-v1"
CASE_SCHEMA_VERSION = "track-b-used-phone-agent-case-v1"
CANDIDATE_SCHEMA_VERSION = "track-b-used-phone-blind-candidate-v3"
BLIND_CASE_SCHEMA_VERSION = "track-b-used-phone-blind-case-v2"
REVIEW_OUTPUT_SCHEMA_VERSION = "track-b-used-phone-blind-reviewer-output-v2"
JUDGMENT_SCHEMA_VERSION = "track-b-used-phone-automatic-judgment-v1"
CATEGORY_KEY = "46/133/185"
CATEGORY_LABEL = "二手/二手手机通讯/二手手机"
EXPECTED_EVIDENCE_PRODUCT_COUNT = 252
STATUS = "ai_only_pilot_pending_independent_blind_audit_not_gold"
GROUP_RULES: dict[str, GroupRule] = {rule.group_id: rule for rule in CATALOG[CATEGORY_KEY]}
ALLOWED_FACT_GROUPS = tuple(GROUP_RULES)
FORBIDDEN_RETRIEVAL_KEYS = {"retrievalSystem", "retrievalRank", "retrievalScore", "sourceRank", "rank", "score"}
BLIND_LEAKAGE_KEYS = {
    "status", "pass", "fail", "unknown", "conflict", "conflictReasons",
    "observedCanonicalValues", "suggestedGrade", "relevanceGrade", "eligible",
    "utility", "decisionUtility", "constraintViolation", "hardEvidenceUnknownOrConflict",
    "caseType", "expectedAction", "goldStatus", "actionContract", "comparisonContract",
    "substituteContract", "multiTurnContract", "expectedFinalState", "constraintContract",
    *FORBIDDEN_RETRIEVAL_KEYS,
}


def atom(atom_id: str, group: str, allowed: list[str], importance: str, *, operator: str = "IN") -> dict[str, Any]:
    return {
        "atomId": atom_id,
        "group": group,
        "operator": operator,
        "allowedValues": allowed,
        "importance": importance,
        "evidenceRule": "attr_primary_title_conflict_check",
        "missingTreatment": "unknown_not_recommendable",
    }


SCORING_CONTRACT = {
    "relevanceGrade": {
        "3": "all hard constraints pass and all soft targets pass",
        "2": "all hard constraints pass but at least one soft target fails, is missing, or conflicts",
        "1": "same-category semantic candidate explicitly violates at least one hard constraint; eligible=false and decisionUtility=0",
        "0": "irrelevant product or wrong category",
        "null": "hard evidence is missing/conflicting or the case is action/comparison-only; no forced grade",
    },
    "eligibility": "separate from relevanceGrade; true only when every hard atom passes",
    "constraintViolation": "explicit list of failed hard atoms; unknown/conflict is recorded separately",
    "decisionUtility": "1 only for eligible candidates; otherwise 0; comparison/action-only uses null",
}


CASE_DEFINITIONS: list[dict[str, Any]] = [
    {
        "caseId": "UP-C01-TRADEOFF-IOS",
        "caseType": "multi_objective_tradeoff",
        "queryText": "想找台 iOS 的二手手机。电池健康在 90% 以上最好，原装屏也很重要；两点不能都满足的话，把取舍说清楚。",
        "expectedAction": "RETRIEVE_FILTER_AND_EXPLAIN_TRADEOFF",
        "atoms": [
            atom("c01-os", "os", ["ios"], "hard"),
            atom("c01-battery", "battery_health", ["90_plus"], "soft"),
            atom("c01-screen", "screen_originality", ["original"], "soft"),
        ],
        "ordinaryProductJudgments": True,
    },
    {
        "caseId": "UP-C02-TRADEOFF-ANDROID",
        "caseType": "multi_objective_tradeoff",
        "queryText": "我想看 Android 二手手机，优先电池健康 90% 以上和主板没修过的；这两项有冲突就解释取舍，无划痕再加分。",
        "expectedAction": "RETRIEVE_FILTER_AND_EXPLAIN_TRADEOFF",
        "atoms": [
            atom("c02-os", "os", ["android"], "hard"),
            atom("c02-battery", "battery_health", ["90_plus"], "soft"),
            atom("c02-board", "motherboard_repair", ["not_repaired"], "soft"),
            atom("c02-scratch", "scratch_level", ["none"], "soft"),
        ],
        "ordinaryProductJudgments": True,
    },
    {
        "caseId": "UP-C03-NEGATION-REPAIR",
        "caseType": "negation_constraint",
        "queryText": "想要 iOS 的二手手机，主板修过的不要，原装屏优先。",
        "expectedAction": "RETRIEVE_FILTER_AND_RANK",
        "atoms": [
            atom("c03-os", "os", ["ios"], "hard"),
            atom("c03-board-not", "motherboard_repair", ["repaired"], "hard", operator="NOT_IN"),
            atom("c03-screen", "screen_originality", ["original"], "soft"),
        ],
        "ordinaryProductJudgments": True,
    },
    {
        "caseId": "UP-C04-CLARIFY-CONDITION",
        "caseType": "must_clarify",
        "queryText": "想找成色新一点的二手手机，别太旧就行。",
        "expectedAction": "CLARIFY",
        "atoms": [],
        "ordinaryProductJudgments": False,
        "actionContract": {
            "ambiguity": "成色新一点没有可执行阈值",
            "clarifyingQuestion": "你说的新一点，最低是 9 新、95 新还是 99 新？",
            "mustNotRetrieveBeforeClarification": True,
        },
    },
    {
        "caseId": "UP-C05-COMPARISON-EVIDENCE",
        "caseType": "product_comparison",
        "queryText": "候选 A 和候选 B 我该选哪个？我更看重电池健康、原装屏和原装电池，也在意有没有维修或划痕。请按证据逐项比较，缺的就说缺。",
        "expectedAction": "COMPARE_WITH_FIELD_EVIDENCE",
        "atoms": [],
        "ordinaryProductJudgments": False,
        "comparisonContract": {
            "dimensions": ["battery_health", "motherboard_repair", "screen_originality", "battery_originality", "scratch_level"],
            "mustCiteItemEvidence": True,
            "mustNotInventWinnerWhenPreferencesTie": True,
        },
    },
    {
        "caseId": "UP-C06-SUBSTITUTE-RETAIN",
        "caseType": "substitute_recommendation",
        "queryText": "刚才那台先不考虑了，换一台同样是 iOS、电池健康 90% 以上、主板没修过的；原装屏优先，其他条件别擅自改。",
        "expectedAction": "RETRIEVE_SUBSTITUTES_RETAINING_CONSTRAINTS",
        "atoms": [
            atom("c06-os", "os", ["ios"], "hard"),
            atom("c06-battery", "battery_health", ["90_plus"], "hard"),
            atom("c06-board-not", "motherboard_repair", ["repaired"], "hard", operator="NOT_IN"),
            atom("c06-screen", "screen_originality", ["original"], "soft"),
        ],
        "ordinaryProductJudgments": True,
        "substituteContract": {
            "excludeAnchorCandidate": True,
            "retainGroups": ["os", "battery_health", "motherboard_repair"],
            "softPreferenceGroups": ["screen_originality"],
            "mustNotRelaxWithoutUserPermission": True,
        },
    },
    {
        "caseId": "UP-C07-ABSTAIN-EVIDENCE",
        "caseType": "no_solution_or_evidence_insufficient",
        "queryText": "想找一台肯定没有暗病、未来两年也不会突然坏，而且打游戏一直流畅的二手手机。",
        "expectedAction": "ABSTAIN_OR_EXPLAIN",
        "atoms": [],
        "ordinaryProductJudgments": False,
        "actionContract": {
            "unsupportedNeeds": ["暗病", "未来可靠性", "游戏流畅度"],
            "explanation": "现有 evidence 没有这些可验证字段，不能用标题或无键名支持/不支持代替",
            "mustNotProduceProductQrel": True,
        },
    },
    {
        "caseId": "UP-C08-MULTITURN-UPDATE",
        "caseType": "same_session_constraint_update",
        "queryText": "先看 Android 二手手机，电池健康 90% 以上最好，原装屏优先。后来我改主意：系统换成 iOS，电池条件和原装屏偏好保留，再加上主板没修过。",
        "expectedAction": "UPDATE_STATE_THEN_RETRIEVE",
        "atoms": [
            atom("c08-final-os", "os", ["ios"], "hard"),
            atom("c08-final-board-not", "motherboard_repair", ["repaired"], "hard", operator="NOT_IN"),
            atom("c08-final-battery", "battery_health", ["90_plus"], "soft"),
            atom("c08-final-screen", "screen_originality", ["original"], "soft"),
        ],
        "ordinaryProductJudgments": True,
        "multiTurnContract": {
            "turns": [
                {"turn": 1, "role": "user", "text": "先看 Android 二手手机，电池健康 90% 以上最好，原装屏优先。"},
                {"turn": 2, "role": "user", "text": "系统换成 iOS，电池条件和原装屏偏好保留，再加上主板没修过。"},
            ],
            "initialState": {
                "hard": [{"group": "os", "operator": "IN", "allowedValues": ["android"]}],
                "soft": [
                    {"group": "battery_health", "operator": "IN", "allowedValues": ["90_plus"]},
                    {"group": "screen_originality", "operator": "IN", "allowedValues": ["original"]},
                ],
            },
            "turnDelta": {
                "supersede": [{"group": "os", "from": ["android"], "to": ["ios"]}],
                "retain": ["battery_health", "screen_originality"],
                "add": [{"group": "motherboard_repair", "operator": "NOT_IN", "allowedValues": ["repaired"], "importance": "hard"}],
            },
            "expectedFinalState": {
                "hard": [
                    {"group": "os", "operator": "IN", "allowedValues": ["ios"]},
                    {"group": "motherboard_repair", "operator": "NOT_IN", "allowedValues": ["repaired"]},
                ],
                "soft": [
                    {"group": "battery_health", "operator": "IN", "allowedValues": ["90_plus"]},
                    {"group": "screen_originality", "operator": "IN", "allowedValues": ["original"]},
                ],
            },
            "supersedeRule": "same-group new hard value replaces old value",
            "retainRule": "all groups listed in retain persist unchanged",
        },
    },
]


def fingerprint(item_id: str) -> str:
    return hashlib.sha256(f"{PILOT_ID}:{item_id}".encode("utf-8")).hexdigest()


def sealed_display_id(case_id: str, item_id: str) -> str:
    return f"{case_id}-B-{hashlib.sha256(f'{case_id}:{item_id}'.encode()).hexdigest()[:10]}"


def opaque_case_id(case_id: str) -> str:
    index = next(index for index, definition in enumerate(CASE_DEFINITIONS, 1) if definition["caseId"] == case_id)
    return f"BLIND-CASE-{index:03d}"


def opaque_candidate_id(case_id: str, original_candidate_id: str) -> str:
    digest = hashlib.sha256(f"{PILOT_ID}:blind-candidate:{original_candidate_id}".encode("utf-8")).hexdigest()[:12]
    return f"{opaque_case_id(case_id)}-CAND-{digest}"


def user_context(definition: dict[str, Any]) -> dict[str, Any]:
    if "multiTurnContract" in definition:
        return {
            "messages": [
                {"role": str(message["role"]), "text": str(message["text"])}
                for message in definition["multiTurnContract"]["turns"]
            ]
        }
    return {"messages": [{"role": "user", "text": definition["queryText"]}]}


def load_products(path: Path, expected_product_count: int = EXPECTED_EVIDENCE_PRODUCT_COUNT) -> list[dict[str, Any]]:
    products = []
    with path.open(encoding="utf-8") as stream:
        for audit_line, line in enumerate(stream, 1):
            if not line.strip():
                continue
            row = json.loads(line)
            if row.get("categoryKey") != CATEGORY_KEY:
                continue
            attrs = tuple(str(value) for value in row.get("normalizedAttrValues", []))
            observations = {
                group_id: observe(attrs, str(row.get("title", "")), rule)
                for group_id, rule in GROUP_RULES.items()
            }
            products.append({"auditLine": audit_line, "row": row, "attrs": attrs, "observations": observations})
    if len(products) != expected_product_count:
        raise ValueError(f"expected {expected_product_count} used-phone evidence products, got {len(products)}")
    return products


def atom_status(product: dict[str, Any], requirement: dict[str, Any]) -> str:
    values, conflict, _ = product["observations"][requirement["group"]]
    if conflict:
        return "conflict"
    if not values:
        return "unknown"
    allowed = set(requirement["allowedValues"])
    if requirement["operator"] == "IN":
        return "pass" if values & allowed else "fail"
    if requirement["operator"] == "NOT_IN":
        return "fail" if values & allowed else "pass"
    raise ValueError(requirement["operator"])


def raw_values_for_group(product: dict[str, Any], group_id: str) -> list[str]:
    rule = GROUP_RULES[group_id]
    aliases = tuple(alias for variants in rule.aliases.values() for alias in variants)
    return sorted({token for token in product["attrs"] if any(token_matches(token, alias) for alias in aliases)})


def atom_evidence(product: dict[str, Any], requirement: dict[str, Any], evidence_path: str) -> dict[str, Any]:
    values, conflict, conflict_reasons = product["observations"][requirement["group"]]
    return {
        "atomId": requirement["atomId"],
        "group": requirement["group"],
        "status": atom_status(product, requirement),
        "observedCanonicalValues": sorted(values),
        "observedRawValues": raw_values_for_group(product, requirement["group"]),
        "conflictReasons": conflict_reasons if conflict else [],
        "source": {"path": evidence_path, "lineNumber": product["auditLine"], "field": "normalizedAttrValues"},
    }


def automatic_judgment(case: dict[str, Any], product: dict[str, Any], evidence_path: str) -> dict[str, Any]:
    evidence = [atom_evidence(product, requirement, evidence_path) for requirement in case["atoms"]]
    hard = [item for item, requirement in zip(evidence, case["atoms"]) if requirement["importance"] == "hard"]
    soft = [item for item, requirement in zip(evidence, case["atoms"]) if requirement["importance"] == "soft"]
    hard_fail = [item["atomId"] for item in hard if item["status"] == "fail"]
    hard_unknown = [item["atomId"] for item in hard if item["status"] in {"unknown", "conflict"}]
    if hard_unknown:
        grade, eligible, utility, stratum = None, "unknown", 0, "hard_unknown_or_conflict"
    elif hard_fail:
        grade, eligible, utility, stratum = 1, False, 0, "hard_fail"
    elif all(item["status"] == "pass" for item in soft):
        grade, eligible, utility, stratum = 3, True, 1, "fully_satisfied"
    else:
        grade, eligible, utility, stratum = 2, True, 1, "hard_pass_soft_partial_or_unknown"
    return {
        "relevanceGrade": grade,
        "eligible": eligible,
        "decisionUtility": utility,
        "constraintViolation": hard_fail,
        "hardEvidenceUnknownOrConflict": hard_unknown,
        "requirementEvidence": evidence,
        "diagnosticStratum": stratum,
    }


def deterministic_take(case_id: str, products: Iterable[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    return sorted(products, key=lambda product: hashlib.sha256(f"{case_id}:{product['row']['itemId']}".encode()).hexdigest())[:limit]


def matches(product: dict[str, Any], conditions: list[tuple[str, str]]) -> bool:
    return all(value in product["observations"][group_id][0] and not product["observations"][group_id][1] for group_id, value in conditions)


def evidence_bundle(product: dict[str, Any], groups: Iterable[str], evidence_path: str) -> list[dict[str, Any]]:
    bundle = []
    for group_id in groups:
        values, conflict, reasons = product["observations"][group_id]
        bundle.append({
            "group": group_id,
            "observedCanonicalValues": sorted(values),
            "observedRawValues": raw_values_for_group(product, group_id),
            "status": "conflict" if conflict else "unknown" if not values else "observed",
            "conflictReasons": reasons,
            "source": {"path": evidence_path, "lineNumber": product["auditLine"], "field": "normalizedAttrValues"},
        })
    return bundle


def raw_evidence_refs(product: dict[str, Any], evidence_path: str) -> list[dict[str, Any]]:
    row = product["row"]
    refs = [{
        "source": {"path": evidence_path, "lineNumber": product["auditLine"]},
        "field": "normalizedAttrValues",
        "rawValue": row.get("normalizedAttrValues", []),
    }]
    for upstream in row.get("evidenceRefs", []):
        refs.append({
            "source": {"datasetPart": upstream.get("source"), "lineNumber": upstream.get("lineNumber")},
            "field": upstream.get("field"),
            "rawValue": upstream.get("rawValue"),
        })
    return refs


def blind_candidate(definition: dict[str, Any], product: dict[str, Any], evidence_path: str, role: str) -> dict[str, Any]:
    row = product["row"]
    case_id = definition["caseId"]
    original_candidate_id = sealed_display_id(case_id, str(row["itemId"]))
    role_labels = {
        "judgment_candidate": "候选商品",
        "candidate_A": "候选 A",
        "candidate_B": "候选 B",
        "excluded_anchor_evidence": "先前商品",
    }
    return {
        "schemaVersion": CANDIDATE_SCHEMA_VERSION,
        "pilotId": PILOT_ID,
        "reviewCaseId": opaque_case_id(case_id),
        "candidateDisplayId": opaque_candidate_id(case_id, original_candidate_id),
        "userVisibleRole": role_labels[role],
        "category": {"categoryKey": CATEGORY_KEY, "label": CATEGORY_LABEL},
        "userContext": user_context(definition),
        "candidate": {
            "title": str(row.get("title", "")),
            "brand": str(row.get("brand", "")),
            "seller": str(row.get("seller", "")),
        },
        "factGroupHints": list(ALLOWED_FACT_GROUPS),
        "evidenceRefs": raw_evidence_refs(product, evidence_path),
        "sourceTrace": {"itemFingerprintSha256": fingerprint(str(row["itemId"]))},
        "provenance": {"humanApproved": False, "aiOnly": True, "automaticSuggestedLabelVisible": False},
    }


def blind_case_record(definition: dict[str, Any], context_refs: list[dict[str, str]]) -> dict[str, Any]:
    return {
        "schemaVersion": BLIND_CASE_SCHEMA_VERSION,
        "pilotId": PILOT_ID,
        "reviewCaseId": opaque_case_id(definition["caseId"]),
        "category": {"categoryKey": CATEGORY_KEY, "label": CATEGORY_LABEL},
        "userContext": user_context(definition),
        "userVisibleCandidateContext": context_refs,
        "factGroupHints": list(ALLOWED_FACT_GROUPS),
        "reviewInstructions": "仅依据用户语言和原始 evidence 独立抽取约束、决定动作并填写 reviewer output；不要读取正式合同或封存自动建议。",
        "provenance": {"humanApproved": False, "aiOnly": True},
    }


def reviewer_output_template(case_id: str, candidate_id: str | None) -> dict[str, Any]:
    return {
        "schemaVersion": REVIEW_OUTPUT_SCHEMA_VERSION,
        "pilotId": PILOT_ID,
        "reviewCaseId": opaque_case_id(case_id),
        "candidateDisplayId": candidate_id,
        "independentlyExtractedConstraints": [],
        "expectedAction": None,
        "relevanceGrade": None,
        "eligible": None,
        "hardViolations": [],
        "hardUnknowns": [],
        "softAssessment": None,
        "evidenceCitations": [],
        "reason": None,
        "aiOnly": True,
        "humanApproved": False,
    }


def judgment_row(case: dict[str, Any], product: dict[str, Any], suggestion: dict[str, Any], candidate_id: str, role: str) -> dict[str, Any]:
    return {
        "schemaVersion": JUDGMENT_SCHEMA_VERSION,
        "pilotId": PILOT_ID,
        "caseId": case["caseId"],
        "candidateDisplayId": candidate_id,
        "candidateRole": role,
        "automaticSuggestion": suggestion,
        "goldStatus": "not_gold_pending_independent_ai_blind_audit",
        "blindAdjudication": {"status": "pending", "adjudicatorType": "independent_ai", "finalJudgment": None},
        "provenance": {
            "labelOrigin": "deterministic_evidence_rule_suggestion",
            "humanApproved": False,
            "usesTitleForEligibility": False,
            "usesPrice": False,
            "usesRetrievalRankOrScore": False,
        },
    }


def case_record(definition: dict[str, Any]) -> dict[str, Any]:
    record = {
        "schemaVersion": CASE_SCHEMA_VERSION,
        "pilotId": PILOT_ID,
        "pilotVersion": PILOT_VERSION,
        "caseId": definition["caseId"],
        "caseType": definition["caseType"],
        "category": {"categoryKey": CATEGORY_KEY, "label": CATEGORY_LABEL},
        "queryText": definition["queryText"],
        "queryOrigin": "synthetic_product_first_ai_draft",
        "expectedAction": definition["expectedAction"],
        "constraintContract": {"operator": "AND", "atoms": definition["atoms"], "allowedFactGroups": list(ALLOWED_FACT_GROUPS)},
        "evaluationContract": SCORING_CONTRACT,
        "ordinaryProductJudgments": definition["ordinaryProductJudgments"],
        "goldStatus": STATUS,
        "blindAudit": {"status": "pending", "adjudicatorType": "independent_ai", "humanApproved": False},
        "provenance": {"humanApproved": False, "aiOnly": True, "usesPrice": False, "usesLlmOrApiDuringBuild": False},
    }
    for key in ("actionContract", "comparisonContract", "substituteContract", "multiTurnContract"):
        if key in definition:
            record[key] = definition[key]
    return record


def assert_no_forbidden_keys(value: Any) -> None:
    if isinstance(value, dict):
        forbidden = set(value) & FORBIDDEN_RETRIEVAL_KEYS
        if forbidden:
            raise ValueError(f"forbidden retrieval fields: {sorted(forbidden)}")
        for child in value.values():
            assert_no_forbidden_keys(child)
    elif isinstance(value, list):
        for child in value:
            assert_no_forbidden_keys(child)


def assert_blind_no_leakage_keys(value: Any) -> None:
    if isinstance(value, dict):
        forbidden = set(value) & BLIND_LEAKAGE_KEYS
        if forbidden:
            raise ValueError(f"blind artifact leaks answer fields: {sorted(forbidden)}")
        for child in value.values():
            assert_blind_no_leakage_keys(child)
    elif isinstance(value, list):
        for child in value:
            assert_blind_no_leakage_keys(child)


def assert_blind_files_have_opaque_ids_only(output_dir: Path, file_names: Iterable[str]) -> None:
    original_case_ids = {definition["caseId"] for definition in CASE_DEFINITIONS}
    semantic_labels = {"clarify", "abstain", "multiturn", "tradeoff", "negation", "comparison", "substitute"}
    for name in file_names:
        text = (output_dir / name).read_text(encoding="utf-8")
        lowered = text.lower()
        leaked_case_ids = sorted(case_id for case_id in original_case_ids if case_id in text)
        leaked_labels = sorted(label for label in semantic_labels if label in lowered)
        if leaked_case_ids or leaked_labels:
            raise ValueError(f"blind artifact {name} leaks semantic IDs/labels: {leaked_case_ids + leaked_labels}")


def build(
    evidence_path: Path,
    output_dir: Path,
    schema_dir: Path,
    *,
    expected_product_count: int = EXPECTED_EVIDENCE_PRODUCT_COUNT,
) -> dict[str, Any]:
    products = load_products(evidence_path, expected_product_count)
    evidence_repo_path = stable_repository_relative_path(evidence_path)
    cases = [case_record(definition) for definition in CASE_DEFINITIONS]
    case_map = {case["caseId"]: case for case in cases}
    definition_map = {case["caseId"]: case for case in CASE_DEFINITIONS}
    blind_rows: list[dict[str, Any]] = []
    judgment_rows: list[dict[str, Any]] = []
    diagnostic_counts: dict[str, dict[str, int]] = {}

    def add_candidate(definition: dict[str, Any], product: dict[str, Any], role: str, suggestion: dict[str, Any]) -> dict[str, Any]:
        blind = blind_candidate(definition, product, evidence_repo_path, role)
        blind_rows.append(blind)
        original_candidate_id = sealed_display_id(definition["caseId"], str(product["row"]["itemId"]))
        judgment_rows.append(judgment_row(definition, product, suggestion, original_candidate_id, role))
        return blind

    comparison_case = definition_map["UP-C05-COMPARISON-EVIDENCE"]
    comparison_patterns = {
        "candidate_A": [("os", "ios"), ("battery_health", "90_plus"), ("motherboard_repair", "repaired"), ("screen_originality", "non_original"), ("battery_originality", "non_original"), ("scratch_level", "light")],
        "candidate_B": [("os", "ios"), ("battery_health", "80_90"), ("motherboard_repair", "not_repaired"), ("screen_originality", "original"), ("battery_originality", "original"), ("scratch_level", "none")],
    }
    comparison_ids = {}
    comparison_blind_ids = {}
    for role, conditions in comparison_patterns.items():
        candidates = deterministic_take(comparison_case["caseId"], (p for p in products if matches(p, conditions)), 1)
        if len(candidates) != 1:
            raise ValueError(f"comparison evidence pattern {role} is unsupported")
        product = candidates[0]
        evidence = evidence_bundle(product, comparison_case["comparisonContract"]["dimensions"], evidence_repo_path)
        suggestion = {"relevanceGrade": None, "eligible": None, "decisionUtility": None, "constraintViolation": [], "hardEvidenceUnknownOrConflict": [], "requirementEvidence": evidence, "diagnosticStratum": "comparison_evidence_only"}
        blind = add_candidate(comparison_case, product, role, suggestion)
        comparison_ids[role] = sealed_display_id(comparison_case["caseId"], str(product["row"]["itemId"]))
        comparison_blind_ids[role] = blind["candidateDisplayId"]
    case_map[comparison_case["caseId"]]["comparisonContract"]["candidateDisplayIds"] = comparison_ids
    diagnostic_counts[comparison_case["caseId"]] = {"comparison_evidence_only": 2}

    substitute_case = definition_map["UP-C06-SUBSTITUTE-RETAIN"]
    anchor_conditions = [("os", "ios"), ("battery_health", "90_plus"), ("motherboard_repair", "not_repaired"), ("screen_originality", "original")]
    anchors = deterministic_take(substitute_case["caseId"] + ":anchor", (p for p in products if matches(p, anchor_conditions)), 1)
    if len(anchors) != 1:
        raise ValueError("substitute anchor evidence is unsupported")
    anchor = anchors[0]
    anchor_evidence = evidence_bundle(anchor, ["os", "battery_health", "motherboard_repair", "screen_originality"], evidence_repo_path)
    anchor_suggestion = {"relevanceGrade": None, "eligible": None, "decisionUtility": None, "constraintViolation": [], "hardEvidenceUnknownOrConflict": [], "requirementEvidence": anchor_evidence, "diagnosticStratum": "excluded_anchor_evidence"}
    anchor_blind = add_candidate(substitute_case, anchor, "excluded_anchor_evidence", anchor_suggestion)
    case_map[substitute_case["caseId"]]["substituteContract"]["excludedAnchorCandidateDisplayId"] = sealed_display_id(substitute_case["caseId"], str(anchor["row"]["itemId"]))

    ordinary_case_ids = [case["caseId"] for case in CASE_DEFINITIONS if case["ordinaryProductJudgments"]]
    for case_id in ordinary_case_ids:
        definition = definition_map[case_id]
        strata: dict[str, list[tuple[dict[str, Any], dict[str, Any]]]] = defaultdict(list)
        for product in products:
            if case_id == substitute_case["caseId"] and product is anchor:
                continue
            suggestion = automatic_judgment(definition, product, evidence_repo_path)
            strata[suggestion["diagnosticStratum"]].append((product, suggestion))
        required_strata = ("fully_satisfied", "hard_pass_soft_partial_or_unknown", "hard_fail", "hard_unknown_or_conflict")
        if any(len(strata[name]) < 2 for name in required_strata):
            raise ValueError(f"{case_id} lacks balanced candidate strata: { {name: len(strata[name]) for name in required_strata} }")
        diagnostic_counts[case_id] = {name: len(strata[name]) for name in required_strata}
        selected: list[tuple[dict[str, Any], dict[str, Any]]] = []
        for name in required_strata:
            chosen_products = deterministic_take(case_id + ":" + name, (pair[0] for pair in strata[name]), 4)
            suggestion_by_item = {str(pair[0]["row"]["itemId"]): pair[1] for pair in strata[name]}
            selected.extend((product, suggestion_by_item[str(product["row"]["itemId"])]) for product in chosen_products)
        for product, suggestion in selected:
            evidence = suggestion["requirementEvidence"]
            add_candidate(definition, product, "judgment_candidate", suggestion)

    for action_case in ("UP-C04-CLARIFY-CONDITION", "UP-C07-ABSTAIN-EVIDENCE"):
        diagnostic_counts[action_case] = {"productCandidateCount": 0}

    type_counts = Counter(case["caseType"] for case in cases)
    expected_distribution = {
        "multi_objective_tradeoff": 2,
        "negation_constraint": 1,
        "must_clarify": 1,
        "product_comparison": 1,
        "substitute_recommendation": 1,
        "no_solution_or_evidence_insufficient": 1,
        "same_session_constraint_update": 1,
    }
    if dict(type_counts) != expected_distribution:
        raise ValueError(f"case distribution drift: {dict(type_counts)}")

    all_titles = {str(product["row"].get("title", "")) for product in products}
    leakage_violations = []
    for case in cases:
        text = case["queryText"]
        if re.search(r"\b\d{5,}\b", text):
            leakage_violations.append({"caseId": case["caseId"], "type": "item_id_like_number"})
        if any(title and title in text for title in all_titles):
            leakage_violations.append({"caseId": case["caseId"], "type": "copied_full_title"})
        if "价格" in text or "预算" in text:
            leakage_violations.append({"caseId": case["caseId"], "type": "price_contract_forbidden"})
    for value in (cases, judgment_rows):
        assert_no_forbidden_keys(value)
    if leakage_violations:
        raise ValueError(f"query leakage: {leakage_violations}")

    output_dir.mkdir(parents=True, exist_ok=True)
    cases = [case_map[definition["caseId"]] for definition in CASE_DEFINITIONS]
    blind_cases = []
    for definition in CASE_DEFINITIONS:
        context_refs: list[dict[str, str]] = []
        if definition["caseId"] == comparison_case["caseId"]:
            context_refs = [
                {"label": "候选 A", "candidateDisplayId": comparison_blind_ids["candidate_A"]},
                {"label": "候选 B", "candidateDisplayId": comparison_blind_ids["candidate_B"]},
            ]
        elif definition["caseId"] == substitute_case["caseId"]:
            context_refs = [{"label": "先前商品", "candidateDisplayId": anchor_blind["candidateDisplayId"]}]
        blind_cases.append(blind_case_record(definition, context_refs))

    blind_rows.sort(key=lambda row: (row["reviewCaseId"], row["candidateDisplayId"]))
    judgment_rows.sort(key=lambda row: (row["caseId"], row["candidateDisplayId"]))
    reviewer_templates: list[dict[str, Any]] = []
    ordinary_ids = {opaque_case_id(definition["caseId"]) for definition in CASE_DEFINITIONS if definition["ordinaryProductJudgments"]}
    for row in blind_rows:
        if row["reviewCaseId"] in ordinary_ids and row["userVisibleRole"] == "候选商品":
            original_case_id = next(case_id for case_id in definition_map if opaque_case_id(case_id) == row["reviewCaseId"])
            reviewer_templates.append(reviewer_output_template(original_case_id, row["candidateDisplayId"]))
    for case_id in ("UP-C04-CLARIFY-CONDITION", "UP-C05-COMPARISON-EVIDENCE", "UP-C07-ABSTAIN-EVIDENCE"):
        reviewer_templates.append(reviewer_output_template(case_id, None))
    reviewer_templates.sort(key=lambda row: (row["reviewCaseId"], row["candidateDisplayId"] or ""))

    case_forward = {definition["caseId"]: opaque_case_id(definition["caseId"]) for definition in CASE_DEFINITIONS}
    case_reverse = {opaque: original for original, opaque in case_forward.items()}
    candidate_forward: dict[str, str] = {}
    for blind, judgment in zip(
        sorted(blind_rows, key=lambda row: (row["reviewCaseId"], row["candidateDisplayId"])),
        sorted(judgment_rows, key=lambda row: (opaque_case_id(row["caseId"]), opaque_candidate_id(row["caseId"], row["candidateDisplayId"]))),
    ):
        expected_opaque = opaque_candidate_id(judgment["caseId"], judgment["candidateDisplayId"])
        if blind["reviewCaseId"] != opaque_case_id(judgment["caseId"]) or blind["candidateDisplayId"] != expected_opaque:
            raise ValueError("blind/sealed candidate ordering drift prevents safe ID mapping")
        candidate_forward[judgment["candidateDisplayId"]] = blind["candidateDisplayId"]
    candidate_reverse = {opaque: original for original, opaque in candidate_forward.items()}
    if len(case_forward) != len(case_reverse) or len(candidate_forward) != len(candidate_reverse) or len(candidate_forward) != len(blind_rows):
        raise ValueError("opaque ID mapping is not bidirectionally unique")
    sealed_id_mapping = {
        "schemaVersion": "track-b-used-phone-blind-id-mapping-sealed-v1",
        "pilotId": PILOT_ID,
        "status": "sealed_not_gold_for_post_blind_audit_merge_only",
        "caseIdToReviewCaseId": case_forward,
        "reviewCaseIdToCaseId": case_reverse,
        "candidateDisplayIdToBlindCandidateDisplayId": candidate_forward,
        "blindCandidateDisplayIdToCandidateDisplayId": candidate_reverse,
        "provenance": {"humanApproved": False, "aiOnly": True},
    }

    assert_blind_no_leakage_keys(blind_cases)
    assert_blind_no_leakage_keys(blind_rows)
    write_jsonl(output_dir / "used_phone_agent_cases_pending.jsonl", cases)
    write_jsonl(output_dir / "blind_cases_pending.jsonl", blind_cases)
    write_jsonl(output_dir / "blind_candidates_pending.jsonl", blind_rows)
    write_jsonl(output_dir / "blind_reviewer_output_templates_pending.jsonl", reviewer_templates)
    write_jsonl(output_dir / "automatic_judgment_suggestions_sealed_not_gold.jsonl", judgment_rows)
    write_json(output_dir / "blind_id_mapping_sealed_not_gold.json", sealed_id_mapping)

    audit = {
        "pilotId": PILOT_ID,
        "pilotVersion": PILOT_VERSION,
        "status": STATUS,
        "category": {"categoryKey": CATEGORY_KEY, "label": CATEGORY_LABEL, "evidenceProductCount": len(products)},
        "caseDistribution": expected_distribution,
        "caseDiagnostics": diagnostic_counts,
        "candidateCounts": {"blindCases": len(blind_cases), "blindCandidates": len(blind_rows), "reviewerOutputTemplates": len(reviewer_templates), "sealedAutomaticSuggestions": len(judgment_rows)},
        "queryLeakageAudit": {"passed": True, "violations": [], "checks": ["no_item_id", "no_copied_full_title", "no_price_contract"]},
        "blindnessAudit": {
            "passed": True,
            "blindCasesContainAnswerContract": False,
            "blindCandidatesContainMappedFactsOrSuggestedLabels": False,
            "blindIdentifiersAreOpaque": True,
            "sealedIdMappingExcludedFromBlindEnvelope": True,
            "forbiddenBlindKeys": sorted(BLIND_LEAKAGE_KEYS),
        },
        "semanticBoundaries": {
            "allowedFactGroups": list(ALLOWED_FACT_GROUPS),
            "forbiddenNeeds": ["真实续航", "FPS/流畅度", "暗病", "未来可靠性", "无键名支持/不支持/是/否对应功能", "价格"],
        },
        "provenance": {"humanApproved": False, "aiOnly": True, "pendingIndependentAiBlindAudit": True, "formalGold": False, "formalQrel": False},
    }
    write_json(output_dir / "pilot_construction_audit.json", audit)
    for schema_name in (
        "track_b_used_phone_agent_case_v1.schema.json",
        "track_b_used_phone_blind_case_v2.schema.json",
        "track_b_used_phone_blind_candidate_v3.schema.json",
        "track_b_used_phone_blind_reviewer_output_v2.schema.json",
        "track_b_used_phone_automatic_judgment_v1.schema.json",
    ):
        write_utf8_lf(output_dir / schema_name, (schema_dir / schema_name).read_text(encoding="utf-8"))
    for legacy_name in (
        "track_b_used_phone_blind_case_v1.schema.json",
        "track_b_used_phone_blind_candidate_v1.schema.json",
        "track_b_used_phone_blind_candidate_v2.schema.json",
        "track_b_used_phone_blind_reviewer_output_v1.schema.json",
    ):
        legacy_schema = output_dir / legacy_name
        if legacy_schema.exists():
            legacy_schema.unlink()

    blind_readme = """# Independent AI blind review envelope\n\nOnly use the files named in `blind_bundle_manifest.json`. Do not read the builder, formal case contract, construction audit, or sealed automatic suggestions before submitting reviewer outputs.\n\nExtract constraints and expected action independently from `blind_cases_pending.jsonl`; use only raw product evidence in `blind_candidates_pending.jsonl`; write answers using the reviewer output schema/templates.\n"""
    write_utf8_lf(output_dir / "BLIND_REVIEW_README.md", blind_readme)
    blind_bundle_files = [
        "BLIND_REVIEW_README.md",
        "blind_cases_pending.jsonl",
        "blind_candidates_pending.jsonl",
        "blind_reviewer_output_templates_pending.jsonl",
        "track_b_used_phone_blind_case_v2.schema.json",
        "track_b_used_phone_blind_candidate_v3.schema.json",
        "track_b_used_phone_blind_reviewer_output_v2.schema.json",
    ]
    assert_blind_files_have_opaque_ids_only(output_dir, blind_bundle_files)
    blind_bundle_manifest = {
        "pilotId": PILOT_ID,
        "envelope": "independent_ai_blind_review_inputs_only",
        "humanApproved": False,
        "aiOnly": True,
        "files": [{"path": name, "bytes": (output_dir / name).stat().st_size, "sha256": sha256_file(output_dir / name)} for name in blind_bundle_files],
        "excludedFromBlindEnvelope": ["used_phone_agent_cases_pending.jsonl", "automatic_judgment_suggestions_sealed_not_gold.jsonl", "blind_id_mapping_sealed_not_gold.json", "pilot_construction_audit.json"],
    }
    write_json(output_dir / "blind_bundle_manifest.json", blind_bundle_manifest)

    readme = f"""# {PILOT_ID}\n\nStatus: **AI-only pilot pending independent blind audit**. This is not final gold and not formal qrel.\n\nBlind reviewers must receive only the files listed in `blind_bundle_manifest.json`. The formal case contract and `automatic_judgment_suggestions_sealed_not_gold.jsonl` must remain sealed until independent adjudication completes.\n\nRebuild offline from `agent/`:\n\n```powershell\npython -m evaluation.build_track_b_used_phone_agent_pilot --evidence ../data/processed/ecommerce/kuaisearch_synthetic_evidence_track_b_v01/{'09807c773ce67360ed8df30842e372182fcf7ad9'}/evidence_products_audit.jsonl --output ../data/processed/ecommerce/kuaisearch_synthetic_evidence_track_b_v01/{'09807c773ce67360ed8df30842e372182fcf7ad9'}/used_phone_agent_pilot_v1\npython -m unittest -v tests.test_track_b_used_phone_agent_pilot\n```\n"""
    write_utf8_lf(output_dir / "README.md", readme)
    report_lines = [
        "# Used-phone complex Agent pilot construction audit", "",
        "> AI-only pilot pending independent blind audit；不是 final gold，也不是正式 qrel。", "",
        f"- 二手手机 evidence products：{len(products)}。",
        f"- Case：{len(cases)}；盲审 case envelope：{len(blind_cases)}；盲审候选：{len(blind_rows)}；reviewer templates：{len(reviewer_templates)}；封存自动建议：{len(judgment_rows)}。",
        "- CLARIFY 与 ABSTAIN_OR_EXPLAIN case 的商品候选数均为 0。",
        "- 比较题仅固定两件真实 evidence 商品并逐字段比较，不生成普通商品 qrel。",
        "", "| Case | 类型 | Expected action | 商品判断 | 全量证据分层 |", "|---|---|---|---|---|",
    ]
    for case in cases:
        diag = diagnostic_counts[case["caseId"]]
        report_lines.append(f"| {case['caseId']} | {case['caseType']} | {case['expectedAction']} | {'是' if case['ordinaryProductJudgments'] else '否'} | {canonical_json(diag)} |")
    report_lines += ["", "## 边界", "", "- 只使用电池健康区间、主板维修、屏幕/电池原装、外壳/划痕、成色、地区版本与 OS。", "- 不使用价格，不把标题或无键名支持/不支持映射为事实。", "- 真实续航、FPS/流畅度、暗病和未来可靠性必须拒答或解释证据不足。", ""]
    write_utf8_lf(output_dir / "pilot_construction_report.md", "\n".join(report_lines))

    artifact_names = sorted(path.name for path in output_dir.iterdir() if path.name != "manifest.json")
    manifest = {
        "pilotId": PILOT_ID,
        "pilotVersion": PILOT_VERSION,
        "status": STATUS,
        "sourceEvidence": {"path": evidence_repo_path, "bytes": evidence_path.stat().st_size, "sha256": sha256_file(evidence_path), "categoryEvidenceProductCount": len(products)},
        "counts": {"cases": len(cases), "blindCases": len(blind_cases), "blindCandidates": len(blind_rows), "reviewerOutputTemplates": len(reviewer_templates), "sealedAutomaticSuggestions": len(judgment_rows), "sealedCaseIdMappings": len(case_forward), "sealedCandidateIdMappings": len(candidate_forward)},
        "caseDistribution": expected_distribution,
        "provenance": {"humanApproved": False, "aiOnly": True, "pendingIndependentAiBlindAudit": True, "formalGold": False, "formalQrel": False},
        "networkUsed": False, "llmOrApiUsedDuringBuild": False, "formalAgentMetricsRun": False,
        "artifacts": [{"path": name, "bytes": (output_dir / name).stat().st_size, "sha256": sha256_file(output_dir / name)} for name in artifact_names],
    }
    write_json(output_dir / "manifest.json", manifest)
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--schema-dir", type=Path, default=Path(__file__).resolve().parent / "schemas")
    args = parser.parse_args()
    manifest = build(args.evidence.resolve(), args.output.resolve(), args.schema_dir.resolve())
    print(canonical_json({"counts": manifest["counts"], "status": manifest["status"]}))


if __name__ == "__main__":
    main()
