"""Offline assertions and production-shaped frozen tool bridge. No model calls."""
import asyncio
import copy
import json
import time
from . import pilot as p


def main():
    manifest = p.verify_freeze()
    checks = []
    for entry in manifest["fixtures"]:
        f = p.read(p.HERE / "inputs" / (entry["id"] + ".json"))
        assert all(h["content"] in f["prompts"]["A_RAW"] for h in f["raw"]["fullHistory"])
        assert p.sha(f["pack"]) == f["packHashForBAndC"]
        assert f["compilerBudgetEvictions"] == 0
        # Oracle container and treatment labels must not be part of model input.
        for prompt in f["prompts"].values():
            assert '"oracle"' not in prompt
            assert not any(arm in prompt for arm in p.ARMS)
        checks.append({"fixture": f["id"], "fullHistoryPresentInA": True,
            "samePackBAndC": True, "budgetEvictions": 0, "oracleContainerExcluded": True,
            "promptChars": {a: len(t) for a, t in f["prompts"].items()},
            "contextEqualBC": f["contexts"]["B_PACK"] == f["contexts"]["C_PACK_COMPILER"]})

    llm, _, _, _, _, _, _, decoder, old = p.load_app()
    negatives = []
    for label, payload in (("unexpected_arguments", {"arguments": {}}), ("missing_required", {})):
        try:
            decoder(payload, llm.TASK_STATE_TOOL_SCHEMA)
        except ValueError as exc:
            negatives.append({"test": label, "rejected": True, "reason": str(exc)})
        else:
            raise AssertionError("strict negative accepted: " + label)

    products = tuple(old._product_from_catalog(json.loads(line))
        for line in p.CATALOG.read_text(encoding="utf-8").splitlines() if line.strip())
    before = p.sha(products)
    transport = old.FrozenCatalogTransport(products)
    ids = [int(x["id"]) for x in products[:2]]
    start = time.perf_counter()
    details = asyncio.run(transport("get_product_details", {"productIds": ids}))
    repeat = asyncio.run(transport("get_product_details", {"productIds": ids}))
    denied = asyncio.run(transport("create_order", {}))
    assert details.ok and repeat.ok and not denied.ok
    assert details.detail == repeat.detail
    assert denied.detail.get("code") == "offline_tool_denied"
    assert before == p.sha(products)
    result = {"status": "OFFLINE_CHECKS_PASS", "at": p.now(), "sourceFilesVerified": len(manifest["sources"]),
        "fixtureChecks": checks, "strictNegativeChecks": negatives, "modelCalls": 0,
        "toolBridge": {"transport": "FrozenCatalogTransport", "readTool": "get_product_details",
            "ids": ids, "successfulReads": 2, "stableResultHash": p.sha(details.detail),
            "immutableCatalog": True, "writeDeniedLocally": True, "toolWallMs": (time.perf_counter()-start)*1000,
            "modelDirectedToolLoop": False, "liveBusinessWrite": False}}
    p.verify_freeze()
    p.write_new(p.HERE / "offline_audit.json", result)
    print(p.canonical(result), flush=True)


if __name__ == "__main__":
    main()
