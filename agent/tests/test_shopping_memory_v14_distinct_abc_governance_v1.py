import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_distinct_abc_governance_executes_real_different_paths(tmp_path):
    # The sealed attempt is evidence-only after production source evolves; do
    # not rerun its frozen-source runner against the current tree.
    report_path = (
        ROOT
        / "agent/evaluation/results/shopping_memory_v14_distinct_abc_governance_v1_20260830_attempt001/report.json"
    )
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["decision"] == "BOUNDED_DISTINCT_ABC_GOVERNANCE_ACCEPT"
    assert report["counts"]["bCDivergenceCases"] > 0
    assert report["metrics"]["B"]["falseInfluenceRate"] > 0
    assert report["metrics"]["C"]["falseInfluenceRate"] == 0
    assert report["explicitBoundaries"]["independentRelevanceQuality"] is False
