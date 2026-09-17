from __future__ import annotations

import asyncio
from copy import deepcopy
import hashlib
import importlib.machinery
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import ModuleType, SimpleNamespace

import pytest

import agent.evaluation.used_phone_public_agent_runner_v1 as runner


_CONTROLLED_RAW = "安卓,90%+,主板未维修,原装屏,原装电池,无划痕,外壳正常"
_CONTROLLED_VALUES = {
    "battery_health": "90_plus",
    "battery_originality": "original",
    "motherboard_repair": "not_repaired",
    "os": "android",
    "scratch_level": "none",
    "screen_originality": "original",
    "shell_condition": "normal",
}


@pytest.fixture(autouse=True)
def _clean_denied_preloads():
    repo_prefix = str(runner.REPO_ROOT).replace("/", "\\").casefold() + "\\"

    def repo_violation(module):
        if not isinstance(module, ModuleType):
            return None
        origins = runner._module_origin_values(module)
        if not any(
            isinstance(value, str)
            and value.replace("/", "\\").casefold().startswith(repo_prefix)
            for value in origins
        ):
            return None
        return runner._module_repo_violation(module)

    removed = {}
    parent = sys.modules.get("agent.evaluation")
    parent_values = {}
    for name, module in list(sys.modules.items()):
        path = str(getattr(module, "__file__", ""))
        violation = repo_violation(module)
        if runner._denied_text(name) or runner._denied_text(path) or violation is not None:
            removed[name] = module
            sys.modules.pop(name, None)
    if parent is not None:
        for name, value in list(vars(parent).items()):
            module_name = getattr(value, "__name__", "") if isinstance(value, ModuleType) else ""
            module_path = str(getattr(value, "__file__", "")) if isinstance(value, ModuleType) else ""
            violation = repo_violation(value)
            if (
                runner._denied_text(name)
                or runner._denied_text(module_name)
                or runner._denied_text(module_path)
                or violation is not None
            ):
                parent_values[name] = value
                vars(parent).pop(name, None)
    try:
        yield
    finally:
        sys.modules.update(removed)
        if parent is not None:
            vars(parent).update(parent_values)


def _case(
    case_id: str = "UPV1-HC-01",
    *,
    family: str = "hard_constraint",
    turns: int = 1,
    visible: bool = False,
) -> runner.PublicCase:
    turn_rows = tuple(
        {"role": "user", "text": f"fixture turn {index}", "turnId": f"turn-{index}"}
        for index in range(1, turns + 1)
    )
    candidates = (
        (
            {"candidateLabel": "A", "itemId": "1001", "title": "phone A"},
            {"candidateLabel": "B", "itemId": "1002", "title": "phone B"},
        )
        if visible
        else ()
    )
    return runner.PublicCase(case_id, "dev", family, turn_rows, candidates)


def _catalog(*item_ids: str) -> dict[str, dict]:
    rows = {}
    for item_id in item_ids:
        rows[item_id] = {
            "itemId": item_id,
            "attributes": {
                group: {
                    "key": group,
                    "status": "known",
                    "value": _CONTROLLED_VALUES[group],
                    "evidenceRefs": [{
                        "source": "relevance",
                        "field": "attr_value",
                        "lineNumber": int(item_id),
                        "rawValue": _CONTROLLED_RAW,
                    }],
                }
                for group in runner.CONTROLLED_GROUPS
            },
        }
    return rows


def _snapshots(*, requirements=None) -> list[dict]:
    return [{
        "phase": "fixture",
        "state": {
            "domainState": {
                "shoppingGuide": {"requirements": requirements or []},
            },
        },
    }]


def test_task_state_extraction_decisions_are_deduplicated_by_revision():
    receipt = {
        "schemaVersion": "used-phone-task-state-extraction-decision-v1",
        "route": "model_fallback",
        "reason": "partial_controlled_coverage",
        "mentionedKeys": ["battery_health", "os"],
        "coveredKeys": ["os"],
        "uncoveredKeys": ["battery_health"],
    }
    snapshots = [
        {
            "phase": "user_state_updated",
            "state": {
                "revision": 2,
                "domainState": {"taskStateExtraction": deepcopy(receipt)},
            },
        },
        {
            "phase": "turn_terminal",
            "state": {
                "revision": 2,
                "domainState": {"taskStateExtraction": deepcopy(receipt)},
            },
        },
    ]

    assert runner._task_state_extraction_decisions(snapshots) == [{
        "phase": "user_state_updated",
        "taskRevision": 2,
        "decision": receipt,
    }]


def test_task_state_extraction_decisions_fail_closed_on_inconsistent_gap():
    snapshots = [{
        "phase": "user_state_updated",
        "state": {
            "revision": 1,
            "domainState": {"taskStateExtraction": {
                "schemaVersion": "used-phone-task-state-extraction-decision-v1",
                "route": "deterministic_complete",
                "reason": "complete_controlled_coverage",
                "mentionedKeys": ["os"],
                "coveredKeys": [],
                "uncoveredKeys": [],
            }},
        },
    }]

    with pytest.raises(runner.PublicRunnerError, match="uncovered keys are inconsistent"):
        runner._task_state_extraction_decisions(snapshots)


def _search_trace(*item_ids: str, evidence: bool = False, turn_index: int = 0) -> dict:
    evidence_rows = []
    candidates = [{"id": int(item), "attributes": [], "checks": [], "evidenceRefs": []} for item in item_ids]
    if evidence:
        ref = f"product:{item_ids[0]}:attribute:os"
        evidence_rows = [{
            "ref": ref,
            "field": "relevance.attr_value",
            "rawValue": _CONTROLLED_RAW,
            "method": runner.ATTRIBUTE_RULESET_VERSION,
        }]
        candidates[0]["attributes"] = [{
            "key": "os",
            "rawValue": _CONTROLLED_RAW,
            "normalizedText": "android",
            "normalizedNumber": None,
            "normalizedBoolean": None,
            "evidenceField": "relevance.attr_value",
            "extractionMethod": runner.ATTRIBUTE_RULESET_VERSION,
        }]
        candidates[0]["evidenceRefs"] = [ref]
        candidates[0]["checks"] = [{
            "key": "os", "actual": "android", "status": "pass", "evidenceRef": ref,
        }]
    return {
        "tool": "search_products",
        "ok": True,
        "publicTurnIndex": turn_index,
        "detail": {
            "candidateIds": [int(item) for item in item_ids],
            "candidates": candidates,
            "evidence": evidence_rows,
        },
    }


def _compare_trace(*item_ids: str, evidence: bool = False, turn_index: int = 0) -> dict:
    products = [
        {"product": {"id": int(item), "attributes": []}, "checks": [], "evidenceRefs": []}
        for item in item_ids
    ]
    evidence_rows = []
    if evidence:
        ref = f"product:{item_ids[0]}:attribute:os"
        products[0]["product"]["attributes"] = [{
            "key": "os", "rawValue": _CONTROLLED_RAW, "normalizedText": "android",
            "normalizedNumber": None, "normalizedBoolean": None,
            "evidenceField": "relevance.attr_value",
            "extractionMethod": runner.ATTRIBUTE_RULESET_VERSION,
        }]
        products[0]["evidenceRefs"] = [ref]
        products[0]["checks"] = [{
            "key": "os", "actual": "android", "status": "pass", "evidenceRef": ref,
        }]
        evidence_rows = [{
            "ref": ref, "field": "relevance.attr_value", "rawValue": _CONTROLLED_RAW,
            "method": runner.ATTRIBUTE_RULESET_VERSION,
        }]
    return {
        "tool": "compare_products",
        "ok": True,
        "publicTurnIndex": turn_index,
        "detail": {
            "products": products,
            "evidence": evidence_rows,
        },
    }


def _selection(action: str, citations=None) -> dict:
    return {"predictedAction": action, "citationRefIds": citations or []}


@pytest.mark.parametrize(
    ("family", "case_id", "action", "turn_count", "tool_kind"),
    [
        ("clarification", "UPV1-CLR-01", "CLARIFY", 1, "none"),
        ("comparison", "UPV1-CMP-01", "COMPARE_WITH_FIELD_EVIDENCE", 1, "compare"),
        ("hard_constraint", "UPV1-HC-01", "RETRIEVE_FILTER_AND_RANK", 1, "search"),
        ("hard_soft_ranking", "UPV1-HS-01", "RETRIEVE_FILTER_AND_RANK", 1, "search"),
        ("multi_turn_update", "UPV1-MT-01", "UPDATE_STATE_THEN_RETRIEVE", 2, "search"),
        ("negation", "UPV1-NEG-01", "RETRIEVE_FILTER_AND_RANK", 1, "search"),
        ("unsatisfiable", "UPV1-NO-01", "ABSTAIN_OR_EXPLAIN", 1, "none"),
        ("substitute", "UPV1-SUB-01", "RETRIEVE_SUBSTITUTES_RETAINING_CONSTRAINTS", 1, "search"),
        ("tradeoff_explanation", "UPV1-TO-01", "RETRIEVE_FILTER_AND_EXPLAIN_TRADEOFF", 1, "search"),
        ("unknown_conflict", "UPV1-UNK-01", "RETRIEVE_FILTER_AND_RANK_WITH_UNKNOWNS", 1, "search"),
    ],
)
def test_materializer_covers_all_ten_public_behavior_families(
    family, case_id, action, turn_count, tool_kind,
):
    case = _case(case_id, family=family, turns=turn_count, visible=tool_kind == "compare")
    traces = []
    if tool_kind == "search":
        traces = [_search_trace("1001", "1002", turn_index=turn_count - 1)]
    elif tool_kind == "compare":
        traces = [_compare_trace("1001", "1002")]
    prediction = runner.materialize_prediction(
        case=case,
        catalog=_catalog("1001", "1002"),
        tool_traces=traces,
        snapshots=_snapshots(),
        selection=_selection(action),
    )
    assert prediction["predictedAction"] == action
    assert prediction["rankedItemIds"] == (["1001", "1002"] if tool_kind == "search" else [])
    if turn_count == 2:
        assert prediction["predictedState"] == prediction["predictedConstraints"]
    if tool_kind == "compare":
        assert prediction["comparison"]["candidateItemIds"] == ["1001", "1002"]
    runner.validate_prediction_row(prediction)


