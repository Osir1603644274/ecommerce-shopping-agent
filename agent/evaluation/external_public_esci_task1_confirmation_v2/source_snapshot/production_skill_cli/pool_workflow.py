"""Deterministic Skill adapter; all decisions execute in the shared core."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[4]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from retrieval_judgment_pool_core import PoolError, PoolService  # noqa: E402


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser()
    root.add_argument("--data-root", required=True, type=Path)
    root.add_argument("--run-root", required=True, type=Path)
    commands = root.add_subparsers(dest="command", required=True)
    create = commands.add_parser("create")
    create.add_argument("--dataset-ref", required=True)
    submit = commands.add_parser("submit-retrieval-run")
    submit.add_argument("--run-id", required=True)
    submit.add_argument("--retriever-id", required=True)
    submit.add_argument("--submission-ref", required=True)
    for name in ("build", "status", "verify", "export-blind"):
        command = commands.add_parser(name)
        command.add_argument("--run-id", required=True)
    return root


def main() -> int:
    args = parser().parse_args()
    service = PoolService(args.data_root, args.run_root)
    try:
        if args.command == "create":
            data = service.create_run(args.dataset_ref)
        elif args.command == "submit-retrieval-run":
            data = service.submit_retrieval_run(args.run_id, args.retriever_id, args.submission_ref)
        elif args.command == "build":
            data = service.build_pool(args.run_id)
        elif args.command == "status":
            data = service.get_run_status(args.run_id)
        elif args.command == "verify":
            data = service.verify_run(args.run_id)
        else:
            data = service.export_blind_packet(args.run_id)
        print(json.dumps({"ok": True, "data": data, "error": None}, ensure_ascii=False, sort_keys=True))
        return 0
    except PoolError as exc:
        print(json.dumps({"ok": False, "data": None, "error": exc.as_dict()}, ensure_ascii=False, sort_keys=True))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
