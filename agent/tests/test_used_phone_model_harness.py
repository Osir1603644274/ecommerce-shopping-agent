from __future__ import annotations

import asyncio
from copy import deepcopy
import hashlib
import importlib.machinery
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import agent.evaluation.used_phone_model_harness as harness_module
from agent.evaluation.used_phone_model_harness import (
    CatalogBinding,
    CaseRunResult,
    CompositionError,
    InMemoryRedis,
    OfflineCatalogToolCaller,
    PublicIsolationError,
    RecordingClient,
    _assert_new_attempt_directory,
    _latest_prior_terminal,
    _load_success_record,
    _materialize_selection,
    _public_case_payload,
    _public_semantic_validate,
    _project_prediction,
    _requirements_from_extractor,
    _success_record,
    _write_manifest,
    business_call_guard,
    in_memory_task_state_store,
    llm_endpoint_network_guard,
    public_isolation_guard,
    run_public_model_composition,
)
from agent.app.task_state import TaskState
from agent.evaluation.used_phone_offline_runtime import canonical_json_bytes, load_public_bundle


REPO_ROOT = Path(__file__).resolve().parents[2]
DATA_ROOT = REPO_ROOT / "data" / "processed" / "ecommerce" / "kuaisearch_synthetic_evidence_track_b_v01" / "09807c773ce67360ed8df30842e372182fcf7ad9"
PUBLIC_DIR = DATA_ROOT / "used_phone_agent_pilot_v1"
EVIDENCE = DATA_ROOT / "evidence_products_audit.jsonl"


def _real_data():
    if not EVIDENCE.is_file():
        pytest.skip("pinned real data unavailable")
    bundle = load_public_bundle(PUBLIC_DIR)
    return bundle, CatalogBinding.load(EVIDENCE, bundle)


def test_catalog_fingerprint_maps_252_to_83_rows_and_68_products() -> None:
    bundle, catalog = _real_data()
    assert len(catalog.rows) == 252
    mappings = [item for case_map in catalog.public_to_item_by_case.values() for item in case_map.values()]
    assert len(mappings) == 83
    assert len(set(mappings)) == 68


def test_offline_tool_uses_only_case_pool_and_returns_integer_ids() -> None:
    bundle, catalog = _real_data()
    case = bundle.by_id()["BLIND-CASE-001"]
    caller = OfflineCatalogToolCaller(case, catalog)
    trace = asyncio.run(caller("search_products", {
        "query": case.query_text,
        "category": "手机",
        "requirements": [],
        "limit": 20,
    }))
    assert trace.ok
    assert trace.detail["catalogSize"] == 252
    assert trace.detail["casePoolSize"] == 16
    assert all(type(item) is int and item > 0 for item in trace.detail["candidateIds"])
    assert set(trace.detail["publicCandidateIds"]) == {item["candidateDisplayId"] for item in case.candidates}
    assert caller.calls[0]["arguments"]["query"] == case.query_text


def test_public_guard_denies_hidden_paths_and_imports(tmp_path: Path) -> None:
    hidden = tmp_path / "used_phone_agent_pilot_ai_judged_freeze_v1" / "qrel.jsonl"
    hidden.parent.mkdir()
    hidden.write_text("{}\n", encoding="utf-8")
    with public_isolation_guard():
        with pytest.raises(PublicIsolationError):
            hidden.read_text(encoding="utf-8")
        with pytest.raises(PublicIsolationError):
            __import__("agent.evaluation.used_phone_complex_metrics")

    import agent.evaluation.used_phone_complex_metrics as preloaded
    import agent.evaluation as evaluation_package

    assert preloaded is not None
    assert evaluation_package.used_phone_complex_metrics is preloaded
    with public_isolation_guard():
        assert "agent.evaluation.used_phone_complex_metrics" not in __import__("sys").modules
        assert "used_phone_complex_metrics" not in vars(evaluation_package)
        with pytest.raises(PublicIsolationError):
            __import__("agent.evaluation.used_phone_complex_metrics")
        with pytest.raises(PublicIsolationError):
            from agent.evaluation import used_phone_complex_metrics as _denied_from_parent  # noqa: F401
        builder = REPO_ROOT / "agent" / "evaluation" / "build_track_b_used_phone_agent_pilot.py"
        with pytest.raises(PublicIsolationError):
            builder.read_text(encoding="utf-8")
        with pytest.raises(PublicIsolationError):
            builder.read_bytes()
        innocent_alias = importlib.machinery.SourceFileLoader("innocent_alias", str(builder))
        with pytest.raises(PublicIsolationError):
            innocent_alias.get_data(str(builder))
        with pytest.raises(PublicIsolationError):
            __import__("agent.evaluation.used_phone_complex_pilot")
    assert __import__("sys").modules["agent.evaluation.used_phone_complex_metrics"] is preloaded
    assert evaluation_package.used_phone_complex_metrics is preloaded


