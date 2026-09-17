import json
from pathlib import Path

import pytest

from agent.scripts.score_track_a_m0_frozen_release import (
    _verify_artifact,
    canonical_sha256,
    evaluate,
    query_metrics,
    sha256_path,
)


def test_query_metrics_follow_frozen_thresholds() -> None:
    metrics = query_metrics(["weak", "best", "other"], {"weak": 1, "best": 3, "other": 2})
    assert metrics["MRR"] == 1.0
    assert metrics["Recall@20"] == 1.0
    assert metrics["Hit@20"] == 1.0
    assert metrics["Top-1"] == 0.0 and metrics["Top-3"] == 1.0


def test_missing_query_is_scored_as_empty_run() -> None:
    overall, per_query = evaluate(
        ["q1", "q2"],
        [{"queryId": "q1", "docId": "d1", "grade": 3}, {"queryId": "q2", "docId": "d2", "grade": 3}],
        [{"queryId": "q1", "docId": "d1", "rank": 1}],
    )
    assert per_query["q2"]["NDCG@5"] == 0.0
    assert overall["NDCG@5"] == 0.5


def test_release_content_hash_rejects_manifest_edit() -> None:
    manifest = {"status": "frozen", "claimPolicy": "NO_STABLE_IMPROVEMENT_CLAIM"}
    manifest["releaseContentSha256"] = canonical_sha256(manifest)
    assert manifest["releaseContentSha256"] == canonical_sha256(manifest)
    manifest["status"] = "changed"
    assert manifest["releaseContentSha256"] != canonical_sha256(manifest)


def test_artifact_verification_fails_on_tamper(tmp_path: Path) -> None:
    artifact = tmp_path / "qrel.jsonl"
    artifact.write_text(json.dumps({"queryId": "q", "docId": "d", "grade": 3}) + "\n", encoding="utf-8")
    contract = {"path": "qrel.jsonl", "rows": 1, "sha256": sha256_path(artifact)}
    assert _verify_artifact(tmp_path, contract) == artifact
    artifact.write_text("tampered\n", encoding="utf-8")
    with pytest.raises(ValueError, match="hash mismatch"):
        _verify_artifact(tmp_path, contract)


def test_run_contract_rejects_non_contiguous_rank() -> None:
    with pytest.raises(ValueError, match="non-contiguous rank"):
        evaluate(["q"], [{"queryId": "q", "docId": "d", "grade": 3}],
                 [{"queryId": "q", "docId": "d", "rank": 2}])

