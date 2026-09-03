import asyncio
import json
from datetime import UTC, datetime
from types import SimpleNamespace

import httpx
import pytest

from app.memory.projection_client import MemoryAccessCredential, MemoryProjectionReason
from app.memory import v3_runtime
from app import harness


CATALOG_REVISION = "shopping-companion-9a8a2a1c13f"


def payload(entries):
    return {
        "success": True,
        "data": {
            "schemaVersion": 3,
            "revision": 9,
            "ownerBinding": "a" * 64,
            "truncated": False,
            "entries": entries,
        },
        "message": "ok",
        "timestamp": "2026-08-30T00:00:00Z",
    }


def entry(entry_id, category, kind, key, value):
    return {
        "entryId": entry_id,
        "categoryId": category,
        "recipientScope": "self",
        "preferenceKind": kind,
        "attributeKey": key,
        "normalizedValue": value,
        "catalogRevision": CATALOG_REVISION,
        "version": 1,
        "status": "ACTIVE",
        "createdAt": "2026-08-01T00:00:00Z",
        "updatedAt": "2026-08-20T00:00:00Z",
        "expiresAt": "2027-02-16T00:00:00Z",
        "supersedes": None,
        "chainVerified": True,
    }


def issued_projection(monkeypatch, selected_entries=None):
    default_entries = [
        entry("phone-1", "phone", "avoid", "brand", "apple"),
        entry("phone-2", "phone", "prefer", "os", "android"),
        entry("laptop-1", "laptop", "prefer", "brand", "lenovo"),
    ]
    response = payload(default_entries if selected_entries is None else selected_entries)
    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, json=response, request=request)
    )
    real_client = httpx.AsyncClient

    def client_factory(*args, **kwargs):
        return real_client(transport=transport)

    monkeypatch.setattr(v3_runtime.httpx, "AsyncClient", client_factory)
    client = v3_runtime.MemoryProjectionV3Client(
        enabled=True, backend_url="http://authority", timeout_seconds=1
    )
    return asyncio.run(client.fetch(MemoryAccessCredential("opaque-token")))


def test_binding_is_category_scoped_overridden_and_invisible_to_executor(monkeypatch):
    projection = issued_projection(monkeypatch)
    assert projection.reason is MemoryProjectionReason.AVAILABLE
    binding = v3_runtime.build_memory_run_binding(
        projection,
        category_id="phone",
        catalog_revision=CATALOG_REVISION,
        current_requirement_keys=frozenset({"os"}),
    )

    planner = binding.payload_for_phase("planner")
    assert planner == {
        "preferences": [{
            "categoryId": "phone",
            "preferenceKind": "avoid",
            "attributeKey": "brand",
            "normalizedValue": "apple",
        }]
    }
    assert binding.payload_for_phase("executor") is None
    assert binding.payload_for_phase("validator") is None
    assert v3_runtime.memory_score(binding, {"brand": "apple"}) == -1.0
    assert v3_runtime.memory_score(binding, {"brand": "huawei"}) == 0.0


def test_multi_preference_score_is_bounded_mean(monkeypatch):
    projection = issued_projection(monkeypatch)
    binding = v3_runtime.build_memory_run_binding(
        projection, category_id="phone", catalog_revision=CATALOG_REVISION
    )
    assert v3_runtime.memory_score(
        binding, {"brand": "apple", "os": "android"}
    ) == 0.0
    assert v3_runtime.memory_score(
        binding, {"brand": "huawei", "os": "android"}
    ) == 0.5


def test_publicly_constructed_or_mutated_binding_is_not_issued():
    forged = v3_runtime.MemoryRunBinding(
        1,
        "phone",
        CATALOG_REVISION,
        (v3_runtime.CatalogRunPreference("phone", "prefer", "os", "android"),),
    )
    with pytest.raises(ValueError, match="unissued"):
        forged.payload_for_phase("planner")

    issued = v3_runtime.empty_memory_run_binding("phone", CATALOG_REVISION)
    object.__setattr__(issued, "memory_revision", 99)
    with pytest.raises(ValueError, match="unissued"):
        issued.payload_for_phase("planner")


def test_invalid_or_unavailable_projection_yields_empty_binding():
    unavailable = v3_runtime.ProjectionV3Result(
        reason=MemoryProjectionReason.UNAVAILABLE
    )
    binding = v3_runtime.build_memory_run_binding(
        unavailable, category_id="phone", catalog_revision=CATALOG_REVISION
    )
    assert binding.payload_for_phase("planner") is None
    assert v3_runtime.memory_score(binding, {"brand": "apple"}) == 0.0


def test_expired_or_catalog_mismatched_preferences_are_suppressed(monkeypatch):
    expired = entry("expired", "phone", "avoid", "brand", "apple")
    expired["expiresAt"] = "2026-08-29T00:00:00Z"
    old_catalog = entry("old-catalog", "phone", "prefer", "os", "android")
    old_catalog["catalogRevision"] = "used-phone-old-revision"
    projection = issued_projection(monkeypatch, [expired, old_catalog])
    binding = v3_runtime.build_memory_run_binding(
        projection,
        category_id="phone",
        catalog_revision=CATALOG_REVISION,
        now=datetime(2026, 8, 30, tzinfo=UTC),
    )
    assert binding.payload_for_phase("planner") is None


