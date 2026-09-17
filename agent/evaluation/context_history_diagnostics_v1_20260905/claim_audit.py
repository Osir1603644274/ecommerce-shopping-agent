"""Bind the complete 24-turn review's budget claims to recorded outputs."""
import argparse
import json

from agent.evaluation.context_history_strategies_v1_20260905.artifacts import HERE, file_sha, write_new
from .budget_after_compare import load_case, price


def run(output):
    findings = []
    for arm, turns in (("A", [7]), ("B", [7]), ("C", [7, 9, 10])):
        original, arguments, extraction_hashes = load_case(arm)
        model_price = price(arguments["domainStatePatch"]["shoppingGuide"]["upsertRequirements"])
        assert model_price == 140000
        for turn in turns:
            path = HERE / f"core24_v3_{arm}001/turn-{turn:02}.json"
            row = json.loads(path.read_text(encoding="utf-8"))
            current_price = price(row["postState"]["domainState"]["shoppingGuide"]["requirements"])
            assert current_price == 160000
            cards = [{"product": card["product"], "selectionType": card["selectionType"]}
                for card in row["publishedGuideResult"]["products"]
                if card["product"].get("syntheticReferencePriceMinor", 0) > 140000]
            findings.append({"arm": arm, "turn": turn, "status": "CONFIRMED_SERIOUS_BUDGET_FAILURE",
                "source": str(path), "sourceSha256": file_sha(path), "extractionHashes": extraction_hashes,
                "currentUserQuery": row["query"], "budgetChangeQuery": original["query"],
                "answer": row["answer"], "expectedPriceMinor": model_price,
                "persistedPriceMinor": current_price, "overBudgetPublishedCards": cards,
                "scope": "false update or current-budget confirmation" + (" and over-budget full-match card" if cards else "")})
    census = HERE / "core24_v3_C001_census001.json"
    assert json.loads(census.read_text(encoding="utf-8"))["summaryCommits"] == 0
    review = HERE / "core24_v3_complete_review001/result.json"
    value = {"status": "DEVELOPMENT_QUALITY_HOLD", "modelCalls": 0,
        "reviewSource": str(review), "reviewSha256": file_sha(review),
        "censusSource": str(census), "censusSha256": file_sha(census),
        "findings": findings, "humanGold": False, "formalAcceptance": False,
        "conclusions": [
            "All three extraction call-012 payloads proposed 140000 correctly; stale compare-mode canonicalization restored 160000.",
            "A and B corrected on turn 9; C remained wrong through turn 10. C made no summaries in this episode, so this is not evidence of summary-caused loss.",
            "C turn 10 has no over-budget visible card in the recorded first three, but falsely confirms 1400 while authoritative filter remains 1600.",
            "Prices are disclosed synthetic references, not live marketplace prices.",
            "Average score closeness and native token savings do not override these serious failures. Concurrent timings remain descriptive."]}
    write_new(output, value)
    print(json.dumps({"status": value["status"], "confirmedSeriousClaims": len(findings)}, ensure_ascii=False))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("output")
    run(parser.parse_args().output)
