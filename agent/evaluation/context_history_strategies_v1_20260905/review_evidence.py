"""Compact complete-trajectory evidence for blind review; no gold TaskState."""
from copy import deepcopy


def validated_turn_evidence(row):
    common = {"answerFormatContracts": [deepcopy(item["answerFormat"])
        for item in row.get("contextPolicyEvidence", []) if isinstance(item.get("answerFormat"), dict)],
        "persistedRequirements": deepcopy(row.get("postState", {}).get("domainState", {}).get("shoppingGuide", {}).get("requirements", []))}
    published = row.get("publishedGuideResult") or {}
    displayed_ids = [str(item["product"]["id"]) for item in published.get("products", [])
        if isinstance(item.get("product"), dict) and item["product"].get("id") is not None]
    phases = (row.get("traceSummary") or {}).get("phases", [])
    passed = any(item.get("phase") == "validator" and item.get("outcome") == "passed" for item in phases)
    if not passed:
        return {**common, "turn": row["turn"], "validationPassedThisTurn": False, "products": [],
                "displayedProductIds": displayed_ids,
                "note": "No passed receipt supplied for this turn; absence is not proof of fabrication. Earlier verified evidence may still be relevant."}
    products = {}
    ranked_ids = []
    for trace in row.get("toolTraces", []):
        if not trace.get("ok") or trace["tool"] not in {"search_products", "compare_products", "get_product_details"}:
            continue
        detail = trace["detail"]
        if detail.get("rankedItemIds"):
            ranked_ids = [str(value) for value in detail["rankedItemIds"]]
        for product in detail.get("products", detail.get("candidates", [])):
            facts = product.get("facts")
            if not isinstance(facts, dict):
                continue
            identifier = facts.get("productId", product.get("id"))
            if identifier is None:
                continue
            products[str(identifier)] = {key: deepcopy(facts[key]) for key in
                ("productId", "title", "brand", "priceMinor", "priceStatus", "currency", "specifications") if key in facts}
            products[str(identifier)].update({"displayedThisTurn": str(identifier) in displayed_ids,
                "selectionType": product.get("selectionType"), "checks": deepcopy(product.get("checks", []))})
    return {**common, "turn": row["turn"], "validationPassedThisTurn": True, "products": list(products.values()),
            "rankedProductIds": ranked_ids, "displayedProductIds": displayed_ids,
            "note": "A passed tool result can retain explicitly uncertain closest_alternative candidates. Retained is not the same as displayed, recommended, or confirmed satisfying every hard condition. Checks describe the SUT's evaluation, not gold interpretation of user requirements."}
