"""Bounded M1/M2 extraction evaluation; immutable attempts, no hidden retries."""
import asyncio
import hashlib
import json
from pathlib import Path
import sys
import time
from unittest.mock import patch

from agent.app import memory_candidate_worker as worker
from agent.evaluation.context_history_strategies_v1_20260905.subscription import SubscriptionClient
from agent.evaluation.context_history_strategies_v1_20260905.artifacts import write_new
from .dataset import rows

ROOT = Path(__file__).resolve().parents[3]
OUT = Path("D:/agent-experiments/memory-natural-v1-20260907")


def semantic(values):
    return sorted(tuple(item[key] for key in ("preferenceKind", "attributeKey", "normalizedValue"))
                  for item in values)


async def main(name, split):
    output = OUT / name
    output.mkdir(exist_ok=False)
    cases = rows(split)
    source_files = [Path(worker.__file__), Path(__file__), Path(__file__).with_name("dataset.py")]
    hashes = {str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in source_files}
    write_new(output / "started.json", {"kind": "EXTRACTION_ONLY", "split": split,
        "cases": cases, "sourceSha256": hashes, "maxCalls": 120, "timeoutSeconds": 3600,
        "provenance": "Authored synthetic cases using frozen V17 seeds and catalog vocabulary; not real conversations",
        "M0": "no extraction or candidates by design", "usageScope": "CODEX_TURN_INCLUDING_HOST_OVERHEAD"})
    client = SubscriptionClient(output / "model_calls", max_calls=120,
        timeout_seconds=180, application_input_budget=16000)
    observed = []
    started = time.monotonic()
    with patch.object(worker.settings, "memory_catalog_values_path", str(ROOT / "agent" /
            worker.settings.memory_catalog_values_path)):
        worker._catalog.cache_clear()
        for index, case in enumerate(cases):
            # Alternate paired order; identical common catalog and provider.
            for arm in (("M1", "M2") if index % 2 == 0 else ("M2", "M1")):
                call_start = len(client.calls)
                try:
                    proposals, receipt = await worker._extract_observed(
                        case["message"], "phone", case["recipientScope"],
                        strategy="explicit_v1" if arm == "M1" else "natural_v1",
                        message_id=case["caseId"], client=client, model="gpt-5.6-sol")
                    actual = semantic(proposals)
                    expected = semantic(case["expected"]) if (
                        arm == "M2" or worker.is_explicit_memory_request(case["message"])) else []
                    record = {"caseId": case["caseId"], "arm": arm,
                        "proposals": proposals, "observation": receipt.plain(),
                        "actual": actual, "strategyExpected": expected,
                        "naturalOpportunity": semantic(case["expected"]), "exact": actual == expected}
                except Exception as exc:
                    record = {"caseId": case["caseId"], "arm": arm,
                              "error": type(exc).__name__, "detail": str(exc), "exact": False}
                record["nativeOrdinals"] = [row["ordinal"] for row in client.calls[call_start:]]
                observed.append(record)
                write_new(output / f"{case['caseId']}-{arm}.json", record)
                print(json.dumps({"case": case["caseId"], "arm": arm,
                    "exact": record["exact"], "modelCalls": len(client.calls)}, ensure_ascii=True), flush=True)
                if record.get("error"):
                    break  # Retain failure; do not auto-retry the same sample.
    metrics = {}
    for arm in ("M1", "M2"):
        selected = [row for row in observed if row["arm"] == arm]
        correct = sum(len(set(map(tuple, row.get("actual", []))) & set(map(tuple, row.get("naturalOpportunity", [])))) for row in selected)
        proposed = sum(len(row.get("actual", [])) for row in selected)
        positives = sum(len(row["expected"]) for row in cases)
        metrics[arm] = {"observedCases": len(selected), "exactCases": sum(row["exact"] for row in selected),
            "correctPreferences": correct, "proposedPreferences": proposed, "eligiblePreferences": positives,
            "precision": correct/proposed if proposed else None, "recall": correct/positives if positives else None,
            "errors": sum("error" in row for row in selected)}
    changed = [path for path, expected in hashes.items() if hashlib.sha256(Path(path).read_bytes()).hexdigest() != expected]
    write_new(output / "report.json", {"split": split, "metrics": metrics,
        "durationSeconds": time.monotonic()-started, "calls": len(client.calls),
        "usage": [row.get("usage") for row in client.calls], "sourceChanged": changed,
        "complete": len(observed) == 2*len(cases) and not changed,
        "fullAgentQuality": "NOT_MEASURED_BY_THIS_COMPONENT_EVALUATION"})
    print(json.dumps(metrics), flush=True)


if __name__ == "__main__":
    asyncio.run(asyncio.wait_for(main(*sys.argv[1:]), timeout=3600))
