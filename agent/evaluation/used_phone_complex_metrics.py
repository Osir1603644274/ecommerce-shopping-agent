"""Hidden-label scorer for the used-phone complex-intent pilot.

Public floors do not import this module.  Hidden files are opened only from the
explicit ``score_hidden`` entry point after public predictions already exist.
"""

from __future__ import annotations

from collections import Counter
import hashlib
import json
import math
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError

from agent.evaluation.used_phone_offline_runtime import (
    PublicBundle,
    canonical_json_bytes,
    load_public_bundle,
    normalize_public_fact,
    sha256_file,
    write_json,
)


HIDDEN_FILES = (
    "ai_judged_candidate_qrel_v1.jsonl",
    "ai_judged_case_action_v1.jsonl",
    "ai_judged_query_constraints_v1.jsonl",
)
PINNED_HIDDEN_MANIFEST_SHA256 = "a7d69ba667e4d34b4e17255648019c38fd699b12e06968a91032c08426f512ec"
RETRIEVAL_CASES = (
    "BLIND-CASE-001",
    "BLIND-CASE-002",
    "BLIND-CASE-003",
    "BLIND-CASE-006",
    "BLIND-CASE-008",
)
FORMAL_ACTIONS = {
    "RETRIEVE_FILTER_AND_EXPLAIN_TRADEOFF",
    "RETRIEVE_FILTER_AND_RANK",
    "CLARIFY",
    "COMPARE_WITH_FIELD_EVIDENCE",
    "RETRIEVE_SUBSTITUTES_RETAINING_CONSTRAINTS",
    "ABSTAIN_OR_EXPLAIN",
    "UPDATE_STATE_THEN_RETRIEVE",
}
ACTION_ALIASES = {
    "retrieve_rank_with_tradeoff_explanation": "RETRIEVE_FILTER_AND_EXPLAIN_TRADEOFF",
    "retrieve_rank_with_priority_tradeoff": "RETRIEVE_FILTER_AND_EXPLAIN_TRADEOFF",
    "retrieve_filter_and_rank": "RETRIEVE_FILTER_AND_RANK",
    "clarify_condition_threshold_before_retrieval": "CLARIFY",
    "compare_visible_candidates": "COMPARE_WITH_FIELD_EVIDENCE",
    "exclude_prior_and_retrieve_replacement": "RETRIEVE_SUBSTITUTES_RETAINING_CONSTRAINTS",
    "abstain_from_unverifiable_guarantees_and_clarify_proxies": "ABSTAIN_OR_EXPLAIN",
    "update_constraints_then_retrieve": "UPDATE_STATE_THEN_RETRIEVE",
}
TERMINAL_PROJECTION_PROTOCOL = "v2-selection-materializer"
EXPECTED_CASE_IDS = tuple(f"BLIND-CASE-{index:03d}" for index in range(1, 9))


class HiddenScoringError(ValueError):
    pass


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8", newline="") as stream:
        for line_number, line in enumerate(stream, 1):
            if not line.strip():
                raise HiddenScoringError(f"blank row: {path.name}:{line_number}")
            value = json.loads(line)
            if not isinstance(value, dict):
                raise HiddenScoringError(f"non-object row: {path.name}:{line_number}")
            rows.append(value)
    return rows


