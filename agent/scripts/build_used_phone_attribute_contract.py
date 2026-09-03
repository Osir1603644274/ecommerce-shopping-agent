"""Build the pinned used-phone attribute contract from one evidence audit."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Sequence


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--expected-input-sha256", required=True)
    parser.add_argument("--dataset-revision", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    agent_root = Path(__file__).resolve().parents[1]
    if str(agent_root) not in sys.path:
        sys.path.insert(0, str(agent_root))
    from evaluation.used_phone_attribute_contract import (
        build_used_phone_attribute_contract,
    )

    result = build_used_phone_attribute_contract(
        input_path=args.input,
        output_dir=args.output_dir,
        expected_input_sha256=args.expected_input_sha256,
        dataset_revision=args.dataset_revision,
    )
    print(result["manifestFileSha256"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
