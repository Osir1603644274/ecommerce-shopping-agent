from __future__ import annotations

import copy
import pickle
import subprocess
import sys
from datetime import UTC, datetime, timedelta

import pytest

from app.domains.ecommerce.strategy_routing import (
    BoundedToolRequest,
    ErrorCode,
    ReceiptIssuer,
)
from app.domains.ecommerce.ranking_contract import normalize_search_products_detail
from app.domains.ecommerce.models import (
    CANONICAL_ECOMMERCE_ATTRIBUTE_CODES,
    SPEC_REGISTRY,
    ShoppingRequirement,
)
from tests.test_ecommerce_ranking_contract import valid_search_detail
from tests.test_brand_negation_ranking import _phone, _two_stage_detail
from app.graph.observation_snapshot import (
    ObservationOutcome,
    ObservationSnapshot,
    restore_observation,
    serialize_observation,
)
from app.graph.strategy_receipt_ledger import StrategyLedgerSlot


class Clock:
    def __init__(self) -> None:
        self.value = datetime.now(UTC).replace(microsecond=0)

    def now_utc(self) -> datetime:
        return self.value


def _parts(tool: str = "get_product_details"):
    clock = Clock()
    if tool == "get_product_details":
        arguments = {"productId": 101}
    elif tool == "compare_products":
        arguments = {"productIds": [101, 102]}
    else:
        arguments = {"query": "phone", "limit": 2}
    request = BoundedToolRequest.create(
        tool_name=tool, arguments=arguments, state_revision=7, step_id="step-1",
    )
    slot = StrategyLedgerSlot.create(
        task_id="task-1", run_id="run-1", thread_id="thread-1", plan_id="plan-1",
        step_id="step-1", state_revision=7, tool_name=tool,
        canonical_args_digest=request.args_digest,
    )
    receipt = ReceiptIssuer(clock=clock).issue(
        request, receipt_id="receipt-1", outcome="succeeded",
    )
    return clock, request, slot, receipt


def _search_values() -> dict[str, object]:
    return normalize_search_products_detail(valid_search_detail()).normalized_values()


@pytest.mark.parametrize(
    ("tool", "observation"),
    [
        ("search_products", _search_values()),
        ("get_product_details", {"productIds": [101], "evidenceRefs": ["product:101:details"]}),
        ("compare_products", {"productIds": [101, 102], "evidenceRefs": ["product:101:comparison", "product:102:comparison"]}),
    ],
)
def test_three_read_tools_create_and_restore_exactly(tool, observation):
    _clock, request, slot, receipt = _parts(tool)
    snapshot = ObservationSnapshot.create(
        slot=slot, receipt=receipt, request=request, observation=observation,
    )
    raw = serialize_observation(snapshot)
    restored = restore_observation(raw, slot=slot, receipt=receipt, request=request)
    assert restored.observation_digest == snapshot.observation_digest
    assert restored.normalized == observation
    assert restored.outcome is ObservationOutcome.SUCCEEDED
    assert serialize_observation(restored) == raw


def test_attribute_source_codes_come_from_canonical_spec_registry():
    expected = frozenset(
        key for category_specs in SPEC_REGISTRY.values() for key in category_specs
    )
    from app.graph.observation_snapshot import _ATTRIBUTE_SOURCE_CODES, _valid_evidence_ref

    assert _ATTRIBUTE_SOURCE_CODES == expected
    assert "memory_gb" in _ATTRIBUTE_SOURCE_CODES
    assert _valid_evidence_ref(
        "product:101:attribute:memory_gb", product_id=101, tool_name="search_products",
    )
    assert len(CANONICAL_ECOMMERCE_ATTRIBUTE_CODES) == 19
    assert all(
        _valid_evidence_ref(
            f"product:101:attribute:{key}", product_id=101, tool_name="search_products",
        )
        for key in CANONICAL_ECOMMERCE_ATTRIBUTE_CODES
    )


def test_mutating_spec_registry_before_first_snapshot_import_cannot_expand_closure():
    code = """
from app.domains.ecommerce.models import SPEC_REGISTRY
SPEC_REGISTRY['phone']['attacker_code'] = ('text', 'text', ('eq',))
from app.graph.observation_snapshot import _CANONICAL_ATTRIBUTE_CODES, _valid_evidence_ref
assert 'attacker_code' not in _CANONICAL_ATTRIBUTE_CODES
assert not _valid_evidence_ref('product:1:attribute:attacker_code', product_id=1, tool_name='search_products')
assert 'memory_gb' in _CANONICAL_ATTRIBUTE_CODES
"""
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True,
        cwd="F:/agent", check=False,
    )
    assert result.returncode == 0, result.stderr


