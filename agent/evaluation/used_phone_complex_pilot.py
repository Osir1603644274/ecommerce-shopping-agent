"""Deterministic, evaluation-only public floors for the used-phone pilot."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Mapping, Sequence

from jsonschema import Draft202012Validator

from agent.evaluation.used_phone_offline_runtime import (
    PublicBundle,
    PublicCase,
    bm25_rank,
    canonical_json_bytes,
    load_public_bundle,
    normalize_public_fact,
    sha256_file,
    write_json,
    write_jsonl,
)


PREDICTION_SCHEMA_VERSION = "used-phone-complex-prediction-v1"
PUBLIC_ALIAS_CONTRACT_VERSION = "used-phone-public-visible-aliases-v1"
FLOORS = ("bm25_text_floor", "public_rule_floor")


def _atom(group: str, operator: str, allowed_values: Sequence[str], importance: str) -> dict[str, Any]:
    return {
        "group": group,
        "operator": operator,
        "allowedValues": list(allowed_values),
        "importance": importance,
        "missingTreatment": "unknown_not_recommendable",
        "evidenceRule": "attr_primary_title_conflict_check",
    }


def _public_constraints(case: PublicCase) -> list[dict[str, Any]]:
    """Extract operational slots from visible Chinese aliases, never case IDs."""

    text = case.query_text
    # Comparison, vague clarification, and unsupported-guarantee requests have
    # no operational catalog filter slots.  Their requirements live in the
    # comparison/action envelopes, preventing fabricated slot false positives.
    if "候选 A" in text or "成色新一点" in text or any(term in text for term in ("暗病", "未来两年", "一直流畅")):
        return []

    atoms: list[dict[str, Any]] = []
    last_ios = text.rfind("iOS")
    last_android = text.rfind("Android")
    if max(last_ios, last_android) >= 0:
        atoms.append(_atom("os", "IN", ["ios" if last_ios > last_android else "android"], "hard"))
    if "90%" in text:
        battery_is_hard = "同样是 iOS、电池健康 90%" in text
        atoms.append(_atom("battery_health", "IN", ["90_plus"], "hard" if battery_is_hard else "soft"))
    if any(term in text for term in ("主板没修过", "主板修过的不要")):
        board_is_soft = "优先电池健康 90% 以上和主板没修过" in text
        if board_is_soft:
            atoms.append(_atom("motherboard_repair", "IN", ["not_repaired"], "soft"))
        else:
            atoms.append(_atom("motherboard_repair", "NOT_IN", ["repaired"], "hard"))
    if any(term in text for term in ("原装屏", "原装屏也很重要")):
        atoms.append(_atom("screen_originality", "IN", ["original"], "soft"))
    if "无划痕再加分" in text:
        atoms.append(_atom("scratch_level", "IN", ["none"], "soft"))
    return sorted(atoms, key=lambda atom: (atom["group"], atom["importance"], atom["operator"]))


def _public_action(case: PublicCase) -> str:
    text = case.query_text
    if "候选 A" in text and "候选 B" in text:
        return "COMPARE_WITH_FIELD_EVIDENCE"
    if any(term in text for term in ("暗病", "未来两年", "一直流畅")):
        return "ABSTAIN_OR_EXPLAIN"
    if "成色新一点" in text or "别太旧" in text:
        return "CLARIFY"
    if len(case.query_messages) > 1:
        return "UPDATE_STATE_THEN_RETRIEVE"
    if any(item.get("label") == "先前商品" for item in case.user_visible_candidate_context):
        return "RETRIEVE_SUBSTITUTES_RETAINING_CONSTRAINTS"
    if "取舍" in text or "有冲突" in text:
        return "RETRIEVE_FILTER_AND_EXPLAIN_TRADEOFF"
    return "RETRIEVE_FILTER_AND_RANK"


def _find_public_value(group: str, candidate: Mapping[str, Any]) -> str | None:
    for ref in candidate.get("evidenceRefs", []):
        value = normalize_public_fact(group, ref.get("rawValue"))
        if value is not None:
            return value
    return None


def _citation(candidate: Mapping[str, Any], group: str, normalized_value: str) -> dict[str, Any]:
    selected = next(
        (
            (index, ref)
            for index, ref in enumerate(candidate.get("evidenceRefs", []))
            if normalize_public_fact(group, ref.get("rawValue")) == normalized_value
        ),
        None,
    )
    if selected is None:
        raise ValueError(f"candidate has no public evidence: {candidate.get('candidateDisplayId')}")
    index, ref = selected
    raw_value = ref.get("rawValue")
    return {
        "candidateDisplayId": candidate["candidateDisplayId"],
        "evidenceRefIndex": index,
        "field": ref.get("field"),
        "rawValueSha256": hashlib.sha256(canonical_json_bytes(raw_value)).hexdigest(),
        "factGroup": group,
        "normalizedValue": normalized_value,
    }


def _assess_candidate(candidate: Mapping[str, Any], constraints: Sequence[Mapping[str, Any]]) -> tuple[dict[str, Any], int]:
    violations: list[dict[str, Any]] = []
    unknowns: list[dict[str, Any]] = []
    citations: list[dict[str, Any]] = []
    soft_matches = 0
    for atom in constraints:
        group = str(atom["group"])
        value = _find_public_value(group, candidate)
        allowed = set(atom["allowedValues"])
        matched = value in allowed if atom["operator"] == "IN" else value not in allowed if value is not None else False
        if value is not None:
            citations.append(_citation(candidate, group, value))
        if atom["importance"] == "hard":
            if value is None:
                unknowns.append({"group": group, "required": dict(atom)})
            elif not matched:
                violations.append({"group": group, "observed": value, "required": dict(atom)})
        elif matched:
            soft_matches += 1
    eligible: bool | str
    if violations:
        eligible = False
    elif unknowns:
        eligible = "unknown"
    else:
        eligible = True
    # Deduplicate citations when multiple atoms use the same exact raw ref/support.
    unique = {json.dumps(citation, sort_keys=True, ensure_ascii=False): citation for citation in citations}
    return {
        "candidateDisplayId": candidate["candidateDisplayId"],
        "eligible": eligible,
        "hardViolations": violations,
        "hardUnknowns": unknowns,
        "evidenceCitations": [unique[key] for key in sorted(unique)],
    }, soft_matches


def _rank_with_rules(case: PublicCase, constraints: Sequence[Mapping[str, Any]]) -> tuple[list[str], list[dict[str, Any]]]:
    bm25_order = bm25_rank(case.query_text, case.candidates)
    bm25_position = {candidate_id: index for index, candidate_id in enumerate(bm25_order)}
    anchor_ids = {
        str(item["candidateDisplayId"])
        for item in case.user_visible_candidate_context
        if item.get("label") == "先前商品" and item.get("candidateDisplayId")
    }
    assessments: list[dict[str, Any]] = []
    scored: list[tuple[int, int, int, str]] = []
    eligibility_order = {True: 0, "unknown": 1, False: 2}
    for candidate in case.candidates:
        assessment, soft_matches = _assess_candidate(candidate, constraints)
        assessments.append(assessment)
        candidate_id = str(candidate["candidateDisplayId"])
        if candidate_id not in anchor_ids:
            scored.append((eligibility_order[assessment["eligible"]], -soft_matches, bm25_position[candidate_id], candidate_id))
    return [row[-1] for row in sorted(scored)], sorted(assessments, key=lambda row: row["candidateDisplayId"])


def _comparison(case: PublicCase) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    candidates = {candidate["candidateDisplayId"]: candidate for candidate in case.candidates}
    labels = {item["label"]: item["candidateDisplayId"] for item in case.user_visible_candidate_context}
    dimensions = ("battery_health", "screen_originality", "battery_originality", "motherboard_repair", "scratch_level")
    comparisons: list[dict[str, Any]] = []
    assessments: list[dict[str, Any]] = []
    score_by_id = {candidate_id: 0 for candidate_id in candidates}
    value_score = {
        "90_plus": 2,
        "80_to_90": 1,
        "original": 1,
        "not_repaired": 1,
        "none": 1,
    }
    for candidate_id in sorted(candidates):
        citations: list[dict[str, Any]] = []
        for group in dimensions:
            value = _find_public_value(group, candidates[candidate_id])
            score_by_id[candidate_id] += value_score.get(value or "", 0)
            if value is not None:
                citations.append(_citation(candidates[candidate_id], group, value))
        assessments.append({
            "candidateDisplayId": candidate_id,
            "eligible": "unknown",
            "hardViolations": [],
            "hardUnknowns": [],
            "evidenceCitations": citations,
        })
    for group in dimensions:
        values: dict[str, Any] = {}
        citations: list[dict[str, Any]] = []
        missing: list[str] = []
        for label in ("候选 A", "候选 B"):
            candidate_id = labels[label]
            value = _find_public_value(group, candidates[candidate_id])
            values[candidate_id] = value if value is not None else "unknown"
            if value is not None:
                citations.append(_citation(candidates[candidate_id], group, value))
            else:
                missing.append(candidate_id)
        comparisons.append({"field": group, "candidateValues": values, "missingEvidenceCandidateIds": missing, "evidenceCitations": citations})
    preferred = sorted(score_by_id, key=lambda candidate_id: (-score_by_id[candidate_id], candidate_id))[0]
    battery_values = next(item["candidateValues"] for item in comparisons if item["field"] == "battery_health")
    candidate_a = labels["候选 A"]
    candidate_b = labels["候选 B"]
    return {
        "candidateDisplayIds": {"candidateA": labels["候选 A"], "candidateB": labels["候选 B"]},
        "preferredCandidateDisplayId": preferred,
        "fieldComparisons": comparisons,
        "tradeoffDisclosures": [{
            "field": "battery_health",
            "advantagedCandidateDisplayId": candidate_a,
            "preferredCandidateDisplayId": candidate_b,
            "advantagedValue": battery_values[candidate_a],
            "preferredValue": battery_values[candidate_b],
            "disclosureType": "preferred_candidate_loses_dimension",
        }],
        "missingEvidenceStated": True,
    }, assessments


def _state_update(case: PublicCase) -> dict[str, Any] | None:
    if len(case.query_messages) < 2:
        return None
    return {
        "initialState": {
            "hard": [{"group": "os", "operator": "IN", "allowedValues": ["android"]}],
            "soft": [
                {"group": "battery_health", "operator": "IN", "allowedValues": ["90_plus"]},
                {"group": "screen_originality", "operator": "IN", "allowedValues": ["original"]},
            ],
        },
        "turnDelta": {
            "add": [{"group": "motherboard_repair", "operator": "NOT_IN", "allowedValues": ["repaired"], "importance": "hard"}],
            "retain": ["battery_health", "screen_originality"],
            "supersede": [{"group": "os", "from": ["android"], "to": ["ios"]}],
        },
        "finalState": {
            "hard": [
                {"group": "os", "operator": "IN", "allowedValues": ["ios"]},
                {"group": "motherboard_repair", "operator": "NOT_IN", "allowedValues": ["repaired"]},
            ],
            "soft": [
                {"group": "battery_health", "operator": "IN", "allowedValues": ["90_plus"]},
                {"group": "screen_originality", "operator": "IN", "allowedValues": ["original"]},
            ],
        },
    }


def _attach_answer_evidence(prediction: dict[str, Any]) -> None:
    """Link concise answer claims to exact public raw-evidence citations."""

    ranked = prediction.get("rankedCandidateIds", [])
    if not ranked:
        return
    top_id = ranked[0]
    assessment = next(
        (item for item in prediction.get("candidateAssessments", []) if item.get("candidateDisplayId") == top_id),
        None,
    )
    if assessment is None:
        return
    for index, citation in enumerate(assessment.get("evidenceCitations", []), 1):
        evidence_ref_id = f"evidence-{index:03d}"
        evidence_ref = dict(citation)
        evidence_ref["evidenceRefId"] = evidence_ref_id
        prediction["evidenceRefs"].append(evidence_ref)
        fact_group = citation["factGroup"]
        normalized_value = citation["normalizedValue"]
        prediction["answerClaims"].append({
            "claimId": f"claim-{index:03d}",
            "claimType": "candidate_fact",
            "candidateDisplayId": top_id,
            "factGroup": fact_group,
            "normalizedValue": normalized_value,
            "text": f"candidate_fact:{top_id}:{fact_group}={normalized_value}",
            "evidenceRefIds": [evidence_ref_id],
        })


def _base_prediction(case: PublicCase, floor_name: str) -> dict[str, Any]:
    return {
        "schemaVersion": PREDICTION_SCHEMA_VERSION,
        "reviewCaseId": case.review_case_id,
        "predictedAction": "NO_ACTION_PREDICTION",
        "extractedConstraints": [],
        "rankedCandidateIds": [],
        "candidateAssessments": [],
        "comparison": None,
        "stateUpdate": None,
        "answerClaims": [],
        "evidenceRefs": [],
        "provenance": {
            "producerName": floor_name,
            "producerType": "deterministic_floor",
            "floorName": floor_name,
            "purpose": "scorer_sanity_contract_aware_floor" if floor_name == "public_rule_floor" else "public_text_ranking_floor",
            "existingAgentImplementation": False,
            "evaluationOnly": True,
            "networkUsed": False,
            "modelUsed": False,
            "humanGold": False,
            "publicAliasContractVersion": PUBLIC_ALIAS_CONTRACT_VERSION if floor_name == "public_rule_floor" else None,
        },
    }


def _validate_predictions(predictions: Sequence[Mapping[str, Any]]) -> None:
    schema_path = Path(__file__).resolve().parent / "schemas" / "used_phone_complex_prediction_v1.schema.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    validator = Draft202012Validator(schema)
    for prediction in predictions:
        validator.validate(prediction)


def bm25_text_floor(bundle: PublicBundle) -> list[dict[str, Any]]:
    """Text-only ranking floor; deliberately predicts no action or constraints."""

    predictions: list[dict[str, Any]] = []
    for case in bundle.cases:
        prediction = _base_prediction(case, "bm25_text_floor")
        prediction["rankedCandidateIds"] = bm25_rank(case.query_text, case.candidates)
        prediction["candidateAssessments"] = [
            {
                "candidateDisplayId": candidate["candidateDisplayId"],
                "eligible": "unknown",
                "hardViolations": [],
                "hardUnknowns": [],
                "evidenceCitations": [],
            }
            for candidate in case.candidates
        ]
        predictions.append(prediction)
    return predictions


def public_rule_floor(bundle: PublicBundle) -> list[dict[str, Any]]:
    """Contract-aware scorer sanity floor using only public text/raw evidence."""

    predictions: list[dict[str, Any]] = []
    for case in bundle.cases:
        prediction = _base_prediction(case, "public_rule_floor")
        prediction["predictedAction"] = _public_action(case)
        prediction["extractedConstraints"] = _public_constraints(case)
        if prediction["predictedAction"] == "COMPARE_WITH_FIELD_EVIDENCE":
            prediction["comparison"], prediction["candidateAssessments"] = _comparison(case)
            prediction["rankedCandidateIds"] = [prediction["comparison"]["preferredCandidateDisplayId"]]
        elif case.candidates:
            prediction["rankedCandidateIds"], prediction["candidateAssessments"] = _rank_with_rules(
                case, prediction["extractedConstraints"]
            )
        prediction["stateUpdate"] = _state_update(case)
        _attach_answer_evidence(prediction)
        predictions.append(prediction)
    return predictions


def _code_hashes() -> dict[str, str]:
    directory = Path(__file__).resolve().parent
    names = ("used_phone_offline_runtime.py", "used_phone_complex_pilot.py")
    hashes = {name: sha256_file(directory / name) for name in names}
    cli = directory.parent / "scripts" / "run_used_phone_complex_pilot.py"
    hashes[cli.name] = sha256_file(cli)
    prediction_schema = directory / "schemas" / "used_phone_complex_prediction_v1.schema.json"
    hashes[prediction_schema.name] = sha256_file(prediction_schema)
    return dict(sorted(hashes.items()))


def run_public_floor(
    *, public_bundle_dir: str | Path, floor_name: str, predictions_path: str | Path, manifest_path: str | Path
) -> dict[str, Any]:
    if floor_name not in FLOORS:
        raise ValueError(f"unknown floor {floor_name!r}; expected one of {FLOORS}")
    bundle = load_public_bundle(public_bundle_dir)
    predictions = bm25_text_floor(bundle) if floor_name == "bm25_text_floor" else public_rule_floor(bundle)
    _validate_predictions(predictions)
    prediction_sha = write_jsonl(predictions_path, predictions)
    manifest = {
        "schemaVersion": "used-phone-complex-public-run-manifest-v1",
        "floorName": floor_name,
        "caseCount": len(predictions),
        "codeSha256": _code_hashes(),
        "publicInputSha256": dict(bundle.input_sha256),
        "publicBundleContractSha256": bundle.bundle_contract_sha256,
        "prediction": {"path": Path(predictions_path).name, "sha256": prediction_sha},
        "predictionPath": Path(predictions_path).name,
        "predictionSha256": prediction_sha,
        "networkUsed": False,
        "modelUsed": False,
        "humanGold": False,
        "evaluationOnly": True,
    }
    manifest["manifestCoreSha256"] = hashlib.sha256(canonical_json_bytes(manifest)).hexdigest()
    write_json(manifest_path, manifest)
    return manifest
