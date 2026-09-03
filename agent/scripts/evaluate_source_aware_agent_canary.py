import argparse
import json
import sys
import time
import uuid
from pathlib import Path
from urllib.parse import urlparse

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.source_aware_agent_canary import (  # noqa: E402
    build_agent_canary_report,
    evaluate_agent_canary_case,
)


AGENT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CASES_PATH = (
    AGENT_ROOT / "knowledge_data" / "eval" / "source_aware_agent_canary_cases.json"
)
DEFAULT_REPORT_PATH = (
    AGENT_ROOT / "knowledge_data" / "eval" / "source_aware_agent_canary_report.json"
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://localhost:8001")
    parser.add_argument("--cases-path", type=Path, default=DEFAULT_CASES_PATH)
    parser.add_argument("--report-path", type=Path, default=DEFAULT_REPORT_PATH)
    parser.add_argument("--timeout-seconds", type=float, default=60.0)
    return parser.parse_args()


def require_isolated_canary_url(base_url: str) -> str:
    normalized = base_url.rstrip("/")
    parsed = urlparse(normalized)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("canary base URL must use http or https")
    if parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
        raise ValueError("canary evaluator only permits a local host")
    if parsed.port != 8001:
        raise ValueError("canary evaluator refuses non-canary ports; expected 8001")
    return normalized


def wait_for_health(
    client: httpx.Client,
    base_url: str,
    *,
    attempts: int = 10,
    interval_seconds: float = 0.5,
) -> None:
    last_error: Exception | None = None
    for _ in range(attempts):
        try:
            response = client.get(f"{base_url}/health")
            response.raise_for_status()
            return
        except httpx.HTTPError as exc:
            last_error = exc
            time.sleep(interval_seconds)
    raise RuntimeError(f"canary health check did not become ready: {last_error}")


def main() -> int:
    args = parse_args()
    base_url = require_isolated_canary_url(args.base_url)
    payload = json.loads(args.cases_path.read_text(encoding="utf-8"))
    if payload.get("sealed") is not False:
        raise ValueError("canary cases must be explicitly marked sealed=false")
    cases = payload.get("cases")
    if not isinstance(cases, list) or not cases:
        raise ValueError("canary cases file must contain a non-empty cases list")

    results: list[dict] = []
    stopped_early = False
    with httpx.Client(timeout=args.timeout_seconds) as client:
        wait_for_health(client, base_url)
        for case in cases:
            session_id = f"rag-prod-canary-{case['id']}-{uuid.uuid4().hex[:8]}"
            response = client.post(
                f"{base_url}/agent/chat-llm",
                json={"message": case["question"], "sessionId": session_id},
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
    args.report_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = args.report_path.with_suffix(".json.tmp")
    temporary_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temporary_path.replace(args.report_path)
    print(
        "source-aware Agent canary: "
        f"passed={report['summary']['passed']} "
        f"cases={report['summary']['passedCases']}/{report['summary']['cases']} "
        f"executed={report['summary']['executed']} "
        f"warnings={report['summary']['warnings']}"
    )
    print(f"Wrote {args.report_path}")
    return 0 if report["summary"]["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
