from __future__ import annotations

import time
import unittest
from datetime import datetime, timezone
from unittest import mock

from app import reference_context
from app.domains.ecommerce import CandidateScope
from app.llm import _ordinal_comparison_binding
from app.reference_context import (
    ReferenceContextError,
    ResolvedReferenceContext,
    publish_reference_context,
    refresh_reference_context,
    resolve_reference_context,
)
from app.schemas import EcommerceChatRequest, ReferenceContextHint
from app.task_state import TaskState
from tests.fake_redis import FakeRedis


BIG_ID = 9_007_199_254_740_993


def _state(*, revision: int = 7, status: str = "active") -> TaskState:
    scope = CandidateScope(
        scopeId="scope-current",
        taskId="task-current",
        sourceRevision=5,
        sourcePlanId="plan-current",
        sourceStepId="step-current",
        category="phone",
        candidatePoolIds=[11, BIG_ID, 33, 44],
        rankedItemIds=[11, BIG_ID, 33, 44],
        visibleProductIds=[11, BIG_ID, 33],
        requirementsSnapshot=[],
        brandAvoidancesSnapshot=[],
        evidenceRefs=["validator:current"],
        createdAt="2026-08-30T00:00:00Z",
        status=status,
        invalidationReason=("changed" if status == "invalidated" else None),
    )
    now = datetime.now(timezone.utc)
    return TaskState(
        taskId="task-current",
        taskType="ecommerce_guide",
        sessionId="session-current",
        status="ready",
        revision=revision,
        goal="选手机",
        domainState={
            "turnCount": 3,
            "candidateScope": scope.model_dump(by_alias=True, mode="json"),
        },
        createdAt=now,
        updatedAt=now,
    )


def _row(product_id: int) -> dict:
    # Browser IDs are intentionally strings because catalog IDs can exceed 2**53.
    return {"product": {"id": str(product_id), "title": str(product_id)}}


def _guide() -> dict:
    # This is the real browser order after a memory rerank, not CandidateScope order.
    return {
        "products": [_row(BIG_ID), _row(11), _row(33)],
        "expandedProducts": [
            _row(BIG_ID), _row(11), _row(33), _row(44),
        ],
        "comparisonMatrix": [
            {"productId": str(BIG_ID)},
            {"productId": "11"},
        ],
    }


class ReferenceContextTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        reference_context._client = FakeRedis()

    async def test_publishes_and_resolves_exact_compact_browser_order(self):
        state = _state()
        public = await publish_reference_context(
            session_id="session-current",
            state=state,
            guide_result=_guide(),
        )

        resolved = await resolve_reference_context(
            session_id="session-current",
            state=state,
            hint=ReferenceContextHint(
                handle=public["handle"],
                presentationMode="compact",
                focusedProductId=str(BIG_ID),
            ),
        )

        self.assertEqual(resolved.presentation_ids, (BIG_ID, 11, 33))
        self.assertEqual(resolved.focused_product_id, BIG_ID)
        self.assertEqual(resolved.compared_product_ids, (BIG_ID, 11))

    async def test_expanded_mode_uses_the_exact_expanded_order(self):
        state = _state()
        public = await publish_reference_context(
            session_id="session-current", state=state, guide_result=_guide()
        )
        resolved = await resolve_reference_context(
            session_id="session-current",
            state=state,
            hint=ReferenceContextHint(
                handle=public["handle"], presentationMode="expanded"
            ),
        )
        self.assertEqual(resolved.presentation_ids, (BIG_ID, 11, 33, 44))

    async def test_cross_session_stale_revision_and_out_of_view_focus_fail_closed(self):
        state = _state()
        public = await publish_reference_context(
            session_id="session-current", state=state, guide_result=_guide()
        )
        cases = [
            (
                "other-session",
                state,
                ReferenceContextHint(handle=public["handle"]),
                "session_binding_missing",
            ),
            (
                "session-current",
                _state(revision=8),
                ReferenceContextHint(handle=public["handle"]),
                "reference_context_stale_task",
            ),
            (
                "session-current",
                state,
                ReferenceContextHint(
                    handle=public["handle"],
                    presentationMode="compact",
                    focusedProductId="44",
                ),
                "focused_product_outside_presentation",
            ),
        ]
        for session_id, current_state, hint, expected_code in cases:
            with self.subTest(expected_code=expected_code):
                with self.assertRaises(ReferenceContextError) as captured:
                    await resolve_reference_context(
                        session_id=session_id,
                        state=current_state,
                        hint=hint,
                    )
                self.assertEqual(captured.exception.code, expected_code)

    async def test_invalidated_scope_and_expired_receipt_fail_closed(self):
        state = _state()
        public = await publish_reference_context(
            session_id="session-current", state=state, guide_result=_guide()
        )
        with self.assertRaises(ReferenceContextError) as invalidated:
            await resolve_reference_context(
                session_id="session-current",
                state=_state(status="invalidated"),
                hint=ReferenceContextHint(handle=public["handle"]),
            )
        self.assertEqual(invalidated.exception.code, "candidate_scope_stale")

        with mock.patch(
            "app.reference_context.time.time",
            return_value=time.time() + 7200,
        ):
            with self.assertRaises(ReferenceContextError) as expired:
                await resolve_reference_context(
                    session_id="session-current",
                    state=state,
                    hint=ReferenceContextHint(handle=public["handle"]),
                )
        self.assertEqual(expired.exception.code, "reference_context_expired")

    async def test_ordinal_and_focus_resolution_uses_reranked_receipt_not_scope_order(self):
        state = _state()
        resolved = ResolvedReferenceContext(
            task_id=state.task_id,
            task_revision=state.revision,
            scope_id="scope-current",
            scope_source_revision=5,
            presentation_mode="compact",
            presentation_ids=(BIG_ID, 11, 33),
            compact_product_ids=(BIG_ID, 11, 33),
            expanded_product_ids=(BIG_ID, 11, 33, 44),
            compared_product_ids=(BIG_ID, 11),
            previous_batch_product_ids=(),
            focused_product_id=BIG_ID,
        )
        self.assertEqual(
            _ordinal_comparison_binding(
                state, "请比较第一个和第三个", resolved
            ),
            ("bound", [BIG_ID, 33]),
        )
        self.assertEqual(
            _ordinal_comparison_binding(
                state, "比较这个和另一个", resolved
            ),
            ("bound", [BIG_ID, 11]),
        )
        self.assertEqual(
            _ordinal_comparison_binding(
                state, "比较上一批前两个", resolved
            ),
            ("missing_or_out_of_range", None),
        )

    async def test_focused_other_is_ambiguous_with_three_visible_choices(self):
        state = _state()
        resolved = ResolvedReferenceContext(
            task_id=state.task_id,
            task_revision=state.revision,
            scope_id="scope-current",
            scope_source_revision=5,
            presentation_mode="compact",
            presentation_ids=(BIG_ID, 11, 33),
            compact_product_ids=(BIG_ID, 11, 33),
            expanded_product_ids=(BIG_ID, 11, 33, 44),
            compared_product_ids=(),
            previous_batch_product_ids=(),
            focused_product_id=BIG_ID,
        )

        self.assertEqual(
            _ordinal_comparison_binding(state, "比较这个和另一个", resolved),
            ("ambiguous", None),
        )

    async def test_refresh_preserves_presentation_and_updates_comparison_revision(self):
        state = _state()
        public = await publish_reference_context(
            session_id="session-current", state=state, guide_result=_guide()
        )
        resolved = await resolve_reference_context(
            session_id="session-current",
            state=state,
            hint=ReferenceContextHint(
                handle=public["handle"],
                presentationMode="compact",
                focusedProductId=str(BIG_ID),
            ),
        )
        advanced = state.model_copy(update={
            "revision": state.revision + 4,
            "domain_state": {
                **state.domain_state,
                "shoppingGuide": {
                    "mode": "compare",
                    "category": "phone",
                    "requirements": [],
                    "candidateIds": [BIG_ID, 11, 33],
                    "comparedIds": [11, 33],
                    "evidenceStatus": "complete",
                },
            },
        })

        refreshed = await refresh_reference_context(
            session_id="session-current",
            state=advanced,
            resolved=resolved,
        )
        rebound = await resolve_reference_context(
            session_id="session-current",
            state=advanced,
            hint=ReferenceContextHint(
                handle=refreshed["handle"],
                presentationMode="compact",
                focusedProductId=str(BIG_ID),
            ),
        )

        self.assertEqual(refreshed["taskRevision"], advanced.revision)
        self.assertEqual(rebound.presentation_ids, (BIG_ID, 11, 33))
        self.assertEqual(rebound.compared_product_ids, (11, 33))
        self.assertEqual(rebound.focused_product_id, BIG_ID)

    async def test_refresh_rejects_changed_scope_source_revision(self):
        state = _state()
        resolved = ResolvedReferenceContext(
            task_id=state.task_id,
            task_revision=state.revision,
            scope_id="scope-current",
            scope_source_revision=4,
            presentation_mode="compact",
            presentation_ids=(BIG_ID, 11, 33),
            compact_product_ids=(BIG_ID, 11, 33),
            expanded_product_ids=(BIG_ID, 11, 33, 44),
            compared_product_ids=(),
            previous_batch_product_ids=(),
            focused_product_id=BIG_ID,
        )
        with self.assertRaises(ReferenceContextError) as captured:
            await refresh_reference_context(
                session_id="session-current",
                state=state.model_copy(update={"revision": state.revision + 1}),
                resolved=resolved,
            )
        self.assertEqual(captured.exception.code, "reference_context_stale_scope")

    def test_wire_schema_rejects_numeric_or_noncanonical_focused_ids(self):
        base = {
            "message": "比较这个和另一个",
            "sessionId": "session-current",
            "referenceContext": {"handle": "A" * 43},
        }
        for bad_id in (BIG_ID, "01", "0", "-1"):
            payload = {
                **base,
                "referenceContext": {
                    **base["referenceContext"],
                    "focusedProductId": bad_id,
                },
            }
            with self.subTest(bad_id=bad_id):
                with self.assertRaises(ValueError):
                    EcommerceChatRequest.model_validate(payload)


if __name__ == "__main__":
    unittest.main()