def test_network_guard_allows_only_llm_origin(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_send(_client, request, *args, **kwargs):
        return SimpleNamespace(request=request)

    import httpx
    monkeypatch.setattr(httpx.AsyncClient, "send", fake_send)
    async def exercise() -> None:
        async with httpx.AsyncClient() as client:
            for base_url in ("https://api.deepseek.com", "https://api.deepseek.com:443"):
                with llm_endpoint_network_guard(base_url):
                    allowed = await client.send(httpx.Request(
                        "POST", "https://api.deepseek.com:443/chat/completions",
                    ))
                    assert allowed.request.url.host == "api.deepseek.com"
                    with pytest.raises(PublicIsolationError):
                        await client.send(httpx.Request("GET", "http://localhost:8080/api/products"))

    asyncio.run(exercise())
    with pytest.raises(CompositionError, match="api.deepseek.com"):
        with llm_endpoint_network_guard("http://localhost:8080"):
            pass
    with pytest.raises(CompositionError, match="api.deepseek.com"):
        with llm_endpoint_network_guard("https://localhost:443"):
            pass
    with pytest.raises(CompositionError, match="api.deepseek.com"):
        with llm_endpoint_network_guard("https://api.deepseek.com.example"):
            pass
    with pytest.raises(CompositionError, match="api.deepseek.com"):
        with llm_endpoint_network_guard("https://user:password@api.deepseek.com"):
            pass


def test_projector_receives_precomputed_citations_and_rejects_wrong_hash() -> None:
    bundle, _catalog = _real_data()
    case = bundle.by_id()["BLIND-CASE-001"]
    payload = _public_case_payload(case)
    option = payload["evidenceReferenceOptions"][0]
    candidate = next(item for item in case.candidates if item["candidateDisplayId"] == option["candidateDisplayId"])
    raw = candidate["evidenceRefs"][option["evidenceRefIndex"]]["rawValue"]
    assert option["rawValueSha256"] == hashlib.sha256(canonical_json_bytes(raw)).hexdigest()
    prediction = {
        "rankedCandidateIds": [], "candidateAssessments": [], "comparison": None,
        "answerClaims": [], "evidenceRefs": [{**option, "rawValueSha256": "0" * 64}],
    }
    with pytest.raises(CompositionError, match="hash mismatch"):
        _public_semantic_validate(prediction, case)
    _public_semantic_validate({**prediction, "evidenceRefs": [], "answerClaims": []}, case)


def _empty_selection(action: str = "CLARIFY") -> dict[str, object]:
    return {
        "predictedAction": action,
        "extractedConstraints": [],
        "rankedCandidateIds": [],
        "candidateAssessments": [],
        "comparison": None,
        "stateUpdate": None,
        "claimSelections": [],
    }


def _successful_tool_result(tool_name: str, candidate_ids: list[str]) -> dict[str, object]:
    if tool_name == "compare_products":
        detail = {"products": [
            {"product": {"candidateDisplayId": candidate_id}} for candidate_id in candidate_ids
        ]}
    else:
        detail = {"publicCandidateIds": candidate_ids}
    return {"toolName": tool_name, "trace": {"ok": True, "detail": detail}}


def _successful_compare_result(options: list[dict[str, object]]) -> dict[str, object]:
    candidate_ids = list(dict.fromkeys(str(option["candidateDisplayId"]) for option in options))
    evidence = [{
        "ref": (
            f"public:{option['candidateDisplayId']}:{option['evidenceRefIndex']}:{option['factGroup']}"
        ),
        "candidateDisplayId": option["candidateDisplayId"],
        "evidenceRefIndex": option["evidenceRefIndex"],
        "field": option["factGroup"],
        "value": option["normalizedValue"],
    } for option in options]
    return {
        "toolName": "compare_products",
        "trace": {
            "ok": True,
            "detail": {
                "products": [
                    {"product": {"candidateDisplayId": candidate_id}} for candidate_id in candidate_ids
                ],
                "evidence": evidence,
                "evidenceRefs": [item["ref"] for item in evidence],
            },
        },
    }


def _prediction_schema() -> dict[str, object]:
    path = REPO_ROOT / "agent" / "evaluation" / "schemas" / "used_phone_complex_prediction_v1.schema.json"
    return json.loads(path.read_text(encoding="utf-8"))


def test_selection_string_ref_materializes_claim_and_assessment_citations() -> None:
    bundle, _catalog = _real_data()
    case = bundle.by_id()["BLIND-CASE-001"]
    option = _public_case_payload(case)["evidenceReferenceOptions"][0]
    candidate_id = option["candidateDisplayId"]
    selection = _empty_selection("RETRIEVE_FILTER_AND_RANK")
    selection.update({
        "rankedCandidateIds": [candidate_id],
        "candidateAssessments": [{
            "candidateDisplayId": candidate_id,
            "eligible": True,
            "hardViolations": [],
            "hardUnknowns": [],
            "assessmentFacts": [{
                "group": option["factGroup"], "normalizedValue": option["normalizedValue"],
            }],
            "evidenceSelections": [{
                "group": option["factGroup"], "evidenceRefId": option["evidenceRefId"],
            }],
        }],
        "claimSelections": [{
            "candidateDisplayId": candidate_id,
            "factGroup": option["factGroup"],
            "normalizedValue": option["normalizedValue"],
            "evidenceRefIds": [option["evidenceRefId"]],
        }],
    })
    materialized = _materialize_selection(
        selection,
        case=case,
        tool_calls=[_successful_tool_result("search_products", [candidate_id])],
    )
    citation = materialized["candidateAssessments"][0]["evidenceCitations"][0]
    assert "evidenceRefId" not in citation
    assert set(citation) == {
        "candidateDisplayId", "evidenceRefIndex", "field", "rawValueSha256",
        "factGroup", "normalizedValue",
    }
    assert materialized["evidenceRefs"] == [option]
    assert materialized["answerClaims"][0]["text"] == (
        f"candidate_fact:{candidate_id}:{option['factGroup']}={option['normalizedValue']}"
    )


def test_comparison_selection_materializes_only_selected_public_refs() -> None:
    bundle, _catalog = _real_data()
    case = bundle.by_id()["BLIND-CASE-005"]
    visible_ids = [str(row["candidateDisplayId"]) for row in case.user_visible_candidate_context]
    options = _public_case_payload(case)["evidenceReferenceOptions"]
    by_candidate_group = {
        (option["candidateDisplayId"], option["factGroup"]): option for option in options
    }
    common_groups = [
        group for group in harness_module.PUBLIC_FACT_GROUPS
        if all((candidate_id, group) in by_candidate_group for candidate_id in visible_ids)
    ]
    group = common_groups[0]
    selected_options = [by_candidate_group[(candidate_id, group)] for candidate_id in visible_ids]
    selection = _empty_selection("COMPARE_WITH_FIELD_EVIDENCE")
    selection["comparison"] = {
        "candidateDisplayIds": {"candidateA": visible_ids[0], "candidateB": visible_ids[1]},
        "preferredCandidateDisplayId": visible_ids[0],
        "fieldComparisons": [{
            "field": group,
            "candidateValues": {
                option["candidateDisplayId"]: option["normalizedValue"] for option in selected_options
            },
            "missingEvidenceCandidateIds": [],
            "evidenceRefIds": [option["evidenceRefId"] for option in selected_options],
        }],
        "tradeoffDisclosures": [],
        "missingEvidenceStated": False,
    }
    with pytest.raises(CompositionError, match="absent from actual compare trace"):
        _materialize_selection(
            selection,
            case=case,
            tool_calls=[_successful_tool_result("compare_products", visible_ids)],
        )
    materialized = _materialize_selection(
        selection, case=case, tool_calls=[_successful_compare_result(selected_options)],
    )
    citations = materialized["comparison"]["fieldComparisons"][0]["evidenceCitations"]
    assert len(citations) == 2
    assert all("evidenceRefId" not in citation for citation in citations)
    assert {row["evidenceRefId"] for row in materialized["evidenceRefs"]} == {
        option["evidenceRefId"] for option in selected_options
    }


def test_assessment_evidence_group_must_match_declared_claim() -> None:
    bundle, _catalog = _real_data()
    case = bundle.by_id()["BLIND-CASE-001"]
    option = next(
        row for row in _public_case_payload(case)["evidenceReferenceOptions"]
        if row["factGroup"] == "battery_health"
    )
    candidate_id = option["candidateDisplayId"]
    selection = _empty_selection("RETRIEVE_FILTER_AND_RANK")
    selection["candidateAssessments"] = [{
        "candidateDisplayId": candidate_id,
        "eligible": False,
        "hardViolations": [{"group": "motherboard_repair"}],
        "hardUnknowns": [],
        "assessmentFacts": [],
        "evidenceSelections": [{
            "group": "motherboard_repair", "evidenceRefId": option["evidenceRefId"],
        }],
    }]
    with pytest.raises(CompositionError, match="factGroup mismatch"):
        _materialize_selection(
            selection,
            case=case,
            tool_calls=[_successful_tool_result("search_products", [candidate_id])],
        )


def test_selection_rejects_unknown_or_mismatched_evidence_identity() -> None:
    bundle, _catalog = _real_data()
    case = bundle.by_id()["BLIND-CASE-001"]
    option = _public_case_payload(case)["evidenceReferenceOptions"][0]
    other_candidate = next(
        row["candidateDisplayId"] for row in case.candidates
        if row["candidateDisplayId"] != option["candidateDisplayId"]
    )
    base = _empty_selection("RETRIEVE_FILTER_AND_RANK")
    base["claimSelections"] = [{
        "candidateDisplayId": option["candidateDisplayId"],
        "factGroup": option["factGroup"],
        "normalizedValue": option["normalizedValue"],
        "evidenceRefIds": [option["evidenceRefId"]],
    }]
    tool_calls = [_successful_tool_result(
        "search_products", [option["candidateDisplayId"], other_candidate],
    )]
    mutations = []
    unknown = deepcopy(base)
    unknown["claimSelections"][0]["evidenceRefIds"] = ["evidence-999"]
    mutations.append(unknown)
    wrong_candidate = deepcopy(base)
    wrong_candidate["claimSelections"][0]["candidateDisplayId"] = other_candidate
    mutations.append(wrong_candidate)
    wrong_group = deepcopy(base)
    wrong_group["claimSelections"][0]["factGroup"] = next(
        group for group in harness_module.PUBLIC_FACT_GROUPS if group != option["factGroup"]
    )
    mutations.append(wrong_group)
    wrong_value = deepcopy(base)
    wrong_value["claimSelections"][0]["normalizedValue"] = next(
        value for value in harness_module.NORMALIZED_VALUES if value != option["normalizedValue"]
    )
    mutations.append(wrong_value)
    other_case_option = _public_case_payload(bundle.by_id()["BLIND-CASE-002"])[
        "evidenceReferenceOptions"
    ][0]
    wrong_case = deepcopy(base)
    wrong_case["claimSelections"][0] = {
        "candidateDisplayId": other_case_option["candidateDisplayId"],
        "factGroup": other_case_option["factGroup"],
        "normalizedValue": other_case_option["normalizedValue"],
        "evidenceRefIds": [other_case_option["evidenceRefId"]],
    }
    mutations.append(wrong_case)
    for invalid in mutations:
        with pytest.raises(CompositionError):
            _materialize_selection(invalid, case=case, tool_calls=tool_calls)


def test_selection_cannot_rank_or_compare_without_actual_tool_results() -> None:
    bundle, _catalog = _real_data()
    retrieval_case = bundle.by_id()["BLIND-CASE-001"]
    ranked = _empty_selection("RETRIEVE_FILTER_AND_RANK")
    ranked["rankedCandidateIds"] = [retrieval_case.candidates[0]["candidateDisplayId"]]
    with pytest.raises(CompositionError, match="without actual tool results"):
        _materialize_selection(ranked, case=retrieval_case, tool_calls=[])

    comparison_case = bundle.by_id()["BLIND-CASE-005"]
    visible_ids = [str(row["candidateDisplayId"]) for row in comparison_case.user_visible_candidate_context]
    comparison = _empty_selection("COMPARE_WITH_FIELD_EVIDENCE")
    comparison["comparison"] = {
        "candidateDisplayIds": {"candidateA": visible_ids[0], "candidateB": visible_ids[1]},
        "preferredCandidateDisplayId": visible_ids[0],
        "fieldComparisons": [{
            "field": "battery_health",
            "candidateValues": {visible_ids[0]: "unknown", visible_ids[1]: "unknown"},
            "missingEvidenceCandidateIds": visible_ids,
            "evidenceRefIds": [],
        }],
        "tradeoffDisclosures": [],
        "missingEvidenceStated": True,
    }
    with pytest.raises(CompositionError, match="actual compare results"):
        _materialize_selection(comparison, case=comparison_case, tool_calls=[])


def _update_call(requirements: list[dict[str, object]]) -> dict[str, object]:
    arguments = {"domainStatePatch": {"shoppingGuide": {"requirements": requirements}}}
    return {"response": {"toolCalls": [{"name": "update_task_state", "arguments": json.dumps(arguments)}]}}


def _state() -> TaskState:
    now = __import__("datetime").datetime.now(__import__("datetime").timezone.utc)
    return TaskState(
        taskId="fixture-task", revision=1, status="ready", goal="fixture",
        taskType="ecommerce_guide", createdAt=now, updatedAt=now,
    )


def test_requirements_are_case_local_and_multiturn_values_merge_replace() -> None:
    prior_case_calls = [_update_call([{
        "key": "os", "operator": "eq", "value": "ios", "unit": "enum",
        "priority": "hard", "source": "user",
    }])]
    current_case_calls = [_update_call([{
        "key": "scratch_level", "operator": "not_in", "value": ["present"], "unit": "enum",
        "priority": "hard", "source": "user",
    }])]
    current = _requirements_from_extractor(current_case_calls, _state())
    assert {item["key"] for item in current} == {"scratch_level"}
    assert "os" not in {item["key"] for item in current}
    assert _requirements_from_extractor(prior_case_calls, _state())[0]["key"] == "os"

    turns = [
        _update_call([
            {"key": "os", "operator": "eq", "value": "android", "unit": "enum", "priority": "hard", "source": "user"},
            {"key": "battery_health", "operator": "in", "value": ["90_plus"], "unit": "enum", "priority": "soft", "source": "user"},
            {"key": "screen_originality", "operator": "in", "value": ["original"], "unit": "enum", "priority": "soft", "source": "user"},
        ]),
        _update_call([
            {"key": "os", "operator": "eq", "value": "ios", "unit": "enum", "priority": "hard", "source": "user"},
            {"key": "motherboard_repair", "operator": "not_in", "value": ["repaired"], "unit": "enum", "priority": "hard", "source": "user"},
        ]),
    ]
    merged = {item["key"]: item for item in _requirements_from_extractor(turns, _state())}
    assert set(merged) == {"os", "battery_health", "screen_originality", "motherboard_repair"}
    assert merged["os"]["value"] == "ios"
    assert merged["battery_health"]["priority"] == "soft"


def test_attempt_markers_are_exclusive_and_latest_failure_blocks_old_success(tmp_path: Path) -> None:
    case_root = tmp_path / "BLIND-CASE-001"
    attempt1 = case_root / "attempt-001"
    attempt2 = case_root / "attempt-002"
    attempt1.mkdir(parents=True)
    attempt2.mkdir(parents=True)
    result = CaseRunResult(prediction={"x": 1}, trace={"modelCalls": []})
    (attempt1 / "success.json").write_bytes(canonical_json_bytes(_success_record(result, 1)))
    (attempt2 / "failure.json").write_bytes(canonical_json_bytes({"terminalState": "failure"}))
    assert _latest_prior_terminal(case_root, 3) == ("failure", attempt2 / "failure.json")
    with pytest.raises(CompositionError, match="use a new --attempt"):
        _assert_new_attempt_directory(attempt1)


def test_v2_resume_rejects_v1_success_terminal(tmp_path: Path) -> None:
    v1_result = CaseRunResult(
        prediction={
            "provenance": {
                "producerName": harness_module.COMPOSITION_NAME,
                "projectionProtocolVersion": "v1-direct-schema",
            },
        },
        trace={"projectionProtocolVersion": "v1-direct-schema", "modelCalls": []},
    )
    path = tmp_path / "success.json"
    path.write_bytes(canonical_json_bytes(_success_record(v1_result, 1)))
    with pytest.raises(CompositionError, match="incompatible success projection protocol"):
        _load_success_record(path)


def test_manifest_roundtrip_is_identical_and_sha_is_external(tmp_path: Path) -> None:
    manifest = {"schemaVersion": "fixture", "manifestCoreSha256": "abc"}
    path = tmp_path / "manifest.json"
    digest = _write_manifest(path, manifest)
    assert json.loads(path.read_text(encoding="utf-8")) == manifest
    assert "manifestSha256" not in manifest
    assert digest == hashlib.sha256(path.read_bytes()).hexdigest()


def test_run_manifest_records_successful_model_ledger_and_guard_scope(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle, _catalog = _real_data()
    case_id = bundle.cases[0].review_case_id

    class FakeCompletions:
        async def create(self, **_kwargs):
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
                content="fixture", tool_calls=[],
            ))])

    client = RecordingClient(SimpleNamespace(
        chat=SimpleNamespace(completions=FakeCompletions()),
    ))

    async def fake_run_case(*, case, client, **_kwargs):
        await client.chat.completions.create(model="fixture", messages=[])
        return CaseRunResult(
            prediction={
                "reviewCaseId": case.review_case_id,
                "provenance": {
                    "producerName": harness_module.COMPOSITION_NAME,
                    "projectionProtocolVersion": "v2-selection-materializer",
                },
            },
            trace={
                "reviewCaseId": case.review_case_id,
                "projectionProtocolVersion": "v2-selection-materializer",
            },
        )

    monkeypatch.setattr(harness_module, "run_case", fake_run_case)
    manifest = asyncio.run(run_public_model_composition(
        public_bundle_dir=PUBLIC_DIR,
        evidence_audit_path=EVIDENCE,
        run_dir=tmp_path,
        case_ids=[case_id],
        client=client,
    ))

    assert manifest["networkUsed"] is True
    assert manifest["modelUsed"] is True
    assert manifest["businessNetworkUsed"] is False
    assert manifest["networkGuardScope"] == "known_httpx_and_business_call_paths_not_process_egress"
    assert manifest["currentAttemptLogicalModelCallCount"] == 1
    assert manifest["resumedLogicalModelCallCount"] == 0
    assert manifest["totalLogicalModelCallCount"] == 1
    expected_evaluation_paths = {
        "agent/evaluation/used_phone_model_harness.py",
        "agent/scripts/run_used_phone_model_harness.py",
        "agent/tests/test_used_phone_model_harness.py",
        "agent/evaluation/used_phone_offline_runtime.py",
        "agent/evaluation/schemas/used_phone_complex_prediction_v1.schema.json",
    }
    expected_production_paths = {
        path.relative_to(REPO_ROOT).as_posix()
        for path in (REPO_ROOT / "agent" / "app").rglob("*.py")
    }
    assert set(manifest["codeSha256"]) == expected_production_paths | expected_evaluation_paths
    assert manifest["productionCodeScope"] == (
        "agent/app/**/*.py full snapshot because llm/tool import closure is broad"
    )
    assert manifest["projectionProtocolVersion"] == "v2-selection-materializer"
    for relative_path, digest in manifest["codeSha256"].items():
        assert digest == hashlib.sha256((REPO_ROOT / relative_path).read_bytes()).hexdigest()
    assert json.loads((tmp_path / "manifest.json").read_text(encoding="utf-8")) == manifest


