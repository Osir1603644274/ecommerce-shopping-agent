"""Audit or run the one-shot blind Validation10 two-stage ranking identity."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
import sys


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--public-cases", type=Path, required=True)
    parser.add_argument("--public-catalog", type=Path, required=True)
    parser.add_argument("--audit-only", action="store_true")
    parser.add_argument("--run-dir", type=Path)
    parser.add_argument("--timeout-seconds", type=float, default=90.0)
    args = parser.parse_args()
    from agent.evaluation.used_phone_two_stage_validation_runner_v1 import (
        PublicRunnerError,
        audit_public_inputs,
        run_public_agent,
    )
    try:
        if args.audit_only:
            if args.run_dir is not None:
                raise PublicRunnerError("audit-only does not accept --run-dir")
            result = audit_public_inputs(
                public_cases_path=args.public_cases,
                public_catalog_path=args.public_catalog,
            )
            print(json.dumps(result, ensure_ascii=False, sort_keys=True))
            return 0
        if args.run_dir is None:
            raise PublicRunnerError("--run-dir is required")
        manifest = asyncio.run(run_public_agent(
            public_cases_path=args.public_cases,
            public_catalog_path=args.public_catalog,
            run_dir=args.run_dir,
            splits=["validation"],
            timeout_seconds=args.timeout_seconds,
        ))
    except PublicRunnerError as exc:
        print(json.dumps({
            "outcome": "HOLD", "errorType": type(exc).__name__, "error": str(exc),
        }, ensure_ascii=False, sort_keys=True), file=sys.stderr)
        return 2
    print(json.dumps({
        key: manifest[key]
        for key in (
            "failedCaseIds", "modelCallCount", "modelNetworkCallCount",
            "businessWriteNetworkUsed", "hiddenArtifactsRead", "prediction",
        )
    }, ensure_ascii=False, sort_keys=True))
    return 0 if not manifest["failedCaseIds"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
