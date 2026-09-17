from __future__ import annotations

import ast
import copy
import hashlib
import json
from pathlib import Path
import shutil
import socket

import pytest
from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError

from agent.evaluation.used_phone_complex_metrics import (
    HiddenScoringError,
    _action_metrics,
    _action_contract_compliance,
    _comparison_metrics,
    _action_and_required_behavior_contract,
    _evidence_metrics,
    _constraint_metrics,
    _load_hidden,
    _load_predictions,
    _load_terminal_run,
    _ranking_metrics,
    _validate_report_invariants,
    score_hidden,
)
from agent.evaluation.used_phone_complex_pilot import run_public_floor
from agent.evaluation.used_phone_offline_runtime import (
    EXPECTED_CASE_COUNTS,
    PublicBundle,
    PublicBundleError,
    PublicCase,
    assert_public_input_path,
    canonical_json_bytes,
    load_public_bundle,
)


REPO_ROOT = Path(__file__).resolve().parents[2]
DATA_ROOT = (
    REPO_ROOT
    / "data"
    / "processed"
    / "ecommerce"
    / "kuaisearch_synthetic_evidence_track_b_v01"
    / "09807c773ce67360ed8df30842e372182fcf7ad9"
)
PUBLIC_DIR = DATA_ROOT / "used_phone_agent_pilot_v1"
HIDDEN_DIR = DATA_ROOT / "used_phone_agent_pilot_ai_judged_freeze_v1"
TERMINAL_SCORE_REPORT = (
    DATA_ROOT
    / "evaluation_runs"
    / "used_phone_model_composition_deepseek_baseline_20260810_v2_score"
    / "report.json"
)


def _minimal_prediction(case_id: str) -> dict[str, object]:
    return {
        "schemaVersion": "used-phone-complex-prediction-v1",
        "reviewCaseId": case_id,
        "predictedAction": "CLARIFY",
        "extractedConstraints": [],
        "rankedCandidateIds": [],
        "candidateAssessments": [],
        "comparison": None,
        "stateUpdate": None,
        "answerClaims": [],
        "evidenceRefs": [],
        "provenance": {
            "producerName": "fixture-agent",
            "producerType": "agent",
            "evaluationOnly": True,
            "networkUsed": True,
            "modelUsed": True,
            "humanGold": False,
            "model": "fixture-model",
        },
    }


def _candidate(case_id: str, suffix: str, raw: list[str]) -> dict[str, object]:
    return {
        "candidateDisplayId": f"{case_id}-CAND-{suffix}",
        "candidate": {"title": "fixture"},
        "evidenceRefs": [{"field": "normalizedAttrValues", "rawValue": raw}],
    }


def _citation(candidate: dict[str, object], fact_group: str, value: str, evidence_ref_id: str | None = None) -> dict[str, object]:
    ref = candidate["evidenceRefs"][0]  # type: ignore[index]
    citation: dict[str, object] = {
        "candidateDisplayId": candidate["candidateDisplayId"],
        "evidenceRefIndex": 0,
        "field": "normalizedAttrValues",
        "rawValueSha256": hashlib.sha256(canonical_json_bytes(ref["rawValue"])).hexdigest(),  # type: ignore[index]
        "factGroup": fact_group,
        "normalizedValue": value,
    }
    if evidence_ref_id is not None:
        citation["evidenceRefId"] = evidence_ref_id
    return citation


def _one_case_bundle(case_id: str, candidates: list[dict[str, object]], visible: list[dict[str, str]] | None = None) -> PublicBundle:
    case = PublicCase(
        review_case_id=case_id,
        query_messages=({"role": "user", "text": "fixture"},),
        user_visible_candidate_context=tuple(visible or []),
        candidates=tuple(candidates),
        raw={"reviewCaseId": case_id},
    )
    return PublicBundle(directory=Path("."), cases=(case,), input_sha256={})


def _require_real_data() -> None:
    if not (PUBLIC_DIR / "blind_cases_pending.jsonl").is_file():
        pytest.skip("locally generated frozen pilot data is not present")


def _write_terminal_fixture(
    root: Path, *, failed_case: str = "BLIND-CASE-008", all_failed: bool = False,
    include_manifest: bool = True, failure_model_calls: list[dict[str, object]] | None = None,
) -> None:
    for index in range(1, 9):
        case_id = f"BLIND-CASE-{index:03d}"
        attempt_dir = root / "cases" / case_id / "attempt-001"
        attempt_dir.mkdir(parents=True)
        if all_failed or case_id == failed_case:
            terminal = {
                "schemaVersion": "used-phone-agent-case-failure-v1",
                "terminalState": "failure",
                "reviewCaseId": case_id,
                "attempt": 1,
                "errorType": "FixtureFailure",
                "error": "fixture",
                "modelCalls": copy.deepcopy(failure_model_calls or []),
            }
            (attempt_dir / "failure.json").write_bytes(canonical_json_bytes(terminal))
            continue
        prediction = _minimal_prediction(case_id)
        prediction["provenance"]["projectionProtocolVersion"] = "v2-selection-materializer"  # type: ignore[index]
        trace = {
            "schemaVersion": "used-phone-agent-trace-v1",
            "reviewCaseId": case_id,
            "projectionProtocolVersion": "v2-selection-materializer",
            "modelCalls": [],
        }
        terminal = {
            "schemaVersion": "used-phone-agent-case-success-v1",
            "terminalState": "success",
            "attempt": 1,
            "predictionSha256": hashlib.sha256(canonical_json_bytes(prediction)).hexdigest(),
            "traceSha256": hashlib.sha256(canonical_json_bytes(trace)).hexdigest(),
            "prediction": prediction,
            "trace": trace,
        }
        (attempt_dir / "success.json").write_bytes(canonical_json_bytes(terminal))
    if include_manifest:
        (root / "manifest.json").write_bytes(canonical_json_bytes({
            "attempt": 1,
            "projectionProtocolVersion": "v2-selection-materializer",
            "model": "fixture-model",
            "modelUsed": True,
            "networkUsed": True,
        }))