def test_business_guard_fails_closed_on_live_dispatch() -> None:
    import agent.app.tools as tools_module

    with business_call_guard():
        with pytest.raises(PublicIsolationError):
            asyncio.run(tools_module.call_tool("search_products", {}))


def test_in_memory_taskstate_and_controlled_graph_integration() -> None:
    from agent.app.agent_trace import TraceBuilder
    from agent.app.harness import HarnessStepResult
    from agent.app.react_graph import ReActGraphRuntime, run_controlled_react_graph
    from agent.app.task_state import TaskStateCreateRequest, create_task_state

    async def exercise() -> None:
        with in_memory_task_state_store() as store:
            assert isinstance(store, InMemoryRedis)
            state = await create_task_state(TaskStateCreateRequest(taskType="ecommerce_guide", goal="fixture"))

            async def step_runner(current, *_args, **_kwargs):
                return HarnessStepResult(action="task_completed", task_state=current)

            graph = await run_controlled_react_graph(
                state,
                ReActGraphRuntime(
                    user_message="fixture", client=SimpleNamespace(), model="fixture",
                    resolve_tool_schemas=lambda _state: [], step_runner=step_runner,
                    tool_caller=lambda *_args, **_kwargs: None,
                    trace_builder=TraceBuilder("fixture"), projector=None, max_transitions=2,
                ),
            )
            assert graph["action"] == "task_completed"
            assert graph["transition_count"] == 1

    asyncio.run(exercise())