def test_real_rule_rerank_memory_attribute_normalizes_and_snapshots():
    product = _phone(101, "苹果/Apple", os_token="iOS")
    product["attributes"].append({
        "key": "memory_gb", "rawValue": "12GB", "normalizedNumber": 12,
        "normalizedText": None, "normalizedBoolean": None,
        "evidenceField": "catalog.attribute", "extractionMethod": "catalog-v1",
        "confidence": 1.0,
    })
    requirement = ShoppingRequirement(
        key="memory_gb", operator="gte", value=8, unit="GB",
        priority="hard", source="server-rule",
    )
    detail = _two_stage_detail([product], [requirement], {101: 1.0})
    values = normalize_search_products_detail(
        detail, requirements=[requirement.model_dump()], category="phone",
    ).normalized_values()
    _clock, request, slot, receipt = _parts("search_products")
    snapshot = ObservationSnapshot.create(
        slot=slot, receipt=receipt, request=request, observation=values,
    )
    assert snapshot.normalized["contractVersion"] == "ecommerce-two-stage-ranking-v2"


@pytest.mark.parametrize("field", ["tool_name", "step_id", "state_revision", "canonical_args_digest"])
def test_receipt_slot_binding_is_fail_closed(field):
    _clock, request, slot, receipt = _parts()
    changed = {
        "task_id": slot.task_id, "run_id": slot.run_id, "thread_id": slot.thread_id,
        "plan_id": slot.plan_id, "step_id": slot.step_id,
        "state_revision": slot.state_revision, "tool_name": slot.tool_name,
        "canonical_args_digest": slot.canonical_args_digest,
    }
    if field == "tool_name":
        changed[field] = "compare_products"
    elif field == "step_id":
        changed[field] = "other-step"
    elif field == "state_revision":
        changed[field] = 8
    else:
        changed[field] = "0" * 64
    foreign_slot = StrategyLedgerSlot.create(**changed)
    with pytest.raises((ValueError, PermissionError)):
        ObservationSnapshot.create(
            slot=foreign_slot, receipt=receipt, request=request,
            observation={"productIds": [101], "evidenceRefs": ["product:101:details"]},
        )


def test_search_contract_and_sensitive_injection_are_closed():
    _clock, request, slot, receipt = _parts("search_products")
    values = _search_values()
    values["query"] = "private prompt"
    with pytest.raises((ValueError, PermissionError)):
        ObservationSnapshot.create(slot=slot, receipt=receipt, request=request, observation=values)

    values = _search_values()
    values["candidateSupport"]["hardUnknownsByProduct"] = {"101": ["Authorization: secret"]}
    values["candidateSupport"]["closestAlternativeProductIds"] = [101]
    values["candidateSupport"]["fullySupportedProductIds"] = []
    with pytest.raises((ValueError, PermissionError)):
        ObservationSnapshot.create(slot=slot, receipt=receipt, request=request, observation=values)


@pytest.mark.parametrize(
    "observation",
    [
        {"query": "phone"},
        {"productIds": [True]},
        {"productIds": list(range(1, 52))},
        {"productIds": [102], "evidenceRefs": ["product:102:title"]},
    ],
)
def test_product_projection_allowlist_rejects_extra_or_bad_fields(observation):
    _clock, request, slot, receipt = _parts()
    with pytest.raises((ValueError, PermissionError)):
        ObservationSnapshot.create(slot=slot, receipt=receipt, request=request, observation=observation)


def test_failed_observation_has_enum_error_and_no_payload():
    clock, request, slot, _success = _parts()
    request = BoundedToolRequest.create(
        tool_name=slot.tool_name, arguments={"productId": 101}, state_revision=7, step_id="step-1",
    )
    failed = ReceiptIssuer(clock=clock).issue(
        request, receipt_id="receipt-failed", outcome="failed", error_code=ErrorCode.TOOL_FAILED,
    )
    snapshot = ObservationSnapshot.create(slot=slot, receipt=failed, request=request, observation={})
    assert snapshot.outcome is ObservationOutcome.FAILED
    assert snapshot.error_code is ErrorCode.TOOL_FAILED
    assert snapshot.normalized == {}
    with pytest.raises(ValueError):
        ObservationSnapshot.create(slot=slot, receipt=failed, request=request, observation={"productIds": [101], "evidenceRefs": ["product:101:details"]})


def test_canonical_bytes_digest_and_tamper_are_checked():
    _clock, request, slot, receipt = _parts()
    snapshot = ObservationSnapshot.create(slot=slot, receipt=receipt, request=request, observation={"productIds": [101], "evidenceRefs": ["product:101:details"]})
    raw = serialize_observation(snapshot)
    assert len(raw) <= 32 * 1024
    assert raw == serialize_observation(restore_observation(raw, slot=slot, receipt=receipt, request=request))
    tampered = raw.replace(b'"productIds":[101]', b'"productIds":[102]')
    with pytest.raises(ValueError):
        restore_observation(tampered, slot=slot, receipt=receipt, request=request)
    with pytest.raises(ValueError):
        restore_observation(raw.replace(b'"observationDigest":"', b'"observationDigest":"0'), slot=slot, receipt=receipt, request=request)


