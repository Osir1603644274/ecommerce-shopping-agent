"""Explicit user-request regression checks; not a substitute for blind quality."""
import argparse
import json
from pathlib import Path

from agent.evaluation.context_history_strategies_v1_20260905.artifacts import file_sha, write_new


def inspect(directory):
    problems, rows, hashes = [], [], {}
    result = json.loads((directory / "result.json").read_text(encoding="utf-8"))
    if result.get("turns") != 12 or result.get("status") != "INTEGRATION_PASS":
        problems.append({"code": "probe_execution_not_complete_and_safe"})
    for turn in range(1, 13):
        path = directory / f"turn-{turn:02}.json"
        row = json.loads(path.read_text(encoding="utf-8"))
        hashes[path.name] = file_sha(path)
        if turn == 1:
            continue
        expected = 160000 if turn < 7 else 140000 if turn < 11 else 150000
        state = row["postState"]
        guide = state["domainState"]["shoppingGuide"]
        v2 = state["domainState"]["shoppingTaskStateV2"]["shoppingGuide"]
        lanes = {"guide": guide["requirements"], "v2Guide": v2["requirements"], "constraints": state["constraints"]}
        prices = {}
        for name, requirements in lanes.items():
            match = [r for r in requirements if r["key"] == "price_minor"]
            prices[name] = match[0]["value"] if len(match) == 1 else None
            if prices[name] != expected:
                problems.append({"turn": turn, "code": "budget_not_applied", "lane": name, "expected": expected, "actual": prices[name]})
        requirements = {r["key"]: r for r in guide["requirements"]}
        expected_fields = {"os": "android"}
        if turn >= 3:
            expected_fields.update({"screen_originality": "original", "shell_condition": "normal"})
        for key, value in expected_fields.items():
            actual = requirements.get(key, {})
            if actual.get("value") != value or actual.get("priority") != "hard":
                problems.append({"turn": turn, "code": "unchanged_hard_requirement_lost", "key": key, "actual": actual})
        for key in ("brand", "battery_health", "battery_originality"):
            if requirements.get(key, {}).get("priority") == "hard":
                problems.append({"turn": turn, "code": "unrequested_hard_requirement", "key": key})
        over_budget = []
        for card in (row.get("publishedGuideResult") or {}).get("products", []):
            product = card["product"]
            price = product.get("snapshotPriceMinor")
            if price is None:
                price = product.get("syntheticReferencePriceMinor")
            if card.get("selectionType") == "full_match" and (not isinstance(price, (int, float)) or price > expected):
                over_budget.append({"productId": product["id"], "priceMinor": price})
        if over_budget:
            problems.append({"turn": turn, "code": "invalid_full_match_price", "cards": over_budget})
        if turn == 12 and any(t.get("tool") in {"search_products", "compare_products", "get_product_details"} for t in row["toolTraces"]):
            problems.append({"turn": turn, "code": "recall_only_request_ran_commerce_tool"})
        rows.append({"turn": turn, "expectedBudgetMinor": expected, "actualBudgets": prices,
            "answer": row["answer"], "modelCalls": len(row["modelCalls"]), "toolCalls": len(row["toolTraces"])})
    return {"status": "BUDGET_REGRESSION_PASS_NOT_FULL_QUALITY" if not problems else "BUDGET_REGRESSION_HOLD",
        "source": str(directory.resolve()), "problems": problems, "checks": rows, "turnHashes": hashes,
        "sourceResultSha256": file_sha(directory / "result.json"), "auditSourceSha256": file_sha(__file__),
        "expectedValuesSource": "Explicit fixed user requests in budget_v5_probe.json, not injected into SUT.",
        "modelCallsIssuedByAudit": 0, "fullAnswerQualityAcceptance": False, "formalAcceptance": False}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("directory", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    value = inspect(args.directory)
    write_new(args.output, value)
    print(json.dumps({"status": value["status"], "problems": value["problems"]}, ensure_ascii=False))
