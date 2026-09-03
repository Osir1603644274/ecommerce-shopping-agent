"""Run the public-only used-phone model evaluation composition."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from typing import Sequence


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--public-bundle", type=Path, required=True)
    parser.add_argument("--evidence-audit", type=Path, required=True)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--case", action="append", dest="cases")
    parser.add_argument("--attempt", type=int, default=1)
    parser.add_argument("--resume-failed", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    from agent.evaluation.used_phone_model_harness import run_public_model_composition
    from agent.evaluation.used_phone_offline_runtime import sha256_file

    manifest = asyncio.run(run_public_model_composition(
        public_bundle_dir=args.public_bundle,
        evidence_audit_path=args.evidence_audit,
        run_dir=args.run_dir,
        case_ids=args.cases,
        attempt=args.attempt,
        resume_failed=args.resume_failed,
    ))
    print(json.dumps({
        "succeededCaseIds": manifest["succeededCaseIds"],
        "failedCaseIds": sorted(manifest["failedCases"]),
        "currentAttemptLogicalModelCallCount": manifest["currentAttemptLogicalModelCallCount"],
        "resumedLogicalModelCallCount": manifest["resumedLogicalModelCallCount"],
        "totalLogicalModelCallCount": manifest["totalLogicalModelCallCount"],
        "predictionSha256": manifest["prediction"]["sha256"],
        "manifestSha256": sha256_file(args.run_dir.resolve() / "manifest.json"),
    }, ensure_ascii=False, sort_keys=True))
    return 0 if not manifest["failedCases"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
