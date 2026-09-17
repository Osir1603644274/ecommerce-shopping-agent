import importlib.util
import json
from pathlib import Path

from app import memory_candidate_worker as worker


PACKAGE = Path(__file__).parents[1] / "evaluation" / "shopping_memory_extraction_v15_20260830"
RUNNER = PACKAGE / "run_extraction_eval.py"


def load_runner():
    spec = importlib.util.spec_from_file_location("memory_extraction_eval_v15", RUNNER)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_consumed_package_hash_chain_and_rule_layering():
    runner = load_runner()
    manifest = runner.load_manifest()
    scenarios = runner.load_scenarios(manifest["scenarioCount"])
    assert runner.ATTEMPT.exists()
    assert runner.sha256(runner.SCENARIOS) == manifest["scenarioSha256"]
    assert runner.sha256(runner.WORKER_SOURCE) != manifest["workerSha256"]
    started = json.loads((runner.ATTEMPT / "started.json").read_text(encoding="utf-8"))
    receipt = json.loads((runner.ATTEMPT / "receipt.json").read_text(encoding="utf-8"))
    report = json.loads((runner.ATTEMPT / "report.json").read_text(encoding="utf-8"))
    assert started["workerSha256"] == manifest["workerSha256"]
    assert started["promptSha256"] == manifest["promptSha256"]
    assert receipt["workerSha256"] == manifest["workerSha256"]
    assert receipt["promptSha256"] == manifest["promptSha256"]
    assert receipt["manifestSha256"] == runner.sha256(runner.MANIFEST)
    assert receipt["traceSha256"] == runner.sha256(runner.ATTEMPT / "trace.jsonl")
    assert receipt["reportSha256"] == runner.sha256(runner.ATTEMPT / "report.json")
    assert report["decision"] == "HOLD_LLM_EXTRACTION"
    assert len(scenarios) == 24
    for scenario in scenarios:
        assert worker.is_explicit_memory_request(scenario["message"]) is scenario["expectedGate"]
        assert worker.is_sensitive_memory_request(scenario["message"]) is scenario["expectedSensitive"]
        assert worker.current_request_recipient_scope(scenario["message"]) == scenario["expectedRecipient"]


def test_invalid_or_failed_empty_output_cannot_pass_negative_semantics():
    runner = load_runner()
    assert runner.negative_semantic_match([], [], "empty", "empty", None)
    assert not runner.negative_semantic_match(
        [], [], "invalid_model_output", "empty", None
    )
    assert not runner.negative_semantic_match(
        [], [], "catalog_escape_rejected", "empty", None
    )
    assert not runner.negative_semantic_match([], [], "empty", "empty", "TimeoutError")


def test_production_eight_field_result_projects_to_scoring_triple():
    runner = load_runner()
    production_result = [{
        "categoryId": "phone",
        "preferenceKind": "avoid",
        "attributeKey": "brand",
        "normalizedValue": "apple",
        "catalogRevision": "catalog-rev",
        "recipientScope": "self",
        "source": "user_confirmed",
        "displayLabel": "苹果",
    }]
    assert runner._preference_triples(
        production_result, exact_schema=False
    ) == [{
        "preferenceKind": "avoid",
        "attributeKey": "brand",
        "normalizedValue": "apple",
    }]
