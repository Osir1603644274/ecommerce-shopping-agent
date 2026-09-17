from __future__ import annotations

import asyncio
from copy import deepcopy
import hashlib
import json
from pathlib import Path

import pytest

from agent.evaluation import used_phone_natural_guide_validation20_evaluator_v1 as evaluator
from agent.evaluation import used_phone_natural_guide_validation20_runner_v1 as runner


ASSET_DIR = Path(__file__).resolve().parents[2] / ".agents" / "evaluation-assets" / "used-phone-natural-guide-validation20-20260814"
CASES_PATH = ASSET_DIR / "cases_public.jsonl"
PREREG_PATH = ASSET_DIR / "validation20_preregistration_v1.json"


def _write(path: Path, payload: bytes) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return hashlib.sha256(payload).hexdigest()


def _catalog(tmp_path: Path) -> tuple[Path, dict[str, dict]]:
    fixed = ["3934984", "1810261", "4194515", "3191410", "3956311", "413580"]
    item_ids = fixed + [str(3_000_000 + index) for index in range(246)]
    values = {"battery_health":"90_plus","battery_originality":"original","motherboard_repair":"not_repaired","os":"ios","scratch_level":"none","screen_originality":"original","shell_condition":"normal"}
    rows, by_id = [], {}
    for line, item_id in enumerate(item_ids, 1):
        attrs = {group:{"key":group,"status":"known","value":value,"evidenceRefs":[{"source":"relevance","field":"attr_value","lineNumber":line,"rawValue":f"{group}={value}"}]} for group,value in values.items()}
        row = {"itemId":item_id,"attributes":attrs}
        rows.append(row)
        by_id[item_id] = row
    for group in values:
        by_id["1810261"]["attributes"][group] = {"key":group,"status":"unknown","value":None,"evidenceRefs":[]}
    path = tmp_path / "catalog.jsonl"
    _write(path, b"".join(evaluator.canonical_bytes(row) for row in rows))
    return path, by_id


def _citation(item_id: str, group: str, catalog: dict[str, dict]) -> dict:
    return {"itemId":item_id,"group":group,**catalog[item_id]["attributes"][group]["evidenceRefs"][0]}


