"""P5 mechanical ablation on new scripted development data; zero model calls."""
import json
from collections import Counter

from agent.evaluation.context_program_v1_20260904 import budget_ablation as lib
from .common import HERE, append, check_freeze, file_sha, json_new, now
from .datasets import requirement


def main():
    check_freeze()
    out = HERE / "p5/offline001"
    out.mkdir(parents=True, exist_ok=False)
    data = HERE / "p1/development.json"
    cases = json.loads(data.read_text(encoding="utf-8"))
    json_new(out / "started.json", {"at": now(), "datasetSha256": file_sha(data),
        "runnerSha256": file_sha(__file__), "helperSha256": file_sha(lib.__file__),
        "inputNature": "SYNTHETIC_SCRIPTED_USER_HISTORY_NOT_MODEL_DIALOGUE", "modelCalls": 0})
    rows_out = []
    for case in cases:
        cid = case["conversationId"]
        last = case["turns"][-1]
        specs = {"os": ("eq", "enum"), "price_minor": ("lte", "CNY_MINOR"), "storage_gb": ("gte", "GB")}
        hard = [requirement(k, specs[k][0], v, specs[k][1]) for k, v in last["expectedHard"].items()]
        payload = {"runId": "p5-" + cid, "taskId": "p5-" + cid, "baseContextRevision": 5,
            "goal": last["rawUserText"], "confirmedFacts": [{"key": "category", "value": "phone"}],
            "hardConstraints": hard, "softPreferences": [{"useCase": "日常稳定性"}],
            "historySummaries": [{"role": "user", "summary": t["rawUserText"],
                "kind": "older_summary", "sourceTurns": [t["semanticTurn"]]} for t in case["turns"][:-1]],
            "candidateScopeState": {"candidateIds": [], "comparedIds": [], "evidenceStatus": "missing"},
            "evidenceRefs": []}
        run = lib.make_run(cid, 5)
        items = lib.context_items_from_pack(lib.Pack(payload), run)
        for component in lib.COMPONENTS:
            selected = lib.variant(items, component)
            for policy in lib.POLICIES:
                for budget in lib.BUDGETS:
                    one = lib.compile_once(run, selected, budget, policy, payload["goal"])
                    two = lib.compile_once(run, selected, budget, policy, payload["goal"])
                    row = {"conversationId": cid, "familyId": case["familyId"],
                        "component": component, "policy": policy, "budget": budget,
                        "deterministicExact": one == two, **one}
                    append(out / "rows.jsonl", row)
                    rows_out.append(row)
    passed = len(rows_out) == 800 and all(r["deterministicExact"] and (
        r.get("allProtectedRetained", False) if r["status"] == "COMPILED"
        else r.get("expectedBecauseProtectedExceedsBudget", False)) for r in rows_out)
    check_freeze()
    result = {"status": "PASS_OFFLINE_MECHANICS" if passed else "HOLD_OFFLINE_MECHANICS",
        "rows": len(rows_out), "compilations": len(rows_out)*2,
        "byStatus": dict(Counter(r["status"] for r in rows_out)), "modelCalls": 0,
        "qualityOrEfficiencyClaim": False, "onlineAblation": "PENDING_MAIN_QUALITY_GATE"}
    json_new(out / "result.json", result)
    print(json.dumps(result), flush=True)


if __name__ == "__main__":
    main()
