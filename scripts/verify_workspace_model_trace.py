"""One explicit local read-only model/trace acceptance; never overwrites evidence."""
import argparse
import json
import uuid
from pathlib import Path

import httpx

parser = argparse.ArgumentParser()
parser.add_argument("--attempt", required=True, choices=["02", "03"])
args = parser.parse_args()
target = Path(__file__).resolve().parent.parent / "docs/acceptance/unified-commerce-20260908-evidence" / f"model-chat-attempt{args.attempt}.json"
if target.exists():
    raise RuntimeError("Attempt already exists; no request was sent")
question = "两千元预算，给父母买二手手机，主要微信视频和看新闻，在苹果和安卓之间怎么选？"
session = "workspace-model-qa-" + uuid.uuid4().hex
with httpx.Client(timeout=190) as client:
    reference = None
    if args.attempt == "03":
        initial = client.post("http://127.0.0.1:8000/agent/chat-llm/stream", json={
            "message": "推荐2000元以内的二手手机", "sessionId": session, "domainHint": "ecommerce"})
        initial.raise_for_status()
        first = next(json.loads(line[6:])["data"] for line in initial.text.splitlines()
                     if line.startswith("data: ") and json.loads(line[6:]).get("type") == "complete")
        assert len((first.get("guideResult") or {}).get("products", [])) >= 2
        reference = first.get("referenceContext")
        question = "对比第一款和第二款，哪些信息已经确认，哪些还需要我核实？"
    response = client.post("http://127.0.0.1:8000/agent/chat-llm/stream", json={
        "message": question, "sessionId": session, "domainHint": "ecommerce",
        **({"referenceContext": {"handle": reference["handle"], "presentationMode": "compact"}} if reference else {})})
    response.raise_for_status()
    events = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")]
    final = next((e["data"] for e in events if e.get("type") == "complete"), None)
    assert final, "No final event"
    trace = final.get("trace") or {}
    # Keep only safe request/phase metrics, not credentials, raw tools or hidden context.
    safe_trace = {key: value for key, value in trace.items() if key in {
        "requestId", "status", "runId", "modelCallCounts", "llmDurationMs", "llmDurationByStageMs", "modelCallFailures", "totalDurationMs"}}
    evidence = {"question": question, "answer": final.get("answer"), "runId": final.get("runId"),
        "sessionId": session, "trace": safe_trace, "traceFieldNames": sorted(trace),
        "deltaEvents": sum(e.get("type") == "delta" for e in events),
        "scope": "Same existing chat handler used by BFF; direct read-only HTTP trace check, not a browser transaction."}
    target.write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(evidence, ensure_ascii=True))
