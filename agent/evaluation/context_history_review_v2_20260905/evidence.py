"""Factor repeated evidence, retaining a verified exact round trip."""
from copy import deepcopy

from agent.evaluation.context_history_strategies_v1_20260905.artifacts import canonical, sha
from agent.evaluation.context_history_strategies_v1_20260905.review_evidence import validated_turn_evidence as legacy_turn_evidence


def validated_turn_evidence(row):
    value = legacy_turn_evidence(row)
    batches = []
    if value.get("validationPassedThisTurn"):
        for index, trace in enumerate(row.get("toolTraces", [])):
            if not trace.get("ok") or trace.get("tool") not in {"search_products", "compare_products", "get_product_details"}:
                continue
            ids = trace.get("detail", {}).get("rankedItemIds")
            if isinstance(ids, list) and ids:
                batches.append({"traceIndex": index, "tool": trace["tool"], "orderedProductIds": [str(item) for item in ids]})
    # Legacy helper used sorted(set(ids)), which is membership order, not the
    # tool's actual ranking. Never present that diagnostic set as a ranking.
    value["rankedProductIds"] = batches[-1]["orderedProductIds"] if batches else []
    value["rankedProductBatches"] = batches
    value["rankingOrderSource"] = "literal rankedItemIds from last successful relevant tool trace, never sorted by product ID"
    return value


def encode(samples):
    catalog = {}
    layouts = {}
    by_hash = {}
    layout_by_keys = {}
    encoded = deepcopy(samples)

    def intern(value):
        digest = sha(value)
        if digest in by_hash:
            identifier = by_hash[digest]
            if canonical(expand_entry(catalog[identifier], layouts)) != canonical(value):
                raise ValueError("evidence_hash_collision")
            return identifier
        keys = tuple(sorted(value))
        if keys not in layout_by_keys:
            layout_id = "l" + str(len(layouts) + 1)
            layout_by_keys[keys] = layout_id
            layouts[layout_id] = list(keys)
        identifier = "e" + str(len(catalog) + 1)
        by_hash[digest] = identifier
        catalog[identifier] = {"layout": layout_by_keys[keys], "values": [deepcopy(value[key]) for key in keys]}
        return identifier

    for sample in encoded:
        for row in sample["verifiedEvidenceByTurn"]:
            products = row.pop("products")
            row["productEvidenceRefs"] = []
            for product in products:
                value = deepcopy(product)
                checks = value.pop("checks", [])
                if isinstance(value.get("specifications"), dict):
                    if "specificationsRef" in value:
                        raise ValueError("reserved_evidence_encoding_key")
                    value["specificationsRef"] = intern(value.pop("specifications"))
                # Product facts and per-condition checks repeat across many
                # turns; keep their exact objects once, not textual summaries.
                row["productEvidenceRefs"].append({"product": intern(value),
                    "checks": [intern(check) for check in checks]})
    packet = {"evidenceLayouts": layouts, "evidenceDictionary": catalog, "samples": encoded}
    if canonical(decode(packet)) != canonical(samples):
        raise ValueError("evidence_round_trip_mismatch")
    return packet


def expand_entry(entry, layouts):
    keys = layouts[entry["layout"]]
    if len(keys) != len(entry["values"]):
        raise ValueError("evidence_column_count_mismatch")
    return {key: deepcopy(value) for key, value in zip(keys, entry["values"])}


def decode(packet):
    samples = deepcopy(packet["samples"])
    catalog = packet["evidenceDictionary"]
    layouts = packet["evidenceLayouts"]
    for sample in samples:
        for row in sample["verifiedEvidenceByTurn"]:
            row["products"] = []
            for ref in row.pop("productEvidenceRefs"):
                value = expand_entry(catalog[ref["product"]], layouts)
                if "specificationsRef" in value:
                    value["specifications"] = expand_entry(catalog[value.pop("specificationsRef")], layouts)
                value["checks"] = [expand_entry(catalog[key], layouts) for key in ref["checks"]]
                row["products"].append(value)
    return samples
