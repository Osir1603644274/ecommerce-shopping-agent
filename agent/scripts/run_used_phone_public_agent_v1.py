"""Audit or run the frozen public-only 100-case used-phone production Agent."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from typing import Sequence


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--public-cases", type=Path, required=True)
    parser.add_argument("--public-catalog", type=Path, required=True)
    parser.add_argument("--java-base-url", default="http://127.0.0.1:18081")
    parser.add_argument("--audit-only", action="store_true")
    parser.add_argument("--run-dir", type=Path)
    parser.add_argument("--split", action="append", choices=("dev", "validation", "test"))
    parser.add_argument("--case-id", action="append")
    parser.add_argument("--allow-sealed-test", action="store_true")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--timeout-seconds", type=float, default=90.0)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    from agent.evaluation.used_phone_public_agent_runner_v1 import (
        JAVA_BASE_URL,
        PublicRunnerError,
        audit_public_inputs,
        run_public_agent,
    )

    if args.java_base_url.rstrip("/") != JAVA_BASE_URL:
        raise PublicRunnerError("Java API endpoint pin mismatch")
    if args.audit_only:
        if args.run_dir is not None or args.resume or args.split or args.case_id or args.allow_sealed_test:
            raise PublicRunnerError("audit-only does not accept run selection/output flags")
        result = audit_public_inputs(
            public_cases_path=args.public_cases,
            public_catalog_path=args.public_catalog,
            verify_java=True,
        )
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0
    if args.run_dir is None:
        raise PublicRunnerError("--run-dir is required outside --audit-only")
    manifest = asyncio.run(run_public_agent(
        public_cases_path=args.public_cases,
        public_catalog_path=args.public_catalog,
        run_dir=args.run_dir,
        splits=args.split,
        case_ids=args.case_id,
        allow_sealed_test=args.allow_sealed_test,
        resume=args.resume,
        timeout_seconds=args.timeout_seconds,
    ))
    print(json.dumps({
        "businessNetworkUsed": manifest["businessNetworkUsed"],
        "businessWriteNetworkUsed": manifest["businessWriteNetworkUsed"],
        "failedCaseIds": manifest["failedCaseIds"],
        "modelCallCount": manifest["modelCallCount"],
        "predictionSha256": manifest["prediction"]["sha256"],
        "succeededCaseIds": manifest["succeededCaseIds"],
    }, ensure_ascii=False, sort_keys=True))
    return 0 if not manifest["failedCaseIds"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
