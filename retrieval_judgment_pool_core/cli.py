"""Command-line adapter for the shared deterministic pool service."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Callable

from .core import PoolError, PoolService


def _emit(value: dict[str, Any]) -> None:
    print(json.dumps(value, ensure_ascii=False, sort_keys=True))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="retrieval-judgment-pool")
    parser.add_argument("--data-root", required=True, type=Path)
    parser.add_argument("--run-root", required=True, type=Path)
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("create")
    create.add_argument("--dataset-ref", required=True)
    submit = commands.add_parser("submit-retrieval-run")
    submit.add_argument("--run-id", required=True)
    submit.add_argument("--retriever-id", required=True)
    submit.add_argument("--submission-ref", required=True)
    build = commands.add_parser("build")
    build.add_argument("--run-id", required=True)
    build.add_argument("--inject-failure-after", choices=("RETRIEVE", "ANALYZE", "CE_SCORE", "SELECT", "PACKAGE", "VERIFY"))
    status = commands.add_parser("status")
    status.add_argument("--run-id", required=True)
    verify = commands.add_parser("verify")
    verify.add_argument("--run-id", required=True)
    export = commands.add_parser("export-blind")
    export.add_argument("--run-id", required=True)
    one_shot = commands.add_parser("run")
    one_shot.add_argument("--dataset-ref", required=True)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    service = PoolService(args.data_root, args.run_root)
    try:
        if args.command == "create":
            data = service.create_run(args.dataset_ref)
        elif args.command == "submit-retrieval-run":
            data = service.submit_retrieval_run(args.run_id, args.retriever_id, args.submission_ref)
        elif args.command == "build":
            data = service.build_pool(args.run_id, inject_failure_after=args.inject_failure_after)
        elif args.command == "status":
            data = service.get_run_status(args.run_id)
        elif args.command == "verify":
            data = service.verify_run(args.run_id)
        elif args.command == "export-blind":
            data = service.export_blind_packet(args.run_id)
        else:
            created = service.create_run(args.dataset_ref)
            built = service.build_pool(created["run_id"])
            data = {"created": created, "built": built, "blind": service.export_blind_packet(created["run_id"])}
        _emit({"ok": True, "data": data, "error": None})
        return 0
    except PoolError as exc:
        _emit({"ok": False, "data": None, "error": exc.as_dict()})
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
