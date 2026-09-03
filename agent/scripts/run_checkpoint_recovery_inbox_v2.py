"""CLI entrypoint for the new-only Checkpoint Recovery Inbox V2 smoke."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent.evaluation.checkpoint_recovery_inbox_v2 import run_checkpoint_recovery_inbox_v2


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out-dir", type=Path, required=True)
    args = parser.parse_args()
    result = run_checkpoint_recovery_inbox_v2(args.out_dir)
    print(json.dumps(result, sort_keys=True))
    # GraphV2 recovery is intentionally unexercised here, so a healthy inbox
    # smoke is PASS while the enclosing experiment remains HOLD.
    return 0 if result["score"].get("toolInboxSmokeStatus") == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