def test_public_loader_enforces_pinned_opaque_envelope() -> None:
    _require_real_data()
    bundle = load_public_bundle(PUBLIC_DIR)
    assert len(bundle.cases) == 8
    assert {case.review_case_id: len(case.candidates) for case in bundle.cases} == EXPECTED_CASE_COUNTS
    case006 = bundle.by_id()["BLIND-CASE-006"]
    assert case006.user_visible_candidate_context == (
        {"candidateDisplayId": "BLIND-CASE-006-CAND-3a3ac1826cba", "label": "先前商品"},
    )


def test_public_modules_have_no_hidden_scorer_or_label_imports() -> None:
    evaluation = REPO_ROOT / "agent" / "evaluation"
    for name in ("used_phone_offline_runtime.py", "used_phone_complex_pilot.py"):
        source = (evaluation / name).read_text(encoding="utf-8")
        tree = ast.parse(source)
        imports = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imports.append(node.module or "")
        assert not any("used_phone_complex_metrics" in item for item in imports)
        assert not any(item in {"glob", "os"} for item in imports)
        forbidden_calls = {
            node.func.attr
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        }
        assert not ({"glob", "rglob", "walk", "getenv"} & forbidden_calls)
        assert "ai_judged_candidate_qrel_v1" not in source
        assert "ai_judged_case_action_v1" not in source
        assert "ai_judged_query_constraints_v1" not in source