@pytest.mark.parametrize("raw,headers", [
    (
        b'{"success":true,"success":true,"data":{},"message":"ok",'
        b'"timestamp":"2026-08-30T00:00:00Z"}',
        {},
    ),
    (
        b'{"success":true,"data":{},"message":"ok",'
        b'"timestamp":"2026-08-30T00:00:00Z"}',
        {"content-encoding": "gzip"},
    ),
])
def test_projection_rejects_duplicate_keys_and_encoded_bodies(monkeypatch, raw, headers):
    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, content=raw, headers=headers, request=request)
    )
    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        v3_runtime.httpx,
        "AsyncClient",
        lambda *args, **kwargs: real_client(transport=transport),
    )
    result = asyncio.run(v3_runtime.MemoryProjectionV3Client(
        enabled=True, backend_url="http://authority",
    ).fetch(MemoryAccessCredential("opaque-token")))
    assert result.reason is MemoryProjectionReason.INVALID


def test_presentation_rerank_is_soft_bounded_and_current_task_can_suppress(monkeypatch):
    binding = v3_runtime.build_memory_run_binding(
        issued_projection(monkeypatch), category_id="phone",
        catalog_revision=CATALOG_REVISION,
    )
    presentations = []
    for index in range(20):
        presentations.append({
            "productId": index + 1,
            "brand": "Apple" if index == 0 else "Huawei",
            "attributes": [{
                "key": "os",
                "status": "known",
                "value": "unknown-os" if index == 0 else "android",
                "evidenceRef": f"product:{index + 1}:attribute:os",
            }],
        })

    reranked, receipt = v3_runtime.rerank_product_presentations(
        binding, presentations, weight=0.08
    )
    assert len(reranked) == len(presentations)
    assert {item["productId"] for item in reranked} == set(range(1, 21))
    assert reranked[0]["productId"] == 2
    assert receipt["filteredProductCount"] == 0
    assert receipt["inputProductIds"][0] == "1"
    assert receipt["outputProductIds"][0] == "2"

    suppressed, _ = v3_runtime.rerank_product_presentations(
        binding,
        presentations,
        weight=0.08,
        suppressed_attribute_keys=frozenset({"brand", "os"}),
    )
    assert [item["productId"] for item in suppressed] == list(range(1, 21))


def test_validated_browser_projection_applies_rerank_without_mutating_state(monkeypatch):
    binding = v3_runtime.build_memory_run_binding(
        issued_projection(monkeypatch), category_id="phone",
        catalog_revision=CATALOG_REVISION,
    )
    presentations = [{
        "productId": index + 1,
        "title": f"phone-{index + 1}",
        "brand": "Apple" if index == 0 else "Huawei",
        "attributes": [{
            "key": "os", "status": "known",
            "value": "unknown-os" if index == 0 else "android",
            "evidenceRef": f"product:{index + 1}:attribute:os",
        }],
    } for index in range(20)]
    summary = {
        "hasCompleteMatch": True,
        "rankedItemCount": 20,
        "productPresentations": presentations[:3],
    }
    base_results = [{
        "tool": "search_products",
        "validationSummary": {"requiresProductCandidates": summary},
    }]
    monkeypatch.setattr(harness, "_build_validated_results", lambda state, traces: base_results)
    monkeypatch.setattr(
        harness, "_expanded_search_presentations",
        lambda *args, **kwargs: presentations,
    )
    state = SimpleNamespace(
        task_type="ecommerce_guide",
        active_plan=object(),
        domain_state={
            "candidateScope": {"scopeId": "scope-1"},
            "shoppingGuide": {"category": "phone", "requirements": []},
        },
    )
    before = json.dumps(state.domain_state, sort_keys=True)
    result = harness.build_validated_guide_result(
        state, memory_run_binding=binding, memory_rerank_weight=0.08
    )
    assert result["products"][0]["product"]["id"] == "2"
    assert result["memoryRerank"] == {
        "applied": True,
        "orderChanged": True,
        "filteredProductCount": 0,
    }
    assert json.dumps(state.domain_state, sort_keys=True) == before
    answer_results = harness.apply_memory_rerank_to_validated_results(
        state,
        base_results,
        memory_run_binding=binding,
        memory_rerank_weight=0.08,
    )
    answer_summary = answer_results[0]["validationSummary"]["requiresProductCandidates"]
    assert answer_summary["productPresentations"][0]["productId"] == 2
    assert answer_results[0]["memoryOrderAdjusted"] is True
    assert "memoryRevision" not in json.dumps(answer_results)
    assert "memoryScores" not in json.dumps(answer_results)
    assert base_results[0]["validationSummary"]["requiresProductCandidates"][
        "productPresentations"
    ][0]["productId"] == 1