def _authenticated_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    catalog_path, catalog = _catalog(tmp_path)
    catalog_sha = evaluator.sha256_file(catalog_path)
    prereg = json.loads(PREREG_PATH.read_text(encoding="utf-8"))
    prereg["inputIdentity"]["publicCatalogSha256"] = catalog_sha
    prereg_path = tmp_path / "validation20_preregistration_v1.json"
    _write(prereg_path, evaluator.canonical_bytes(prereg))
    monkeypatch.setattr(evaluator, "PUBLIC_CATALOG_SHA256", catalog_sha)
    monkeypatch.setattr(evaluator, "PREREGISTRATION_SHA256", evaluator.sha256_file(prereg_path))
    expected = {row["caseId"]:row for row in prereg["cases"]}
    run_dir = tmp_path / evaluator.AUTHORIZED_RUN_DIRECTORY
    predictions, traces = [], {}
    multi = {f"UPNG-V{i:02d}" for i in range(11, 16)}
    for case_id in evaluator.CASE_IDS:
        expectation = expected[case_id]
        constraints = {importance:[{**deepcopy(atom),"importance":importance} for atom in expectation["expectedFinalConstraints"][importance]] for importance in ("hard","soft")}
        prediction = {"caseId":case_id,"rankedItemIds":[],"predictedAction":expectation["allowedActions"][0],"predictedConstraints":constraints,"evidenceCitations":[]}
        tool_traces = []
        if expectation["expectedTool"] == "search_products":
            ranked = [] if case_id == "UPNG-V10" else ["3000000"]
            prediction["rankedItemIds"] = ranked
            tool_traces = [{"tool":"search_products","ok":True,"publicTurnIndex":1 if case_id in multi else 0,"detail":{"candidateIds":[int(item) for item in ranked]}}]
            if expectation.get("minimumCitationCount"):
                group = expectation["expectedFinalConstraints"]["hard"][0]["group"]
                prediction["evidenceCitations"] = [_citation("3000000",group,catalog)]
        elif expectation["expectedTool"] == "compare_products":
            pair = expectation["expectedComparedIds"]
            prediction["comparison"] = {"candidateItemIds":pair}
            prediction["evidenceCitations"] = [_citation(pair[0],"battery_health",catalog)]
            tool_traces = [{"tool":"compare_products","ok":True,"publicTurnIndex":0,"detail":{"products":[{"product":{"id":int(item)}} for item in pair]}}]
        if case_id in multi:
            prediction["predictedState"] = deepcopy(constraints)
        predictions.append(prediction)
        traces[case_id] = {"caseId":case_id,"protocolVersion":evaluator.PROTOCOL_VERSION,"turns":[{"text":"one"}]+([{"text":"two"}] if case_id in multi else []),"toolTraces":tool_traces,"answers":["没有找到证据完整的商品，不能把未知当作满足。" if case_id == "UPNG-V10" else "商品证据已核验：电池、屏幕、主板和外观，未知项已标明。"],"businessWriteNetworkUsed":False}

    contract = {"protocolVersion":evaluator.PROTOCOL_VERSION,"publicCasesSha256":evaluator.PUBLIC_CASES_SHA256,"publicCatalogSha256":catalog_sha,"productionCodeScopeSha256":evaluator.PRODUCTION_CODE_SCOPE_SHA256,"runnerCodeSha256":deepcopy(evaluator.RUNNER_HASHES),"attributeContractCodeSha256":"545c4f7d528ed9875dd468d9d9bbf5e4e6034b6cdbcf0860800bfc2b4b18688a","attributeRuleset":"used-phone-exact-token-seven-field-v2","javaCatalog":deepcopy(evaluator._shared.EXPECTED_JAVA_CATALOG),"model":"fixture","modelEndpoint":"https://api.deepseek.com","productionRuntimeConfig":deepcopy(evaluator._shared.EXPECTED_RUNTIME_CONFIG),"timeoutSeconds":90.0,"caseCount":20,"selectedCaseIds":list(evaluator.CASE_IDS),"selectedSplits":["validation"],"allowSealedTest":False}
    contract_sha = hashlib.sha256(evaluator.canonical_bytes(contract)).hexdigest()
    _write(run_dir / "run_contract.json", evaluator.canonical_bytes({"schemaVersion":"used-phone-public-agent-run-contract-v1","contractSha256":contract_sha,"contract":contract}))
    prediction_sha = _write(run_dir / "predictions.jsonl", b"".join(evaluator.canonical_bytes(row) for row in predictions))
    for prediction in predictions:
        case_id = prediction["caseId"]
        attempt = run_dir / "cases" / case_id / "attempt-001"
        case_sha = _write(attempt / "prediction.json", evaluator.canonical_bytes(prediction))
        trace_sha = _write(attempt / "trace.json", evaluator.canonical_bytes(traces[case_id]))
        terminal = {"terminalState":"success","caseId":case_id,"attempt":1,"protocolVersion":evaluator.PROTOCOL_VERSION,"contractSha256":contract_sha,"businessWriteNetworkUsed":False,"modelCallCount":1,"prediction":prediction,"predictionSha256":case_sha,"trace":traces[case_id],"traceSha256":trace_sha}
        _write(attempt / "success.json", evaluator.canonical_bytes(terminal))
    manifest = {"contractSha256":contract_sha,"contract":contract,"selectedCaseIds":list(evaluator.CASE_IDS),"selectedSplits":["validation"],"execution":{case_id:True for case_id in evaluator.CASE_IDS},"failedCaseIds":[],"prediction":{"path":"predictions.jsonl","rowCount":20,"sha256":prediction_sha},"hiddenArtifactsRead":False,"businessWriteNetworkUsed":False}
    manifest_path = run_dir / "manifest.json"
    _write(manifest_path, evaluator.canonical_bytes(manifest))
    return run_dir, catalog_path, prereg_path, manifest_path


