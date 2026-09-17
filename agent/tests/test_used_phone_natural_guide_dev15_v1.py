from __future__ import annotations

import asyncio
from copy import deepcopy
import hashlib
import json
from pathlib import Path

import pytest

from agent.evaluation import used_phone_natural_guide_dev15_evaluator_v1 as evaluator
from agent.evaluation import used_phone_natural_guide_dev15_runner_v1 as runner
from agent.evaluation import used_phone_natural_guide_dev15_runner_v2 as runner_v2


ASSET_DIR = (
    Path(__file__).resolve().parents[2]
    / ".agents"
    / "evaluation-assets"
    / "used-phone-natural-guide-dev15-20260814"
)
CASES_PATH = ASSET_DIR / "cases_public.jsonl"
PREREG_PATH = ASSET_DIR / "dev15_preregistration_v1.json"


def _write(path: Path, payload: bytes) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return hashlib.sha256(payload).hexdigest()


def _synthetic_catalog(tmp_path: Path) -> tuple[Path, dict[str, dict]]:
    fixed = ["1086995", "1092120", "1068548", "1092202"]
    item_ids = fixed + [str(2_000_000 + index) for index in range(248)]
    rows = []
    by_id = {}
    values = {
        "battery_health": "90_plus",
        "battery_originality": "original",
        "motherboard_repair": "not_repaired",
        "os": "ios",
        "scratch_level": "none",
        "screen_originality": "original",
        "shell_condition": "normal",
    }
    for line_number, item_id in enumerate(item_ids, 1):
        attributes = {}
        for group, value in values.items():
            attributes[group] = {
                "key": group,
                "status": "known",
                "value": value,
                "evidenceRefs": [{
                    "source": "relevance",
                    "field": "attr_value",
                    "lineNumber": line_number,
                    "rawValue": f"{group}={value}",
                }],
            }
        row = {"itemId": item_id, "attributes": attributes}
        rows.append(row)
        by_id[item_id] = row
    path = tmp_path / "catalog.jsonl"
    _write(path, b"".join(evaluator.canonical_bytes(row) for row in rows))
    return path, by_id


def _synthetic_preregistration(tmp_path: Path, catalog_sha: str) -> Path:
    value = json.loads(PREREG_PATH.read_text(encoding="utf-8"))
    value["inputIdentity"]["publicCatalogSha256"] = catalog_sha
    path = tmp_path / "dev15_preregistration_v1.json"
    _write(path, evaluator.canonical_bytes(value))
    return path


def _citation(item_id: str, group: str, catalog: dict[str, dict]) -> dict:
    ref = catalog[item_id]["attributes"][group]["evidenceRefs"][0]
    return {"itemId": item_id, "group": group, **ref}