def test_constraints_and_state_are_deterministically_derived_from_taskstate():
    requirements = [
        {"key": "os", "operator": "eq", "value": "android", "priority": "hard"},
        {"key": "motherboard_repair", "operator": "not_in", "value": ["repaired"], "priority": "hard"},
        {"key": "scratch_level", "operator": "in", "value": ["none", "light"], "priority": "soft"},
        {"key": "brand", "operator": "eq", "value": "x", "priority": "hard"},
    ]
    prediction = runner.materialize_prediction(
        case=_case("UPV1-MT-01", turns=2),
        catalog=_catalog("1001"),
        tool_traces=[_search_trace("1001", turn_index=1)],
        snapshots=_snapshots(requirements=requirements),
        selection=_selection("UPDATE_STATE_THEN_RETRIEVE"),
    )
    assert prediction["predictedConstraints"] == prediction["predictedState"]
    assert {row["group"] for row in prediction["predictedState"]["hard"]} == {
        "motherboard_repair", "os",
    }
    assert prediction["predictedState"]["soft"][0]["operator"] == "IN"
    assert all(row["group"] != "brand" for rows in prediction["predictedState"].values() for row in rows)


def test_single_visible_substitute_anchor_is_not_misclassified_as_comparison():
    case = runner.PublicCase(
        "UPV1-SUB-01",
        "dev",
        "substitute",
        _case().turns,
        ({"candidateLabel": "当前商品", "itemId": "1001", "title": "anchor"},),
    )
    guide = runner._initial_domain_state(case)["shoppingGuide"]
    assert guide["mode"] == "recommend"
    assert guide["candidateIds"] == [1001]
    assert guide["comparedIds"] == []


def test_substitute_anchor_seeds_only_known_non_relaxable_public_fields():
    case = runner.PublicCase(
        "UPV1-SUB-02",
        "dev",
        "substitute",
        _case().turns,
        ({"candidateLabel": "褰撳墠鍟嗗搧", "itemId": "1001", "title": "anchor"},),
    )
    catalog = _catalog("1001")
    catalog["1001"]["attributes"] = {
        group: {
            "key": group,
            "status": "unknown",
            "value": None,
        }
        for group in runner.CONTROLLED_GROUPS
    }
    catalog["1001"]["attributes"]["os"] = {
        "key": "os", "status": "known", "value": "android",
    }
    catalog["1001"]["attributes"]["battery_health"] = {
        "key": "battery_health", "status": "conflict", "value": None,
    }

    guide = runner._initial_domain_state(case, catalog)["shoppingGuide"]

    assert guide["requirements"] == [{
        "key": "os", "operator": "eq", "value": "android",
        "unit": "enum", "priority": "hard",
        "source": "system:reference_product:1001",
    }]


def test_no_successful_tool_cannot_rank_compare_or_cite():
    with pytest.raises(runner.PublicRunnerError, match="without a successful tool"):
        runner.materialize_prediction(
            case=_case(), catalog=_catalog("1001"), tool_traces=[], snapshots=_snapshots(),
            selection=_selection("RETRIEVE_FILTER_AND_RANK"),
        )
    with pytest.raises(runner.PublicRunnerError, match="successful compare"):
        runner.materialize_prediction(
            case=_case("UPV1-CMP-01", visible=True), catalog=_catalog("1001", "1002"),
            tool_traces=[], snapshots=_snapshots(),
            selection=_selection("COMPARE_WITH_FIELD_EVIDENCE"),
        )
    with pytest.raises(runner.PublicRunnerError, match="unsupported citation"):
        runner.materialize_prediction(
            case=_case("UPV1-CLR-01"), catalog=_catalog("1001"),
            tool_traces=[], snapshots=_snapshots(),
            selection=_selection("CLARIFY", ["pubref-another-case"]),
        )


def test_latest_search_result_wins_and_out_of_catalog_item_is_rejected():
    prediction = runner.materialize_prediction(
        case=_case("UPV1-MT-01", turns=2),
        catalog=_catalog("1001", "1002"),
        tool_traces=[_search_trace("1001", turn_index=0), _search_trace("1002", turn_index=1)],
        snapshots=_snapshots(),
        selection=_selection("UPDATE_STATE_THEN_RETRIEVE"),
    )
    assert prediction["rankedItemIds"] == ["1002"]
    with pytest.raises(runner.PublicRunnerError, match="outside the pinned public catalog"):
        runner.materialize_prediction(
            case=_case(), catalog=_catalog("1001"),
            tool_traces=[_search_trace("9999")], snapshots=_snapshots(),
            selection=_selection("RETRIEVE_FILTER_AND_RANK"),
        )


def test_citation_requires_current_tool_raw_value_and_canonical_public_ref():
    case = _case()
    catalog = _catalog("1001")
    traces = [_search_trace("1001", evidence=True)]
    options = runner._citation_options(case, catalog, traces)
    assert len(options) == 1
    ref_id = next(iter(options))
    prediction = runner.materialize_prediction(
        case=case, catalog=catalog, tool_traces=traces, snapshots=_snapshots(),
        selection=_selection("RETRIEVE_FILTER_AND_RANK", [ref_id]),
    )
    assert prediction["evidenceCitations"] == [{
        "itemId": "1001", "group": "os", "source": "relevance",
        "field": "attr_value", "lineNumber": 1001, "rawValue": _CONTROLLED_RAW,
    }]
    tampered = deepcopy(traces)
    tampered[0]["detail"]["evidence"][0]["rawValue"] = "forged"
    assert runner._citation_options(case, catalog, tampered) == {}


def test_projection_cannot_erase_successful_search_and_selects_bound_citations():
    case = _case()
    catalog = _catalog("1001")
    traces = [_search_trace("1001", evidence=True)]
    selection = runner._normalize_projection_selection(
        case=case,
        catalog=catalog,
        tool_traces=traces,
        snapshots=_snapshots(requirements=[{
            "key": "os", "operator": "eq", "value": "android", "priority": "hard",
        }]),
        selection=_selection("ABSTAIN_OR_EXPLAIN"),
    )
    assert selection["predictedAction"] == "RETRIEVE_FILTER_AND_RANK"
    assert selection["citationRefIds"] == list(runner._citation_options(case, catalog, traces))
    prediction = runner.materialize_prediction(
        case=case,
        catalog=catalog,
        tool_traces=traces,
        snapshots=_snapshots(),
        selection=selection,
    )
    assert prediction["rankedItemIds"] == ["1001"]
    assert len(prediction["evidenceCitations"]) == 1


def test_deterministic_projection_uses_minimal_bound_constraint_witnesses():
    case = _case("UPV1-HC-01")
    traces = [_search_trace("1001", evidence=True)]
    snapshots = _snapshots(requirements=[{
        "key": "os", "operator": "eq", "value": "android",
        "priority": "hard",
    }])
    selection = runner._deterministic_projection_selection(
        case=case,
        catalog=_catalog("1001"),
        tool_traces=traces,
        snapshots=snapshots,
    )

    assert selection["predictedAction"] == "RETRIEVE_FILTER_AND_RANK"
    assert len(selection["citationRefIds"]) <= 1
    assert set(selection["citationRefIds"]).issubset(
        runner._citation_options(case, _catalog("1001"), traces)
    )


def test_deterministic_projection_never_cites_a_clarification():
    selection = runner._deterministic_projection_selection(
        case=_case("UPV1-CLR-01"),
        catalog=_catalog("1001"),
        tool_traces=[],
        snapshots=_snapshots(),
    )

    assert selection == {"predictedAction": "CLARIFY", "citationRefIds": []}


def test_one_visible_reference_plus_search_is_observed_as_substitution():
    case = runner.PublicCase(
        "UPV1-SUB-01", "dev", "substitute",
        ({"role": "user", "text": "find a similar one", "turnId": "turn-1"},),
        ({"candidateLabel": "A", "itemId": "1002", "title": "reference"},),
    )

    action = runner._observed_projection_action(
        case, [_search_trace("1001")], _snapshots(), "CLARIFY"
    )

    assert action == "RETRIEVE_SUBSTITUTES_RETAINING_CONSTRAINTS"


def test_projection_without_evidence_requires_an_empty_citation_array():
    schema = runner._projection_tool_schema([])["function"]["parameters"]
    citations = schema["properties"]["citationRefIds"]
    assert citations["maxItems"] == 0
    assert "enum" not in citations["items"]

    with pytest.raises(runner.PublicRunnerError, match="unsupported citation"):
        runner.materialize_prediction(
            case=_case("UPV1-CLR-01", family="clarification"),
            catalog=_catalog("1001"),
            tool_traces=[],
            snapshots=[{"state": {"status": "collecting_information", "domainState": {}}}],
            selection={
                "predictedAction": "CLARIFY",
                "citationRefIds": ["__none_available__"],
            },
        )