def test_frozen_assets_scope_and_runner_hashes_are_bound():
    assert evaluator.sha256_file(CASES_PATH) == runner.PUBLIC_CASES_SHA256 == evaluator.PUBLIC_CASES_SHA256
    assert evaluator.sha256_file(PREREG_PATH) == evaluator.PREREGISTRATION_SHA256
    rows = [json.loads(line) for line in CASES_PATH.read_text(encoding="utf-8").splitlines()]
    assert [row["caseId"] for row in rows] == list(evaluator.CASE_IDS)
    assert all(row["split"] == "validation" for row in rows)
    with pytest.raises(runner.PublicRunnerError, match="scope SHA mismatch"):
        runner.verify_production_scope()
    assert runner.production_scope_sha256() != evaluator.PRODUCTION_CODE_SCOPE_SHA256
    assert runner._hashes() != evaluator.RUNNER_HASHES
    assert (
        runner._hashes()["agent/evaluation/used_phone_public_agent_runner_v1.py"]
        != evaluator.RUNNER_HASHES["agent/evaluation/used_phone_public_agent_runner_v1.py"]
    )


def test_runner_rejects_partial_or_sealed_selection_before_kernel(monkeypatch, tmp_path):
    called = False
    async def forbidden(**_kwargs):
        nonlocal called
        called = True
        return {}
    monkeypatch.setattr(runner._kernel, "run_public_agent", forbidden)
    with pytest.raises(runner.PublicRunnerError, match="complete ordered"):
        asyncio.run(runner.run_public_agent(public_cases_path=CASES_PATH,public_catalog_path=tmp_path/"catalog.jsonl",run_dir=tmp_path/"run",case_ids=["UPNG-V01"]))
    with pytest.raises(runner.PublicRunnerError, match="fresh run"):
        asyncio.run(runner.run_public_agent(public_cases_path=CASES_PATH,public_catalog_path=tmp_path/"catalog.jsonl",run_dir=tmp_path/"run",allow_sealed_test=True))
    assert called is False


def test_prediction_first_evaluator_accepts_complete_fixture(monkeypatch, tmp_path):
    run_dir, catalog, prereg, manifest = _authenticated_run(tmp_path, monkeypatch)
    result = evaluator.score_validation20(predictions_path=run_dir/"predictions.jsonl",manifest_path=manifest,public_catalog_path=catalog,preregistration_path=prereg)
    assert result["predictionAuthenticatedBeforeExpectations"] is True
    assert result["gatePassed"] is True


def test_tamper_rejected_before_preregistration_read(monkeypatch, tmp_path):
    run_dir, catalog, prereg, manifest = _authenticated_run(tmp_path, monkeypatch)
    payload = (run_dir / "predictions.jsonl").read_bytes().replace(b"UPNG-V01", b"UPNG-V99", 1)
    (run_dir / "predictions.jsonl").write_bytes(payload)
    reads = 0
    original = Path.read_text
    def observed(self, *args, **kwargs):
        nonlocal reads
        if self.resolve() == prereg.resolve():
            reads += 1
        return original(self, *args, **kwargs)
    monkeypatch.setattr(Path, "read_text", observed)
    with pytest.raises(ValueError):
        evaluator.score_validation20(predictions_path=run_dir/"predictions.jsonl",manifest_path=manifest,public_catalog_path=catalog,preregistration_path=prereg)
    assert reads == 0


def test_unknown_risk_rule_is_generic_not_case_or_item_hardcoded():
    catalog = {"11":{"attributes":{group:{"status":"known"} for group in evaluator._shared.CONTROLLED_GROUPS}},"22":{"attributes":{group:{"status":"unknown"} for group in evaluator._shared.CONTROLLED_GROUPS}}}
    expectation = {"requirePreferFewerUnknowns":True,"expectedComparedIds":["11","22"]}
    good = {"trace":{"answers":["按证据缺口优先，商品 11 更适合作为当前选择。"]}}
    bad = {"trace":{"answers":["商品 22 风险更低。"]}}
    assert evaluator._unknown_risk_ordering(good, expectation, catalog) is True
    assert evaluator._unknown_risk_ordering(bad, expectation, catalog) is False


def test_preregistered_known_hard_violation_is_a_supplemental_exact_gate(monkeypatch, tmp_path):
    run_dir, catalog, prereg, manifest = _authenticated_run(tmp_path, monkeypatch)
    result = evaluator.score_validation20(predictions_path=run_dir/"predictions.jsonl",manifest_path=manifest,public_catalog_path=catalog,preregistration_path=prereg)
    assert result["metrics"]["knownHardViolationFreeAccuracy"] == 1.0
    assert result["gateChecks"]["knownHardViolationFreeAccuracy"] is True
