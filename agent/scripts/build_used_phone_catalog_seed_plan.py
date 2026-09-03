"""Build a pinned dry-run MySQL row plan for the frozen used-phone catalog."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Sequence


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--catalog",
        type=Path,
        required=True,
        help="Pinned external frozen v2 catalog.jsonl; revision/SHA/counts fail closed.",
    )
    parser.add_argument("--output-dir", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    agent_root = Path(__file__).resolve().parents[1]
    if str(agent_root) not in sys.path:
        sys.path.insert(0, str(agent_root))
    from evaluation.used_phone_catalog_seed_plan import (
        EXPECTED_CATALOG_SHA256,
        FROZEN_CATALOG_ARTIFACT,
        build_used_phone_catalog_seed_plan,
    )

    result = build_used_phone_catalog_seed_plan(
        catalog_path=args.catalog,
        output_dir=args.output_dir,
    )
    print(
        json.dumps(
            {
                "databaseConnected": False,
                "databaseWritten": False,
                "catalogArtifactRelativePath": FROZEN_CATALOG_ARTIFACT,
                "catalogSha256": EXPECTED_CATALOG_SHA256,
                "manifestFileSha256": result["manifestFileSha256"],
                "outputDir": str(args.output_dir.resolve()),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