def _build_authenticated_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> tuple[Path, Path, Path, Path]:
    catalog_path, catalog = _synthetic_catalog(tmp_path)
    catalog_sha = evaluator.sha256_file(catalog_path)
    prereg_path = _synthetic_preregistration(tmp_path, catalog_sha)
    monkeypatch.setattr(evaluator, "PUBLIC_CATALOG_SHA256", catalog_sha)
    monkeypatch.setattr(evaluator, "PREREGISTRATION_SHA256", evaluator.sha256_file(prereg_path))
    prereg = json.loads(prereg_path.read_text(encoding="utf-8"))
    expected = {row["caseId"]: row for row in prereg["cases"]}
    run_dir = tmp_path / "run-001"
    predictions = []
    traces = {}
    for case_id in evaluator.CASE_IDS:
        row = expected[case_id]
        action = row["allowedActions"][0]
        predicted_constraints = {
            importance: [
                {**deepcopy(atom), "importance": importance}
                for atom in row["expectedFinalConstraints"][importance]
            ]
            for importance in ("hard", "soft")
        }
        prediction = {
            "caseId": case_id,
            "rankedItemIds": [],
            "predictedAction": action,
            "predictedConstraints": predicted_constraints,
            "evidenceCitations": [],
        }
        tool_traces = []
        tool = row["expectedTool"]
        if tool == "search_products":
            ranked = [] if case_id == "UPNG-D09" else ["2000000"]
            prediction["rankedItemIds"] = ranked
            tool_traces = [{
                "tool": tool, "ok": True, "publicTurnIndex": 1 if case_id in {"UPNG-D10", "UPNG-D11", "UPNG-D12"} else 0,
                "detail": {"candidateIds": [int(item) for item in ranked]},
            }]
            if row.get("minimumCitationCount"):
                group = row["expectedFinalConstraints"]["hard"][0]["group"]
                prediction["evidenceCitations"] = [_citation("2000000", group, catalog)]
        elif tool == "compare_products":
            pair = row["expectedComparedIds"]
            prediction["comparison"] = {"candidateItemIds": pair}
            tool_traces = [{
                "tool": tool, "ok": True, "publicTurnIndex": 0,
                "detail": {"products": [{"product": {"id": int(item)}} for item in pair]},
            }]
            prediction["evidenceCitations"] = [_citation(pair[0], "battery_health", catalog)]
        if case_id in {"UPNG-D10", "UPNG-D11", "UPNG-D12"}:
            prediction["predictedState"] = deepcopy(predicted_constraints)
        predictions.append(prediction)
        turns = [{"text": "turn one"}]
        if case_id in {"UPNG-D10", "UPNG-D11", "UPNG-D12"}:
            turns.append({"text": "turn two"})
        traces[case_id] = {
            "caseId": case_id,
            "protocolVersion": evaluator.PROTOCOL_VERSION,
            "turns": turns,
            "toolTraces": tool_traces,
            "answers": [(
                "没有找到证据完整的商品，无法确认全部硬条件。"
                if case_id == "UPNG-D09"
                else "商品 1 与商品 2 的电池、屏幕和主板证据对比完成。"
                if case_id in {"UPNG-D13", "UPNG-D14"}
                else "fixture answer"
            )],
            "businessWriteNetworkUsed": False,
        }

    runner_hashes = deepcopy(evaluator.BASELINE_RUNNER_HASHES)
    contract = {
        "protocolVersion": evaluator.PROTOCOL_VERSION,
        "publicCasesSha256": evaluator.PUBLIC_CASES_SHA256,
        "publicCatalogSha256": catalog_sha,
        "productionCodeScopeSha256": evaluator.PRODUCTION_CODE_SCOPE_SHA256,
        "runnerCodeSha256": runner_hashes,
        "attributeContractCodeSha256": "545c4f7d528ed9875dd468d9d9bbf5e4e6034b6cdbcf0860800bfc2b4b18688a",
        "attributeRuleset": "used-phone-exact-token-seven-field-v2",
        "javaCatalog": deepcopy(evaluator.EXPECTED_JAVA_CATALOG),
        "model": "fixture-model",
        "modelEndpoint": "https://api.deepseek.com",
        "productionRuntimeConfig": deepcopy(evaluator.EXPECTED_RUNTIME_CONFIG),
        "timeoutSeconds": 90.0,
        "caseCount": 15,
        "selectedCaseIds": list(evaluator.CASE_IDS),
        "selectedSplits": ["dev"],
        "allowSealedTest": False,
    }
    contract_sha = hashlib.sha256(evaluator.canonical_bytes(contract)).hexdigest()
    _write(run_dir / "run_contract.json", evaluator.canonical_bytes({
        "schemaVersion": "used-phone-public-agent-run-contract-v1",
        "contractSha256": contract_sha,
        "contract": contract,
    }))
    prediction_sha = _write(
        run_dir / "predictions.jsonl",
        b"".join(evaluator.canonical_bytes(row) for row in predictions),
    )
    for prediction in predictions:
        case_id = prediction["caseId"]
        attempt = run_dir / "cases" / case_id / "attempt-001"
        prediction_file_sha = _write(
            attempt / "prediction.json", evaluator.canonical_bytes(prediction),
        )
        trace_sha = _write(attempt / "trace.json", evaluator.canonical_bytes(traces[case_id]))
        terminal = {
            "terminalState": "success",
            "caseId": case_id,
            "attempt": 1,
            "protocolVersion": evaluator.PROTOCOL_VERSION,
            "contractSha256": contract_sha,
            "businessWriteNetworkUsed": False,
            "modelCallCount": 1,
            "prediction": prediction,
            "predictionSha256": prediction_file_sha,
            "trace": traces[case_id],
            "traceSha256": trace_sha,
        }
        _write(attempt / "success.json", evaluator.canonical_bytes(terminal))
    manifest = {
        "contractSha256": contract_sha,
        "contract": contract,
        "selectedCaseIds": list(evaluator.CASE_IDS),
        "selectedSplits": ["dev"],
        "execution": {case_id: True for case_id in evaluator.CASE_IDS},
        "failedCaseIds": [],
        "prediction": {"path": "predictions.jsonl", "rowCount": 15, "sha256": prediction_sha},
        "hiddenArtifactsRead": False,
        "businessWriteNetworkUsed": False,
    }
    _write(run_dir / "manifest.json", evaluator.canonical_bytes(manifest))
    return run_dir, catalog_path, prereg_path, run_dir / "manifest.json"