def _load_hidden(hidden_dir: str | Path) -> tuple[dict[str, Any], dict[str, list[dict[str, Any]]]]:
    directory = Path(hidden_dir).resolve()
    manifest_path = directory / "manifest.json"
    actual_manifest_sha = sha256_file(manifest_path)
    if actual_manifest_sha != PINNED_HIDDEN_MANIFEST_SHA256:
        raise HiddenScoringError(f"unpinned hidden freeze manifest: {actual_manifest_sha}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    artifact_hashes = {item["path"]: item["sha256"] for item in manifest.get("artifacts", [])}
    loaded: dict[str, list[dict[str, Any]]] = {}
    for name in HIDDEN_FILES:
        path = directory / name
        if name not in artifact_hashes or sha256_file(path) != artifact_hashes[name]:
            raise HiddenScoringError(f"hidden freeze hash mismatch: {name}")
        loaded[name] = _read_jsonl(path)
    actual = {
        "candidateQrelRows": len(loaded[HIDDEN_FILES[0]]),
        "caseActionRows": len(loaded[HIDDEN_FILES[1]]),
        "queryConstraintRows": len(loaded[HIDDEN_FILES[2]]),
    }
    required_counts = {"candidateQrelRows": 80, "caseActionRows": 8, "queryConstraintRows": 8}
    if actual != required_counts or any(manifest.get("counts", {}).get(key) != value for key, value in required_counts.items()):
        raise HiddenScoringError(f"hidden freeze count mismatch: {actual}")
    qrel_counts = Counter(row.get("reviewCaseId") for row in loaded[HIDDEN_FILES[0]])
    if set(qrel_counts) != set(RETRIEVAL_CASES) or any(qrel_counts[case_id] != 16 for case_id in RETRIEVAL_CASES):
        raise HiddenScoringError(f"hidden qrel case pool mismatch: {dict(qrel_counts)}")
    return manifest, loaded


def _prediction_validator() -> Draft202012Validator:
    schema_path = Path(__file__).resolve().parent / "schemas" / "used_phone_complex_prediction_v1.schema.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


def _validate_prediction(row: Mapping[str, Any], validator: Draft202012Validator) -> None:
    try:
        validator.validate(row)
    except ValidationError as exc:
        raise HiddenScoringError(f"prediction schema violation: {exc.message}") from exc


def _load_predictions(path: str | Path) -> list[dict[str, Any]]:
    rows = _read_jsonl(Path(path))
    validator = _prediction_validator()
    for row in rows:
        _validate_prediction(row, validator)
    by_id = {row.get("reviewCaseId"): row for row in rows}
    expected_ids = set(EXPECTED_CASE_IDS)
    if len(rows) != 8 or set(by_id) != expected_ids:
        raise HiddenScoringError("predictions must contain exactly one row for each of 8 blind cases")
    required = {
        "predictedAction", "extractedConstraints", "rankedCandidateIds", "candidateAssessments",
        "comparison", "stateUpdate", "answerClaims", "evidenceRefs", "provenance",
    }
    for row in rows:
        missing = required - set(row)
        if missing:
            raise HiddenScoringError(f"prediction {row.get('reviewCaseId')} missing {sorted(missing)}")
    return [by_id[case_id] for case_id in sorted(by_id)]


def _failed_prediction_placeholder(case_id: str) -> dict[str, Any]:
    """Internal-only empty prediction used with an explicit failed execution mask."""

    return {
        "schemaVersion": "used-phone-complex-terminal-failure-placeholder-v1",
        "reviewCaseId": case_id,
        "predictedAction": "INVALID_OR_NO_ACTION",
        "extractedConstraints": [],
        "rankedCandidateIds": [],
        "candidateAssessments": [],
        "comparison": None,
        "stateUpdate": None,
        "answerClaims": [],
        "evidenceRefs": [],
        "provenance": {
            "producerName": "terminal_execution_failure_placeholder",
            "producerType": "scorer_placeholder",
            "evaluationOnly": True,
            "networkUsed": False,
            "modelUsed": False,
            "humanGold": False,
        },
    }


def _load_terminal_run(
    terminal_run_dir: str | Path, *, attempt: int,
) -> tuple[list[dict[str, Any]], dict[str, bool], dict[str, Any]]:
    if attempt < 1:
        raise HiddenScoringError("terminal attempt must be >= 1")
    run_dir = Path(terminal_run_dir).resolve()
    validator = _prediction_validator()
    predictions: list[dict[str, Any]] = []
    execution_mask: dict[str, bool] = {}
    terminals: dict[str, Any] = {}
    input_models: set[str] = set()
    observed_model_used = False
    observed_network_used = False

    for case_id in EXPECTED_CASE_IDS:
        attempt_dir = run_dir / "cases" / case_id / f"attempt-{attempt:03d}"
        success_path = attempt_dir / "success.json"
        failure_path = attempt_dir / "failure.json"
        running_path = attempt_dir / "running.json"
        present = [path for path in (success_path, failure_path) if path.is_file()]
        if len(present) != 1:
            raise HiddenScoringError(
                f"terminal case must have exactly one success/failure: {case_id} attempt-{attempt:03d}"
            )
        if running_path.exists():
            raise HiddenScoringError(f"stale running marker beside terminal: {case_id}")
        terminal_path = present[0]
        try:
            terminal = json.loads(terminal_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise HiddenScoringError(f"invalid terminal JSON: {case_id}") from exc
        if not isinstance(terminal, dict):
            raise HiddenScoringError(f"terminal JSON must be an object: {case_id}")
        terminal_sha = sha256_file(terminal_path)
        if terminal_path == success_path:
            if (
                terminal.get("schemaVersion") != "used-phone-agent-case-success-v1"
                or terminal.get("terminalState") != "success"
                or terminal.get("attempt") != attempt
            ):
                raise HiddenScoringError(f"invalid success terminal metadata: {case_id}")
            prediction = terminal.get("prediction")
            trace = terminal.get("trace")
            if not isinstance(prediction, dict) or not isinstance(trace, dict):
                raise HiddenScoringError(f"invalid success payload: {case_id}")
            if terminal.get("predictionSha256") != hashlib.sha256(canonical_json_bytes(prediction)).hexdigest():
                raise HiddenScoringError(f"stale success prediction hash: {case_id}")
            if terminal.get("traceSha256") != hashlib.sha256(canonical_json_bytes(trace)).hexdigest():
                raise HiddenScoringError(f"stale success trace hash: {case_id}")
            _validate_prediction(prediction, validator)
            provenance = prediction.get("provenance", {})
            if prediction.get("reviewCaseId") != case_id or trace.get("reviewCaseId") != case_id:
                raise HiddenScoringError(f"success case identity mismatch: {case_id}")
            if (
                trace.get("projectionProtocolVersion") != TERMINAL_PROJECTION_PROTOCOL
                or provenance.get("projectionProtocolVersion") != TERMINAL_PROJECTION_PROTOCOL
            ):
                raise HiddenScoringError(f"incompatible success projection protocol: {case_id}")
            predictions.append(prediction)
            execution_mask[case_id] = True
            model = provenance.get("model")
            if isinstance(model, str) and model:
                input_models.add(model)
            observed_model_used = observed_model_used or provenance.get("modelUsed") is True
            observed_network_used = observed_network_used or provenance.get("networkUsed") is True
            terminals[case_id] = {
                "terminalState": "success",
                "terminalSha256": terminal_sha,
                "predictionSha256": terminal["predictionSha256"],
                "traceSha256": terminal["traceSha256"],
            }
        else:
            model_calls = terminal.get("modelCalls")
            if (
                terminal.get("schemaVersion") != "used-phone-agent-case-failure-v1"
                or terminal.get("terminalState") != "failure"
                or terminal.get("attempt") != attempt
                or terminal.get("reviewCaseId") != case_id
                or not isinstance(terminal.get("errorType"), str)
                or not terminal["errorType"]
                or not isinstance(model_calls, list)
            ):
                raise HiddenScoringError(f"invalid failure terminal metadata: {case_id}")
            if model_calls:
                observed_model_used = True
                observed_network_used = True
                for call in model_calls:
                    if isinstance(call, Mapping):
                        model = call.get("model")
                        if isinstance(model, str) and model:
                            input_models.add(model)
            predictions.append(_failed_prediction_placeholder(case_id))
            execution_mask[case_id] = False
            terminals[case_id] = {
                "terminalState": "failure",
                "terminalSha256": terminal_sha,
                "errorType": terminal["errorType"],
            }

    run_manifest_path = run_dir / "manifest.json"
    run_manifest: dict[str, Any] = {}
    if run_manifest_path.is_file():
        try:
            run_manifest = json.loads(run_manifest_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise HiddenScoringError("invalid terminal run manifest JSON") from exc
        if run_manifest.get("attempt") != attempt:
            raise HiddenScoringError("terminal run manifest attempt mismatch")
        protocol = run_manifest.get("projectionProtocolVersion")
        if protocol not in {None, TERMINAL_PROJECTION_PROTOCOL}:
            raise HiddenScoringError("terminal run manifest projection protocol mismatch")
        model = run_manifest.get("model")
        if isinstance(model, str) and model:
            input_models.add(model)

    succeeded = sum(execution_mask.values())
    execution = {
        "total": len(EXPECTED_CASE_IDS),
        "succeeded": succeeded,
        "failed": len(EXPECTED_CASE_IDS) - succeeded,
        "coverage": succeeded / len(EXPECTED_CASE_IDS),
        "perCase": {
            case_id: {
                "succeeded": execution_mask[case_id],
                "terminalState": terminals[case_id]["terminalState"],
            }
            for case_id in EXPECTED_CASE_IDS
        },
    }
    provenance = {
        "runDirBasename": run_dir.name,
        "attempt": attempt,
        "terminalRecords": terminals,
        "runManifestSha256": sha256_file(run_manifest_path) if run_manifest_path.is_file() else None,
        "inputModelProvenance": {
            "models": sorted(input_models),
            "modelUsed": bool(observed_model_used or run_manifest.get("modelUsed") is True),
            "networkUsed": bool(observed_network_used or run_manifest.get("networkUsed") is True),
            "source": "validated success prediction provenance, failure sidecar modelCalls, plus optional terminal run manifest",
            "policy": "Successful prediction provenance, failure sidecar modelCalls, and the optional run manifest are unioned. A failure modelCalls entry proves a model/network attempt; false means none of those inputs recorded use, not that process-level egress was independently observed.",
        },
        "execution": execution,
    }
    return predictions, execution_mask, provenance


def _canonical_action(action: Any) -> str:
    value = str(action)
    if value in FORMAL_ACTIONS:
        return value
    return ACTION_ALIASES.get(value, "INVALID_OR_NO_ACTION")


def _atom_key(atom: Mapping[str, Any]) -> tuple[str, str, tuple[str, ...], str]:
    return (
        str(atom.get("group", atom.get("field", ""))).casefold(),
        str(atom.get("operator", "")).upper(),
        tuple(sorted(str(item).casefold() for item in atom.get("allowedValues", []))),
        str(atom.get("importance", atom.get("priority", ""))).casefold(),
    )


def _constraint_metrics(
    predictions: Mapping[str, Mapping[str, Any]], constraints: Sequence[Mapping[str, Any]],
    execution_mask: Mapping[str, bool] | None = None,
) -> dict[str, Any]:
    execution_mask = execution_mask or {case_id: True for case_id in predictions}
    true_positive = false_positive = false_negative = 0
    exact = 0
    per_case: dict[str, Any] = {}
    for gold in constraints:
        case_id = gold["reviewCaseId"]
        gold_atoms = {_atom_key(atom) for atom in gold["formalContracts"]["constraintContract"]["atoms"]}
        execution_failed = not execution_mask.get(case_id, False)
        if execution_failed:
            predicted_atoms: set[tuple[str, str, tuple[str, ...], str]] = set()
            tp = fp = 0
            fn = len(gold_atoms)
        else:
            predicted_atoms = {_atom_key(atom) for atom in predictions[case_id]["extractedConstraints"]}
            tp = len(gold_atoms & predicted_atoms)
            fp = len(predicted_atoms - gold_atoms)
            fn = len(gold_atoms - predicted_atoms)
        true_positive += tp
        false_positive += fp
        false_negative += fn
        is_exact = not execution_failed and predicted_atoms == gold_atoms
        exact += int(is_exact)
        per_case[case_id] = {
            "goldAtoms": len(gold_atoms), "predictedAtoms": len(predicted_atoms),
            "tp": tp, "fp": fp, "fn": fn, "exact": is_exact,
            "executionFailed": execution_failed,
        }
    precision = true_positive / (true_positive + false_positive) if true_positive + false_positive else None
    recall = true_positive / (true_positive + false_negative) if true_positive + false_negative else None
    f1 = 2 * precision * recall / (precision + recall) if precision is not None and recall is not None and precision + recall else None
    return {
        "canonicalAtomMicro": {"tp": true_positive, "fp": false_positive, "fn": false_negative, "precision": precision, "recall": recall, "f1": f1},
        "caseExact": {"correct": exact, "total": len(constraints), "accuracy": exact / len(constraints)},
        "perCase": per_case,
        "note": "Cases whose formal operational atom set is empty count invented slots as false positives.",
    }


def _dcg(grades: Sequence[int | None], k: int) -> float:
    return sum(((2 ** grade - 1) if grade is not None else 0.0) / math.log2(index + 2) for index, grade in enumerate(grades[:k]))


def _mean(values: Iterable[float | None]) -> float | None:
    applicable = [value for value in values if value is not None]
    return sum(applicable) / len(applicable) if applicable else None


def _ranking_metrics(
    predictions: Mapping[str, Mapping[str, Any]], qrels: Sequence[Mapping[str, Any]], bundle: PublicBundle,
    execution_mask: Mapping[str, bool] | None = None,
) -> dict[str, Any]:
    execution_mask = execution_mask or {case_id: True for case_id in predictions}
    qrels_by_case: dict[str, dict[str, Mapping[str, Any]]] = {case_id: {} for case_id in RETRIEVAL_CASES}
    for row in qrels:
        qrels_by_case[row["reviewCaseId"]][row["candidateDisplayId"]] = row
    per_case: dict[str, Any] = {}
    case006_anchor = next(
        item["candidateDisplayId"]
        for item in bundle.by_id()["BLIND-CASE-006"].user_visible_candidate_context
        if item.get("label") == "先前商品"
    )
    for case_id in (f"BLIND-CASE-{index:03d}" for index in range(1, 9)):
        if case_id not in qrels_by_case:
            per_case[case_id] = None
            continue
        if not execution_mask.get(case_id, False):
            per_case[case_id] = {
                "executionSucceeded": False,
                "judgedRankedCount": 0,
                "rankingSlotCount": 0,
                "anchorConsumedRankingSlot": False,
                "unjudgedSkipped": [],
                "ndcg@5": 0.0,
                "ndcg@10": 0.0,
                "eligiblePrecision@5": 0.0,
                "eligiblePrecision@10": 0.0,
                "hardViolationRate@5": None,
                "hardViolationRate@10": None,
                "hardUnknownRate@5": None,
                "hardUnknownRate@10": None,
                "judgedReturnedCount@5": 0,
                "judgedReturnedCount@10": 0,
                "hardViolationRateAmongReturned@5": None,
                "hardViolationRateAmongReturned@10": None,
                "hardUnknownRateAmongReturned@5": None,
                "hardUnknownRateAmongReturned@10": None,
                "riskStatus": "not_applicable_due_execution_failure",
            }
            continue
        pool = qrels_by_case[case_id]
        ranked_slots: list[str | None] = []
        seen: set[str] = set()
        unjudged: list[str] = []
        for candidate_id in predictions[case_id]["rankedCandidateIds"]:
            if candidate_id in seen:
                continue
            seen.add(candidate_id)
            if candidate_id in pool:
                ranked_slots.append(candidate_id)
            elif case_id == "BLIND-CASE-006" and candidate_id == case006_anchor:
                # The public substitute anchor is not a qrel row, but returning
                # it violates the action contract and consumes a ranking slot.
                ranked_slots.append(None)
            elif candidate_id not in pool:
                unjudged.append(candidate_id)
        ideal = sorted((row["relevanceGrade"] for row in pool.values()), key=lambda grade: -1 if grade is None else grade, reverse=True)
        metrics: dict[str, Any] = {
            "executionSucceeded": True,
            "judgedRankedCount": sum(candidate_id is not None for candidate_id in ranked_slots),
            "rankingSlotCount": len(ranked_slots),
            "anchorConsumedRankingSlot": case_id == "BLIND-CASE-006" and None in ranked_slots,
            "unjudgedSkipped": unjudged,
        }
        for k in (5, 10):
            top_slots = ranked_slots[:k]
            top_rows = [pool[candidate_id] for candidate_id in top_slots if candidate_id is not None]
            grades = [pool[candidate_id]["relevanceGrade"] if candidate_id is not None else None for candidate_id in top_slots]
            denominator = _dcg(ideal, k)
            metrics[f"ndcg@{k}"] = _dcg(grades, k) / denominator if denominator else None
            metrics[f"eligiblePrecision@{k}"] = sum(row["eligible"] is True for row in top_rows) / k
            metrics[f"hardViolationRate@{k}"] = sum(bool(row["hardViolations"]) for row in top_rows) / k
            metrics[f"hardUnknownRate@{k}"] = sum(bool(row["hardUnknowns"]) for row in top_rows) / k
            returned = len(top_rows)
            metrics[f"judgedReturnedCount@{k}"] = returned
            metrics[f"hardViolationRateAmongReturned@{k}"] = (
                sum(bool(row["hardViolations"]) for row in top_rows) / returned if returned else None
            )
            metrics[f"hardUnknownRateAmongReturned@{k}"] = (
                sum(bool(row["hardUnknowns"]) for row in top_rows) / returned if returned else None
            )
        per_case[case_id] = metrics
    macro = {
        metric: _mean(per_case[case_id][metric] if per_case[case_id] is not None else None for case_id in per_case)
        for metric in (
            "ndcg@5", "ndcg@10", "eligiblePrecision@5", "eligiblePrecision@10",
            "hardViolationRate@5", "hardViolationRate@10", "hardUnknownRate@5", "hardUnknownRate@10",
            "hardViolationRateAmongReturned@5", "hardViolationRateAmongReturned@10",
            "hardUnknownRateAmongReturned@5", "hardUnknownRateAmongReturned@10",
        )
    }
    return {
        "applicableCases": list(RETRIEVAL_CASES),
        "perCase": per_case,
        "macroApplicableOnly": macro,
        "judgmentPolicy": "Closed 80-row judged pool only. Null grade has decision gain 0 but is not asserted negative. General unjudged/out-of-pool IDs are skipped; the forbidden case006 anchor consumes a zero-gain ranking slot and is separately an action-contract failure. Eligible precision and legacy hard-risk rates use fixed k to expose short returns; AmongReturned risk rates use judged returned count and are N/A when none are returned. Execution failures receive zero NDCG/eligible precision quality credit, while all hard-risk rates are N/A and excluded from risk macros because a failed execution cannot claim zero violations or unknowns.",
    }


def _public_candidates(bundle: PublicBundle) -> dict[str, Mapping[str, Any]]:
    return {candidate["candidateDisplayId"]: candidate for case in bundle.cases for candidate in case.candidates}


def _citation_valid(
    citation: Mapping[str, Any], candidates: Mapping[str, Mapping[str, Any]], *, expected_case_id: str,
) -> bool:
    candidate_id = str(citation.get("candidateDisplayId"))
    if not candidate_id.startswith(f"{expected_case_id}-CAND-"):
        return False
    candidate = candidates.get(candidate_id)
    if candidate is None:
        return False
    index = citation.get("evidenceRefIndex")
    refs = candidate.get("evidenceRefs", [])
    if not isinstance(index, int) or index < 0 or index >= len(refs):
        return False
    ref = refs[index]
    if citation.get("field") != ref.get("field"):
        return False
    expected_hash = hashlib.sha256(canonical_json_bytes(ref.get("rawValue"))).hexdigest()
    if citation.get("rawValueSha256") != expected_hash:
        return False
    fact_group = citation.get("factGroup")
    normalized_value = citation.get("normalizedValue")
    if not isinstance(fact_group, str) or not isinstance(normalized_value, str):
        return False
    return normalize_public_fact(fact_group, ref.get("rawValue")) == normalized_value


def _all_citations(prediction: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    citations: list[Mapping[str, Any]] = []
    for assessment in prediction.get("candidateAssessments", []):
        citations.extend(assessment.get("evidenceCitations", []))
    comparison = prediction.get("comparison")
    if isinstance(comparison, dict):
        for field in comparison.get("fieldComparisons", []):
            citations.extend(field.get("evidenceCitations", []))
    for ref in prediction.get("evidenceRefs", []):
        if isinstance(ref, dict) and "candidateDisplayId" in ref:
            citations.append(ref)
    return citations


def _evidence_metrics(
    predictions: Mapping[str, Mapping[str, Any]], bundle: PublicBundle,
    execution_mask: Mapping[str, bool] | None = None,
) -> dict[str, Any]:
    execution_mask = execution_mask or {case_id: True for case_id in predictions}
    candidates = _public_candidates(bundle)
    per_case: dict[str, Any] = {}
    valid_total = citation_total = 0
    valid_claim_refs = claim_ref_total = 0
    valid_claims = claim_total = 0
    applicable_citation_case_accuracies: list[float] = []
    applicable_claim_case_accuracies: list[float] = []
    gate_applicable = gate_passed = 0
    for case_id, prediction in predictions.items():
        if not execution_mask.get(case_id, False):
            per_case[case_id] = {
                "valid": 0,
                "total": 0,
                "accuracy": None,
                "validClaimReferences": 0,
                "claimReferences": 0,
                "claimReferenceAccuracy": None,
                "validSemanticClaims": 0,
                "semanticClaims": 0,
                "claimSemanticAccuracy": None,
                "groundingGatePassed": None,
                "groundingGateStatus": "not_applicable_due_execution_failure",
            }
            continue
        citations_by_key: dict[tuple[Any, ...], Mapping[str, Any]] = {}
        for citation in _all_citations(prediction):
            key = (
                citation.get("candidateDisplayId"),
                citation.get("evidenceRefIndex"),
                citation.get("field"),
                citation.get("rawValueSha256"),
                citation.get("factGroup"),
                citation.get("normalizedValue"),
            )
            citations_by_key.setdefault(key, citation)
        citations = list(citations_by_key.values())
        valid = sum(_citation_valid(citation, candidates, expected_case_id=case_id) for citation in citations)
        citation_total += len(citations)
        valid_total += valid
        evidence_by_id = {
            ref.get("evidenceRefId"): ref
            for ref in prediction.get("evidenceRefs", [])
            if isinstance(ref, dict) and _citation_valid(ref, candidates, expected_case_id=case_id)
        }
        referenced_ids = [
            evidence_id
            for claim in prediction.get("answerClaims", [])
            if isinstance(claim, dict)
            for evidence_id in claim.get("evidenceRefIds", [])
        ]
        valid_refs = sum(evidence_id in evidence_by_id for evidence_id in referenced_ids)
        claim_ref_total += len(referenced_ids)
        valid_claim_refs += valid_refs
        case_valid_claims = 0
        claims = [claim for claim in prediction.get("answerClaims", []) if isinstance(claim, dict)]
        case_candidate_ids = {
            candidate["candidateDisplayId"]
            for case in bundle.cases
            if case.review_case_id == case_id
            for candidate in case.candidates
        }
        for claim in claims:
            expected_text = (
                f"candidate_fact:{claim.get('candidateDisplayId')}:{claim.get('factGroup')}="
                f"{claim.get('normalizedValue')}"
            )
            refs = [evidence_by_id.get(evidence_id) for evidence_id in claim.get("evidenceRefIds", [])]
            semantic_match = bool(refs) and all(
                ref is not None
                and ref.get("candidateDisplayId") == claim.get("candidateDisplayId")
                and ref.get("factGroup") == claim.get("factGroup")
                and ref.get("normalizedValue") == claim.get("normalizedValue")
                for ref in refs
            )
            case_valid_claims += int(
                claim.get("claimType") == "candidate_fact"
                and claim.get("text") == expected_text
                and claim.get("candidateDisplayId") in case_candidate_ids
                and semantic_match
            )
        claim_total += len(claims)
        valid_claims += case_valid_claims
        citation_accuracy = valid / len(citations) if citations else None
        claim_accuracy = case_valid_claims / len(claims) if claims else None
        if citation_accuracy is not None:
            applicable_citation_case_accuracies.append(citation_accuracy)
        if claim_accuracy is not None:
            applicable_claim_case_accuracies.append(claim_accuracy)
        if citations or claims:
            gate_applicable += 1
            citation_gate = citation_accuracy == 1.0 if citations else True
            claim_gate = claim_accuracy == 1.0 if claims else True
            gate_passed += int(citation_gate and claim_gate)
        per_case[case_id] = {
            "valid": valid,
            "total": len(citations),
            "accuracy": citation_accuracy,
            "validClaimReferences": valid_refs,
            "claimReferences": len(referenced_ids),
            "claimReferenceAccuracy": valid_refs / len(referenced_ids) if referenced_ids else None,
            "validSemanticClaims": case_valid_claims,
            "semanticClaims": len(claims),
            "claimSemanticAccuracy": claim_accuracy,
            "groundingGatePassed": (citation_accuracy == 1.0 if citations else True) and (claim_accuracy == 1.0 if claims else True) if citations or claims else None,
            "groundingGateStatus": (
                "passed" if (citations or claims) and (citation_accuracy == 1.0 if citations else True) and (claim_accuracy == 1.0 if claims else True)
                else "failed" if citations or claims
                else "not_applicable_no_output"
            ),
        }
    return {
        "valid": valid_total,
        "total": citation_total,
        "accuracy": valid_total / citation_total if citation_total else None,
        "validClaimReferences": valid_claim_refs,
        "claimReferences": claim_ref_total,
        "claimReferenceAccuracy": valid_claim_refs / claim_ref_total if claim_ref_total else None,
        "validSemanticClaims": valid_claims,
        "semanticClaims": claim_total,
        "claimSemanticAccuracy": valid_claims / claim_total if claim_total else None,
        "citationAccuracyMacroByApplicableCase": _mean(applicable_citation_case_accuracies),
        "claimSemanticAccuracyMacroByApplicableCase": _mean(applicable_claim_case_accuracies),
        "groundingGate": {
            "passedCases": gate_passed,
            "applicableCases": gate_applicable,
            "macroPassRate": gate_passed / gate_applicable if gate_applicable else None,
        },
        "perCase": per_case,
        "policy": "A citation is correct only when candidate, ref index, field, raw-value hash, fact group, and independently normalized value match public raw evidence. A claim is correct only when its structured candidate/group/value, exact canonical text, and every referenced valid citation agree.",
    }


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _state_metrics(predictions: Mapping[str, Mapping[str, Any]], constraints: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    gold = next(row for row in constraints if row["reviewCaseId"] == "BLIND-CASE-008")
    contract = gold["formalContracts"]["multiTurnContract"]
    predicted = predictions["BLIND-CASE-008"].get("stateUpdate") or {}
    return {
        "case": "BLIND-CASE-008",
        "finalStateExact": _canonical(predicted.get("finalState")) == _canonical(contract["expectedFinalState"]),
        "turnDeltaExact": _canonical(predicted.get("turnDelta")) == _canonical(contract["turnDelta"]),
        "initialStateExact": _canonical(predicted.get("initialState")) == _canonical(contract["initialState"]),
    }


def _comparison_metrics(
    predictions: Mapping[str, Mapping[str, Any]], actions: Sequence[Mapping[str, Any]],
    constraints: Sequence[Mapping[str, Any]], bundle: PublicBundle,
) -> dict[str, Any]:
    case_id = "BLIND-CASE-005"
    action = next(row for row in actions if row["reviewCaseId"] == case_id)
    constraint = next(row for row in constraints if row["reviewCaseId"] == case_id)
    predicted = predictions[case_id].get("comparison") or {}
    expected_preferred = action["comparisonSupport"]["recommendedCandidateDisplayId"]
    dimensions = constraint["formalContracts"]["comparisonContract"]["dimensions"]
    public_case = bundle.by_id()[case_id]
    expected_ids = {item["candidateDisplayId"] for item in public_case.user_visible_candidate_context}
    candidates = _public_candidates(bundle)
    covered = 0
    value_correct = 0
    expected = len(dimensions) * len(expected_ids)
    by_field = {field.get("field"): field for field in predicted.get("fieldComparisons", []) if isinstance(field, dict)}
    field_detail: dict[str, Any] = {}
    for dimension in dimensions:
        citations = by_field.get(dimension, {}).get("evidenceCitations", [])
        predicted_values = by_field.get(dimension, {}).get("candidateValues", {})
        valid_ids: set[str] = set()
        field_value_correct = 0
        for candidate_id in expected_ids:
            expected_value = next(
                (
                    normalize_public_fact(dimension, ref.get("rawValue"))
                    for ref in candidates[candidate_id].get("evidenceRefs", [])
                    if normalize_public_fact(dimension, ref.get("rawValue")) is not None
                ),
                None,
            )
            value_matches = predicted_values.get(candidate_id) == (expected_value or "unknown")
            field_value_correct += int(value_matches)
            if value_matches and any(
                citation.get("candidateDisplayId") == candidate_id
                and citation.get("factGroup") == dimension
                and citation.get("normalizedValue") == expected_value
                and _citation_valid(citation, candidates, expected_case_id=case_id)
                for citation in citations
            ):
                valid_ids.add(candidate_id)
        count = len(valid_ids)
        covered += count
        value_correct += field_value_correct
        field_detail[dimension] = {
            "coveredCandidates": count,
            "correctCandidateValues": field_value_correct,
            "expectedCandidates": len(expected_ids),
        }
    return {
        "case": case_id,
        "preferredCandidateExact": predicted.get("preferredCandidateDisplayId") == expected_preferred,
        "fieldEvidenceCoverage": covered / expected if expected else None,
        "candidateValueAccuracy": value_correct / expected if expected else None,
        "covered": covered,
        "correctCandidateValues": value_correct,
        "expected": expected,
        "perField": field_detail,
    }


def _action_metrics(predictions: Mapping[str, Mapping[str, Any]], actions: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    exact = canonical = 0
    confusion: Counter[str] = Counter()
    per_case: dict[str, Any] = {}
    for gold in actions:
        case_id = gold["reviewCaseId"]
        expected = gold["formalExpectedAction"]
        raw_predicted = predictions[case_id]["predictedAction"]
        canonical_predicted = _canonical_action(raw_predicted)
        is_exact = raw_predicted == expected
        is_canonical = canonical_predicted == expected
        exact += int(is_exact)
        canonical += int(is_canonical)
        confusion[f"{expected} -> {canonical_predicted}"] += 1
        per_case[case_id] = {"expected": expected, "predicted": raw_predicted, "canonicalPredicted": canonical_predicted, "exact": is_exact, "canonicalExact": is_canonical}
    return {
        "exact": {"correct": exact, "total": len(actions), "accuracy": exact / len(actions)},
        "canonical": {"correct": canonical, "total": len(actions), "accuracy": canonical / len(actions)},
        "confusion": dict(sorted(confusion.items())),
        "perCase": per_case,
        "policy": "Only prediction.predictedAction is scored; control/runtime finalAction fields are never interpreted as business actions.",
    }


def _action_contract_compliance(
    predictions: Mapping[str, Mapping[str, Any]], actions: Sequence[Mapping[str, Any]], bundle: PublicBundle,
    execution_mask: Mapping[str, bool] | None = None,
) -> dict[str, Any]:
    execution_mask = execution_mask or {case_id: True for case_id in predictions}
    actions_by_case = {row["reviewCaseId"]: row for row in actions}
    public_cases = bundle.by_id()
    per_case: dict[str, Any] = {}
    compliant = 0
    for case_id in sorted(predictions):
        expected_action = actions_by_case[case_id]["formalExpectedAction"]
        prediction = predictions[case_id]
        ranked = prediction.get("rankedCandidateIds", [])
        checks: dict[str, bool] = {"executionSucceeded": bool(execution_mask.get(case_id, False))}
        if case_id in RETRIEVAL_CASES:
            public_ids = {candidate["candidateDisplayId"] for candidate in public_cases[case_id].candidates}
            checks["minimumFivePublicCandidatesReturned"] = len({candidate_id for candidate_id in ranked if candidate_id in public_ids}) >= 5
        if expected_action in {"CLARIFY", "ABSTAIN_OR_EXPLAIN"}:
            checks["noProductsReturned"] = len(ranked) == 0
        if case_id == "BLIND-CASE-006":
            anchor = next(
                item["candidateDisplayId"]
                for item in public_cases[case_id].user_visible_candidate_context
                if item.get("label") == "先前商品"
            )
            checks["substituteAnchorExcluded"] = anchor not in ranked
        if case_id == "BLIND-CASE-005":
            comparison = prediction.get("comparison") or {}
            expected_preferred = actions_by_case[case_id]["comparisonSupport"]["recommendedCandidateDisplayId"]
            visible = {item["label"]: item["candidateDisplayId"] for item in public_cases[case_id].user_visible_candidate_context}
            candidate_a = visible["候选 A"]
            candidate_b = visible["候选 B"]
            disclosures = comparison.get("tradeoffDisclosures", [])
            checks["preferredCandidateMatches"] = comparison.get("preferredCandidateDisplayId") == expected_preferred == candidate_b
            checks["batteryTradeoffDisclosed"] = any(
                disclosure.get("field") == "battery_health"
                and disclosure.get("advantagedCandidateDisplayId") == candidate_a
                and disclosure.get("preferredCandidateDisplayId") == candidate_b
                and disclosure.get("advantagedValue") == "90_plus"
                and disclosure.get("preferredValue") == "80_to_90"
                and disclosure.get("disclosureType") == "preferred_candidate_loses_dimension"
                for disclosure in disclosures
            )
        case_compliant = all(checks.values()) if checks else True
        compliant += int(case_compliant)
        per_case[case_id] = {"applicableChecks": checks, "compliant": case_compliant}
    return {
        "metricKind": "behavior_contract_only_not_action_label",
        "correct": compliant,
        "total": len(predictions),
        "accuracy": compliant / len(predictions),
        "perCase": per_case,
        "policy": "Behavior-only checks, intentionally independent of predictedAction correctness. Expected retrieval cases must return at least five unique public candidates; clarify/abstain must return none; substitute anchors are forbidden; comparison must disclose the preferred candidate's battery disadvantage.",
    }


def _action_and_required_behavior_contract(
    action: Mapping[str, Any], behavior: Mapping[str, Any], constraints: Mapping[str, Any],
    comparison: Mapping[str, Any], state: Mapping[str, Any],
) -> dict[str, Any]:
    per_case: dict[str, Any] = {}
    correct = 0
    for case_id in sorted(action["perCase"]):
        checks: dict[str, bool] = {
            "canonicalActionExact": bool(action["perCase"][case_id]["canonicalExact"]),
            "behaviorContractCompliant": bool(behavior["perCase"][case_id]["compliant"]),
        }
        if case_id in RETRIEVAL_CASES:
            checks["constraintAtomSetExact"] = bool(constraints["perCase"][case_id]["exact"])
        if case_id == "BLIND-CASE-005":
            checks.update({
                "comparisonPreferredCandidateExact": bool(comparison["preferredCandidateExact"]),
                "comparisonCandidateValuesExact": comparison["candidateValueAccuracy"] == 1.0,
                "comparisonFieldEvidenceComplete": comparison["fieldEvidenceCoverage"] == 1.0,
                "comparisonTradeoffDisclosed": bool(
                    behavior["perCase"][case_id]["applicableChecks"].get("batteryTradeoffDisclosed")
                ),
            })
        if case_id == "BLIND-CASE-008":
            checks.update({
                "initialStateExact": bool(state["initialStateExact"]),
                "turnDeltaExact": bool(state["turnDeltaExact"]),
                "finalStateExact": bool(state["finalStateExact"]),
            })
        passed = all(checks.values())
        correct += int(passed)
        per_case[case_id] = {
            "checks": checks,
            "passed": passed,
        }
    return {
        "correct": correct,
        "total": len(per_case),
        "accuracy": correct / len(per_case),
        "perCase": per_case,
        "policy": "A case passes only when its canonical business action and action-required behavior subcontracts pass: retrieval constraints/minimum return/anchor rules, case005 comparison preferred/value/evidence/tradeoff, and case008 initial/delta/final state. This metric does not claim general ranking quality or completeness of every answer claim.",
    }


def _validate_report_invariants(report: Mapping[str, Any]) -> None:
    execution = report.get("execution", {})
    succeeded = execution.get("succeeded")
    failed = execution.get("failed")
    total = execution.get("total")
    coverage = execution.get("coverage")
    if type(total) is not int or type(succeeded) is not int or type(failed) is not int:
        raise HiddenScoringError("execution counts must be integers")
    if total != len(EXPECTED_CASE_IDS) or succeeded + failed != total:
        raise HiddenScoringError("execution succeeded/failed counts do not sum to 8")
    if not isinstance(coverage, (int, float)) or isinstance(coverage, bool):
        raise HiddenScoringError("execution coverage must be numeric")
    if not math.isclose(float(coverage), succeeded / total, rel_tol=0.0, abs_tol=1e-12):
        raise HiddenScoringError("execution coverage does not equal succeeded/8")
    per_case = execution.get("perCase")
    if not isinstance(per_case, Mapping) or set(per_case) != set(EXPECTED_CASE_IDS):
        raise HiddenScoringError("execution perCase must contain exactly 8 blind cases")
    per_case_succeeded = sum(
        item.get("succeeded") is True for item in per_case.values() if isinstance(item, Mapping)
    )
    if per_case_succeeded != succeeded:
        raise HiddenScoringError("execution perCase success count mismatch")
    if any(
        not isinstance(item, Mapping)
        or item.get("status") != ("success" if item.get("succeeded") is True else "execution_failure")
        for item in per_case.values()
    ):
        raise HiddenScoringError("execution perCase status/succeeded mismatch")


def _build_score_report(
    *, bundle: PublicBundle, hidden: Mapping[str, Sequence[Mapping[str, Any]]],
    prediction_rows: Sequence[Mapping[str, Any]], execution_mask: Mapping[str, bool],
) -> dict[str, Any]:
    predictions = {row["reviewCaseId"]: row for row in prediction_rows}
    actions = hidden["ai_judged_case_action_v1.jsonl"]
    constraints = hidden["ai_judged_query_constraints_v1.jsonl"]
    qrels = hidden["ai_judged_candidate_qrel_v1.jsonl"]
    action_metrics = _action_metrics(predictions, actions)
    behavior_contract = _action_contract_compliance(predictions, actions, bundle, execution_mask)
    constraint_metrics = _constraint_metrics(predictions, constraints, execution_mask)
    comparison_metrics = _comparison_metrics(predictions, actions, constraints, bundle)
    state_metrics = _state_metrics(predictions, constraints)
    required_behavior = _action_and_required_behavior_contract(
        action_metrics, behavior_contract, constraint_metrics, comparison_metrics, state_metrics
    )
    report = {
        "schemaVersion": "used-phone-complex-hidden-score-v1",
        "status": "AI_JUDGED_PILOT_NOT_HUMAN_GOLD",
        "execution": {
            "total": len(EXPECTED_CASE_IDS),
            "succeeded": sum(bool(execution_mask.get(case_id, False)) for case_id in EXPECTED_CASE_IDS),
            "failed": sum(not bool(execution_mask.get(case_id, False)) for case_id in EXPECTED_CASE_IDS),
            "coverage": sum(bool(execution_mask.get(case_id, False)) for case_id in EXPECTED_CASE_IDS) / len(EXPECTED_CASE_IDS),
            "perCase": {
                case_id: {
                    "succeeded": bool(execution_mask.get(case_id, False)),
                    "status": "success" if execution_mask.get(case_id, False) else "execution_failure",
                }
                for case_id in EXPECTED_CASE_IDS
            },
        },
        "action": action_metrics,
        "actionContractCompliance": behavior_contract,
        "actionAndRequiredBehaviorContract": required_behavior,
        "constraints": constraint_metrics,
        "ranking": _ranking_metrics(predictions, qrels, bundle, execution_mask),
        "comparison": comparison_metrics,
        "clarifyAbstain": {
            "BLIND-CASE-004": predictions["BLIND-CASE-004"]["predictedAction"] == "CLARIFY",
            "BLIND-CASE-007": predictions["BLIND-CASE-007"]["predictedAction"] == "ABSTAIN_OR_EXPLAIN",
        },
        "multiTurn": state_metrics,
        "evidenceCitation": _evidence_metrics(predictions, bundle, execution_mask),
        "limitations": [
            "8-case methodology pilot, not a final closed test",
            "AI-only judgments, not human gold",
            "80-row closed judged pool only; out-of-pool products remain unknown",
            "Non-applicable case metrics are N/A/null and are excluded from macro averages",
            "Execution failures receive zero ranking quality credit but N/A hard-risk rates; they cannot claim zero violations",
        ],
    }
    report_schema_path = Path(__file__).resolve().parent / "schemas" / "used_phone_complex_hidden_score_v1.schema.json"
    report_schema = json.loads(report_schema_path.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(report_schema)
    Draft202012Validator(report_schema).validate(report)
    _validate_report_invariants(report)
    return report


def _score_code_hashes() -> dict[str, str]:
    code_path = Path(__file__).resolve()
    paths = [
        code_path,
        code_path.with_name("used_phone_offline_runtime.py"),
        code_path.parent.parent / "scripts" / "run_used_phone_complex_pilot.py",
        code_path.parent / "schemas" / "used_phone_complex_prediction_v1.schema.json",
        code_path.parent / "schemas" / "used_phone_complex_hidden_score_v1.schema.json",
    ]
    return {path.name: sha256_file(path) for path in paths}


def score_hidden(
    *, public_bundle_dir: str | Path, hidden_freeze_dir: str | Path, predictions_path: str | Path,
    report_path: str | Path, manifest_path: str | Path,
) -> dict[str, Any]:
    bundle = load_public_bundle(public_bundle_dir)
    hidden_manifest, hidden = _load_hidden(hidden_freeze_dir)
    prediction_rows = _load_predictions(predictions_path)
    execution_mask = {case_id: True for case_id in EXPECTED_CASE_IDS}
    report = _build_score_report(
        bundle=bundle, hidden=hidden, prediction_rows=prediction_rows, execution_mask=execution_mask,
    )
    report_sha = write_json(report_path, report)
    hidden_hashes = {name: sha256_file(Path(hidden_freeze_dir) / name) for name in HIDDEN_FILES}
    hidden_hashes["manifest.json"] = sha256_file(Path(hidden_freeze_dir) / "manifest.json")
    input_model_used = any(row.get("provenance", {}).get("modelUsed") is True for row in prediction_rows)
    input_network_used = any(row.get("provenance", {}).get("networkUsed") is True for row in prediction_rows)
    manifest = {
        "schemaVersion": "used-phone-complex-hidden-score-manifest-v1",
        "freezeId": hidden_manifest.get("freezeId"),
        "codeSha256": _score_code_hashes(),
        "publicInputSha256": dict(bundle.input_sha256),
        "publicBundleContractSha256": bundle.bundle_contract_sha256,
        "hiddenInputSha256": hidden_hashes,
        "predictionSha256": sha256_file(Path(predictions_path)),
        "report": {"path": Path(report_path).name, "sha256": report_sha},
        "scoreReportPath": Path(report_path).name,
        "scoreReportSha256": report_sha,
        "inputPredictionProvenance": {
            "modelUsed": input_model_used,
            "networkUsed": input_network_used,
        },
        "scorerProcess": {"networkUsed": False, "modelUsed": False},
        "humanGold": False,
        "evaluationOnly": True,
    }
    manifest["manifestCoreSha256"] = hashlib.sha256(canonical_json_bytes(manifest)).hexdigest()
    write_json(manifest_path, manifest)
    return report


def score_terminal_run(
    *, public_bundle_dir: str | Path, hidden_freeze_dir: str | Path,
    terminal_run_dir: str | Path, attempt: int, report_path: str | Path, manifest_path: str | Path,
) -> dict[str, Any]:
    bundle = load_public_bundle(public_bundle_dir)
    hidden_manifest, hidden = _load_hidden(hidden_freeze_dir)
    prediction_rows, execution_mask, terminal_provenance = _load_terminal_run(
        terminal_run_dir, attempt=attempt,
    )
    report = _build_score_report(
        bundle=bundle, hidden=hidden, prediction_rows=prediction_rows, execution_mask=execution_mask,
    )
    report_sha = write_json(report_path, report)
    hidden_hashes = {name: sha256_file(Path(hidden_freeze_dir) / name) for name in HIDDEN_FILES}
    hidden_hashes["manifest.json"] = sha256_file(Path(hidden_freeze_dir) / "manifest.json")
    manifest = {
        "schemaVersion": "used-phone-complex-terminal-run-score-manifest-v1",
        "freezeId": hidden_manifest.get("freezeId"),
        "codeSha256": _score_code_hashes(),
        "publicInputSha256": dict(bundle.input_sha256),
        "publicBundleContractSha256": bundle.bundle_contract_sha256,
        "hiddenInputSha256": hidden_hashes,
        "terminalRun": {
            "runDirBasename": terminal_provenance["runDirBasename"],
            "attempt": attempt,
            "runManifestSha256": terminal_provenance["runManifestSha256"],
            "terminalRecords": terminal_provenance["terminalRecords"],
        },
        "execution": terminal_provenance["execution"],
        "inputModelProvenance": terminal_provenance["inputModelProvenance"],
        "report": {"path": Path(report_path).name, "sha256": report_sha},
        "scoreReportPath": Path(report_path).name,
        "scoreReportSha256": report_sha,
        "scorerProcess": {"networkUsed": False, "modelUsed": False},
        "humanGold": False,
        "evaluationOnly": True,
    }
    manifest["manifestCoreSha256"] = hashlib.sha256(canonical_json_bytes(manifest)).hexdigest()
    write_json(manifest_path, manifest)
    return report