def test_receipt_id_and_request_identity_are_bound_on_restore():
    clock, request, slot, receipt = _parts()
    snapshot = ObservationSnapshot.create(
        slot=slot, receipt=receipt, request=request,
        observation={"productIds": [101], "evidenceRefs": ["product:101:details"]},
    )
    raw = serialize_observation(snapshot)
    other_request = BoundedToolRequest.create(
        tool_name="get_product_details", arguments={"productId": 102},
        state_revision=7, step_id="step-1",
    )
    other_receipt = ReceiptIssuer(clock=clock).issue(
        other_request, receipt_id="receipt-other", outcome="succeeded",
    )
    with pytest.raises(ValueError):
        restore_observation(raw, slot=slot, receipt=other_receipt, request=request)
    with pytest.raises(ValueError):
        restore_observation(raw, slot=slot, receipt=receipt, request=other_request)
    changed_clock = Clock()
    changed_clock.value = clock.value + timedelta(seconds=1)
    same_id_different_bytes = ReceiptIssuer(clock=changed_clock).issue(
        request, receipt_id=receipt.receipt_id, outcome="succeeded",
    )
    with pytest.raises(ValueError):
        restore_observation(raw, slot=slot, receipt=same_id_different_bytes, request=request)


def test_mutation_copy_pickle_and_repr_do_not_expose_payload():
    _clock, request, slot, receipt = _parts()
    snapshot = ObservationSnapshot.create(slot=slot, receipt=receipt, request=request, observation={"productIds": [101], "evidenceRefs": ["product:101:details"]})
    assert "productIds" not in repr(snapshot)
    # A cryptographic digest may coincidentally contain the decimal substring
    # "101".  Check the actual sensitive value instead of banning an arbitrary
    # three-character sequence from an opaque SHA-256 representation.
    assert "product:101:details" not in repr(snapshot)
    with pytest.raises(TypeError):
        copy.copy(snapshot)
    with pytest.raises(TypeError):
        copy.deepcopy(snapshot)
    with pytest.raises(TypeError):
        pickle.dumps(snapshot)
    object.__setattr__(snapshot, "base_revision", 8)
    with pytest.raises(ValueError):
        serialize_observation(snapshot)


def test_time_and_plain_json_boundaries():
    clock, request, slot, receipt = _parts()
    with pytest.raises(ValueError):
        ObservationSnapshot.create(
            slot=slot, receipt=receipt, request=request, observation={"productIds": [101], "evidenceRefs": ["product:101:details"]},
            observed_at=receipt.issued_at.replace(tzinfo=None),
        )
    with pytest.raises(ValueError):
        ObservationSnapshot.create(
            slot=slot, receipt=receipt, request=request, observation={"productIds": [101], "evidenceRefs": ["product:101:details"]},
            observed_at=receipt.issued_at + timedelta(days=1),
        )
    with pytest.raises(ValueError):
        ObservationSnapshot.create(
            slot=slot, receipt=receipt, request=request, observation={"productIds": (101,), "evidenceRefs": ["product:101:details"]},
        )
    with pytest.raises(ValueError):
        ObservationSnapshot.create(
            slot=slot, receipt=receipt, request=request, observation={"productIds": [1.0], "evidenceRefs": ["product:1:details"]},
        )


@pytest.mark.parametrize("code", ["RuntimeError: db.internal", "db.internal.example", "internal failure"])
def test_unknown_codes_reject_free_exception_or_hostname_text(code):
    _clock, request, slot, receipt = _parts("search_products")
    values = _search_values()
    values["candidateSupport"]["closestAlternativeProductIds"] = [101]
    values["candidateSupport"]["fullySupportedProductIds"] = []
    values["candidateSupport"]["hardUnknownsByProduct"] = {"101": [code]}
    with pytest.raises((ValueError, PermissionError)):
        ObservationSnapshot.create(slot=slot, receipt=receipt, request=request, observation=values)


@pytest.mark.parametrize("tool,ref", [
    ("get_product_details", "product:101:fake.source"),
    ("compare_products", "product:101:details"),
])
def test_fake_or_cross_tool_evidence_sources_are_rejected(tool, ref):
    _clock, request, slot, receipt = _parts(tool)
    ids = [101] if tool == "get_product_details" else [101, 102]
    refs = [ref] if tool == "get_product_details" else [ref, "product:102:comparison"]
    with pytest.raises(ValueError):
        ObservationSnapshot.create(
            slot=slot, receipt=receipt, request=request,
            observation={"productIds": ids, "evidenceRefs": refs},
        )
