import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from evaluation.rag_context_selection_evaluation import (  # noqa: E402
    build_context_selection_validation_report,
)


REPORT_PATH = (
    Path(__file__).resolve().parents[1]
    / "knowledge_data"
    / "eval"
    / "context_selection_validation_report.json"
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--report-path", type=Path, default=REPORT_PATH)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    report = build_context_selection_validation_report()
    args.report_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = args.report_path.with_suffix(".json.tmp")
    temporary_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temporary_path.replace(args.report_path)
    print(json.dumps(report["metrics"], ensure_ascii=False, indent=2))
    print(f"Wrote {args.report_path}")


if __name__ == "__main__":
    main()
