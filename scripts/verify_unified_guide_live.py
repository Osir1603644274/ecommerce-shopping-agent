"""Read-only shopping dialogue smoke test through the real 5173 BFF.

It creates a fresh guest conversation and never selects, orders or pays.
An interrupted run is left for explicit recovery rather than silently retried.
"""
from __future__ import annotations

import argparse
import json
import time
import uuid
from pathlib import Path

import httpx


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:5173")
    parser.add_argument("--scenario", choices=("core", "cross", "compare", "product", "business"), default="core")
    parser.add_argument("--turns", type=int, choices=(1, 2, 3), default=1)
    parser.add_argument("--timeout", type=float, default=240)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    scenarios = {
        "core": ["找一个玻璃杯", "要带盖的", "撤销上次修改"],
        "cross": ["找一个玻璃杯", "换成无线耳机"],
        "compare": ["找一个玻璃杯", "比较第1项和第2项"],
        "product": ["找一个玻璃杯", "第1项多少钱"],
        "business": ["我想了解申请退款需要哪些信息，先不要提交申请"],
    }
    messages = scenarios[args.scenario][:args.turns]
    records = []
    with httpx.Client(base_url=args.base_url, headers={"Origin": args.base_url,
                       "Sec-Fetch-Site": "same-origin"}, timeout=20) as client:
        start = client.get("/api/commerce-demo/workspace")
        start.raise_for_status()
        csrf = start.json()["csrfToken"]
        for message in messages:
            request_id = uuid.uuid4().hex
            began = time.monotonic()
            launched = client.post("/api/commerce-demo/workspace/run",
                headers={"X-CSRF-Token": csrf},
                json={"message": message, "requestId": request_id, "mode": "continuous"})
            launched.raise_for_status()
            while True:
                response = client.get("/api/commerce-demo/workspace/control",
                                      headers={"X-CSRF-Token": csrf})
                if response.status_code >= 400:
                    raise RuntimeError(f"control status={response.status_code} detail={response.text[:250]}")
                response.raise_for_status()
                workspace = response.json()
                run = workspace.get("run") or {}
                if run.get("requestId") != request_id:
                    raise RuntimeError("run identity changed during verification")
                if run.get("status") not in {"running", "pausing"}:
                    break
                if time.monotonic() - began >= args.timeout:
                    raise TimeoutError(f"run {request_id} exceeded {args.timeout}s")
                time.sleep(1)
            assistant = next((row for row in reversed(workspace["messages"])
                              if row.get("role") == "assistant" and row.get("requestId") == request_id), None)
            record = {"requestId": request_id, "message": message,
                      "status": run.get("status"), "durationSeconds": round(time.monotonic()-began, 2),
                      "answer": assistant.get("content") if assistant else None,
                      "nodes": [{"label": n.get("label"), "outcome": n.get("outcome"),
                                 "durationMs": n.get("durationMs"),
                                 "parseAttempts": (n.get("detail") or {}).get("parseAttempts"),
                                 "parseDurationMs": (n.get("detail") or {}).get("parseDurationMs"),
                                 "parseUsage": (n.get("detail") or {}).get("parseUsage"),
                                 "answerDurationMs": (n.get("detail") or {}).get("answerDurationMs"),
                                 "answerUsage": (n.get("detail") or {}).get("answerUsage")}
                                for n in run.get("nodes", [])]}
            records.append(record)
            print(json.dumps({"requestId": request_id, "message": message,
                              "status": record["status"], "durationSeconds": record["durationSeconds"],
                              "answerCharacters": len(record["answer"] or ""),
                              "parseTokens": next(((n["parseUsage"] or {}).get("total_tokens")
                                                   for n in record["nodes"] if n["parseUsage"]), None)},
                             ensure_ascii=False), flush=True)
            if run.get("status") != "completed":
                break
    if args.output is not None:
        if args.output.exists():
            raise FileExistsError(args.output)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")
    if len(records) != len(messages) or any(row["status"] != "completed" for row in records):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
