"""Audit or run the frozen used-phone ranking benchmark v2."""

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
    parser.add_argument("--split", action="append", choices=("dev", "validation", "test"))
    parser.add_argument("--allow-sealed-test", action="store_true")
    parser.add_argument("--timeout-seconds", type=float, default=90.0)
    args = parser.parse_args()
    from agent.evaluation.used_phone_ranking_runner_v2 import PublicRunnerError, audit_public_inputs, run_public_agent
    if args.audit_only:
        if args.run_dir or args.split or args.allow_sealed_test:
            raise PublicRunnerError("audit-only does not accept run flags")
        print(json.dumps(audit_public_inputs(public_cases_path=args.public_cases, public_catalog_path=args.public_catalog), ensure_ascii=False, sort_keys=True))
        return 0
    if args.run_dir is None:
        raise PublicRunnerError("--run-dir is required")
    if args.allow_sealed_test and set(args.split or ()) != {"test"}:
        raise PublicRunnerError("sealed authority requires explicit test-only split")
    manifest = asyncio.run(run_public_agent(
        public_cases_path=args.public_cases, public_catalog_path=args.public_catalog,
        run_dir=args.run_dir, splits=args.split, allow_sealed_test=args.allow_sealed_test,
        timeout_seconds=args.timeout_seconds,
    ))
    print(json.dumps({key: manifest[key] for key in ("failedCaseIds", "modelCallCount", "businessWriteNetworkUsed")}, ensure_ascii=False, sort_keys=True))
    return 0 if not manifest["failedCaseIds"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
