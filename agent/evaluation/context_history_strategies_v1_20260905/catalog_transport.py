"""Frozen catalog transport with successful-empty search semantics.

The historical transport is preserved unchanged. Zero eligible products after
successful recall/ranking is an empty business result, not a transport failure.
All normalizer and downstream Validator checks still execute.
"""
from agent.evaluation.real_user_multiturn_ab_executor_20260902_v2.lane_runtime import FrozenCatalogTransport


class ContextCatalogTransport(FrozenCatalogTransport):
    def _search(self, arguments):
        result = super()._search(arguments)
        detail = result.detail
        if (not result.ok and isinstance(detail, dict)
                and detail.get("candidates") == []
                and detail.get("rankedItemIds") == []
                and bool(detail.get("candidatePoolIds"))
                and detail.get("rankingTrace", {}).get("eligibleCandidateCount") == 0
                and detail.get("retrievalTrace", {}).get("channels", {}).get("offlineFrozenBm25", {}).get("status") == "active"):
            return result.model_copy(update={"ok": True})
        return result