@pytest.mark.parametrize(
    "tamper",
    [
        "missing_check", "duplicate_check", "failed_check", "unknown_check",
        "missing_method", "wrong_field", "wrong_method", "wrong_normalized",
        "known_to_unknown", "opaque_raw", "public_unknown", "public_conflict",
    ],
)
def test_citation_rejects_incomplete_or_noncanonical_tool_evidence(tamper):
    case = _case()
    catalog = _catalog("1001")
    trace = _search_trace("1001", evidence=True)
    wrapper = trace["detail"]["candidates"][0]
    evidence = trace["detail"]["evidence"][0]
    attribute = wrapper["attributes"][0]
    check = wrapper["checks"][0]
    if tamper == "missing_check":
        wrapper["checks"] = []
    elif tamper == "duplicate_check":
        wrapper["checks"].append(deepcopy(check))
    elif tamper == "failed_check":
        check["status"] = "fail"
    elif tamper == "unknown_check":
        check.update({"actual": None, "status": "unknown", "evidenceRef": evidence["ref"]})
    elif tamper == "missing_method":
        evidence.pop("method")
    elif tamper == "wrong_field":
        evidence["field"] = "title"
    elif tamper == "wrong_method":
        evidence["method"] = "llm_guess"
    elif tamper == "wrong_normalized":
        attribute["normalizedText"] = "ios"
    elif tamper == "known_to_unknown":
        attribute["normalizedText"] = None
    elif tamper == "opaque_raw":
        evidence["rawValue"] = attribute["rawValue"] = "opaque-token"
        catalog["1001"]["attributes"]["os"]["evidenceRefs"][0]["rawValue"] = "opaque-token"
    elif tamper == "public_unknown":
        catalog["1001"]["attributes"]["os"].update({"status": "unknown", "value": None})
    elif tamper == "public_conflict":
        catalog["1001"]["attributes"]["os"].update({"status": "conflict", "value": None})
    assert runner._citation_options(case, catalog, [trace]) == {}


def test_cross_case_citation_ref_is_rejected_fail_closed():
    case = _case()
    catalog = _catalog("1001")
    other_case = _case("UPV1-CMP-01", visible=True)
    other_trace = _compare_trace("1001", "1002", evidence=True)
    other_options = runner._citation_options(
        other_case, _catalog("1001", "1002"), [other_trace],
        tool_name="compare_products", final_turn_index=0,
    )
    assert len(other_options) == 1
    other_ref = next(iter(other_options))
    own_options = runner._citation_options(
        case, catalog, [_search_trace("1001", evidence=True)],
    )
    # Same item/group/raw citation under a different case id mints a different ref.
    assert other_ref not in own_options
    with pytest.raises(runner.PublicRunnerError, match="cross-case or unsupported citation"):
        runner.materialize_prediction(
            case=case, catalog=catalog,
            tool_traces=[_search_trace("1001", evidence=True)],
            snapshots=_snapshots(),
            selection=_selection("RETRIEVE_FILTER_AND_RANK", [other_ref]),
        )


def test_legal_search_and_compare_evidence_survive_full_identity_check():
    catalog = _catalog("1001", "1002")
    search_case = _case()
    search_options = runner._citation_options(
        search_case, catalog, [_search_trace("1001", evidence=True)],
        tool_name="search_products", final_turn_index=0,
    )
    assert len(search_options) == 1
    compare_case = _case("UPV1-CMP-01", visible=True)
    compare_trace = _compare_trace("1001", "1002", evidence=True)
    compare_options = runner._citation_options(
        compare_case, catalog, [compare_trace],
        tool_name="compare_products", final_turn_index=0,
    )
    assert len(compare_options) == 1
    prediction = runner.materialize_prediction(
        case=compare_case, catalog=catalog, tool_traces=[compare_trace],
        snapshots=_snapshots(),
        selection=_selection("COMPARE_WITH_FIELD_EVIDENCE", list(compare_options)),
    )
    assert prediction["comparison"]["candidateItemIds"] == ["1001", "1002"]
    assert len(prediction["evidenceCitations"]) == 1


def test_final_turn_cannot_reuse_old_search_or_old_citation():
    case = _case("UPV1-MT-01", turns=2)
    old = _search_trace("1001", evidence=True, turn_index=0)
    with pytest.raises(runner.PublicRunnerError, match="without a successful tool"):
        runner.materialize_prediction(
            case=case, catalog=_catalog("1001", "1002"), tool_traces=[old],
            snapshots=_snapshots(), selection=_selection("UPDATE_STATE_THEN_RETRIEVE"),
        )
    failed_final = {"tool": "search_products", "ok": False, "publicTurnIndex": 1, "detail": {}}
    with pytest.raises(runner.PublicRunnerError, match="without a successful tool"):
        runner.materialize_prediction(
            case=case, catalog=_catalog("1001", "1002"), tool_traces=[old, failed_final],
            snapshots=_snapshots(), selection=_selection("UPDATE_STATE_THEN_RETRIEVE"),
        )
    old_ref = next(iter(runner._citation_options(case, _catalog("1001"), [old])))
    final = _search_trace("1002", turn_index=1)
    with pytest.raises(runner.PublicRunnerError, match="unsupported citation"):
        runner.materialize_prediction(
            case=case, catalog=_catalog("1001", "1002"), tool_traces=[old, final],
            snapshots=_snapshots(),
            selection=_selection("UPDATE_STATE_THEN_RETRIEVE", [old_ref]),
        )


def test_compare_requires_one_final_success_trace_and_ignores_later_failure():
    case = _case("UPV1-CMP-01", visible=True)
    split = [_compare_trace("1001", "1003"), _compare_trace("1002", "1003")]
    with pytest.raises(runner.PublicRunnerError, match="lacks current-case"):
        runner.materialize_prediction(
            case=case, catalog=_catalog("1001", "1002", "1003"),
            tool_traces=split, snapshots=_snapshots(),
            selection=_selection("COMPARE_WITH_FIELD_EVIDENCE"),
        )
    legal = _compare_trace("1001", "1002")
    later_failure = {"tool": "compare_products", "ok": False, "publicTurnIndex": 0, "detail": {}}
    prediction = runner.materialize_prediction(
        case=case, catalog=_catalog("1001", "1002"),
        tool_traces=[legal, later_failure], snapshots=_snapshots(),
        selection=_selection("COMPARE_WITH_FIELD_EVIDENCE"),
    )
    assert prediction["comparison"]["candidateItemIds"] == ["1001", "1002"]


def test_compare_trace_must_match_one_production_product_ids_invocation():
    case = _case("UPV1-CMP-01", visible=True)
    trace = _compare_trace("1001", "1002")
    result_payload = {key: value for key, value in trace.items() if key != "publicTurnIndex"}
    invocation = {
        "tool": "compare_products", "outcome": "success", "publicTurnIndex": 0,
        "arguments": {"productIds": [1001, 1002], "category": "phone", "requirements": []},
        "resultSha256": hashlib.sha256(runner.canonical_json_bytes(result_payload)).hexdigest(),
    }
    prediction = runner.materialize_prediction(
        case=case, catalog=_catalog("1001", "1002"), tool_traces=[trace],
        snapshots=_snapshots(), selection=_selection("COMPARE_WITH_FIELD_EVIDENCE"),
        tool_invocations=[invocation],
    )
    assert prediction["comparison"]["candidateItemIds"] == ["1001", "1002"]
    forged = deepcopy(invocation)
    forged["arguments"]["productIds"] = [1001, 1003]
    with pytest.raises(runner.PublicRunnerError, match="matching productIds"):
        runner.materialize_prediction(
            case=case, catalog=_catalog("1001", "1002", "1003"), tool_traces=[trace],
            snapshots=_snapshots(), selection=_selection("COMPARE_WITH_FIELD_EVIDENCE"),
            tool_invocations=[forged],
        )


def test_multi_turn_compare_cannot_reuse_old_turn_pair():
    case = runner.PublicCase(
        "UPV1-MT-01", "dev", "multi_turn_update", _case(turns=2).turns,
        _case("UPV1-CMP-01", visible=True).visible_candidates,
    )
    with pytest.raises(runner.PublicRunnerError, match="lacks current-case"):
        runner.materialize_prediction(
            case=case, catalog=_catalog("1001", "1002"),
            tool_traces=[_compare_trace("1001", "1002", turn_index=0)],
            snapshots=_snapshots(), selection=_selection("COMPARE_WITH_FIELD_EVIDENCE"),
        )


class _FakeCompletions:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.seen = []

    async def create(self, **kwargs):
        self.seen.append(kwargs)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        call = SimpleNamespace(
            id=f"call-{len(self.seen)}",
            function=SimpleNamespace(
                name="submit_used_phone_public_projection",
                arguments=json.dumps(outcome),
            ),
        )
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
            content=None, tool_calls=[call],
        ))])


def _recording_client(outcomes):
    completions = _FakeCompletions(outcomes)
    raw = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    return runner.RecordingClient(raw), completions


def test_projection_schema_semantic_repair_occurs_at_most_once():
    client, fake = _recording_client([
        _selection("RETRIEVE_FILTER_AND_RANK"),
        _selection("CLARIFY"),
    ])
    prediction, repairs = asyncio.run(runner.project_prediction(
        client,
        case=_case("UPV1-CLR-01"),
        answer="请补充需求",
        catalog=_catalog("1001"),
        tool_traces=[],
        snapshots=_snapshots(),
        model_name="fixture",
    ))
    assert prediction["predictedAction"] == "CLARIFY"
    assert repairs == 1
    assert len(fake.seen) == 2
    assert fake.seen[1]["messages"][-1]["role"] == "tool"
    assert [row["outcome"] for row in client.ledger] == ["success", "success"]


def test_projection_request_failure_is_not_retried():
    client, fake = _recording_client([RuntimeError("first create failed")])
    with pytest.raises(RuntimeError, match="first create failed"):
        asyncio.run(runner.project_prediction(
            client,
            case=_case("UPV1-CLR-01"), answer="x", catalog=_catalog("1001"),
            tool_traces=[], snapshots=_snapshots(), model_name="fixture",
        ))
    assert len(fake.seen) == 1
    assert client.ledger[0]["ok"] is False
    assert client.ledger[0]["outcome"] == "error"