def test_recording_client_fake_is_deterministic() -> None:
    class FakeCompletions:
        def __init__(self) -> None:
            self.seen = []

        async def create(self, **kwargs):
            self.seen.append(kwargs)
            call = SimpleNamespace(function=SimpleNamespace(name="fixture", arguments=json.dumps({"ok": True})))
            message = SimpleNamespace(content=None, tool_calls=[call])
            return SimpleNamespace(choices=[SimpleNamespace(message=message)])

    fake = FakeCompletions()
    client = RecordingClient(SimpleNamespace(chat=SimpleNamespace(completions=fake)))

    async def exercise():
        await client.chat.completions.create(
            model="fixture", messages=[], tools=[{"function": {"name": "fixture"}}]
        )
        return await client.chat.completions.create(model="fixture", messages=[])

    asyncio.run(exercise())
    assert len(client.ledger) == 2
    assert client.ledger[0]["response"]["toolCalls"][0]["name"] == "fixture"
    assert client.ledger[0]["messageSha256"] == hashlib.sha256(b"[]").hexdigest()
    assert fake.seen[0]["extra_body"] == {"thinking": {"type": "disabled"}}
    assert "extra_body" not in fake.seen[1]


def test_projector_single_repair_receives_original_invalid_tool_json() -> None:
    bundle, _catalog = _real_data()
    case = bundle.by_id()["BLIND-CASE-004"]
    invalid = {
        "predictedAction": "CLARIFY",
        "extractedConstraints": [{
            "group": "os", "operator": "IN", "allowedValues": [], "importance": "hard",
            "missingTreatment": "unknown_not_recommendable",
            "evidenceRule": "attr_primary_title_conflict_check",
        }],
        "rankedCandidateIds": [], "candidateAssessments": [], "comparison": None,
        "stateUpdate": None, "claimSelections": [],
    }
    valid = dict(invalid, extractedConstraints=[])

    class FakeCompletions:
        def __init__(self) -> None:
            self.seen = []

        async def create(self, **kwargs):
            self.seen.append(kwargs)
            payload = invalid if len(self.seen) == 1 else valid
            call = SimpleNamespace(
                id="invalid-call-id",
                function=SimpleNamespace(
                    name="submit_used_phone_selection",
                    arguments=json.dumps(payload),
                ),
            )
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content=None, tool_calls=[call]))]
            )

    fake = FakeCompletions()
    client = RecordingClient(SimpleNamespace(chat=SimpleNamespace(completions=fake)))
    schema_path = REPO_ROOT / "agent" / "evaluation" / "schemas" / "used_phone_complex_prediction_v1.schema.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))

    async def exercise():
        return await _project_prediction(
            client,
            case=case,
            snapshots=[],
            graph_trace=None,
            tool_calls=[],
            model_name="fixture",
            prediction_schema=schema,
        )

    prediction, repair_count = asyncio.run(exercise())
    assert prediction["predictedAction"] == "CLARIFY"
    assert repair_count == 1
    for request in fake.seen:
        declared_name = request["tools"][0]["function"]["name"]
        forced_name = request["tool_choice"]["function"]["name"]
        assert declared_name == forced_name == "submit_used_phone_selection"
    repair_messages = fake.seen[1]["messages"]
    assistant = repair_messages[-2]
    tool = repair_messages[-1]
    assert assistant["tool_calls"][0]["id"] == "invalid-call-id"
    assert assistant["tool_calls"][0]["function"]["name"] == "submit_used_phone_selection"
    assert json.loads(assistant["tool_calls"][0]["function"]["arguments"])["extractedConstraints"][0]["allowedValues"] == []
    assert tool["role"] == "tool"
    assert tool["tool_call_id"] == "invalid-call-id"
    assert "ValidationError" in tool["content"]


