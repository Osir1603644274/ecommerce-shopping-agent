import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_real_path_governance_acceptance_is_bounded(tmp_path):
    path = (
        ROOT
        / "agent/evaluation/results/shopping_memory_v14_governance_acceptance_v1_20260830_attempt001/report.json"
    )
    report = json.loads(path.read_text(encoding="utf-8"))
    assert report["decision"] == "BOUNDED_REAL_PATH_GOVERNANCE_ACCEPT"
    assert report["gates"]["realConsentCommandProjection"] is True
    assert report["explicitBoundaries"]["independentRankingQuality"] is False
    assert report["explicitBoundaries"]["productionSwitchAuthority"] is False