def test_recording_client_registers_before_await_and_preserves_cancellation():
    entered = asyncio.Event()

    class Delayed:
        async def create(self, **_kwargs):
            entered.set()
            await asyncio.Future()

    client = runner.RecordingClient(SimpleNamespace(
        chat=SimpleNamespace(completions=Delayed()),
    ))

    async def scenario():
        task = asyncio.create_task(client.chat.completions.create(model="fixture", messages=[]))
        await entered.wait()
        assert len(client.ledger) == 1
        assert client.ledger[0]["outcome"] == "attempted"
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
    assert len(client.ledger) == 1
    assert client.ledger[0]["ok"] is False
    assert client.ledger[0]["outcome"] == "cancelled"


def test_recording_client_timeout_keeps_attempted_call_in_ledger():
    class Delayed:
        async def create(self, **_kwargs):
            await asyncio.Future()

    client = runner.RecordingClient(SimpleNamespace(
        chat=SimpleNamespace(completions=Delayed()),
    ))

    async def scenario():
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(
                client.chat.completions.create(model="fixture", messages=[]), 0.01,
            )

    asyncio.run(scenario())
    assert len(client.ledger) == 1
    assert client.ledger[0]["outcome"] == "cancelled"


def test_hidden_path_read_apis_and_loader_alias_fail_closed(tmp_path: Path):
    public_cases = tmp_path / "cases_public.jsonl"
    public_catalog = tmp_path / "catalog.jsonl"
    hidden = tmp_path / "cases_hidden.jsonl"
    public_cases.write_text("{}\n", encoding="utf-8")
    public_catalog.write_text("{}\n", encoding="utf-8")
    hidden.write_text("secret", encoding="utf-8")
    with runner.public_runtime_isolation(
        public_cases_path=public_cases, public_catalog_path=public_catalog, run_dir=tmp_path / "run",
    ):
        assert public_cases.read_text(encoding="utf-8") == "{}\n"
        probes = [
            lambda: hidden.read_text(encoding="utf-8"),
            lambda: hidden.read_bytes(),
            lambda: open(hidden, encoding="utf-8").read(),
            lambda: __import__("os").close(__import__("os").open(hidden, __import__("os").O_RDONLY)),
            lambda: importlib.machinery.SourceFileLoader("innocent_alias", str(hidden)).get_data(str(hidden)),
        ]
        for probe in probes:
            with pytest.raises(runner.PublicIsolationError):
                probe()


def test_nonproduction_repository_file_is_not_in_runner_read_surface(tmp_path: Path):
    public_cases = tmp_path / "cases_public.jsonl"
    public_catalog = tmp_path / "catalog.jsonl"
    public_cases.write_text("{}\n", encoding="utf-8")
    public_catalog.write_text("{}\n", encoding="utf-8")
    unrelated_evaluator = runner.REPO_ROOT / "agent" / "evaluation" / "bm25_inverted_evaluation.py"
    assert unrelated_evaluator.is_file()
    with runner.public_runtime_isolation(
        public_cases_path=public_cases,
        public_catalog_path=public_catalog,
        run_dir=tmp_path / "run",
    ):
        with pytest.raises(runner.PublicIsolationError, match="denied public-run read"):
            unrelated_evaluator.read_bytes()


def test_real_upstream_hidden_and_nonpublic_worktree_files_are_globally_denied(tmp_path: Path):
    upstream = Path(r"C:\Users\ming\.codex\worktrees\used-phone-benchmark-v1\agent")
    hidden_paths = [
        upstream / "agent" / "evaluation" / "used_phone_benchmark_scorer_v1.py",
        upstream / "agent" / "evaluation" / "bm25_inverted_evaluation.py",
        upstream / "data" / "processed" / "ecommerce" / "kuaisearch_used_phone_complex_benchmark_v1"
        / runner.DATASET_REVISION / "used_phone_complex_cases_v1" / "cases_hidden.jsonl",
    ]
    assert all(path.is_file() for path in hidden_paths)
    public_cases = tmp_path / "cases_public.jsonl"
    public_catalog = tmp_path / "catalog.jsonl"
    public_cases.write_text("{}\n", encoding="utf-8")
    public_catalog.write_text("{}\n", encoding="utf-8")
    with runner.public_runtime_isolation(
        public_cases_path=public_cases, public_catalog_path=public_catalog,
    ):
        for path in hidden_paths:
            with pytest.raises(runner.PublicIsolationError, match="denied public-run read"):
                path.read_bytes()


def test_global_read_allowlist_accepts_public_stdlib_and_site_packages_only(tmp_path: Path):
    import json as json_module
    import jsonschema as jsonschema_module

    public_cases = tmp_path / "cases_public.jsonl"
    public_catalog = tmp_path / "catalog.jsonl"
    outside = tmp_path / "outside-secret.txt"
    alias = tmp_path / "outside-alias.txt"
    public_cases.write_text("{}\n", encoding="utf-8")
    public_catalog.write_text("{}\n", encoding="utf-8")
    outside.write_text("secret", encoding="utf-8")
    try:
        __import__("os").link(outside, alias)
    except OSError as exc:
        pytest.skip(f"Windows hard-link probe unavailable: {exc}")
    with runner.public_runtime_isolation(
        public_cases_path=public_cases, public_catalog_path=public_catalog,
    ):
        assert public_cases.read_text(encoding="utf-8") == "{}\n"
        assert Path(json_module.__file__).read_bytes()
        assert Path(jsonschema_module.__file__).read_bytes()
        for path in (outside, alias):
            with pytest.raises(runner.PublicIsolationError, match="denied public-run read"):
                path.read_bytes()


def test_preloaded_hidden_module_and_parent_attribute_fail_closed(tmp_path: Path):
    public_cases = tmp_path / "cases_public.jsonl"
    public_catalog = tmp_path / "catalog.jsonl"
    hidden = tmp_path / "used_phone_benchmark_scorer_v1.py"
    for path in (public_cases, public_catalog, hidden):
        path.write_text("{}\n", encoding="utf-8")
    alias = ModuleType("innocent_alias")
    alias.__file__ = str(hidden)
    sys.modules[alias.__name__] = alias
    try:
        with pytest.raises(runner.PublicIsolationError, match="preloaded"):
            with runner.public_runtime_isolation(
                public_cases_path=public_cases, public_catalog_path=public_catalog,
            ):
                pass
    finally:
        sys.modules.pop(alias.__name__, None)
    package = sys.modules["agent.evaluation"]
    package.innocent_parent_alias = alias
    try:
        with pytest.raises(runner.PublicIsolationError, match="parent package"):
            with runner.public_runtime_isolation(
                public_cases_path=public_cases, public_catalog_path=public_catalog,
            ):
                pass
    finally:
        del package.innocent_parent_alias


@pytest.mark.parametrize("origin_kind", ["file", "spec", "loader"])
@pytest.mark.parametrize(
    "module_name",
    ["agent.evaluation.bm25_inverted_evaluation", "neutral_preloaded_evaluator"],
)
def test_preloaded_nonproduction_repo_module_is_rejected_by_origin_and_preserved(
    origin_kind, module_name,
):
    repo_file = runner.REPO_ROOT / "agent" / "evaluation" / "bm25_inverted_evaluation.py"
    module = ModuleType(module_name)
    if origin_kind == "file":
        module.__file__ = str(repo_file)
    elif origin_kind == "spec":
        module.__spec__ = importlib.machinery.ModuleSpec(module_name, loader=None, origin=str(repo_file))
    else:
        loader = importlib.machinery.SourceFileLoader(module_name, str(repo_file))
        module.__loader__ = loader
        module.__spec__ = importlib.util.spec_from_loader(module_name, loader)
        module.__file__ = None
    previous = sys.modules.get(module_name)
    sys.modules[module_name] = module
    try:
        with pytest.raises(runner.PublicIsolationError, match="preloaded"):
            with runner.public_runtime_isolation(
                public_cases_path=Path("cases_public.jsonl"),
                public_catalog_path=Path("catalog.jsonl"),
            ):
                pass
        assert sys.modules[module_name] is module
    finally:
        if previous is None:
            sys.modules.pop(module_name, None)
        else:
            sys.modules[module_name] = previous


def test_neutral_parent_alias_to_nonproduction_repo_module_is_rejected_and_preserved():
    repo_file = runner.REPO_ROOT / "agent" / "evaluation" / "bm25_inverted_evaluation.py"
    module = ModuleType("neutral_parent_payload")
    module.__spec__ = importlib.machinery.ModuleSpec(
        module.__name__, loader=None, origin=str(repo_file),
    )
    package = sys.modules["agent.evaluation"]
    package.neutral_parent_alias = module
    try:
        with pytest.raises(runner.PublicIsolationError, match="parent package"):
            with runner.public_runtime_isolation(
                public_cases_path=Path("cases_public.jsonl"),
                public_catalog_path=Path("catalog.jsonl"),
            ):
                pass
        assert package.neutral_parent_alias is module
    finally:
        del package.neutral_parent_alias


def test_pseudo_stdin_origin_is_not_resolved_as_a_repository_file(tmp_path: Path):
    module = ModuleType("neutral_stdin_module")
    module.__spec__ = importlib.machinery.ModuleSpec(
        module.__name__, loader=None, origin="<stdin>",
    )
    previous = sys.modules.get(module.__name__)
    sys.modules[module.__name__] = module
    try:
        with runner.public_runtime_isolation(
            public_cases_path=tmp_path / "cases_public.jsonl",
            public_catalog_path=tmp_path / "catalog.jsonl",
        ):
            assert sys.modules[module.__name__] is module
    finally:
        if previous is None:
            sys.modules.pop(module.__name__, None)
        else:
            sys.modules[module.__name__] = previous