def test_frozen_public_assets_and_current_scope_match_runner_identity():
    assert evaluator.sha256_file(CASES_PATH) == runner.PUBLIC_CASES_SHA256
    assert evaluator.sha256_file(PREREG_PATH) == evaluator.PREREGISTRATION_SHA256
    rows = [json.loads(line) for line in CASES_PATH.read_text(encoding="utf-8").splitlines()]
    assert [row["caseId"] for row in rows] == list(runner.EXPECTED_CASE_IDS)
    assert runner.PRODUCTION_CODE_SCOPE_SHA256 == evaluator.PRODUCTION_CODE_SCOPE_SHA256
    assert runner_v2.PRODUCTION_CODE_SCOPE_SHA256 == evaluator.POSTFIX_PRODUCTION_CODE_SCOPE_SHA256
    assert evaluator.POSTFIX_RUNNER_HASHES["agent/evaluation/used_phone_natural_guide_dev15_runner_v2.py"] == "46fdee80d4621190207f8a35d9108eb236a754fdb8ff51393c8d8e3402e724f3"


def test_runner_rejects_partial_selection_before_calling_kernel(monkeypatch, tmp_path):
    called = False

    async def forbidden(**_kwargs):
        nonlocal called
        called = True
        return {}

    monkeypatch.setattr(runner._kernel, "run_public_agent", forbidden)
    with pytest.raises(runner.PublicRunnerError, match="complete ordered"):
        asyncio.run(runner.run_public_agent(
            public_cases_path=CASES_PATH,
            public_catalog_path=tmp_path / "catalog.jsonl",
            run_dir=tmp_path / "run",
            case_ids=["UPNG-D01"],
        ))
    assert called is False


def test_prediction_first_evaluator_accepts_complete_bound_fixture(monkeypatch, tmp_path):
    run_dir, catalog, prereg, manifest = _build_authenticated_run(tmp_path, monkeypatch)
    result = evaluator.score_dev15(
        predictions_path=run_dir / "predictions.jsonl",
        manifest_path=manifest,
        public_catalog_path=catalog,
        preregistration_path=prereg,
    )
    assert result["gatePassed"] is True
    assert result["failureClusters"] == {}
    assert all(value == 1.0 or value == 0 for value in result["metrics"].values())


def test_prediction_tamper_is_rejected_before_expectations(monkeypatch, tmp_path):
    run_dir, catalog, prereg, manifest = _build_authenticated_run(tmp_path, monkeypatch)
    payload = (run_dir / "predictions.jsonl").read_bytes()
    (run_dir / "predictions.jsonl").write_bytes(payload.replace(b"UPNG-D01", b"UPNG-D99", 1))
    opened = False

    def forbidden(_path):
        nonlocal opened
        opened = True
        raise AssertionError("expectations opened")

    monkeypatch.setattr(evaluator, "_load_preregistration", forbidden)
    with pytest.raises(ValueError, match="manifest public/no-write identity mismatch"):
        evaluator.score_dev15(
            predictions_path=run_dir / "predictions.jsonl",
            manifest_path=manifest,
            public_catalog_path=catalog,
            preregistration_path=prereg,
        )
    assert opened is False


def test_second_attempt_is_rejected_before_scoring(monkeypatch, tmp_path):
    run_dir, catalog, prereg, manifest = _build_authenticated_run(tmp_path, monkeypatch)
    (run_dir / "cases" / "UPNG-D01" / "attempt-002").mkdir()
    with pytest.raises(ValueError, match="unexpected attempt layout"):
        evaluator.score_dev15(
            predictions_path=run_dir / "predictions.jsonl",
            manifest_path=manifest,
            public_catalog_path=catalog,
            preregistration_path=prereg,
        )


def test_cross_item_or_unknown_citation_is_rejected():
    catalog = {
        "1": {"attributes": {"os": {"status": "unknown", "evidenceRefs": []}}},
        "2": {"attributes": {"os": {"status": "known", "evidenceRefs": [{
            "source": "relevance", "field": "attr_value", "lineNumber": 2, "rawValue": "ios",
        }]}}},
    }
    prediction = {
        "rankedItemIds": ["1"],
        "evidenceCitations": [{
            "itemId": "1", "group": "os", "source": "relevance",
            "field": "attr_value", "lineNumber": 2, "rawValue": "ios",
        }],
    }
    assert evaluator._citation_binding(
        prediction, {"expectedTool": "search_products", "minimumCitationCount": 1}, catalog,
    ) is False


def test_known_hard_violation_is_not_treated_as_unknown():
    catalog = {"1": {"attributes": {"os": {"status": "known", "value": "ios"}}}}
    assert evaluator._known_hard_violation(
        "1", [{"group": "os", "operator": "IN", "allowedValues": ["android"]}], catalog,
    ) is True


