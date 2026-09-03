"""Two-stage CLI for the evaluation-only used-phone complex pilot."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Sequence


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    public = subparsers.add_parser("run-public", help="run a label-blind deterministic public floor")
    public.add_argument("--public-bundle", type=Path, required=True)
    public.add_argument("--floor", choices=("bm25_text_floor", "public_rule_floor"), required=True)
    public.add_argument("--predictions", type=Path, required=True)
    public.add_argument("--manifest", type=Path, required=True)

    hidden = subparsers.add_parser("score-hidden", help="score existing predictions against the pinned hidden freeze")
    hidden.add_argument("--public-bundle", type=Path, required=True)
    hidden.add_argument("--hidden-freeze", type=Path, required=True)
    hidden.add_argument("--predictions", type=Path, required=True)
    hidden.add_argument("--report", type=Path, required=True)
    hidden.add_argument("--manifest", type=Path, required=True)

    terminal = subparsers.add_parser(
        "score-terminal-run",
        help="score an 8-case directory of success/failure terminal sidecars",
    )
    terminal.add_argument("--public-bundle", type=Path, required=True)
    terminal.add_argument("--hidden-freeze", type=Path, required=True)
    terminal.add_argument("--terminal-run-dir", type=Path, required=True)
    terminal.add_argument("--attempt", type=int, required=True)
    terminal.add_argument("--report", type=Path, required=True)
    terminal.add_argument("--manifest", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "run-public":
        # Importing the hidden scorer is deliberately impossible on this branch:
        # only the public module is loaded for prediction generation.
        from agent.evaluation.used_phone_complex_pilot import run_public_floor

        run_public_floor(
            public_bundle_dir=args.public_bundle,
            floor_name=args.floor,
            predictions_path=args.predictions,
            manifest_path=args.manifest,
        )
        return 0

    # Hidden labels enter only in an explicit scoring subprocess.
    from agent.evaluation.used_phone_complex_metrics import score_hidden, score_terminal_run

    if args.command == "score-terminal-run":
        score_terminal_run(
            public_bundle_dir=args.public_bundle,
            hidden_freeze_dir=args.hidden_freeze,
            terminal_run_dir=args.terminal_run_dir,
            attempt=args.attempt,
            report_path=args.report,
            manifest_path=args.manifest,
        )
        return 0

    score_hidden(
        public_bundle_dir=args.public_bundle,
        hidden_freeze_dir=args.hidden_freeze,
        predictions_path=args.predictions,
        report_path=args.report,
        manifest_path=args.manifest,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
