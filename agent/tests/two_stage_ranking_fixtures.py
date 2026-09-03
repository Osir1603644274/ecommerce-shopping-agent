from app.domains.ecommerce.ranking_contract import (
    RANKING_FORMULA,
    RANKING_TIE_BREAK,
    TWO_STAGE_RANKING_CONTRACT_VERSION,
)
from app.domains.ecommerce.used_phone_attributes import (
    USED_PHONE_ATTRIBUTE_EVIDENCE_FIELD,
    USED_PHONE_ATTRIBUTE_RULESET_VERSION,
)


def two_stage_search_detail(
    ranked_ids: list[int], *, candidate_pool_ids: list[int] | None = None,
) -> dict:
    pool = list(candidate_pool_ids if candidate_pool_ids is not None else ranked_ids)
    ranked = list(ranked_ids)
    evidence = []
    for item in ranked:
        evidence.extend([
            {"ref": f"product:{item}:title", "field": "title", "rawValue": str(item)},
            {
                "ref": f"product:{item}:attribute:os",
                "field": USED_PHONE_ATTRIBUTE_EVIDENCE_FIELD,
                "method": USED_PHONE_ATTRIBUTE_RULESET_VERSION,
                "rawValue": "iOS",
            },
        ])
    return {
        "contractVersion": TWO_STAGE_RANKING_CONTRACT_VERSION,
        "candidatePoolIds": pool,
        "rankedItemIds": ranked,
        "candidateIds": list(ranked),
        "candidates": [
            {
                "id": item,
                "title": str(item),
                "priceStatus": "unverified",
                "snapshotPriceMinor": None,
                "currency": "CNY",
                "attributes": [{
                    "key": "os",
                    "rawValue": "iOS",
                    "normalizedText": "ios",
                    "evidenceField": USED_PHONE_ATTRIBUTE_EVIDENCE_FIELD,
                    "extractionMethod": USED_PHONE_ATTRIBUTE_RULESET_VERSION,
                }],
                "checks": [{
                    "key": "os", "operator": "eq", "expected": "ios",
                    "unit": "enum", "priority": "hard", "source": "user",
                    "status": "pass", "actual": "ios",
                    "evidenceRef": f"product:{item}:attribute:os",
                }],
                "evidenceRefs": [
                    f"product:{item}:title", f"product:{item}:attribute:os",
                ],
            }
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
            "rankedItemIds": list(ranked),
            "evidenceRefCount": len(evidence),
            "binding": "current_successful_tool_call_ranked_items_only",
        },
        "evidenceRefs": [
            ref
            for item in ranked
            for ref in (
                f"product:{item}:title", f"product:{item}:attribute:os",
            )
        ],
        "evidence": evidence,
        "eliminated": [],
    }