def test_nested_public_isolation_restores_guards_after_inner_exception(tmp_path: Path):
    public_cases = tmp_path / "cases_public.jsonl"
    public_catalog = tmp_path / "catalog.jsonl"
    hidden = tmp_path / "cases_hidden.jsonl"
    public_cases.write_text("{}\n", encoding="utf-8")
    public_catalog.write_text("{}\n", encoding="utf-8")
    hidden.write_text("secret", encoding="utf-8")
    original_open = open
    with runner.public_runtime_isolation(
        public_cases_path=public_cases, public_catalog_path=public_catalog,
    ):
        outer_open = __import__("builtins").open
        with pytest.raises(RuntimeError, match="inner fixture"):
            with runner.public_runtime_isolation(
                public_cases_path=public_cases, public_catalog_path=public_catalog,
            ):
                raise RuntimeError("inner fixture")
        assert __import__("builtins").open is outer_open
        with pytest.raises(runner.PublicIsolationError):
            hidden.read_text(encoding="utf-8")
    assert __import__("builtins").open is original_open


def test_write_outside_run_directory_is_rejected(tmp_path: Path):
    public_cases = tmp_path / "cases_public.jsonl"
    public_catalog = tmp_path / "catalog.jsonl"
    public_cases.write_text("{}\n", encoding="utf-8")
    public_catalog.write_text("{}\n", encoding="utf-8")
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    with runner.public_runtime_isolation(
        public_cases_path=public_cases, public_catalog_path=public_catalog, run_dir=run_dir,
    ):
        with pytest.raises(runner.PublicIsolationError, match="outside"):
            (tmp_path / "escape.json").write_text("x", encoding="utf-8")
        (run_dir / "allowed.json").write_text("x", encoding="utf-8")


def test_isolation_provisions_run_local_tempdir_and_redirects_probe(tmp_path: Path):
    """Uninitialized tempfile.tempdir (None) is redirected to a run-local scratch
    before the write guard installs, so gettempdir() and default tempfile
    creation touch only the run directory and never probe the system temp dir."""
    public_cases = tmp_path / "cases_public.jsonl"
    public_catalog = tmp_path / "catalog.jsonl"
    public_cases.write_text("{}\n", encoding="utf-8")
    public_catalog.write_text("{}\n", encoding="utf-8")
    run_dir = tmp_path / "run"
    scratch = run_dir / runner.RUN_LOCAL_TEMP_DIR_NAME
    prior_tempdir = tempfile.tempdir
    prior_env = {key: os.environ.get(key) for key in ("TEMP", "TMP", "TMPDIR")}
    try:
        tempfile.tempdir = None  # CPython's uninitialized default temp dir
        with runner.public_runtime_isolation(
            public_cases_path=public_cases, public_catalog_path=public_catalog,
            run_dir=run_dir,
        ):
            assert Path(tempfile.gettempdir()).resolve() == scratch.resolve()
            assert Path(tempfile.tempdir).resolve() == scratch.resolve()
            assert os.environ.get("TEMP") == str(scratch)
            assert os.environ.get("TMP") == str(scratch)
            assert os.environ.get("TMPDIR") == str(scratch)
            fd, name = tempfile.mkstemp()
            try:
                assert runner._is_relative_to(Path(name).resolve(), run_dir.resolve())
            finally:
                os.close(fd)
                os.unlink(name)
            with tempfile.NamedTemporaryFile() as stream:
                assert runner._is_relative_to(Path(stream.name).resolve(), run_dir.resolve())
        assert tempfile.tempdir is None  # restored to the uninitialized entry value
        assert {key: os.environ.get(key) for key in ("TEMP", "TMP", "TMPDIR")} == prior_env
        assert not scratch.exists()
    finally:
        tempfile.tempdir = prior_tempdir
        for key, value in prior_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def test_nested_isolation_run_local_tempdir_restores_prior_values(tmp_path: Path):
    public_cases = tmp_path / "cases_public.jsonl"
    public_catalog = tmp_path / "catalog.jsonl"
    public_cases.write_text("{}\n", encoding="utf-8")
    public_catalog.write_text("{}\n", encoding="utf-8")
    outer_run = tmp_path / "outer-run"
    inner_run = tmp_path / "inner-run"
    outer_scratch = outer_run / runner.RUN_LOCAL_TEMP_DIR_NAME
    inner_scratch = inner_run / runner.RUN_LOCAL_TEMP_DIR_NAME
    prior_tempdir = tempfile.tempdir
    prior_env = {key: os.environ.get(key) for key in ("TEMP", "TMP", "TMPDIR")}
    try:
        with runner.public_runtime_isolation(
            public_cases_path=public_cases, public_catalog_path=public_catalog,
            run_dir=outer_run,
        ):
            assert Path(tempfile.gettempdir()).resolve() == outer_scratch.resolve()
            with runner.public_runtime_isolation(
                public_cases_path=public_cases, public_catalog_path=public_catalog,
                run_dir=inner_run,
            ):
                assert Path(tempfile.gettempdir()).resolve() == inner_scratch.resolve()
                assert inner_scratch != outer_scratch
            # inner exit restored the outer scratch, not the pre-isolation value
            assert Path(tempfile.gettempdir()).resolve() == outer_scratch.resolve()
            assert os.environ.get("TEMP") == str(outer_scratch)
        assert tempfile.tempdir == prior_tempdir
        assert {key: os.environ.get(key) for key in ("TEMP", "TMP", "TMPDIR")} == prior_env
        assert not outer_scratch.exists()
        assert not inner_scratch.exists()
    finally:
        tempfile.tempdir = prior_tempdir
        for key, value in prior_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def test_nested_same_run_dir_reuses_scratch_without_double_cleanup(tmp_path: Path):
    public_cases = tmp_path / "cases_public.jsonl"
    public_catalog = tmp_path / "catalog.jsonl"
    public_cases.write_text("{}\n", encoding="utf-8")
    public_catalog.write_text("{}\n", encoding="utf-8")
    run_dir = tmp_path / "shared-run"
    scratch = run_dir / runner.RUN_LOCAL_TEMP_DIR_NAME
    with runner.public_runtime_isolation(
        public_cases_path=public_cases, public_catalog_path=public_catalog,
        run_dir=run_dir,
    ):
        assert scratch.is_dir()
        with runner.public_runtime_isolation(
            public_cases_path=public_cases, public_catalog_path=public_catalog,
            run_dir=run_dir,
        ):
            assert Path(tempfile.gettempdir()).resolve() == scratch.resolve()
        # the inner entry must not remove the scratch it did not create
        assert scratch.is_dir()
        assert Path(tempfile.gettempdir()).resolve() == scratch.resolve()
    assert not scratch.exists()


def test_isolation_restores_tempdir_env_open_and_meta_path_after_exception(tmp_path: Path):
    public_cases = tmp_path / "cases_public.jsonl"
    public_catalog = tmp_path / "catalog.jsonl"
    public_cases.write_text("{}\n", encoding="utf-8")
    public_catalog.write_text("{}\n", encoding="utf-8")
    run_dir = tmp_path / "run"
    prior_tempdir = tempfile.tempdir
    prior_env = {key: os.environ.get(key) for key in ("TEMP", "TMP", "TMPDIR")}
    prior_meta = list(sys.meta_path)
    prior_open = __import__("builtins").open
    try:
        with pytest.raises(RuntimeError, match="boom"):
            with runner.public_runtime_isolation(
                public_cases_path=public_cases, public_catalog_path=public_catalog,
                run_dir=run_dir,
            ):
                assert runner._is_relative_to(
                    Path(tempfile.gettempdir()).resolve(), run_dir.resolve(),
                )
                assert __import__("builtins").open is not prior_open
                assert isinstance(sys.meta_path[0], runner._DeniedImportFinder)
                raise RuntimeError("boom")
        assert tempfile.tempdir == prior_tempdir
        assert {key: os.environ.get(key) for key in ("TEMP", "TMP", "TMPDIR")} == prior_env
        assert __import__("builtins").open is prior_open
        assert list(sys.meta_path) == prior_meta
        assert not (run_dir / runner.RUN_LOCAL_TEMP_DIR_NAME).exists()
    finally:
        tempfile.tempdir = prior_tempdir
        for key, value in prior_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def test_explicit_system_temp_and_other_external_writes_are_still_rejected(tmp_path: Path):
    public_cases = tmp_path / "cases_public.jsonl"
    public_catalog = tmp_path / "catalog.jsonl"
    public_cases.write_text("{}\n", encoding="utf-8")
    public_catalog.write_text("{}\n", encoding="utf-8")
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    system_temp = Path(tempfile.gettempdir()).resolve()
    with runner.public_runtime_isolation(
        public_cases_path=public_cases, public_catalog_path=public_catalog,
        run_dir=run_dir,
    ):
        # default tempfile is redirected to the run-local scratch
        assert runner._is_relative_to(
            Path(tempfile.gettempdir()).resolve(), run_dir.resolve(),
        )
        probe = system_temp / "runner-external-temp-probe.txt"
        for attempt in (
            lambda: open(probe, "w").close(),
            lambda: os.close(os.open(probe, os.O_WRONLY | os.O_CREAT)),
            lambda: probe.write_text("x", encoding="utf-8"),
            lambda: Path(run_dir.parent / "escape.json").write_text("x", encoding="utf-8"),
        ):
            with pytest.raises(runner.PublicIsolationError, match="outside"):
                attempt()
        assert not probe.exists()
        assert not (run_dir.parent / "escape.json").exists()


