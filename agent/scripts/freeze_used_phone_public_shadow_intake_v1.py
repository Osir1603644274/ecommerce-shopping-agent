"""Audit or freeze self-declared human-original used-phone shadow queries."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--audit-only", action="store_true")
    args = parser.parse_args()
    from agent.evaluation.used_phone_public_shadow_intake_v1 import (
        freeze_bundle,
        materialize_bundle,
    )

    if args.audit_only:
        if args.output_dir is not None:
            raise ValueError("audit-only does not accept --output-dir")
        _cases, manifest = materialize_bundle(args.input)
    else:
        if args.output_dir is None:
            raise ValueError("--output-dir is required outside audit-only")
        manifest = freeze_bundle(args.input, args.output_dir)
    print(json.dumps(manifest, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
