"""Versioned deterministic governance evaluation for Shopping Memory V13.

This runner deliberately does not read any dev/validation/sealed dataset.  It
exercises production Python governance, V3 projection, ContextProjector and
presentation-rerank code with synthetic paired fixtures.  The consent pair is
contract-only and is explicitly labelled as such; it is not evidence that the
Java consent service ran.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Iterator

import httpx

from app.context_pack import ContextPack
from app.context_policy import CONTEXT_CONTRACT_VERSION, ECOMMERCE_CONTEXT_POLICY_ID
from app.context_view import ContextProjector
from app.domains.context_skill import ECOMMERCE_CONTEXT_SKILL_ID
from app.memory import v3_runtime
from app.memory.governance import (
    MemoryApplicationContext,
    MemorySnapshot,
    ScopedMemoryRecord,
    resolve_effective_preferences,
)
from app.memory.long_term_memory import ShoppingPreference
from app.memory.projection_client import MemoryAccessCredential, MemoryProjectionReason
from app.task_state import TaskState


SCHEMA_VERSION = "shopping-memory-v13-governance-report-v1"
CONTRACT_VERSION = "shopping-memory-v13-governance-run-contract-v1"
CATALOG_REVISION = "used-phone-439-09807c773ce6"
NOW = datetime(2026, 8, 30, tzinfo=UTC)
ROOT = Path(__file__).resolve().parents[2]
CONTRACT_PATH = ROOT / "docs" / "experiments" / "shopping-memory-v13-2026-08-29" / "governance-run-contract-v1.json"


@dataclass(frozen=True, slots=True)
class CaseResult:
    case_id: str
    family: str
    polarity: str
    execution_layer: str
    passed: bool
    expected: dict[str, Any]
    observed: dict[str, Any]


def _canonical(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), allow_nan=False,
    ).encode("utf-8")


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _entry(
    entry_id: str,
    *,
    category: str = "phone",
    kind: str = "avoid",
    key: str = "brand",
    value: str = "apple",
    catalog_revision: str = CATALOG_REVISION,
    expires_at: str = "2027-02-16T00:00:00Z",
) -> dict[str, object]:
    return {
        "entryId": entry_id,
        "categoryId": category,
        "recipientScope": "self",
        "preferenceKind": kind,
        "attributeKey": key,
        "normalizedValue": value,
        "catalogRevision": catalog_revision,
        "version": 1,
        "status": "ACTIVE",
        "createdAt": "2026-08-01T00:00:00Z",
        "updatedAt": "2026-08-20T00:00:00Z",
        "expiresAt": expires_at,
        "supersedes": None,
        "chainVerified": True,
    }


def _projection_payload(entries: list[dict[str, object]]) -> dict[str, object]:
    return {
        "success": True,
        "data": {
            "schemaVersion": 3,
            "revision": 13,
            "ownerBinding": "a" * 64,
            "truncated": False,
            "entries": entries,
        },
        "message": "ok",
        "timestamp": "2026-08-30T00:00:00Z",
    }


@contextmanager
def _mock_projection_transport(
    payload: dict[str, object], *, status_code: int = 200,
) -> Iterator[None]:
    """Route the production client through an in-memory HTTP authority fixture."""
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            status_code, json=payload, request=request,
        )
    )
    real_client = httpx.AsyncClient
    original = v3_runtime.httpx.AsyncClient
    v3_runtime.httpx.AsyncClient = lambda *args, **kwargs: real_client(transport=transport)  # type: ignore[assignment]
    try:
        yield
    finally:
        v3_runtime.httpx.AsyncClient = original  # type: ignore[assignment]


def _fetch_projection(
    entries: list[dict[str, object]], *, enabled: bool = True,
    status_code: int = 200,
) -> v3_runtime.ProjectionV3Result:
    if not enabled:
        return asyncio.run(v3_runtime.MemoryProjectionV3Client(
            enabled=False, backend_url="http://fixture.invalid",
        ).fetch(MemoryAccessCredential("fixture-token")))
    with _mock_projection_transport(_projection_payload(entries), status_code=status_code):
        return asyncio.run(v3_runtime.MemoryProjectionV3Client(
            enabled=True, backend_url="http://fixture.invalid",
        ).fetch(MemoryAccessCredential("fixture-token")))


def _binding(
    entries: list[dict[str, object]],
    *,
    category: str = "phone",
    catalog_revision: str = CATALOG_REVISION,
    current_keys: frozenset[str] = frozenset(),
    enabled: bool = True,
) -> v3_runtime.MemoryRunBinding:
    projection = _fetch_projection(entries, enabled=enabled)
    return v3_runtime.build_memory_run_binding(
        projection,
        category_id=category,
        catalog_revision=catalog_revision,
        current_requirement_keys=current_keys,
        now=NOW,
    )


def _preference(key: str = "os", value: str = "android") -> ShoppingPreference:
    return ShoppingPreference("shopping_preference", key, value)


def _record(
    entry_id: str,
    *,
    owner: str = "owner-a",
    category: str = "phone",
    preference: ShoppingPreference | None = None,
    status: str = "active",
    version: int = 1,
    supersedes: str | None = None,
) -> ScopedMemoryRecord:
    return ScopedMemoryRecord(
        entry_id=entry_id,
        owner_user_id=owner,
        product_category=category,  # type: ignore[arg-type]
        recipient_scope="self",
        preference=preference or _preference(),
        source="explicit_user",
        status=status,  # type: ignore[arg-type]
        version=version,
        created_at=NOW - timedelta(days=20),
        updated_at=NOW - timedelta(days=1),
        expires_at=NOW + timedelta(days=30),
        supersedes=supersedes,
    )


def _govern(
    records: tuple[ScopedMemoryRecord, ...],
    *,
    snapshot_owner: str = "owner-a",
    request_owner: str = "owner-a",
    recipient: str = "self",
    category: str = "phone",
    enabled: bool = True,
    current_turn: tuple[ShoppingPreference, ...] = (),
) -> tuple[list[dict[str, str]], dict[str, str]]:
    effective = resolve_effective_preferences(
        MemorySnapshot(snapshot_owner, 13, records),
        MemoryApplicationContext(
            request_owner,
            category,  # type: ignore[arg-type]
            recipient,  # type: ignore[arg-type]
            NOW,
            current_turn=current_turn,
            memory_enabled=enabled,
        ),
    )
    preferences = [item.plain() for item in effective.preferences]
    reasons = {item.entry_id: item.reason for item in effective.decisions}
    return preferences, reasons


def _pack(*, current_key: str | None = None) -> ContextPack:
    hard = []
    if current_key is not None:
        hard.append({
            "key": current_key, "operator": "eq",
            "value": "huawei", "source": "user",
        })
    return ContextPack(
        runId="governance-run-1",
        taskId="governance-task-1",
        baseContextRevision=1,
        goal="choose a phone",
        taskType="ecommerce_guide",
        contextPolicyId=ECOMMERCE_CONTEXT_POLICY_ID,
        contextPolicyVersion=CONTEXT_CONTRACT_VERSION,
        contextSkillId=ECOMMERCE_CONTEXT_SKILL_ID,
        contextSkillVersion=CONTEXT_CONTRACT_VERSION,
        hardConstraints=hard,
        shoppingGuideState={
            "mode": "recommend",
            "category": "phone",
            "requirements": [],
            "candidateIds": [],
            "comparedIds": [],
            "evidenceStatus": "missing",
        },
        allowedTools=["search_products"],
    )


def _presentations() -> list[dict[str, object]]:
    return [
        {
            "productId": 1,
            "brand": "Apple",
            "attributes": [{"key": "os", "status": "known", "value": "ios"}],
        },
        {
            "productId": 2,
            "brand": "Huawei",
            "attributes": [{"key": "os", "status": "known", "value": "android"}],
        },
    ]


def _case(
    case_id: str,
    family: str,
    polarity: str,
    execution_layer: str,
    expected: dict[str, Any],
    observed: dict[str, Any],
) -> CaseResult:
    return CaseResult(
        case_id, family, polarity, execution_layer,
        _canonical(expected) == _canonical(observed), expected, observed,
    )


def _consent_cases() -> list[CaseResult]:
    def decide(source: str, confirmed: bool) -> dict[str, object]:
        return {
            "eligible": confirmed and source in {"explicit_user", "user_confirmed"},
            "javaServiceExecuted": False,
        }
    return [
        _case("consent-positive", "consent_boundary", "positive", "contract_fixture_only",
              {"eligible": True, "javaServiceExecuted": False}, decide("explicit_user", True)),
        _case("consent-negative", "consent_boundary", "negative", "contract_fixture_only",
              {"eligible": False, "javaServiceExecuted": False}, decide("model_inferred", False)),
    ]


def _owner_cases() -> list[CaseResult]:
    record = _record("owner-entry")
    positive, positive_reasons = _govern((record,))
    negative, negative_reasons = _govern((record,), request_owner="owner-b")
    return [
        _case("owner-positive", "owner_isolation", "positive", "production_governance_v2",
              {"applied": 1, "reason": "applicable"},
              {"applied": len(positive), "reason": positive_reasons[record.entry_id]}),
        _case("owner-negative", "owner_isolation", "negative", "production_governance_v2",
              {"applied": 0, "reason": "snapshot_owner_mismatch"},
              {"applied": len(negative), "reason": negative_reasons[record.entry_id]}),
    ]


def _recipient_cases() -> list[CaseResult]:
    record = _record("recipient-entry")
    positive, positive_reasons = _govern((record,), recipient="self")
    negative, negative_reasons = _govern((record,), recipient="other")
    return [
        _case("recipient-positive", "recipient_scope", "positive", "production_governance_v2",
              {"applied": 1, "reason": "applicable"},
              {"applied": len(positive), "reason": positive_reasons[record.entry_id]}),
        _case("recipient-negative", "recipient_scope", "negative", "production_governance_v2",
              {"applied": 0, "reason": "recipient_not_self"},
              {"applied": len(negative), "reason": negative_reasons[record.entry_id]}),
    ]


def _category_cases() -> list[CaseResult]:
    entries = [_entry("category-entry")]
    positive = _binding(entries)
    negative = _binding(entries, category="laptop")
    return [
        _case("category-positive", "category_scope", "positive", "production_v3_runtime",
              {"visible": 1}, {"visible": len(positive.payload_for_phase("planner")["preferences"])}),
        _case("category-negative", "category_scope", "negative", "production_v3_runtime",
              {"visible": 0}, {"visible": 0 if negative.payload_for_phase("planner") is None else 1}),
    ]


def _catalog_cases() -> list[CaseResult]:
    entries = [_entry("catalog-entry")]
    positive = _binding(entries)
    negative = _binding(entries, catalog_revision="used-phone-old-revision")
    return [
        _case("catalog-positive", "catalog_revision", "positive", "production_v3_runtime",
              {"visible": 1}, {"visible": len(positive.payload_for_phase("planner")["preferences"])}),
        _case("catalog-negative", "catalog_revision", "negative", "production_v3_runtime",
              {"visible": 0}, {"visible": 0 if negative.payload_for_phase("planner") is None else 1}),
    ]


def _expiry_cases() -> list[CaseResult]:
    positive = _binding([_entry("expiry-future")])
    negative = _binding([_entry("expiry-past", expires_at="2026-08-29T00:00:00Z")])
    return [
        _case("expiry-positive", "expiry", "positive", "production_v3_runtime",
              {"visible": 1}, {"visible": len(positive.payload_for_phase("planner")["preferences"])}),
        _case("expiry-negative", "expiry", "negative", "production_v3_runtime",
              {"visible": 0}, {"visible": 0 if negative.payload_for_phase("planner") is None else 1}),
    ]


def _revocation_cases() -> list[CaseResult]:
    active = _record("revoke-active")
    revoked = _record("revoke-revoked", status="revoked")
    positive, positive_reasons = _govern((active,))
    negative, negative_reasons = _govern((revoked,))
    return [
        _case("revoke-positive", "revocation", "positive", "production_governance_v2",
              {"applied": 1, "reason": "applicable"},
              {"applied": len(positive), "reason": positive_reasons[active.entry_id]}),
        _case("revoke-negative", "revocation", "negative", "production_governance_v2",
              {"applied": 0, "reason": "status_revoked"},
              {"applied": len(negative), "reason": negative_reasons[revoked.entry_id]}),
    ]


def _version_chain_cases() -> list[CaseResult]:
    first = _record("chain-1")
    second = _record("chain-2", preference=_preference("os", "ios"), version=2, supersedes="chain-1")
    good, good_reasons = _govern((first, second))
    broken = _record("chain-3", version=3, supersedes="chain-1")
    bad, bad_reasons = _govern((first, broken))
    return [
        _case("chain-positive", "version_chain", "positive", "production_governance_v2",
              {"applied": 1, "terminal": "ios", "first": "superseded_by_newer_version"},
              {"applied": len(good), "terminal": good[0]["value"], "first": good_reasons[first.entry_id]}),
        _case("chain-negative", "version_chain", "negative", "production_governance_v2",
              {"applied": 0, "reasons": ["version_chain_invalid"]},
              {"applied": len(bad), "reasons": sorted(set(bad_reasons.values()))}),
    ]


def _supersede_cases() -> list[CaseResult]:
    first = _record("supersede-1", preference=_preference("os", "android"))
    latest = _record("supersede-2", preference=_preference("os", "ios"), version=2, supersedes="supersede-1")
    updated, reasons = _govern((first, latest))
    lone, lone_reasons = _govern((first,))
    return [
        _case("supersede-positive", "supersede", "positive", "production_governance_v2",
              {"value": "ios", "oldReason": "superseded_by_newer_version"},
              {"value": updated[0]["value"], "oldReason": reasons[first.entry_id]}),
        _case("supersede-negative", "supersede", "negative", "production_governance_v2",
              {"value": "android", "reason": "applicable"},
              {"value": lone[0]["value"], "reason": lone_reasons[first.entry_id]}),
    ]


def _override_cases() -> list[CaseResult]:
    entries = [_entry("override-entry", kind="prefer", key="os", value="android")]
    positive = _binding(entries)
    negative = _binding(entries, current_keys=frozenset({"os"}))
    return [
        _case("override-positive", "current_turn_override", "positive", "production_v3_runtime",
              {"visible": 1}, {"visible": len(positive.payload_for_phase("planner")["preferences"])}),
        _case("override-negative", "current_turn_override", "negative", "production_v3_runtime",
              {"visible": 0}, {"visible": 0 if negative.payload_for_phase("planner") is None else 1}),
    ]


def _feature_flag_cases() -> list[CaseResult]:
    enabled = _fetch_projection([_entry("flag-entry")], enabled=True)
    disabled = _fetch_projection([_entry("flag-entry")], enabled=False)
    return [
        _case("flags-positive", "feature_flags", "positive", "production_v3_client",
              {"reason": "available"}, {"reason": enabled.reason.value}),
        _case("flags-negative", "feature_flags", "negative", "production_v3_client",
              {"reason": "disabled"}, {"reason": disabled.reason.value}),
    ]


def _authority_cases() -> list[CaseResult]:
    issued = _binding([_entry("authority-entry")])
    forged = v3_runtime.MemoryRunBinding(
        13, "phone", CATALOG_REVISION,
        (v3_runtime.CatalogRunPreference("phone", "avoid", "brand", "apple"),),
    )
    try:
        forged.payload_for_phase("planner")
        rejected = False
    except ValueError:
        rejected = True
    return [
        _case("authority-positive", "authority_failure", "positive", "production_v3_runtime",
              {"visible": 1}, {"visible": len(issued.payload_for_phase("planner")["preferences"])}),
        _case("authority-negative", "authority_failure", "negative", "production_v3_runtime",
              {"forgedRejected": True}, {"forgedRejected": rejected}),
    ]


def _budget_cases() -> list[CaseResult]:
    valid_entries = [
        _entry(f"budget-{index}", kind="prefer", key=f"attribute_{index}", value=f"value-{index}")
        for index in range(8)
    ]
    valid = _binding(valid_entries)
    invalid_projection = _fetch_projection(valid_entries + [_entry("budget-9", key="attribute_9")])
    invalid = v3_runtime.build_memory_run_binding(
        invalid_projection, category_id="phone", catalog_revision=CATALOG_REVISION, now=NOW,
    )
    payload = valid.payload_for_phase("planner")
    return [
        _case("budget-positive", "projection_budget", "positive", "production_v3_runtime",
              {"visible": 8, "withinBytes": True},
              {"visible": len(payload["preferences"]), "withinBytes": len(_canonical(payload)) <= 4096}),
        _case("budget-negative", "projection_budget", "negative", "production_v3_runtime",
              {"projectionReason": "invalid", "visible": 0},
              {"projectionReason": invalid_projection.reason.value,
               "visible": 0 if invalid.payload_for_phase("planner") is None else 1}),
    ]


def _context_cases() -> list[CaseResult]:
    binding = _binding([_entry("secret-entry")])
    projector = ContextProjector(
        _pack(), long_term_memory_context=binding, long_term_memory_enabled=True,
    )
    planner = projector.planner_view(
        ["search_products"], "ready", user_message="for myself choose a phone",
    ).model_dump(by_alias=True, mode="json")
    executor = projector.executor_view(
        plan_id="plan-1", step_id="step-1", step_description="search",
        tool_name="search_products",
    ).model_dump(by_alias=True, mode="json")
    serialized = json.dumps(planner, ensure_ascii=False, sort_keys=True)
    return [
        _case("context-positive", "context_leakage", "positive", "production_context_projector",
              {"visible": 1, "idLeaked": False, "ownerLeaked": False, "revisionLeaked": False},
              {"visible": len(planner.get("longTermMemory", [])),
               "idLeaked": "secret-entry" in serialized,
               "ownerLeaked": "ownerBinding" in serialized,
               "revisionLeaked": "memoryRevision" in serialized}),
        _case("context-negative", "context_leakage", "negative", "production_context_projector",
              {"executorVisible": False}, {"executorVisible": "longTermMemory" in executor}),
    ]


def _task_state_cases() -> list[CaseResult]:
    state = TaskState(
        taskId="memory-governance-task",
        taskType="ecommerce_guide",
        status="ready",
        revision=7,
        goal="choose a phone",
        domainState={"shoppingGuide": {"category": "phone", "requirements": []}},
        createdAt=NOW,
        updatedAt=NOW,
    )
    before = state.model_dump_json(by_alias=True)
    presentations = _presentations()
    positive = _binding([_entry("state-entry")])
    v3_runtime.rerank_product_presentations(positive, presentations, weight=0.08)
    after_positive = state.model_dump_json(by_alias=True)
    empty = v3_runtime.empty_memory_run_binding("phone", CATALOG_REVISION)
    v3_runtime.rerank_product_presentations(empty, presentations, weight=0.08)
    after_negative = state.model_dump_json(by_alias=True)
    return [
        _case("state-positive", "task_state_immutability", "positive", "production_v3_runtime",
              {"unchanged": True}, {"unchanged": before == after_positive}),
        _case("state-negative", "task_state_immutability", "negative", "production_v3_runtime",
              {"unchanged": True}, {"unchanged": before == after_negative}),
    ]


def _rerank_cases() -> list[CaseResult]:
    # With only two candidates the normalized base-rank gap is 1.0, so the
    # frozen maximum lambda (0.08) correctly cannot swap them.  Use the actual
    # supported Top-20 boundary to exercise a legitimate presentation change.
    presentations = []
    for index in range(20):
        presentations.append({
            "productId": index + 1,
            "brand": "Apple" if index == 0 else "Huawei",
            "attributes": [{
                "key": "os", "status": "known",
                "value": "ios" if index == 0 else "android",
            }],
        })
    baseline = deepcopy(presentations)
    binding = _binding([_entry("rerank-entry")])
    reranked, receipt = v3_runtime.rerank_product_presentations(
        binding, presentations, weight=0.08,
    )
    empty = v3_runtime.empty_memory_run_binding("phone", CATALOG_REVISION)
    unchanged, empty_receipt = v3_runtime.rerank_product_presentations(
        empty, presentations, weight=0.08,
    )
    return [
        _case("rerank-positive", "soft_rerank_no_filter", "positive", "production_v3_runtime",
              {"first": "2", "count": 20, "filtered": 0, "inputUnchanged": True},
              {"first": receipt["outputProductIds"][0], "count": len(reranked),
               "filtered": receipt["filteredProductCount"], "inputUnchanged": presentations == baseline}),
        _case("rerank-negative", "soft_rerank_no_filter", "negative", "production_v3_runtime",
              {"first": "1", "count": 20, "filtered": 0},
              {"first": empty_receipt["outputProductIds"][0], "count": len(unchanged),
               "filtered": empty_receipt["filteredProductCount"]}),
    ]


CASE_BUILDERS: tuple[Callable[[], list[CaseResult]], ...] = (
    _consent_cases,
    _owner_cases,
    _recipient_cases,
    _category_cases,
    _catalog_cases,
    _expiry_cases,
    _revocation_cases,
    _version_chain_cases,
    _supersede_cases,
    _override_cases,
    _feature_flag_cases,
    _authority_cases,
    _budget_cases,
    _context_cases,
    _task_state_cases,
    _rerank_cases,
)


def run_governance_evaluation() -> dict[str, object]:
    contract = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
    if contract.get("schemaVersion") != CONTRACT_VERSION:
        raise ValueError("governance contract version mismatch")
    results = [case for builder in CASE_BUILDERS for case in builder()]
    families = sorted({case.family for case in results})
    counts = {family: sum(case.family == family for case in results) for family in families}
    if len(families) < 15 or any(count != 2 for count in counts.values()):
        raise ValueError("governance family pairing contract violated")
    passed = sum(case.passed for case in results)
    return {
        "schemaVersion": SCHEMA_VERSION,
        "contractVersion": CONTRACT_VERSION,
        "boundedDecision": "BOUNDED_GOVERNANCE_ACCEPT" if passed == len(results) else "HOLD",
        "productionDefaultDecision": "HOLD_UNCHANGED",
        "sealedExecuted": False,
        "javaConsentServiceExecuted": False,
        "defaultFlagsChanged": False,
        "familyCount": len(families),
        "caseCount": len(results),
        "passedCaseCount": passed,
        "failedCaseCount": len(results) - passed,
        "families": counts,
        "cases": [asdict(case) for case in results],
        "sourceReceipt": {
            "runnerSha256": _sha(Path(__file__)),
            "contractSha256": _sha(CONTRACT_PATH),
        },
    }


def materialize(output_dir: Path) -> Path:
    """Write one immutable report directory; existing paths are never reused."""
    if output_dir.exists():
        raise FileExistsError(f"refusing to overwrite existing output: {output_dir}")
    report = run_governance_evaluation()
    output_dir.mkdir(parents=True, exist_ok=False)
    report_path = output_dir / "governance-report.json"
    report_path.write_bytes(_canonical(report) + b"\n")
    (output_dir / "SHA256SUMS.txt").write_text(
        f"{_sha(report_path)}  governance-report.json\n", encoding="utf-8",
    )
    return report_path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    report_path = materialize(args.output_dir.resolve())
    print(report_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