def test_run_public_agent_provisions_and_cleans_run_local_tempdir(tmp_path: Path, monkeypatch):
    # Production always imports agent.app.settings (and the seven-attribute
    # production module) before entering the isolation, so the lazy citation
    # import inside the guarded block is a cache hit instead of a .env read.
    # Pre-warm the same surface here so the test is order-independent.
    import agent.app.domains.ecommerce.used_phone_attributes  # noqa: F401
    from agent.app.settings import settings as _settings  # noqa: F401

    bundle = _fixture_bundle(tmp_path)
    run_dir = tmp_path / "temp-run"

    async def behavior(*, case, **_kwargs):
        assert runner._is_relative_to(
            Path(tempfile.gettempdir()).resolve(), run_dir.resolve(),
        )
        with tempfile.NamedTemporaryFile() as stream:
            assert runner._is_relative_to(Path(stream.name).resolve(), run_dir.resolve())
        return _success_result(case.case_id)

    _patch_orchestration(monkeypatch, bundle, behavior)
    manifest = asyncio.run(runner.run_public_agent(
        public_cases_path=bundle.public_cases_path,
        public_catalog_path=bundle.public_catalog_path,
        run_dir=run_dir, client=_unused_client(), model_name="fixture",
    ))
    assert manifest["execution"] == {bundle.cases[0].case_id: True}
    assert manifest["modelCallCount"] == 0
    assert manifest["businessWriteNetworkUsed"] is False
    assert manifest["hiddenArtifactsRead"] is False
    assert not (run_dir / runner.RUN_LOCAL_TEMP_DIR_NAME).exists()
    assert (run_dir / "run_contract.json").is_file()
    assert (run_dir / "manifest.json").is_file()
    assert (run_dir / "predictions.jsonl").is_file()


