import argparse
import json
import sys
import time
import uuid
from pathlib import Path
from urllib.parse import urlparse

import httpx

REPO_ROOT = Path(__file__).resolve().parents[2]
AGENT_ROOT = REPO_ROOT / "agent"
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(AGENT_ROOT))

from app.source_aware_agent_canary import (  # noqa: E402
    build_agent_canary_report,
    evaluate_agent_canary_case,
)


AGENT_ROOT = Path(__file__).resolve().parents[1]
CASES_PATH = (
    AGENT_ROOT / "knowledge_data" / "eval" / "review_hybrid_agent_canary_cases.json"
)
REPORT_PATH = (
    AGENT_ROOT / "knowledge_data" / "eval" / "review_hybrid_agent_canary_report.json"
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://localhost:8002")
    parser.add_argument("--timeout-seconds", type=float, default=60.0)
    return parser.parse_args()


def require_hybrid_canary_url(base_url: str) -> str:
    normalized = base_url.rstrip("/")
    parsed = urlparse(normalized)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("Hybrid canary URL must use http or https")
    if parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
        raise ValueError("Hybrid canary evaluator only permits a local host")
    if parsed.port != 8002:
        raise ValueError("Hybrid canary evaluator refuses non-Hybrid ports; expected 8002")
    return normalized


def wait_for_health(client: httpx.Client, base_url: str) -> None:
    last_error: Exception | None = None
    for _ in range(60):
        try:
            response = client.get(f"{base_url}/health")
            response.raise_for_status()
            return
        except httpx.HTTPError as exc:
            last_error = exc
            time.sleep(0.5)
    raise RuntimeError(f"Hybrid canary health check did not become ready: {last_error}")


def main() -> int:
    args = parse_args()
    base_url = require_hybrid_canary_url(args.base_url)
    payload = json.loads(CASES_PATH.read_text(encoding="utf-8"))
    if payload.get("sealed") is not False:
        raise ValueError("Hybrid canary cases must be marked sealed=false")
    cases = payload.get("cases")
    if not isinstance(cases, list) or not cases:
        raise ValueError("Hybrid canary cases must be non-empty")

    results: list[dict] = []
    stopped_early = False
    with httpx.Client(timeout=args.timeout_seconds) as client:
        wait_for_health(client, base_url)
        for case in cases:
            response = client.post(
                f"{base_url}/agent/chat-llm",
                json={
                    "message": case["question"],
                    "sessionId": f"rag-hybrid-{case['id']}-{uuid.uuid4().hex[:8]}",
                },
            )
            try:
                body = response.json()
            except ValueError:
                body = {"answer": response.text, "tool_trace": [], "trace": {}}
            result = evaluate_agent_canary_case(
                case,
                body,
                http_status=response.status_code,
            )
            results.append(result)
            print(
                f"{case['id']}: passed={result['passed']} "
                f"tools={','.join(result['toolNames']) or '-'} "
                f"failed={','.join(result['failedChecks']) or '-'}"
            )
            if not result["passed"]:
                stopped_early = len(results) < len(cases)
                break

    report = build_agent_canary_report(
        cases,
        results,
        base_url=base_url,
        stopped_early=stopped_early,
    )
    report["experiment"] = "RAG-PROD-01 stage 3 review Hybrid Agent canary"
    report["methodology"]["hybridEnabled"] = True
    report["methodology"]["hybridCanaryPortRequired"] = True
    temporary_path = REPORT_PATH.with_suffix(".json.tmp")
    temporary_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temporary_path.replace(REPORT_PATH)
    print(
        "review Hybrid Agent canary: "
        f"passed={report['summary']['passed']} "
        f"cases={report['summary']['passedCases']}/{report['summary']['cases']}"
    )
    print(f"Wrote {REPORT_PATH}")
    return 0 if report["summary"]["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
