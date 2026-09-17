from copy import deepcopy

import pytest

from agent.app.domains.ecommerce.ranking_contract import (
    RANKING_FORMULA,
    RANKING_TIE_BREAK,
    TWO_STAGE_RANKING_CONTRACT_VERSION,
)
from agent.evaluation import used_phone_public_agent_runner_v1 as v1
from agent.evaluation import used_phone_two_stage_ranking_runner_v3 as v3


def _detail(pool=None, ranked=None):
    pool = [1001, 1002, 1003] if pool is None else pool
    ranked = [1003, 1001] if ranked is None else ranked
    evidence = [
        {"ref": f"product:{item}:title", "field": "title", "rawValue": str(item)}
        for item in ranked
    ]
    return {
        "contractVersion": TWO_STAGE_RANKING_CONTRACT_VERSION,
        "candidatePoolIds": pool,
        "rankedItemIds": ranked,
        "candidateIds": ranked,
        "candidates": [
            {"id": item, "attributes": [], "checks": [], "evidenceRefs": [f"product:{item}:title"]}
            for item in ranked
        ],
        "retrievalTrace": {
            "contractVersion": TWO_STAGE_RANKING_CONTRACT_VERSION,
            "candidatePoolCount": len(pool),
            "authoritativeFactCount": len(pool),
        },
        "rankingTrace": {
            "contractVersion": TWO_STAGE_RANKING_CONTRACT_VERSION,
            "inputCandidateCount": len(pool),
            "rankedItemCount": len(ranked),
            "tieBreak": RANKING_TIE_BREAK,
            "formula": RANKING_FORMULA,
        },
        "citationTrace": {
            "contractVersion": TWO_STAGE_RANKING_CONTRACT_VERSION,
            "sourceTool": "search_products",
            "rankedItemIds": ranked,
            "evidenceRefCount": len(evidence),
            "binding": "current_successful_tool_call_ranked_items_only",
        },
        "evidenceRefs": [item["ref"] for item in evidence],
        "evidence": evidence,
        "eliminated": [],
    }


def _trace(pool=None, ranked=None):
    return {
        "tool": "search_products",
        "ok": True,
        "publicTurnIndex": 0,
        "detail": _detail(pool, ranked),
    }


def _case():
    return v1.PublicCase(
        "UPV2-RK-D01",
        "dev",
        "hard_constraint",
        ({"role": "user", "text": "fixture", "turnId": "turn-1"},),
        (),
    )


def _catalog(*ids):
    return {str(item): {"itemId": str(item), "attributes": {}} for item in ids}


def _snapshots(pool=None, ranked=None):
    pool = [1001, 1002, 1003] if pool is None else pool
    ranked = [1003, 1001] if ranked is None else ranked
    return [{"state": {
        "taskId": "task-v3-fixture",
        "activePlan": {
            "planId": "plan-v3-fixture",
            "steps": [{
                "stepId": "step-search",
                "toolName": "search_products",
                "status": "executed",
            }],
        },
        "domainState": {
            "shoppingGuide": {"requirements": []},
            "stepOutputs": {
                "step-search": {
                    "taskId": "task-v3-fixture",
                    "planId": "plan-v3-fixture",
                    "stepId": "step-search",
                    "values": {
                        "contractVersion": TWO_STAGE_RANKING_CONTRACT_VERSION,
                        "candidatePoolIds": pool,
                        "rankedItemIds": ranked,
                        "productIds": ranked,
                        "evidenceRefs": [
                            f"product:{item}:title" for item in ranked
                        ],
                    },
                }
            },
        },
    }}]


def test_historical_v3_materializer_rejects_current_v2_step_output():
    # This runner/schema is a frozen v1 evaluation identity.  Calling its
    # internal materializer bypasses the normal production-scope gate, so the
    # persisted-output boundary must still reject the current v2 shape.
    with pytest.raises(v3.PublicRunnerError, match="untrusted persisted"):
        v3._materialize_two_stage_prediction(
            case=_case(),
            catalog=_catalog(1001, 1002, 1003),
            tool_traces=[_trace()],
            snapshots=_snapshots(),
            selection={
                "predictedAction": "RETRIEVE_FILTER_AND_RANK",
                "citationRefIds": [],
            },
        )


def test_v3_runtime_allowlist_and_identity_pin_v2_materializer_schema():
    assert v3._V2_SCHEMA_PATH.resolve() in v3._allowed()
    assert (
        "agent/evaluation/schemas/used_phone_ranking_prediction_v2.schema.json"
        in v3._hashes()
    )


def test_historical_v3_materializer_rejects_current_v2_empty_ranked_output(
    monkeypatch,
):
    monkeypatch.setattr(
        v3,
        "_V1_MATERIALIZE_PREDICTION",
        lambda **_kwargs: {
            "caseId": "UPV2-RK-D01",
            "rankedItemIds": [],
        },
    )

    with pytest.raises(v3.PublicRunnerError, match="untrusted persisted"):
        v3._materialize_two_stage_prediction(
            case=_case(),
            catalog=_catalog(1001, 1002),
            tool_traces=[_trace(pool=[1001, 1002], ranked=[])],
            snapshots=_snapshots(pool=[1001, 1002], ranked=[]),
            selection={"predictedAction": "CLARIFY", "citationRefIds": []},
        )


