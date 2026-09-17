from __future__ import annotations

import json

from evaluation.shopping_memory_abc_v1 import evaluate, freeze, scenario_definitions


def test_scenario_contract_has_twelve_families_and_two_cases_each():
    scenarios = scenario_definitions()
    families = {row["family"] for row in scenarios}
    assert len(scenarios) == 24
    assert len(families) == 12
    assert all(sum(row["family"] == family for row in scenarios) == 2 for family in families)


def test_frozen_abc_evaluation_accepts_only_controlled_slice(tmp_path):
    assets = tmp_path / "assets"
    output = tmp_path / "run"
    manifest = freeze(assets)
    report = evaluate(assets, output)

    assert manifest["productionActivationAuthorized"] is False
    assert report["verdict"] == "ACCEPT_CONTROLLED_MEMORY_SLICE"
    assert report["productionActivation"] == "HOLD_DEFAULT_OFF"
    assert report["catalogRows"] == 439
    assert set(report["safetyGates"].values()) == {0}
    assert report["metrics"]["C"]["applicationPrecision"] == 1.0
    assert report["metrics"]["C"]["applicationRecall"] == 1.0
    assert report["metrics"]["B"]["falseInfluenceRate"] == 1.0
    receipt = json.loads((output / "receipt.json").read_text(encoding="utf-8"))
    assert set(receipt) == {
        "schemaVersion", "scoreSha256", "tracesSha256", "manifestSha256"
    }