def test_projector_first_request_failure_is_recorded_in_case_sidecar_without_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle, _catalog = _real_data()
    case = bundle.by_id()["BLIND-CASE-004"]

    class FailingCompletions:
        def __init__(self) -> None:
            self.seen = []

        async def create(self, **kwargs):
            self.seen.append(kwargs)
            raise RuntimeError("fixture request failure")

    fake = FailingCompletions()
    client = RecordingClient(SimpleNamespace(chat=SimpleNamespace(completions=fake)))

    async def failing_run_case(*, case, client, model_name, prediction_schema, **_kwargs):
        await _project_prediction(
            client,
            case=case,
            snapshots=[],
            graph_trace=None,
            tool_calls=[],
            model_name=model_name,
            prediction_schema=prediction_schema,
        )

    monkeypatch.setattr(harness_module, "run_case", failing_run_case)
    manifest = asyncio.run(run_public_model_composition(
        public_bundle_dir=PUBLIC_DIR,
        evidence_audit_path=EVIDENCE,
        run_dir=tmp_path,
        case_ids=[case.review_case_id],
        client=client,
    ))
    assert manifest["succeededCaseIds"] == []
    assert set(manifest["failedCases"]) == {case.review_case_id}
    failure = json.loads((
        tmp_path / "cases" / case.review_case_id / "attempt-001" / "failure.json"
    ).read_text(encoding="utf-8"))
    assert failure["terminalState"] == "failure"
    assert failure["errorType"] == "CompositionError"
    assert "before a repairable selection" in failure["error"]
    assert len(fake.seen) == 1
    assert fake.seen[0]["tools"][0]["function"]["name"] == (
        fake.seen[0]["tool_choice"]["function"]["name"]
    )
    assert len(failure["modelCalls"]) == 1
    assert failure["modelCalls"][0]["ok"] is False
