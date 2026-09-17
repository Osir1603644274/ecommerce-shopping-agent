from typing import Any


DEFAULT_MAX_SHOPS = 5
DEFAULT_MAX_SUPPORT_PER_SHOP = 2
DEFAULT_MAX_CONFLICT_PER_SHOP = 1
DEFAULT_MAX_TOTAL_REVIEWS = 12
DEFAULT_MAX_TOTAL_CHARS = 6000
DEFAULT_MAX_CHARS_PER_REVIEW = 600


def _normalized_text(review: dict[str, Any]) -> str:
    return " ".join(str(review.get("text") or "").split())


def _context_excerpt(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 1].rstrip() + "…"


def _group_top_shops(
    ranked_reviews: list[dict[str, Any]],
    max_shops: int,
) -> list[dict[str, Any]]:
    groups: list[dict[str, Any]] = []
    by_shop_id: dict[int, dict[str, Any]] = {}
    seen_review_ids: set[str] = set()
    seen_text_by_shop: dict[int, set[str]] = {}
    for review in ranked_reviews:
        review_id = str(review["reviewId"])
        if review_id in seen_review_ids:
            continue
        seen_review_ids.add(review_id)
        shop_id = int(review["shopId"])
        group = by_shop_id.get(shop_id)
        if group is None:
            if len(groups) >= max_shops:
                continue
            group = {
                "shopId": shop_id,
                "shopName": review.get("shopName"),
                "shopRank": len(groups) + 1,
                "support": [],
                "conflict": [],
            }
            groups.append(group)
            by_shop_id[shop_id] = group
            seen_text_by_shop[shop_id] = set()
        if shop_id not in by_shop_id:
            continue

        text = _normalized_text(review)
        if not text or text in seen_text_by_shop[shop_id]:
            continue
        seen_text_by_shop[shop_id].add(text)
        relation = str(review.get("contentRelation") or "irrelevant")
        if relation in {"support", "conflict"}:
            group[relation].append(review)
    return groups


def select_reranked_review_context(
    ranked_reviews: list[dict[str, Any]],
    *,
    max_shops: int = DEFAULT_MAX_SHOPS,
    max_support_per_shop: int = DEFAULT_MAX_SUPPORT_PER_SHOP,
    max_conflict_per_shop: int = DEFAULT_MAX_CONFLICT_PER_SHOP,
    max_total_reviews: int = DEFAULT_MAX_TOTAL_REVIEWS,
    max_total_chars: int = DEFAULT_MAX_TOTAL_CHARS,
    max_chars_per_review: int = DEFAULT_MAX_CHARS_PER_REVIEW,
) -> dict[str, Any]:
    """Select a small, shop-balanced context from content-reranked reviews."""
    limits = {
        "max_shops": max_shops,
        "max_support_per_shop": max_support_per_shop,
        "max_conflict_per_shop": max_conflict_per_shop,
        "max_total_reviews": max_total_reviews,
        "max_total_chars": max_total_chars,
        "max_chars_per_review": max_chars_per_review,
    }
    if any(value <= 0 for value in limits.values()):
        raise ValueError("all context selection limits must be positive")

    groups = _group_top_shops(ranked_reviews, max_shops)
    selected_by_shop: dict[int, list[dict[str, Any]]] = {
        int(group["shopId"]): [] for group in groups
    }
    selected_ids: set[str] = set()
    total_chars = 0

    def try_select(group: dict[str, Any], review: dict[str, Any], role: str) -> bool:
        nonlocal total_chars
        review_id = str(review["reviewId"])
        if review_id in selected_ids or len(selected_ids) >= max_total_reviews:
            return False
        excerpt = _context_excerpt(_normalized_text(review), max_chars_per_review)
        if not excerpt or total_chars + len(excerpt) > max_total_chars:
            return False
        selected = {
            **review,
            "shopRank": int(group["shopRank"]),
            "selectedRole": role,
            "contextText": excerpt,
        }
        selected_by_shop[int(group["shopId"])].append(selected)
        selected_ids.add(review_id)
        total_chars += len(excerpt)
        return True

    # Round-robin support selection prevents one shop from consuming the context.
    for support_index in range(max_support_per_shop):
        for group in groups:
            supports = group["support"]
            if support_index < len(supports):
                try_select(group, supports[support_index], "support")

    # Conflicts are useful only when the same shop also has selected support.
    for conflict_index in range(max_conflict_per_shop):
        for group in groups:
            shop_id = int(group["shopId"])
            if not any(
                item["selectedRole"] == "support"
                for item in selected_by_shop[shop_id]
            ):
                continue
            conflicts = group["conflict"]
            if conflict_index < len(conflicts):
                try_select(group, conflicts[conflict_index], "conflict")

    shop_results: list[dict[str, Any]] = []
    selected_reviews: list[dict[str, Any]] = []
    for group in groups:
        shop_id = int(group["shopId"])
        selected = selected_by_shop[shop_id]
        support = [item for item in selected if item["selectedRole"] == "support"]
        conflict = [item for item in selected if item["selectedRole"] == "conflict"]
        if support and conflict:
            evidence_status = "mixed"
        elif support:
            evidence_status = "supported"
        else:
            evidence_status = "insufficient"
        shop_results.append(
            {
                "shopId": shop_id,
                "shopName": group.get("shopName"),
                "shopRank": int(group["shopRank"]),
                "evidenceStatus": evidence_status,
                "availableSupportCount": len(group["support"]),
                "availableConflictCount": len(group["conflict"]),
                "selectedSupportCount": len(support),
                "selectedConflictCount": len(conflict),
                "selectedReviewIds": [str(item["reviewId"]) for item in selected],
            }
        )
        selected_reviews.extend(selected)

    return {
        "candidateReviewCount": len(ranked_reviews),
        "topShopCount": len(groups),
        "selectedReviewCount": len(selected_reviews),
        "selectedCharacterCount": total_chars,
        "shops": shop_results,
        "selectedReviews": selected_reviews,
        "limits": limits,
    }
