"""Run a 20-concurrent local evidence-search latency check against real dependencies."""

import argparse
import asyncio
import json
import math
import time

from app.domains.ecommerce.tools import search_products_tool


def percentile(values: list[float], quantile: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    index = max(0, math.ceil(quantile * len(ordered)) - 1)
    return ordered[index]


async def main_async() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--query", default="预算不超过5000元，至少12GB内存的手机")
    parser.add_argument("--category", default="手机", choices=["手机", "笔记本", "耳机"])
    parser.add_argument("--concurrency", type=int, default=20)
    parser.add_argument("--requests", type=int, default=20)
    parser.add_argument("--p95-threshold-seconds", type=float, default=2.0)
    parser.add_argument("--warmup", type=int, default=1)
    args = parser.parse_args()
    semaphore = asyncio.Semaphore(args.concurrency)

    async def one() -> tuple[float, bool, list]:
        async with semaphore:
            started = time.perf_counter()
            trace = await search_products_tool(args.query, args.category)
            elapsed = time.perf_counter() - started
            degraded = (
                trace.detail.get("retrievalTrace", {}).get("degraded", [])
                if isinstance(trace.detail, dict) else []
            )
            return elapsed, trace.ok, degraded

    warmups = [await one() for _ in range(args.warmup)]
    results = await asyncio.gather(*(one() for _ in range(args.requests)))
    latencies = [item[0] for item in results]
    p95 = percentile(latencies, 0.95)
    report = {
        "mode": "real_local_integration",
        "concurrency": args.concurrency,
        "requests": args.requests,
        "warmupRequests": args.warmup,
        "warmupSuccess": all(item[1] for item in warmups),
        "successCount": sum(item[1] for item in results),
        "p50Seconds": round(percentile(latencies, 0.50), 4),
        "p95Seconds": round(p95, 4),
        "thresholdSeconds": args.p95_threshold_seconds,
        "degradedRequestCount": sum(bool(item[2]) for item in results),
        "passed": (
            all(item[1] for item in warmups)
            and all(item[1] for item in results)
            and p95 <= args.p95_threshold_seconds
        ),
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["passed"] else 2


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main_async()))
