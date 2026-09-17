"""Two real, tool-isolated subscription calls; not a quality benchmark."""
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

ROOT = Path(__file__).resolve().parents[3]
OUT = Path("D:/agent-experiments/memory-natural-v1-20260907")


async def main(name):
    output = OUT / name
    output.mkdir(exist_ok=False)
    sources = {str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
               for path in (ROOT / "agent/app").rglob("*.py")}
    write_new(output / "started.json", {"kind": "CANDIDATE_ADAPTER_SMOKE_ONLY",
        "maxModelCalls": 2, "timeoutSeconds": 660, "sourceSha256": sources,
        "catalogSha256": worker.settings.memory_catalog_values_sha256,
        "noApiFallback": True, "productionEnabled": False})
    client = SubscriptionClient(output / "model_calls", max_calls=2, timeout_seconds=300,
                                application_input_budget=16000)
    rows = []
    started = time.monotonic()
    with patch.object(worker.settings, "memory_catalog_values_path", str(ROOT / "agent" /
                      worker.settings.memory_catalog_values_path)):
        worker._catalog.cache_clear()
        for index, message in enumerate([
            "我买二手机一般优先原装屏幕。",
            "这次预算两千元，给我看看有什么手机。",
        ], 1):
            try:
                result, observation = await worker._extract_observed(
                    message, "phone", "self", strategy="natural_v1", message_id=f"pilot-{index}",
                    client=client, model="gpt-5.6-sol")
                row = {"message": message, "result": result, "observation": observation.plain()}
            except Exception as exc:
                row = {"message": message, "errorType": type(exc).__name__, "error": str(exc)}
            rows.append(row)
            write_new(output / f"case-{index}.json", row)
            print(json.dumps({"case": index, "result": row}, ensure_ascii=False), flush=True)
            if "error" in row:
                break
    changed = [path for path, expected in sources.items()
               if hashlib.sha256((ROOT / path).read_bytes()).hexdigest() != expected]
    passed = (len(rows) == 2 and all("error" not in row for row in rows)
        and len(rows[0]["result"]) == 1 and rows[0]["result"][0]["attributeKey"] == "screen_originality"
        and rows[0]["result"][0]["normalizedValue"] == "original" and rows[1]["result"] == []
        and all(row["observation"]["usageObserved"] for row in rows) and not changed)
    write_new(output / "report.json", {"status": "PASS" if passed else "FAIL",
        "rows": rows, "calls": client.calls, "durationSeconds": time.monotonic()-started,
        "sourcesChangedDuringRun": changed, "fullAgentIntegration": "NOT_TESTED_HERE"})
    print(json.dumps({"status": "PASS" if passed else "FAIL", "calls": len(client.calls)}), flush=True)


if __name__ == "__main__":
    asyncio.run(asyncio.wait_for(main(sys.argv[1]), timeout=660))