def test_hard_unknown_candidates_require_abstain_and_explicit_boundary():
    catalog = {"1": {"attributes": {
        "os": {"status": "known", "value": "android"},
        "battery_health": {"status": "known", "value": "90_plus"},
        "motherboard_repair": {"status": "unknown"},
    }}}
    expectation = {
        "requireNoHardViolationInRanked": True,
        "expectedFinalConstraints": {"hard": [
            {"group": "os", "operator": "IN", "allowedValues": ["android"]},
            {"group": "battery_health", "operator": "IN", "allowedValues": ["90_plus"]},
            {"group": "motherboard_repair", "operator": "IN", "allowedValues": ["repaired"]},
        ]},
    }
    terminal = {"trace": {"answers": ["没有找到证据完整的商品，不能把未知当作满足。"]}}
    assert evaluator._hard_support_boundary(
        {"rankedItemIds": ["1"], "predictedAction": "ABSTAIN_OR_EXPLAIN"},
        terminal, expectation, catalog,
    ) is True
    assert evaluator._hard_support_boundary(
        {"rankedItemIds": ["1"], "predictedAction": "RETRIEVE_FILTER_AND_RANK"},
        terminal, expectation, catalog,
    ) is False


def test_comparison_terminal_fallback_is_not_counted_as_completed_answer():
    expectation = {"expectedTool": "compare_products"}
    assert evaluator._answer_completion(
        {"trace": {"answers": ["抱歉，本轮执行结果没有通过可靠性校验，因此暂时不能据此回答。"]}},
        expectation,
    ) is False
    assert evaluator._answer_completion(
        {"trace": {"answers": ["商品 1 与商品 2 的电池、屏幕和主板证据对比完成。"]}},
        expectation,
    ) is True


def test_unknown_product_cannot_win_comparison_as_zero_risk():
    expectation = {"expectedTool": "compare_products"}
    bad = {"trace": {"answers": [
        "商品 1068548（风险标记 0 项）商品 1092202（风险标记 1 项）按已验证风险标记，商品 1068548 风险更低。"
    ]}}
    good = {"trace": {"answers": [
        "商品 1068548（未知/冲突 7 项，已知风险 0 项）商品 1092202（未知/冲突 0 项，已知风险 1 项）按证据缺口优先、再比较已知风险，商品 1092202 更适合作为当前选择。"
    ]}}
    assert evaluator._unknown_risk_ordering(bad, expectation) is False
    assert evaluator._unknown_risk_ordering(good, expectation) is True


def test_binary_positive_and_negative_complement_constraints_are_equivalent():
    actual = {"hard": [{
        "group": "motherboard_repair", "operator": "IN",
        "allowedValues": ["not_repaired"], "importance": "hard",
    }], "soft": []}
    expected = {"hard": [{
        "group": "motherboard_repair", "operator": "NOT_IN",
        "allowedValues": ["repaired"],
    }], "soft": []}
    assert evaluator._constraints_equal(actual, expected) is True


def test_natural_apple_used_phone_maps_to_user_ios_constraint_only():
    from agent.app.llm import _explicit_used_phone_requirements

    requirements = _explicit_used_phone_requirements(
        "想找苹果二手机，电池健康九成以上，屏幕要原装。"
    )
    assert requirements["os"].value == "ios"
    assert requirements["os"].priority == "hard"
    assert requirements["os"].source == "user"


def test_natural_rejection_preserves_non_original_screen_exclusion():
    from agent.app.llm import (
        _explicit_used_phone_exclusions,
        _explicit_used_phone_requirements,
    )

    message = "我不接受非原装屏，外壳有点磕碰没关系。"
    assert _explicit_used_phone_exclusions(message) == {
        "screen_originality": ["non_original"]
    }
    assert "screen_originality" not in _explicit_used_phone_requirements(message)


def test_shared_natural_bonus_clause_marks_both_preferences_soft():
    from agent.app.llm import _explicit_used_phone_requirements

    requirements = _explicit_used_phone_requirements(
        "iOS 是硬条件；原装电池和无划痕只是加分项。"
    )
    assert requirements["os"].priority == "hard"
    assert requirements["battery_originality"].priority == "soft"
    assert requirements["scratch_level"].priority == "soft"


def test_natural_battery_nine_tenths_alias_remains_soft_when_requested():
    from agent.app.llm import _explicit_used_phone_requirements

    requirement = _explicit_used_phone_requirements(
        "电池九成以上最好，但不是硬门槛。"
    )["battery_health"]
    assert requirement.value == "90_plus"
    assert requirement.priority == "soft"