def test_public_prediction_is_byte_invariant_to_fake_hidden_sentinel(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _require_real_data()
    # Make accidental networking fail immediately for the entire public run.
    def blocked_socket(*args: object, **kwargs: object) -> socket.socket:
        raise AssertionError("network access is forbidden in offline pilot tests")

    monkeypatch.setattr(socket, "socket", blocked_socket)
    copied_public = tmp_path / "public"
    copied_public.mkdir()
    for name in ("blind_cases_pending.jsonl", "blind_candidates_pending.jsonl"):
        shutil.copyfile(PUBLIC_DIR / name, copied_public / name)
    sentinel = copied_public / "fake_hidden_label_sentinel.test-only.json"
    first = tmp_path / "first.jsonl"
    second = tmp_path / "second.jsonl"
    try:
        sentinel.write_text('{"grade":0}\n', encoding="utf-8", newline="\n")
        run_public_floor(
            public_bundle_dir=copied_public,
            floor_name="public_rule_floor",
            predictions_path=first,
            manifest_path=tmp_path / "first.manifest.json",
        )
        sentinel.write_text('{"grade":3,"eligible":true}\n', encoding="utf-8", newline="\n")
        run_public_floor(
            public_bundle_dir=copied_public,
            floor_name="public_rule_floor",
            predictions_path=second,
            manifest_path=tmp_path / "second.manifest.json",
        )
    finally:
        sentinel.unlink(missing_ok=True)
    assert first.read_bytes() == second.read_bytes()


def test_bm25_floor_does_not_fabricate_action_or_constraints(tmp_path: Path) -> None:
    _require_real_data()
    predictions = tmp_path / "bm25.jsonl"
    run_public_floor(
        public_bundle_dir=PUBLIC_DIR,
        floor_name="bm25_text_floor",
        predictions_path=predictions,
        manifest_path=tmp_path / "bm25.manifest.json",
    )
    rows = [json.loads(line) for line in predictions.read_text(encoding="utf-8").splitlines()]
    assert all(row["predictedAction"] == "NO_ACTION_PREDICTION" for row in rows)
    assert all(row["extractedConstraints"] == [] for row in rows)


def test_rule_floor_and_hidden_score_are_byte_deterministic(tmp_path: Path) -> None:
    _require_real_data()
    if not (HIDDEN_DIR / "manifest.json").is_file():
        pytest.skip("locally generated hidden freeze is not present")
    outputs: list[tuple[bytes, bytes, bytes, bytes]] = []
    for iteration in (1, 2):
        iteration_dir = tmp_path / str(iteration)
        iteration_dir.mkdir()
        prediction = iteration_dir / "prediction.jsonl"
        run_manifest = iteration_dir / "run.manifest.json"
        report = iteration_dir / "score.json"
        score_manifest = iteration_dir / "score.manifest.json"
        run_public_floor(
            public_bundle_dir=PUBLIC_DIR,
            floor_name="public_rule_floor",
            predictions_path=prediction,
            manifest_path=run_manifest,
        )
        score = score_hidden(
            public_bundle_dir=PUBLIC_DIR,
            hidden_freeze_dir=HIDDEN_DIR,
            predictions_path=prediction,
            report_path=report,
            manifest_path=score_manifest,
        )
        assert score["action"]["exact"]["accuracy"] == 1.0
        assert score["comparison"]["preferredCandidateExact"] is True
        assert score["comparison"]["candidateValueAccuracy"] == 1.0
        assert score["multiTurn"]["finalStateExact"] is True
        assert score["actionContractCompliance"]["accuracy"] == 1.0
        assert score["actionAndRequiredBehaviorContract"]["accuracy"] == 1.0
        assert score["evidenceCitation"]["groundingGate"]["macroPassRate"] == 1.0
        assert score["ranking"]["perCase"]["BLIND-CASE-004"] is None
        run_manifest_value = json.loads(run_manifest.read_text(encoding="utf-8"))
        score_manifest_value = json.loads(score_manifest.read_text(encoding="utf-8"))
        assert run_manifest_value["predictionSha256"] == hashlib.sha256(prediction.read_bytes()).hexdigest()
        assert score_manifest_value["scoreReportSha256"] == hashlib.sha256(report.read_bytes()).hexdigest()
        outputs.append((prediction.read_bytes(), run_manifest.read_bytes(), report.read_bytes(), score_manifest.read_bytes()))
    assert outputs[0] == outputs[1]


def test_local_schemas_are_valid_json() -> None:
    schema_dir = REPO_ROOT / "agent" / "evaluation" / "schemas"
    for name in ("used_phone_complex_prediction_v1.schema.json", "used_phone_complex_hidden_score_v1.schema.json"):
        schema = json.loads((schema_dir / name).read_text(encoding="utf-8"))
        assert schema["$schema"].endswith("2020-12/schema")


def test_fixture_schema_accepts_real_agent_provenance_and_rejects_fabricated_value(tmp_path: Path) -> None:
    predictions = [_minimal_prediction(f"BLIND-CASE-{index:03d}") for index in range(1, 9)]
    path = tmp_path / "agent.jsonl"
    path.write_bytes(b"".join(canonical_json_bytes(row) for row in predictions))
    assert len(_load_predictions(path)) == 8

    candidate = _candidate("BLIND-CASE-001", "aaaaaaaaaaaa", ["90%+", "iOS"])
    fabricated = copy.deepcopy(predictions)
    fabricated[0]["evidenceRefs"] = [_citation(candidate, "battery_health", "FABRICATED_VALUE", "evidence-001")]
    fabricated[0]["answerClaims"] = [{
        "claimId": "claim-001",
        "claimType": "candidate_fact",
        "candidateDisplayId": candidate["candidateDisplayId"],
        "factGroup": "battery_health",
        "normalizedValue": "FABRICATED_VALUE",
        "text": f"candidate_fact:{candidate['candidateDisplayId']}:battery_health=FABRICATED_VALUE",
        "evidenceRefIds": ["evidence-001"],
    }]
    path.write_bytes(b"".join(canonical_json_bytes(row) for row in fabricated))
    with pytest.raises(HiddenScoringError, match="schema violation"):
        _load_predictions(path)


def test_fixture_semantic_evidence_binding_defeats_adversarial_claims() -> None:
    candidate = _candidate("BLIND-CASE-001", "aaaaaaaaaaaa", ["90%+", "iOS"])
    bundle = _one_case_bundle("BLIND-CASE-001", [candidate])
    evidence_ref = _citation(candidate, "battery_health", "90_plus", "evidence-001")
    prediction = _minimal_prediction("BLIND-CASE-001")
    prediction["evidenceRefs"] = [evidence_ref]
    duplicate_citation = dict(evidence_ref)
    duplicate_citation.pop("evidenceRefId")
    prediction["candidateAssessments"] = [{"evidenceCitations": [duplicate_citation]}]
    prediction["answerClaims"] = [{
        "claimId": "claim-001",
        "claimType": "candidate_fact",
        "candidateDisplayId": candidate["candidateDisplayId"],
        "factGroup": "battery_health",
        "normalizedValue": "90_plus",
        "text": f"candidate_fact:{candidate['candidateDisplayId']}:battery_health=90_plus",
        "evidenceRefIds": ["evidence-001"],
    }]
    baseline = _evidence_metrics({"BLIND-CASE-001": prediction}, bundle)
    assert baseline["total"] == 1
    assert baseline["groundingGate"]["macroPassRate"] == 1.0

    fabricated = copy.deepcopy(prediction)
    fabricated["evidenceRefs"][0]["normalizedValue"] = "FABRICATED_VALUE"
    fabricated["answerClaims"][0]["normalizedValue"] = "FABRICATED_VALUE"
    fabricated["answerClaims"][0]["text"] = f"candidate_fact:{candidate['candidateDisplayId']}:battery_health=FABRICATED_VALUE"
    assert _evidence_metrics({"BLIND-CASE-001": fabricated}, bundle)["groundingGate"]["macroPassRate"] < 1.0

    unrelated = copy.deepcopy(prediction)
    unrelated["evidenceRefs"] = [_citation(candidate, "os", "ios", "evidence-001")]
    assert _evidence_metrics({"BLIND-CASE-001": unrelated}, bundle)["groundingGate"]["macroPassRate"] < 1.0

    unverifiable = copy.deepcopy(prediction)
    unverifiable["answerClaims"][0].update({
        "factGroup": "hidden_defects",
        "normalizedValue": "no_failure_for_two_years",
        "text": f"candidate_fact:{candidate['candidateDisplayId']}:hidden_defects=no_failure_for_two_years",
    })
    assert _evidence_metrics({"BLIND-CASE-001": unverifiable}, bundle)["groundingGate"]["macroPassRate"] < 1.0


def test_fixture_evidence_and_claims_are_case_scoped() -> None:
    candidate_001 = _candidate("BLIND-CASE-001", "aaaaaaaaaaaa", ["90%+"])
    candidate_002 = _candidate("BLIND-CASE-002", "bbbbbbbbbbbb", ["90%+"])
    case_001 = _one_case_bundle("BLIND-CASE-001", [candidate_001]).cases[0]
    case_002 = _one_case_bundle("BLIND-CASE-002", [candidate_002]).cases[0]
    bundle = PublicBundle(Path("."), (case_001, case_002), {})
    prediction = _minimal_prediction("BLIND-CASE-001")
    prediction["evidenceRefs"] = [_citation(candidate_002, "battery_health", "90_plus", "evidence-001")]
    prediction["answerClaims"] = [{
        "claimId": "claim-001",
        "claimType": "candidate_fact",
        "candidateDisplayId": candidate_002["candidateDisplayId"],
        "factGroup": "battery_health",
        "normalizedValue": "90_plus",
        "text": f"candidate_fact:{candidate_002['candidateDisplayId']}:battery_health=90_plus",
        "evidenceRefIds": ["evidence-001"],
    }]
    metrics = _evidence_metrics({"BLIND-CASE-001": prediction}, bundle)
    assert metrics["accuracy"] == 0.0
    assert metrics["claimSemanticAccuracy"] == 0.0
    assert metrics["groundingGate"]["macroPassRate"] == 0.0


def test_fixture_comparison_requires_dimension_bound_values_and_citations() -> None:
    candidate_a = _candidate("BLIND-CASE-005", "aaaaaaaaaaaa", ["90%+", "iOS"])
    candidate_b = _candidate("BLIND-CASE-005", "bbbbbbbbbbbb", ["80%-90%", "iOS"])
    bundle = _one_case_bundle(
        "BLIND-CASE-005",
        [candidate_a, candidate_b],
        [
            {"label": "候选 A", "candidateDisplayId": str(candidate_a["candidateDisplayId"])},
            {"label": "候选 B", "candidateDisplayId": str(candidate_b["candidateDisplayId"])},
        ],
    )
    actions = [{
        "reviewCaseId": "BLIND-CASE-005",
        "comparisonSupport": {"recommendedCandidateDisplayId": candidate_b["candidateDisplayId"]},
    }]
    constraints = [{
        "reviewCaseId": "BLIND-CASE-005",
        "formalContracts": {"comparisonContract": {"dimensions": ["battery_health"]}},
    }]
    prediction = _minimal_prediction("BLIND-CASE-005")
    prediction["comparison"] = {
        "preferredCandidateDisplayId": candidate_b["candidateDisplayId"],
        "fieldComparisons": [{
            "field": "battery_health",
            "candidateValues": {
                candidate_a["candidateDisplayId"]: "ios",
                candidate_b["candidateDisplayId"]: "ios",
            },
            "evidenceCitations": [
                _citation(candidate_a, "os", "ios"),
                _citation(candidate_b, "os", "ios"),
            ],
        }],
    }
    metrics = _comparison_metrics({"BLIND-CASE-005": prediction}, actions, constraints, bundle)
    assert metrics["candidateValueAccuracy"] == 0.0
    assert metrics["fieldEvidenceCoverage"] == 0.0


def test_fixture_action_contract_blocks_empty_retrieval_products_and_anchor() -> None:
    cases: list[PublicCase] = []
    predictions: dict[str, dict[str, object]] = {}
    actions: list[dict[str, object]] = []
    for index in range(1, 9):
        case_id = f"BLIND-CASE-{index:03d}"
        candidates = [_candidate(case_id, f"{number:012x}", ["iOS", "90%+"]) for number in range(1, 7)]
        visible: list[dict[str, str]] = []
        if case_id == "BLIND-CASE-006":
            visible = [{"label": "先前商品", "candidateDisplayId": str(candidates[0]["candidateDisplayId"])}]
        if case_id == "BLIND-CASE-005":
            visible = [
                {"label": "候选 A", "candidateDisplayId": str(candidates[0]["candidateDisplayId"])},
                {"label": "候选 B", "candidateDisplayId": str(candidates[1]["candidateDisplayId"])},
            ]
        cases.append(PublicCase(case_id, ({"role": "user", "text": "fixture"},), tuple(visible), tuple(candidates), {}))
        predictions[case_id] = _minimal_prediction(case_id)
        expected = "RETRIEVE_FILTER_AND_RANK" if case_id in {"BLIND-CASE-001", "BLIND-CASE-002", "BLIND-CASE-003"} else "CLARIFY"
        action: dict[str, object] = {"reviewCaseId": case_id, "formalExpectedAction": expected}
        if case_id == "BLIND-CASE-005":
            action["formalExpectedAction"] = "COMPARE_WITH_FIELD_EVIDENCE"
            action["comparisonSupport"] = {"recommendedCandidateDisplayId": candidates[1]["candidateDisplayId"]}
        if case_id == "BLIND-CASE-006":
            action["formalExpectedAction"] = "RETRIEVE_SUBSTITUTES_RETAINING_CONSTRAINTS"
        if case_id == "BLIND-CASE-007":
            action["formalExpectedAction"] = "ABSTAIN_OR_EXPLAIN"
        if case_id == "BLIND-CASE-008":
            action["formalExpectedAction"] = "UPDATE_STATE_THEN_RETRIEVE"
        actions.append(action)
    bundle = PublicBundle(Path("."), tuple(cases), {})
    predictions["BLIND-CASE-004"]["rankedCandidateIds"] = [cases[3].candidates[0]["candidateDisplayId"]]
    predictions["BLIND-CASE-006"]["rankedCandidateIds"] = [cases[5].candidates[0]["candidateDisplayId"]]
    predictions["BLIND-CASE-007"]["rankedCandidateIds"] = [cases[6].candidates[0]["candidateDisplayId"]]
    metrics = _action_contract_compliance(predictions, actions, bundle)
    assert metrics["perCase"]["BLIND-CASE-001"]["applicableChecks"]["minimumFivePublicCandidatesReturned"] is False
    assert metrics["perCase"]["BLIND-CASE-004"]["applicableChecks"]["noProductsReturned"] is False
    assert metrics["perCase"]["BLIND-CASE-006"]["applicableChecks"]["substituteAnchorExcluded"] is False
    assert metrics["perCase"]["BLIND-CASE-007"]["applicableChecks"]["noProductsReturned"] is False


def test_fixture_required_behavior_contract_requires_label_and_subcontracts() -> None:
    action = {"perCase": {"BLIND-CASE-001": {"canonicalExact": False}}}
    behavior = {"perCase": {"BLIND-CASE-001": {"compliant": True}}}
    constraints = {"perCase": {"BLIND-CASE-001": {"exact": True}}}
    required = _action_and_required_behavior_contract(action, behavior, constraints, {}, {})
    assert required["accuracy"] == 0.0
    assert required["perCase"]["BLIND-CASE-001"]["checks"] == {
        "canonicalActionExact": False,
        "behaviorContractCompliant": True,
        "constraintAtomSetExact": True,
    }


def test_fixture_required_behavior_contract_gates_comparison_and_state() -> None:
    action = {
        "perCase": {
            "BLIND-CASE-005": {"canonicalExact": True},
            "BLIND-CASE-008": {"canonicalExact": True},
        }
    }
    behavior = {
        "perCase": {
            "BLIND-CASE-005": {"compliant": True, "applicableChecks": {"batteryTradeoffDisclosed": True}},
            "BLIND-CASE-008": {"compliant": True, "applicableChecks": {}},
        }
    }
    constraints = {"perCase": {"BLIND-CASE-008": {"exact": True}}}
    comparison = {
        "preferredCandidateExact": True,
        "candidateValueAccuracy": 0.0,
        "fieldEvidenceCoverage": 1.0,
    }
    state = {"initialStateExact": False, "turnDeltaExact": False, "finalStateExact": False}
    required = _action_and_required_behavior_contract(action, behavior, constraints, comparison, state)
    assert required["accuracy"] == 0.0
    assert required["perCase"]["BLIND-CASE-005"]["checks"]["comparisonCandidateValuesExact"] is False
    assert required["perCase"]["BLIND-CASE-008"]["checks"]["finalStateExact"] is False


def test_fixture_risk_rates_use_returned_denominator_and_empty_is_na() -> None:
    cases = []
    qrels = []
    predictions = {f"BLIND-CASE-{index:03d}": _minimal_prediction(f"BLIND-CASE-{index:03d}") for index in range(1, 9)}
    for case_id in ("BLIND-CASE-001", "BLIND-CASE-002", "BLIND-CASE-003", "BLIND-CASE-006", "BLIND-CASE-008"):
        candidate = _candidate(case_id, "aaaaaaaaaaaa", ["iOS"])
        visible = [{"label": "先前商品", "candidateDisplayId": str(candidate["candidateDisplayId"])}] if case_id == "BLIND-CASE-006" else []
        cases.append(PublicCase(case_id, ({"role": "user", "text": "fixture"},), tuple(visible), (candidate,), {}))
        qrels.append({
            "reviewCaseId": case_id,
            "candidateDisplayId": candidate["candidateDisplayId"],
            "relevanceGrade": 1,
            "eligible": False,
            "hardViolations": [{"group": "os"}],
            "hardUnknowns": [],
        })
    bundle = PublicBundle(Path("."), tuple(cases), {})
    metrics = _ranking_metrics(predictions, qrels, bundle)
    assert metrics["perCase"]["BLIND-CASE-001"]["hardViolationRate@5"] == 0.0
    assert metrics["perCase"]["BLIND-CASE-001"]["hardViolationRateAmongReturned@5"] is None
    predictions["BLIND-CASE-001"]["rankedCandidateIds"] = [qrels[0]["candidateDisplayId"]]
    metrics = _ranking_metrics(predictions, qrels, bundle)
    assert metrics["perCase"]["BLIND-CASE-001"]["hardViolationRate@5"] == 0.2
    assert metrics["perCase"]["BLIND-CASE-001"]["hardViolationRateAmongReturned@5"] == 1.0


def test_fixture_public_read_guard_and_unpinned_hidden_manifest(tmp_path: Path) -> None:
    with pytest.raises(PublicBundleError, match="denied"):
        assert_public_input_path(tmp_path / "ai_labels.jsonl", tmp_path)
    hidden = tmp_path / "hidden"
    hidden.mkdir()
    (hidden / "manifest.json").write_text('{"counts":{}}\n', encoding="utf-8", newline="\n")
    with pytest.raises(HiddenScoringError, match="unpinned"):
        _load_hidden(hidden)


def test_real_hidden_manifest_and_recomputed_internal_hash_tamper_is_rejected(tmp_path: Path) -> None:
    _require_real_data()
    if not (HIDDEN_DIR / "manifest.json").is_file():
        pytest.skip("locally generated hidden freeze is not present")
    copied = tmp_path / "hidden"
    copied.mkdir()
    for name in (
        "manifest.json",
        "ai_judged_candidate_qrel_v1.jsonl",
        "ai_judged_case_action_v1.jsonl",
        "ai_judged_query_constraints_v1.jsonl",
    ):
        shutil.copyfile(HIDDEN_DIR / name, copied / name)
    qrel_path = copied / "ai_judged_candidate_qrel_v1.jsonl"
    qrel_rows = qrel_path.read_text(encoding="utf-8").splitlines()
    first = json.loads(qrel_rows[0])
    first["relevanceGrade"] = 0
    qrel_rows[0] = json.dumps(first, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    qrel_path.write_text("\n".join(qrel_rows) + "\n", encoding="utf-8", newline="\n")
    manifest_path = copied / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    for artifact in manifest["artifacts"]:
        if artifact["path"] == qrel_path.name:
            artifact["bytes"] = qrel_path.stat().st_size
            artifact["sha256"] = hashlib.sha256(qrel_path.read_bytes()).hexdigest()
    manifest_path.write_bytes(canonical_json_bytes(manifest))
    with pytest.raises(HiddenScoringError, match="unpinned"):
        _load_hidden(copied)


def test_terminal_fixture_uses_execution_mask_and_unconditional_action_denominator(tmp_path: Path) -> None:
    run_dir = tmp_path / "fixture-terminal-run"
    _write_terminal_fixture(run_dir)
    rows, mask, provenance = _load_terminal_run(run_dir, attempt=1)
    assert sum(mask.values()) == 7
    assert provenance["execution"]["coverage"] == 0.875
    assert provenance["terminalRecords"]["BLIND-CASE-008"]["errorType"] == "FixtureFailure"
    assert len(provenance["terminalRecords"]["BLIND-CASE-008"]["terminalSha256"]) == 64
    assert len(provenance["terminalRecords"]["BLIND-CASE-001"]["predictionSha256"]) == 64
    predictions = {row["reviewCaseId"]: row for row in rows}
    actions = [
        {
            "reviewCaseId": f"BLIND-CASE-{index:03d}",
            "formalExpectedAction": "CLARIFY" if index <= 2 else "RETRIEVE_FILTER_AND_RANK",
        }
        for index in range(1, 9)
    ]
    action = _action_metrics(predictions, actions)
    assert action["exact"] == {"correct": 2, "total": 8, "accuracy": 0.25}
    assert action["perCase"]["BLIND-CASE-008"]["predicted"] == "INVALID_OR_NO_ACTION"
    constraints = [
        {
            "reviewCaseId": f"BLIND-CASE-{index:03d}",
            "formalContracts": {"constraintContract": {"atoms": [{
                "group": "os", "operator": "IN", "allowedValues": ["ios"], "importance": "hard",
            }]}},
        }
        for index in range(1, 9)
    ]
    constraint = _constraint_metrics(predictions, constraints, mask)
    assert constraint["perCase"]["BLIND-CASE-008"]["predictedAtoms"] == 0
    assert constraint["perCase"]["BLIND-CASE-008"]["fn"] == 1
    assert constraint["perCase"]["BLIND-CASE-008"]["executionFailed"] is True


def test_terminal_fixture_rejects_double_and_missing_terminals(tmp_path: Path) -> None:
    double = tmp_path / "double"
    _write_terminal_fixture(double)
    source = double / "cases" / "BLIND-CASE-007" / "attempt-001" / "success.json"
    target = double / "cases" / "BLIND-CASE-008" / "attempt-001" / "success.json"
    target.write_bytes(source.read_bytes())
    with pytest.raises(HiddenScoringError, match="exactly one success/failure"):
        _load_terminal_run(double, attempt=1)

    missing = tmp_path / "missing"
    _write_terminal_fixture(missing)
    (missing / "cases" / "BLIND-CASE-008" / "attempt-001" / "failure.json").unlink()
    with pytest.raises(HiddenScoringError, match="exactly one success/failure"):
        _load_terminal_run(missing, attempt=1)


def test_terminal_fixture_rejects_bad_hash_and_v1_protocol(tmp_path: Path) -> None:
    bad_hash = tmp_path / "bad-hash"
    _write_terminal_fixture(bad_hash)
    path = bad_hash / "cases" / "BLIND-CASE-001" / "attempt-001" / "success.json"
    terminal = json.loads(path.read_text(encoding="utf-8"))
    terminal["predictionSha256"] = "0" * 64
    path.write_bytes(canonical_json_bytes(terminal))
    with pytest.raises(HiddenScoringError, match="stale success prediction hash"):
        _load_terminal_run(bad_hash, attempt=1)

    v1 = tmp_path / "v1"
    _write_terminal_fixture(v1)
    path = v1 / "cases" / "BLIND-CASE-001" / "attempt-001" / "success.json"
    terminal = json.loads(path.read_text(encoding="utf-8"))
    terminal["trace"]["projectionProtocolVersion"] = "v1-direct-schema"
    terminal["prediction"]["provenance"]["projectionProtocolVersion"] = "v1-direct-schema"
    terminal["predictionSha256"] = hashlib.sha256(canonical_json_bytes(terminal["prediction"])).hexdigest()
    terminal["traceSha256"] = hashlib.sha256(canonical_json_bytes(terminal["trace"])).hexdigest()
    path.write_bytes(canonical_json_bytes(terminal))
    with pytest.raises(HiddenScoringError, match="incompatible success projection protocol"):
        _load_terminal_run(v1, attempt=1)


def _eight_case_ranking_fixture() -> tuple[PublicBundle, list[dict[str, object]]]:
    cases: list[PublicCase] = []
    qrels: list[dict[str, object]] = []
    retrieval = {"BLIND-CASE-001", "BLIND-CASE-002", "BLIND-CASE-003", "BLIND-CASE-006", "BLIND-CASE-008"}
    for index in range(1, 9):
        case_id = f"BLIND-CASE-{index:03d}"
        candidates = tuple(
            _candidate(case_id, f"{candidate_index:012x}", ["iOS"])
            for candidate_index in range(1, 17)
        )
        if index == 5:
            visible = (
                {"candidateDisplayId": candidates[0]["candidateDisplayId"], "label": "\u5019\u9009 A"},
                {"candidateDisplayId": candidates[1]["candidateDisplayId"], "label": "\u5019\u9009 B"},
            )
        elif index == 6:
            visible = ({"candidateDisplayId": candidates[0]["candidateDisplayId"], "label": "\u5148\u524d\u5546\u54c1"},)
        else:
            visible = ()
        cases.append(PublicCase(
            review_case_id=case_id,
            query_messages=({"role": "user", "text": "fixture"},),
            user_visible_candidate_context=visible,
            candidates=candidates,
            raw={"reviewCaseId": case_id},
        ))
        if case_id in retrieval:
            for candidate_index, candidate in enumerate(candidates):
                qrels.append({
                    "reviewCaseId": case_id,
                    "candidateDisplayId": candidate["candidateDisplayId"],
                    "relevanceGrade": 1,
                    "eligible": True,
                    "hardViolations": ["fixture"] if case_id == "BLIND-CASE-002" and candidate_index == 0 else [],
                    "hardUnknowns": [],
                })
    return PublicBundle(directory=Path("."), cases=tuple(cases), input_sha256={}), qrels


def test_failed_retrieval_quality_is_zero_but_risk_is_na() -> None:
    bundle, qrels = _eight_case_ranking_fixture()
    predictions = {
        case_id: _minimal_prediction(case_id)
        for case_id in (f"BLIND-CASE-{index:03d}" for index in range(1, 9))
    }
    predictions["BLIND-CASE-002"]["rankedCandidateIds"] = [qrels[16]["candidateDisplayId"]]
    mask = {case_id: True for case_id in predictions}
    mask["BLIND-CASE-001"] = False
    ranking = _ranking_metrics(predictions, qrels, bundle, mask)
    failed = ranking["perCase"]["BLIND-CASE-001"]
    assert failed["ndcg@5"] == 0.0
    assert failed["eligiblePrecision@5"] == 0.0
    assert failed["hardViolationRate@5"] is None
    assert failed["hardViolationRateAmongReturned@5"] is None
    assert failed["riskStatus"] == "not_applicable_due_execution_failure"
    assert ranking["macroApplicableOnly"]["hardViolationRateAmongReturned@5"] == 1.0
    evidence = _evidence_metrics(predictions, bundle, mask)
    assert evidence["perCase"]["BLIND-CASE-001"]["groundingGateStatus"] == "not_applicable_due_execution_failure"


def test_all_failed_execution_scores_zero_action_behavior_and_constraint_exact(tmp_path: Path) -> None:
    run_dir = tmp_path / "all-failed"
    _write_terminal_fixture(run_dir, all_failed=True, include_manifest=False)
    rows, mask, _provenance = _load_terminal_run(run_dir, attempt=1)
    predictions = {row["reviewCaseId"]: row for row in rows}
    bundle, _qrels = _eight_case_ranking_fixture()
    actions: list[dict[str, object]] = []
    for index in range(1, 9):
        case_id = f"BLIND-CASE-{index:03d}"
        action: dict[str, object] = {
            "reviewCaseId": case_id,
            "formalExpectedAction": "CLARIFY",
        }
        if index == 5:
            action["comparisonSupport"] = {
                "recommendedCandidateDisplayId": bundle.by_id()[case_id].candidates[1]["candidateDisplayId"],
            }
        actions.append(action)
    action_metric = _action_metrics(predictions, actions)
    behavior = _action_contract_compliance(predictions, actions, bundle, mask)
    constraints = [
        {
            "reviewCaseId": f"BLIND-CASE-{index:03d}",
            "formalContracts": {"constraintContract": {"atoms": []}},
        }
        for index in range(1, 9)
    ]
    constraint = _constraint_metrics(predictions, constraints, mask)
    assert action_metric["exact"] == {"correct": 0, "total": 8, "accuracy": 0.0}
    assert behavior["correct"] == 0 and behavior["total"] == 8
    assert constraint["caseExact"] == {"correct": 0, "total": 8, "accuracy": 0.0}
    assert all(row["executionFailed"] and row["fp"] == 0 for row in constraint["perCase"].values())


def test_failure_model_calls_supply_input_provenance_without_run_manifest(tmp_path: Path) -> None:
    with_calls = tmp_path / "with-calls"
    _write_terminal_fixture(
        with_calls,
        all_failed=True,
        include_manifest=False,
        failure_model_calls=[{"model": "fixture-failure-model", "ok": False}],
    )
    _rows, _mask, provenance = _load_terminal_run(with_calls, attempt=1)
    assert provenance["runManifestSha256"] is None
    assert provenance["inputModelProvenance"]["models"] == ["fixture-failure-model"]
    assert provenance["inputModelProvenance"]["modelUsed"] is True
    assert provenance["inputModelProvenance"]["networkUsed"] is True

    without_calls = tmp_path / "without-calls"
    _write_terminal_fixture(without_calls, all_failed=True, include_manifest=False)
    _rows, _mask, provenance = _load_terminal_run(without_calls, attempt=1)
    assert provenance["inputModelProvenance"]["models"] == []
    assert provenance["inputModelProvenance"]["modelUsed"] is False
    assert provenance["inputModelProvenance"]["networkUsed"] is False
    assert "false means none" in provenance["inputModelProvenance"]["policy"]


def test_execution_semantic_invariants_reject_inconsistent_counts() -> None:
    per_case = {
        f"BLIND-CASE-{index:03d}": {"succeeded": index <= 7, "status": "success"}
        for index in range(1, 9)
    }
    invalid = {
        "execution": {
            "total": 8,
            "succeeded": 7,
            "failed": 0,
            "coverage": 0.875,
            "perCase": per_case,
        }
    }
    with pytest.raises(HiddenScoringError, match="do not sum"):
        _validate_report_invariants(invalid)


def test_hidden_score_schema_rejects_bogus_missing_and_wrong_total_core_contracts() -> None:
    if not TERMINAL_SCORE_REPORT.is_file():
        pytest.skip("real terminal score report is unavailable")
    schema_path = (
        REPO_ROOT / "agent" / "evaluation" / "schemas" / "used_phone_complex_hidden_score_v1.schema.json"
    )
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    validator = Draft202012Validator(schema)
    report = json.loads(TERMINAL_SCORE_REPORT.read_text(encoding="utf-8"))
    validator.validate(report)

    bogus = copy.deepcopy(report)
    bogus["actionAndRequiredBehaviorContract"] = {"bogus": True}
    with pytest.raises(ValidationError):
        validator.validate(bogus)

    missing = copy.deepcopy(report)
    del missing["actionContractCompliance"]["policy"]
    with pytest.raises(ValidationError):
        validator.validate(missing)

    wrong_total = copy.deepcopy(report)
    wrong_total["actionAndRequiredBehaviorContract"]["total"] = 7
    with pytest.raises(ValidationError):
        validator.validate(wrong_total)