@pytest.mark.parametrize(
    "tamper",
    ["missing_version", "pool_outside_ranked", "alias", "row_order", "raw_v2"],
)
def test_v3_runner_rejects_untrusted_search_shapes(tamper):
    trace = _trace()
    detail = trace["detail"]
    if tamper == "missing_version":
        detail.pop("contractVersion")
    elif tamper == "pool_outside_ranked":
        detail["rankedItemIds"] = detail["candidateIds"] = [9999]
        detail["candidates"] = [{"id": 9999, "evidenceRefs": []}]
        detail["evidence"] = detail["evidenceRefs"] = []
    elif tamper == "alias":
        detail["candidateIds"] = [1001, 1003]
    elif tamper == "row_order":
        detail["candidates"].reverse()
    else:
        trace["detail"] = {"candidateIds": [1001, 1002]}

    with pytest.raises(v3.PublicRunnerError, match="untrusted two-stage"):
        v3._materialize_two_stage_prediction(
            case=_case(), catalog=_catalog(1001, 1002, 1003, 9999),
            tool_traces=[trace], snapshots=_snapshots(),
            selection={"predictedAction": "RETRIEVE_FILTER_AND_RANK", "citationRefIds": []},
        )


def test_v3_schema_rejects_citation_outside_ranked_ids():
    prediction = {
        "caseId": "UPV2-RK-D01",
        "rankingContractVersion": "ecommerce-two-stage-ranking-v1",
        "candidatePoolIds": ["1001", "1002"],
        "rankedItemIds": ["1001"],
        "evidenceCitations": [{
            "itemId": "1002", "group": "os", "source": "relevance",
            "field": "attr_value", "lineNumber": 1, "rawValue": "android",
        }],
    }

    with pytest.raises(v3.PublicRunnerError, match="citation item must be ranked"):
        v3.validate_prediction_row(prediction)


def test_v3_runner_rejects_raw_and_persisted_ranking_drift():
    snapshots = _snapshots()
    snapshots[0]["state"]["domainState"]["stepOutputs"]["step-search"]["values"][
        "productIds"
    ] = [1001]

    with pytest.raises(v3.PublicRunnerError, match="untrusted persisted"):
        v3._materialize_two_stage_prediction(
            case=_case(), catalog=_catalog(1001, 1002, 1003),
            tool_traces=[_trace()], snapshots=snapshots,
            selection={"predictedAction": "RETRIEVE_FILTER_AND_RANK", "citationRefIds": []},
        )


def test_v3_runner_cannot_reuse_success_before_later_final_turn_failure():
    failed = {
        "tool": "search_products",
        "ok": False,
        "publicTurnIndex": 0,
        "detail": {"code": "backend_unavailable"},
    }

    with pytest.raises(v3.PublicRunnerError, match="older results cannot be reused"):
        v3._materialize_two_stage_prediction(
            case=_case(), catalog=_catalog(1001, 1002, 1003),
            tool_traces=[_trace(), failed], snapshots=_snapshots(),
            selection={"predictedAction": "RETRIEVE_FILTER_AND_RANK", "citationRefIds": []},
        )


def test_v3_identity_is_public_dev_only_and_restores_v1():
    before = {name: deepcopy(getattr(v1, name)) for name in v3._FIELDS}
    with pytest.raises(v3.PublicRunnerError, match="public-dev-only"):
        v3._validate_dev_selection(["dev"], None, True)
    with pytest.raises(v3.PublicRunnerError, match="exactly one ordered public dev"):
        v3._validate_dev_selection(["validation"], None, False)
    with pytest.raises(v3.PublicRunnerError, match="exactly one ordered public dev"):
        v3._validate_dev_selection(["dev", "dev"], None, False)
    with pytest.raises(v3.PublicRunnerError, match="complete ordered public dev"):
        v3._validate_dev_selection(["dev"], ["UPV2-RK-D01"], False)
    assert v3._validate_dev_selection(["dev"], None, False) == (
        ["dev"], list(v3.EXPECTED_CASE_IDS[:10])
    )
    with v3._identity():
        assert v1.PROTOCOL_VERSION == v3.PROTOCOL_VERSION
    assert {name: getattr(v1, name) for name in v3._FIELDS} == before


def test_v3_run_rejects_resume_before_kernel_dispatch():
    import asyncio

    with pytest.raises(v3.PublicRunnerError, match="resume is forbidden"):
        asyncio.run(v3.run_public_agent(
            public_cases_path=v3.Path("cases_public.jsonl"),
            public_catalog_path=v3.Path("catalog.jsonl"),
            run_dir=v3.Path("run"),
            resume=True,
        ))


@pytest.mark.parametrize(
    "kwargs",
    [
        {"client": object()},
        {"model_name": "deepseek-chat"},
    ],
)
def test_v3_run_rejects_model_authority_before_kernel_dispatch(kwargs):
    import asyncio

    with pytest.raises(v3.PublicRunnerError, match="no-model identity|forbids model"):
        asyncio.run(v3.run_public_agent(
            public_cases_path=v3.Path("cases_public.jsonl"),
            public_catalog_path=v3.Path("catalog.jsonl"),
            run_dir=v3.Path("run"),
            **kwargs,
        ))


def test_v3_no_model_client_fails_closed_without_network():
    import asyncio

    client = v3._NoModelClient()
    with pytest.raises(v3.PublicRunnerError, match="forbids model calls"):
        asyncio.run(client.chat.completions.create(model="anything"))
    assert client.ledger == []


def test_v3_network_guard_denies_model_before_send():
    import asyncio
    import httpx

    ledger = []

    async def attempt():
        with v3._no_model_network_guard(ledger):
            async with httpx.AsyncClient() as client:
                await client.send(httpx.Request(
                    "POST", "https://api.deepseek.com/chat/completions"
                ))

    with pytest.raises(v3.PublicRunnerError, match="denied model network before send"):
        asyncio.run(attempt())
    assert ledger == []
