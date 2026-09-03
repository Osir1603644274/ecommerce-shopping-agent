import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from evaluation.rag_q15_evaluation import build_q15_test_report  # noqa: E402


REPORT_PATH = (
    Path(__file__).resolve().parents[1]
    / "knowledge_data"
    / "eval"
    / "q15_sealed_test_report.json"
)


async def main() -> None:
    if REPORT_PATH.exists():
        raise SystemExit(
            f"Refusing to overwrite sealed test report: {REPORT_PATH}"
        )
    report = await build_q15_test_report()
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = REPORT_PATH.with_suffix(".json.tmp")
    temporary_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temporary_path.replace(REPORT_PATH)
    print(json.dumps({
        "caseCount": report["caseCount"],
        "retrieval": report["retrieval"],
        "contextSelection": report["contextSelection"],
        "generation": report["generation"],
        "timing": report["timing"],
    }, ensure_ascii=False, indent=2))
    print(f"Wrote {REPORT_PATH}")


if __name__ == "__main__":
    asyncio.run(main())
