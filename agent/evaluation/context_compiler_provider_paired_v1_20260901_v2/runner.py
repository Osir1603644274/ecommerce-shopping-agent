"""V2 entry point correcting the first-turn reference oracle contract."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from agent.evaluation.context_compiler_provider_paired_v1_20260901_v1 import (
    runner as base,
)


PACKAGE_DIR = Path(__file__).resolve().parent
DEFAULT_MANIFEST = PACKAGE_DIR / "manifest.json"
DEFAULT_OUTPUT = PACKAGE_DIR / "attempt001"

base.SYSTEM_PROMPT = """You are a deterministic context-fidelity checker.
Call record_context_fidelity exactly once. Copy currentGoal, candidateIds and
evidenceRefs exactly from goal, candidateScopeState.rankedItemIds and
evidenceRefs. If referenceQuery is true, select only user history items whose
atTurn is a positive integer and copy the summary from the greatest atTurn;
if no such item exists return null. If referenceQuery is false return null.
Do not infer, translate, reorder, add or omit values."""


def deterministic_preflight(manifest_path: Path = DEFAULT_MANIFEST):
    return base.deterministic_preflight(manifest_path)


async def execute(manifest_path: Path, output_dir: Path, attempt_id: str) -> int:
    return await base.execute(manifest_path, output_dir, attempt_id)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument(
        "--attempt-id",
        default="context-compiler-provider-paired-v1-20260901-v2-attempt001",
    )
    parser.add_argument("--preflight", action="store_true")
    args = parser.parse_args()
    manifest = (
        args.manifest
        if args.manifest.is_absolute()
        else base.ROOT / args.manifest
    )
    output = (
        args.output_dir
        if args.output_dir.is_absolute()
        else base.ROOT / args.output_dir
    )
    if args.preflight:
        print(json.dumps(
            deterministic_preflight(manifest),
            ensure_ascii=False,
            indent=2,
        ))
        return 0
    return asyncio.run(execute(manifest, output, args.attempt_id))


if __name__ == "__main__":
    raise SystemExit(main())

