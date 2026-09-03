"""Audit or run the versioned Dev15 post-TaskState-fix identity."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--public-cases", type=Path, required=True)
    parser.add_argument("--public-catalog", type=Path, required=True)
    parser.add_argument("--audit-only", action="store_true")
    parser.add_argument("--run-dir", type=Path)
    parser.add_argument("--timeout-seconds", type=float, default=90.0)
    args = parser.parse_args()
    from agent.evaluation.used_phone_natural_guide_dev15_runner_v2 import (
        PublicRunnerError, audit_public_inputs, run_public_agent,
    )
    if args.audit_only:
        if args.run_dir is not None:
            raise PublicRunnerError("audit-only does not accept a run directory")
        print(json.dumps(audit_public_inputs(
            public_cases_path=args.public_cases,
            public_catalog_path=args.public_catalog,
            verify_java=True,
        ), ensure_ascii=False, sort_keys=True))
        return 0
    if args.run_dir is None:
        raise PublicRunnerError("--run-dir is required outside audit-only")
    manifest = asyncio.run(run_public_agent(
        public_cases_path=args.public_cases,
        public_catalog_path=args.public_catalog,
        run_dir=args.run_dir,
        timeout_seconds=args.timeout_seconds,
    ))
    print(json.dumps({
        "failedCaseIds": manifest["failedCaseIds"],
        "modelCallCount": manifest["modelCallCount"],
        "predictionSha256": manifest["prediction"]["sha256"],
        "succeededCaseIds": manifest["succeededCaseIds"],
    }, ensure_ascii=False, sort_keys=True))
    return 0 if not manifest["failedCaseIds"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