def test_fresh_subprocess_tempfile_provisioning_is_no_model_no_business(tmp_path: Path):
    """A fresh interpreter starts with tempfile.tempdir=None; the run-local
    provisioning must redirect the default-temp-dir probe into the run directory,
    reject explicit system-temp writes, restore every override, and never touch a
    model or business network path (fake/no-model subprocess)."""
    run_root = tmp_path / "subprocess"
    run_root.mkdir()
    script = r"""
import json
import os
import sys
import tempfile
from pathlib import Path

import agent.evaluation.used_phone_public_agent_runner_v1 as runner

root = Path(sys.argv[1]).resolve()
run_dir = root / "run"
run_dir.mkdir(parents=True)
cases = root / "cases_public.jsonl"
catalog = root / "catalog.jsonl"
cases.write_text("{}\n", encoding="utf-8")
catalog.write_text("{}\n", encoding="utf-8")

result = {
    "tempdirStartedNone": tempfile.tempdir is None,
    "modelCallCount": 0,
    "businessWriteNetworkUsed": False,
}
system_temp = Path(tempfile.gettempdir()).resolve()
prior_tempdir = tempfile.tempdir
prior_env = {key: os.environ.get(key) for key in ("TEMP", "TMP", "TMPDIR")}
try:
    with runner.public_runtime_isolation(
        public_cases_path=cases, public_catalog_path=catalog, run_dir=run_dir,
    ):
        scratch = run_dir / runner.RUN_LOCAL_TEMP_DIR_NAME
        result["gettempdirUnderRunDir"] = (
            Path(tempfile.gettempdir()).resolve() == scratch.resolve()
        )
        fd, name = tempfile.mkstemp()
        try:
            result["mkstempUnderRunDir"] = runner._is_relative_to(
                Path(name).resolve(), run_dir.resolve(),
            )
        finally:
            os.close(fd)
            os.unlink(name)
        with tempfile.NamedTemporaryFile() as stream:
            result["namedTempUnderRunDir"] = runner._is_relative_to(
                Path(stream.name).resolve(), run_dir.resolve(),
            )
        result["externalTempWriteRejected"] = False
        try:
            (system_temp / "runner-external-temp-probe.txt").write_text(
                "x", encoding="utf-8",
            )
        except runner.PublicIsolationError:
            result["externalTempWriteRejected"] = True
    result["tempdirRestored"] = tempfile.tempdir == prior_tempdir
    result["envRestored"] = (
        {key: os.environ.get(key) for key in ("TEMP", "TMP", "TMPDIR")} == prior_env
    )
    result["scratchCleaned"] = not scratch.exists()
except BaseException as exc:  # noqa: BLE001
    result["error"] = f"{type(exc).__name__}: {exc}"
    print(json.dumps(result))
    raise SystemExit(1)
print(json.dumps(result))
"""
    env = dict(os.environ)
    env["PYTHONPATH"] = str(runner.REPO_ROOT)
    completed = subprocess.run(
        [sys.executable, "-c", script, str(run_root)],
        cwd=str(runner.REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert completed.returncode == 0, (
        f"subprocess temp provisioning failed:\nstdout={completed.stdout}\nstderr={completed.stderr}"
    )
    result = json.loads(completed.stdout)
    assert result["tempdirStartedNone"] is True
    assert result["gettempdirUnderRunDir"] is True
    assert result["mkstempUnderRunDir"] is True
    assert result["namedTempUnderRunDir"] is True
    assert result["externalTempWriteRejected"] is True
    assert result["tempdirRestored"] is True
    assert result["envRestored"] is True
    assert result["scratchCleaned"] is True
    assert result["modelCallCount"] == 0
    assert result["businessWriteNetworkUsed"] is False


def test_wrong_business_tool_is_rejected_before_dispatch():
    import agent.app.tools as tools_module

    ledger = []
    with runner.production_network_guard(ledger):
        with pytest.raises(runner.PublicIsolationError, match="non-read"):
            asyncio.run(tools_module.call_tool("create_order", {}))
    assert ledger == []


def test_production_runtime_config_is_pinned_and_restored():
    from agent.app.settings import settings

    original = {
        "agent_context_mode": settings.agent_context_mode,
        "agent_orchestrator_mode": settings.agent_orchestrator_mode,
        "agent_legacy_fallback_enabled": settings.agent_legacy_fallback_enabled,
        "agent_request_deadline_seconds": settings.agent_request_deadline_seconds,
        "agent_tool_transport_mode": settings.agent_tool_transport_mode,
        "ecommerce_guide_enabled": settings.ecommerce_guide_enabled,
        "agent_transaction_enabled": settings.agent_transaction_enabled,
        "evidence_critic_enabled": settings.evidence_critic_enabled,
        "product_retrieval_mode": settings.product_retrieval_mode,
        "backend_base_url": settings.backend_base_url,
    }
    with runner.production_network_guard([]):
        assert settings.agent_context_mode == "context_pack"
        assert settings.agent_orchestrator_mode == "unified"
        assert settings.agent_legacy_fallback_enabled is False
        assert settings.agent_request_deadline_seconds == 20.0
        assert settings.agent_tool_transport_mode == "live"
        assert settings.ecommerce_guide_enabled is True
        assert settings.agent_transaction_enabled is False
        assert settings.evidence_critic_enabled is False
        assert settings.product_retrieval_mode == "bm25"
        assert settings.backend_base_url == runner.JAVA_BASE_URL
    assert {
        "agent_context_mode": settings.agent_context_mode,
        "agent_orchestrator_mode": settings.agent_orchestrator_mode,
        "agent_legacy_fallback_enabled": settings.agent_legacy_fallback_enabled,
        "agent_request_deadline_seconds": settings.agent_request_deadline_seconds,
        "agent_tool_transport_mode": settings.agent_tool_transport_mode,
        "ecommerce_guide_enabled": settings.ecommerce_guide_enabled,
        "agent_transaction_enabled": settings.agent_transaction_enabled,
        "evidence_critic_enabled": settings.evidence_critic_enabled,
        "product_retrieval_mode": settings.product_retrieval_mode,
        "backend_base_url": settings.backend_base_url,
    } == original


def test_frozen_v1_scope_pin_fails_closed_after_later_production_change():
    assert runner.PRODUCTION_CODE_SCOPE_SHA256 == (
        "f948ee5aae0fd30657d187dcca0d61803ef1e2f488a9462ade64c21e507ec3fe"
    )
    assert runner.production_scope_sha256() != runner.PRODUCTION_CODE_SCOPE_SHA256
    with pytest.raises(runner.PublicRunnerError, match="production code scope SHA mismatch"):
        runner.verify_production_scope()


def test_public_loader_rejects_tamper_before_parsing(tmp_path: Path, monkeypatch):
    cases = tmp_path / "cases_public.jsonl"
    catalog = tmp_path / "catalog.jsonl"
    cases.write_text("{}\n", encoding="utf-8")
    catalog.write_text("{}\n", encoding="utf-8")
    with pytest.raises(runner.PublicRunnerError, match="SHA mismatch"):
        runner.load_public_bundle(cases, catalog)


def test_java_preflight_pins_manifest_ids_attribute_count_and_projection(monkeypatch):
    bundle = runner.PublicBundle((_case(),), _catalog("1001"), Path("cases_public.jsonl"), Path("catalog.jsonl"))
    product = {key: None for key in runner.JAVA_PRODUCT_FIELDS}
    product["id"] = 1001
    product["attributes"] = [{key: None for key in runner.JAVA_ATTRIBUTE_FIELDS}]
    payload_sha = hashlib.sha256(json.dumps(
        [product], ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")).hexdigest()
    monkeypatch.setattr(runner, "PUBLIC_CATALOG_COUNT", 1)
    monkeypatch.setattr(runner, "JAVA_ATTRIBUTE_COUNT", 1)
    monkeypatch.setattr(runner, "JAVA_PROJECTION_SHA256", payload_sha)

    def fake_get(url):
        if url.endswith("/internal/catalog/manifest"):
            return {"success": True, "data": {
                "catalogVersion": runner.JAVA_CATALOG_VERSION,
                "productCount": 1,
                "contentHash": runner.JAVA_CATALOG_CONTENT_SHA256,
            }}
        return {"success": True, "data": {"items": [1001], "complete": True}}

    monkeypatch.setattr(runner, "_json_get", fake_get)
    monkeypatch.setattr(runner, "_json_post", lambda *_args, **_kwargs: {"success": True, "data": [product]})
    result = runner.verify_java_public_catalog(bundle)
    assert result["productCount"] == result["attributeCount"] == 1
    assert result["javaProjectionSha256"] == payload_sha


class _UnusedCompletions:
    async def create(self, **_kwargs):
        raise AssertionError("fixture orchestration must not call a model")


def _unused_client():
    raw = SimpleNamespace(chat=SimpleNamespace(completions=_UnusedCompletions()))
    return runner.RecordingClient(raw)


def _patch_orchestration(monkeypatch, bundle, behavior):
    monkeypatch.setattr(runner, "verify_production_scope", lambda: {"sha256": runner.PRODUCTION_CODE_SCOPE_SHA256})
    monkeypatch.setattr(runner, "load_public_bundle", lambda *_args, **_kwargs: bundle)
    monkeypatch.setattr(runner, "verify_java_public_catalog", lambda *_args, **_kwargs: {
        "attributeCount": 1547,
        "catalogVersion": runner.JAVA_CATALOG_VERSION,
        "contentSha256": runner.JAVA_CATALOG_CONTENT_SHA256,
        "javaProjectionSha256": runner.JAVA_PROJECTION_SHA256,
        "productCount": 252,
        "resolveBatchCount": 26,
    })
    monkeypatch.setattr(runner, "run_case", behavior)


def _fixture_bundle(tmp_path: Path, cases=None):
    public_cases = tmp_path / "cases_public.jsonl"
    public_catalog = tmp_path / "catalog.jsonl"
    public_cases.write_text("{}\n", encoding="utf-8")
    public_catalog.write_text("{}\n", encoding="utf-8")
    case_rows = tuple(cases or [_case()])
    return runner.PublicBundle(case_rows, _catalog("1001", "1002"), public_cases, public_catalog)


def _success_result(case_id: str) -> runner.CaseResult:
    case = _case(case_id)
    tool_trace = _search_trace("1001")
    result_payload = {key: value for key, value in tool_trace.items() if key != "publicTurnIndex"}
    invocation = {
        "index": 0,
        "tool": "search_products",
        "arguments": {"query": "fixture", "category": "手机"},
        "argumentsSha256": hashlib.sha256(runner.canonical_json_bytes(
            {"query": "fixture", "category": "手机"},
        )).hexdigest(),
        "outcome": "success",
        "resultSha256": hashlib.sha256(runner.canonical_json_bytes(result_payload)).hexdigest(),
        "publicTurnIndex": 0,
    }
    prediction = runner.materialize_prediction(
        case=case, catalog=_catalog("1001", "1002"),
        tool_traces=[tool_trace], snapshots=_snapshots(),
        selection=_selection("RETRIEVE_FILTER_AND_RANK"),
        tool_invocations=[invocation],
    )
    return runner.CaseResult(prediction, {
        "schemaVersion": "used-phone-public-agent-trace-v1",
        "caseId": case_id,
        "toolTraces": [tool_trace],
        "toolInvocations": [invocation],
        "taskStateSnapshots": _snapshots(),
        "networkCalls": [],
        "protocolVersion": runner.PROTOCOL_VERSION,
    })


def test_new_run_directory_failure_is_fail_closed(tmp_path: Path, monkeypatch):
    bundle = _fixture_bundle(tmp_path)

    async def never(**_kwargs):
        raise AssertionError("must not run")

    _patch_orchestration(monkeypatch, bundle, never)
    run_dir = tmp_path / "already-exists"
    run_dir.mkdir()
    with pytest.raises(FileExistsError):
        asyncio.run(runner.run_public_agent(
            public_cases_path=bundle.public_cases_path,
            public_catalog_path=bundle.public_catalog_path,
            run_dir=run_dir,
            client=_unused_client(), model_name="fixture",
        ))
    assert list(run_dir.iterdir()) == []


def test_failure_terminal_stays_in_denominator_and_resume_uses_new_attempt(tmp_path: Path, monkeypatch):
    bundle = _fixture_bundle(tmp_path)

    async def fail(**_kwargs):
        raise RuntimeError("fixture failure")

    _patch_orchestration(monkeypatch, bundle, fail)
    run_dir = tmp_path / "run"
    first = asyncio.run(runner.run_public_agent(
        public_cases_path=bundle.public_cases_path,
        public_catalog_path=bundle.public_catalog_path,
        run_dir=run_dir,
        client=_unused_client(), model_name="fixture",
    ))
    case_id = bundle.cases[0].case_id
    assert first["execution"] == {case_id: False}
    assert json.loads((run_dir / "predictions.jsonl").read_text(encoding="utf-8")) == {
        "caseId": case_id, "rankedItemIds": [],
    }
    attempt1 = run_dir / "cases" / case_id / "attempt-001"
    assert (attempt1 / "failure.json").is_file()
    assert not (attempt1 / "running.json").exists()

    async def succeed(*, case, **_kwargs):
        return _success_result(case.case_id)

    monkeypatch.setattr(runner, "run_case", succeed)
    second = asyncio.run(runner.run_public_agent(
        public_cases_path=bundle.public_cases_path,
        public_catalog_path=bundle.public_catalog_path,
        run_dir=run_dir,
        resume=True,
        client=_unused_client(), model_name="fixture",
    ))
    attempt2 = run_dir / "cases" / case_id / "attempt-002"
    assert second["execution"] == {case_id: True}
    assert (attempt1 / "failure.json").is_file()
    assert (attempt2 / "success.json").is_file()


def test_interrupted_attempt_is_preserved_and_resume_advances(tmp_path: Path, monkeypatch):
    bundle = _fixture_bundle(tmp_path)

    async def fail(**_kwargs):
        raise RuntimeError("first")

    _patch_orchestration(monkeypatch, bundle, fail)
    run_dir = tmp_path / "run"
    asyncio.run(runner.run_public_agent(
        public_cases_path=bundle.public_cases_path,
        public_catalog_path=bundle.public_catalog_path,
        run_dir=run_dir, client=_unused_client(), model_name="fixture",
    ))
    case_id = bundle.cases[0].case_id
    attempt2 = run_dir / "cases" / case_id / "attempt-002"
    attempt2.mkdir()
    (attempt2 / "running.json").write_bytes(runner.canonical_json_bytes({"terminalState": "running"}))

    async def succeed(*, case, **_kwargs):
        return _success_result(case.case_id)

    monkeypatch.setattr(runner, "run_case", succeed)
    asyncio.run(runner.run_public_agent(
        public_cases_path=bundle.public_cases_path,
        public_catalog_path=bundle.public_catalog_path,
        run_dir=run_dir, resume=True, client=_unused_client(), model_name="fixture",
    ))
    assert (attempt2 / "running.json").is_file()
    assert (run_dir / "cases" / case_id / "attempt-003" / "success.json").is_file()


def test_resume_rejects_protocol_or_code_data_hash_change(tmp_path: Path, monkeypatch):
    bundle = _fixture_bundle(tmp_path)

    async def succeed(*, case, **_kwargs):
        return _success_result(case.case_id)

    _patch_orchestration(monkeypatch, bundle, succeed)
    run_dir = tmp_path / "run"
    asyncio.run(runner.run_public_agent(
        public_cases_path=bundle.public_cases_path,
        public_catalog_path=bundle.public_catalog_path,
        run_dir=run_dir, client=_unused_client(), model_name="fixture",
    ))
    manifest = json.loads((run_dir / "run_contract.json").read_text(encoding="utf-8"))
    assert manifest["contract"]["productionRuntimeConfig"] == runner.PRODUCTION_RUNTIME_CONFIG
    manifest["contractSha256"] = "0" * 64
    (run_dir / "run_contract.json").write_bytes(runner.canonical_json_bytes(manifest))
    with pytest.raises(runner.PublicRunnerError, match="contract mismatch"):
        asyncio.run(runner.run_public_agent(
            public_cases_path=bundle.public_cases_path,
            public_catalog_path=bundle.public_catalog_path,
            run_dir=run_dir, resume=True, client=_unused_client(), model_name="fixture",
        ))


def _create_valid_success_run(tmp_path: Path, monkeypatch):
    bundle = _fixture_bundle(tmp_path)

    async def succeed(*, case, **_kwargs):
        return _success_result(case.case_id)

    _patch_orchestration(monkeypatch, bundle, succeed)
    run_dir = tmp_path / "valid-run"
    asyncio.run(runner.run_public_agent(
        public_cases_path=bundle.public_cases_path,
        public_catalog_path=bundle.public_catalog_path,
        run_dir=run_dir, client=_unused_client(), model_name="fixture",
    ))
    attempt = run_dir / "cases" / bundle.cases[0].case_id / "attempt-001"
    return bundle, run_dir, attempt


def test_resume_validates_and_reuses_untampered_success(tmp_path: Path, monkeypatch):
    bundle, run_dir, attempt = _create_valid_success_run(tmp_path, monkeypatch)
    result = asyncio.run(runner.run_public_agent(
        public_cases_path=bundle.public_cases_path,
        public_catalog_path=bundle.public_catalog_path,
        run_dir=run_dir, resume=True, client=_unused_client(), model_name="fixture",
    ))
    assert result["execution"] == {bundle.cases[0].case_id: True}
    assert not (attempt.parent / "attempt-002").exists()


@pytest.mark.parametrize(
    "tamper",
    [
        "terminal_contract", "terminal_context", "terminal_case", "terminal_schema",
        "runtime_identity", "prediction_bytes", "prediction_catalog_item",
        "trace_protocol", "trace_provenance", "missing_trace", "multiple_terminals",
    ],
)
def test_resume_success_terminal_prediction_and_trace_tamper_fail_closed(
    tmp_path: Path, monkeypatch, tamper,
):
    bundle, run_dir, attempt = _create_valid_success_run(tmp_path, monkeypatch)
    terminal_path = attempt / "success.json"
    prediction_path = attempt / "prediction.json"
    trace_path = attempt / "trace.json"
    terminal = json.loads(terminal_path.read_text(encoding="utf-8"))
    if tamper == "terminal_contract":
        terminal["contractSha256"] = "0" * 64
    elif tamper == "terminal_context":
        terminal["contextSha256"] = "0" * 64
    elif tamper == "terminal_case":
        terminal["caseId"] = "UPV1-HC-02"
    elif tamper == "terminal_schema":
        terminal["schemaVersion"] = "old-success-v0"
    elif tamper == "runtime_identity":
        terminal["runtimeIdentity"]["protocolVersion"] = "old-protocol-v0"
    elif tamper == "prediction_bytes":
        prediction_path.write_bytes(prediction_path.read_bytes() + b" ")
    elif tamper == "prediction_catalog_item":
        prediction = json.loads(prediction_path.read_text(encoding="utf-8"))
        prediction["rankedItemIds"] = ["9999"]
        payload = runner.canonical_json_bytes(prediction)
        prediction_path.write_bytes(payload)
        terminal["prediction"] = prediction
        terminal["predictionSha256"] = hashlib.sha256(payload).hexdigest()
    elif tamper in {"trace_protocol", "trace_provenance"}:
        trace = json.loads(trace_path.read_text(encoding="utf-8"))
        if tamper == "trace_protocol":
            trace["protocolVersion"] = "old-protocol-v0"
        else:
            trace["toolInvocations"] = []
        payload = runner.canonical_json_bytes(trace)
        trace_path.write_bytes(payload)
        terminal["trace"] = trace
        terminal["traceSha256"] = hashlib.sha256(payload).hexdigest()
    elif tamper == "missing_trace":
        trace_path.unlink()
    elif tamper == "multiple_terminals":
        (attempt / "failure.json").write_text("{}\n", encoding="utf-8")
    if tamper not in {"prediction_bytes", "missing_trace", "multiple_terminals"}:
        terminal_path.write_bytes(runner.canonical_json_bytes(terminal))
    with pytest.raises(runner.PublicRunnerError):
        asyncio.run(runner.run_public_agent(
            public_cases_path=bundle.public_cases_path,
            public_catalog_path=bundle.public_catalog_path,
            run_dir=run_dir, resume=True, client=_unused_client(), model_name="fixture",
        ))
    assert not (attempt.parent / "attempt-002").exists()


@pytest.mark.parametrize(
    ("exception", "expected_outcome"),
    [(asyncio.TimeoutError(), "timeout"), (RuntimeError("failed"), "error")],
)
def test_case_failure_sidecar_and_manifest_classify_timeout_or_error(
    tmp_path: Path, monkeypatch, exception, expected_outcome,
):
    bundle = _fixture_bundle(tmp_path)

    async def fail(**_kwargs):
        raise exception

    _patch_orchestration(monkeypatch, bundle, fail)
    run_dir = tmp_path / expected_outcome
    manifest = asyncio.run(runner.run_public_agent(
        public_cases_path=bundle.public_cases_path,
        public_catalog_path=bundle.public_catalog_path,
        run_dir=run_dir, client=_unused_client(), model_name="fixture",
    ))
    case_id = bundle.cases[0].case_id
    failure = json.loads((
        run_dir / "cases" / case_id / "attempt-001" / "failure.json"
    ).read_text(encoding="utf-8"))
    assert failure["failureOutcome"] == expected_outcome
    assert manifest["failureOutcomes"] == {case_id: expected_outcome}


def test_caller_cancellation_writes_sidecar_and_manifest_then_reraises(
    tmp_path: Path, monkeypatch,
):
    bundle = _fixture_bundle(tmp_path)

    async def cancelled(**_kwargs):
        raise asyncio.CancelledError()

    _patch_orchestration(monkeypatch, bundle, cancelled)
    run_dir = tmp_path / "cancelled"
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(runner.run_public_agent(
            public_cases_path=bundle.public_cases_path,
            public_catalog_path=bundle.public_catalog_path,
            run_dir=run_dir, client=_unused_client(), model_name="fixture",
        ))
    case_id = bundle.cases[0].case_id
    failure = json.loads((
        run_dir / "cases" / case_id / "attempt-001" / "failure.json"
    ).read_text(encoding="utf-8"))
    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    assert failure["failureOutcome"] == "cancelled"
    assert manifest["failureOutcomes"] == {case_id: "cancelled"}


def test_sealed_test_is_denied_by_default(tmp_path: Path, monkeypatch):
    test_case = runner.PublicCase("UPV1-HC-01", "test", "hard_constraint", _case().turns, ())
    bundle = _fixture_bundle(tmp_path, [test_case])

    async def never(**_kwargs):
        raise AssertionError("must not execute sealed case")

    _patch_orchestration(monkeypatch, bundle, never)
    with pytest.raises(runner.PublicRunnerError, match="sealed test"):
        asyncio.run(runner.run_public_agent(
            public_cases_path=bundle.public_cases_path,
            public_catalog_path=bundle.public_catalog_path,
            run_dir=tmp_path / "run", splits=["test"],
            client=_unused_client(), model_name="fixture",
        ))
    with pytest.raises(runner.PublicRunnerError, match="sealed test"):
        asyncio.run(runner.run_public_agent(
            public_cases_path=bundle.public_cases_path,
            public_catalog_path=bundle.public_catalog_path,
            run_dir=tmp_path / "run-by-id", case_ids=[test_case.case_id],
            client=_unused_client(), model_name="fixture",
        ))


def test_run_case_timeout_does_not_reach_projector(monkeypatch):
    import agent.app.llm as llm_module

    async def sleeping_run_agent(*_args, **_kwargs):
        await asyncio.sleep(0.2)

    monkeypatch.setattr(llm_module, "run_agent", sleeping_run_agent)
    client = _unused_client()
    with pytest.raises(asyncio.TimeoutError):
        asyncio.run(runner.run_case(
            case=_case(), catalog=_catalog("1001"), client=client,
            model_name="fixture", timeout_seconds=0.01,
        ))
    assert client.ledger == []


def test_materializer_is_byte_deterministic():
    kwargs = {
        "case": _case(),
        "catalog": _catalog("1001"),
        "tool_traces": [_search_trace("1001", evidence=True)],
        "snapshots": _snapshots(),
        "selection": _selection("RETRIEVE_FILTER_AND_RANK"),
    }
    first = runner.canonical_json_bytes(runner.materialize_prediction(**kwargs))
    second = runner.canonical_json_bytes(runner.materialize_prediction(**deepcopy(kwargs)))
    assert first == second


@pytest.mark.skipif(
    not os.getenv("USED_PHONE_BENCHMARK_AUDIT_ASSETS_DIR"),
    reason="frozen public asset revision root not configured",
)
def test_real_subprocess_cli_audit_only_keeps_public_isolation():
    """Run the single official module entry in a real subprocess and assert the
    audit contract: 100 public cases, 252 catalog items, pinned Java projection
    SHA, zero model calls, no hidden-artifact reads and no business writes.

    Opt-in like the Benchmark DB integration tests: the operator points
    ``USED_PHONE_BENCHMARK_AUDIT_ASSETS_DIR`` at the frozen
    ``.../09807c77.../`` revision root that holds both public files.
    """
    asset_root = Path(os.environ["USED_PHONE_BENCHMARK_AUDIT_ASSETS_DIR"]).resolve()
    cases_path = asset_root / "used_phone_complex_cases_v1" / "cases_public.jsonl"
    catalog_path = asset_root / "used_phone_attribute_contract_v2" / "catalog.jsonl"
    assert cases_path.is_file(), f"missing frozen public cases: {cases_path}"
    assert catalog_path.is_file(), f"missing frozen public catalog: {catalog_path}"

    env = dict(os.environ)
    env["PYTHONPATH"] = str(runner.REPO_ROOT)
    completed = subprocess.run(
        [
            sys.executable, "-m",
            "agent.scripts.run_used_phone_public_agent_v1",
            "--audit-only",
            "--public-cases", str(cases_path),
            "--public-catalog", str(catalog_path),
        ],
        cwd=str(runner.REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert completed.returncode == 0, (
        f"audit-only CLI failed:\nstdout={completed.stdout}\nstderr={completed.stderr}"
    )
    result = json.loads(completed.stdout)
    assert result["caseCount"] == runner.PUBLIC_CASE_COUNT == 100
    assert result["catalogCount"] == runner.PUBLIC_CATALOG_COUNT == 252
    assert result["publicCasesSha256"] == runner.PUBLIC_CASES_SHA256
    assert result["publicCatalogSha256"] == runner.PUBLIC_CATALOG_SHA256
    assert result["modelCalls"] == 0
    assert result["hiddenArtifactsRead"] is False
    java = result["java"]
    assert java["catalogVersion"] == runner.JAVA_CATALOG_VERSION
    assert java["productCount"] == runner.PUBLIC_CATALOG_COUNT
    assert java["contentSha256"] == runner.JAVA_CATALOG_CONTENT_SHA256
    assert java["javaProjectionSha256"] == runner.JAVA_PROJECTION_SHA256
    assert java["businessWriteNetworkUsed"] is False
