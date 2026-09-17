"""Regression tests for the human-adjudicated Shopping Task State V2 MVP r2."""

from __future__ import annotations

import json

import pytest

from agent.evaluation import shopping_task_state_v2_mvp as frozen_v1
from agent.evaluation import shopping_task_state_v2_mvp_r2 as r2


def test_r2_applies_exactly_the_twelve_human_rewrites():
    review = r2.validate_human_adjudication()
    assert review["resultCount"] == 12
    assert {result["decision"] for result in review["results"]} == {"REWRITE"}


def test_r2_private_oracle_is_byte_identical_to_frozen_v1():
    r2.validate_private_oracle_identity()
    assert r2.PRIVATE_PATH.read_bytes() == frozen_v1.PRIVATE_PATH.read_bytes()


def test_r2_manifest_binds_base_artifacts_review_and_current_bytes():
    manifest = r2.validate_manifest()
    assert manifest["humanLanguageReviewStatus"] == "TARGETED_ADJUDICATION_APPLIED"
    assert manifest["humanReviewEvidence"]["decisionCount"] == 12
    assert manifest["strategyRunStatus"] == "NOT_RUN"
    assert manifest["status"] == "READY_FOR_HIGH_REVIEW"
    assert manifest["schemaVersion"] == "shopping-task-state-v2-mvp-manifest-v1"
    assert manifest["authoringMethod"] == "ai_assisted_source_with_human_targeted_colloquial_adjudication"
    assert manifest["baseDataset"]["sourceVersion"] == "shopping-task-state-v2-mvp-20260822"
    assert manifest["humanReviewEvidence"]["reviewType"] == "HUMAN_TARGETED_COLLOQUIAL_ADJUDICATION"
    assert all(set(item) == r2.EXPECTED_ARTIFACT_KEYS for item in manifest["artifacts"])
    assert all(set(item) == r2.EXPECTED_SCHEMA_KEYS for item in manifest["schemas"])


def test_r2_keeps_all_state_semantics_and_coverage():
    public_rows, private_rows = r2.load_and_validate()
    summary = r2.dataset_summary()
    assert len(public_rows) == len(private_rows) == 14
    assert summary["turnCount"] == 56
    assert summary["sessionCount"] == 18
    assert summary["humanRewriteCount"] == 12
    assert frozen_v1.REQUIRED_COVERAGE <= set(summary["coverageTags"])


def test_r2_rejects_an_unreviewed_public_change(monkeypatch, tmp_path):
    rows = [json.loads(line) for line in r2.PUBLIC_PATH.read_text(encoding="utf-8").splitlines()]
    rows[0]["turns"][0]["text"] += "随便看看。"
    changed = tmp_path / "scenarios.jsonl"
    changed.write_text(
        "\n".join(json.dumps(row, ensure_ascii=False, separators=(",", ":")) for row in rows) + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(r2, "PUBLIC_PATH", changed)
    with pytest.raises(ValueError, match="not exactly the human-adjudicated set"):
        r2.validate_human_adjudication()


def test_r2_rejects_private_oracle_drift(monkeypatch, tmp_path):
    changed = tmp_path / "state_oracle.private.jsonl"
    changed.write_bytes(r2.PRIVATE_PATH.read_bytes() + b"\n")
    monkeypatch.setattr(r2, "PRIVATE_PATH", changed)
    with pytest.raises(ValueError, match="differs from frozen v1"):
        r2.validate_private_oracle_identity()


def test_r2_rejects_external_temp_repin_of_review_bytes(monkeypatch, tmp_path):
    review = json.loads(r2.REVIEW_PATH.read_text(encoding="utf-8"))
    review["summary"]["decision"] = "HUMAN_GOLD"
    review["results"][0]["proposedRewrite"] += "同步改写"
    changed = tmp_path / "human-review.json"
    changed.write_text(json.dumps(review, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(r2, "REVIEW_PATH", changed)
    with pytest.raises(ValueError, match="frozen adjudication evidence"):
        r2.validate_human_adjudication()


@pytest.mark.parametrize("mutation", ["drop", "extra", "duplicate"])
def test_r2_manifest_rejects_non_exact_artifact_and_schema_closure(monkeypatch, tmp_path, mutation):
    manifest = json.loads(r2.MANIFEST_PATH.read_text(encoding="utf-8"))
    if mutation == "drop":
        manifest["artifacts"].pop()
        expected = "artifact path closure"
    elif mutation == "extra":
        manifest["schemas"].append({"path": "agent/evaluation/schemas/unlisted.json", "sha256": "0"})
        expected = r"schema\[5\] key closure"
    else:
        manifest["artifacts"].append(dict(manifest["artifacts"][0]))
        expected = "duplicate r2 artifact"
    changed = tmp_path / "manifest.json"
    changed.write_text(json.dumps(manifest), encoding="utf-8")
    monkeypatch.setattr(r2, "MANIFEST_PATH", changed)
    with pytest.raises(ValueError, match=expected):
        r2.validate_manifest()


@pytest.mark.parametrize(
    "section, mutation, expected",
    [
        ("baseDataset", "extra", "baseDataset key closure"),
        ("baseDataset", "drop", "baseDataset key closure"),
        ("humanReviewEvidence", "extra", "humanReviewEvidence key closure"),
        ("humanReviewEvidence", "drop", "humanReviewEvidence key closure"),
    ],
)
def test_r2_manifest_rejects_nested_top_level_metadata_key_mutations(monkeypatch, tmp_path, section, mutation, expected):
    manifest = json.loads(r2.MANIFEST_PATH.read_text(encoding="utf-8"))
    if mutation == "extra":
        manifest[section]["bogus"] = "attacker"
    else:
        manifest[section].pop(next(iter(manifest[section])))
    changed = tmp_path / f"{section}-{mutation}.json"
    changed.write_text(json.dumps(manifest), encoding="utf-8")
    monkeypatch.setattr(r2, "MANIFEST_PATH", changed)
    with pytest.raises(ValueError, match=expected):
        r2.validate_manifest()


@pytest.mark.parametrize("section", ["baseDataset", "humanReviewEvidence"])
def test_r2_manifest_rejects_nested_metadata_value_drift(monkeypatch, tmp_path, section):
    manifest = json.loads(r2.MANIFEST_PATH.read_text(encoding="utf-8"))
    key = "datasetId" if section == "baseDataset" else "decision"
    manifest[section][key] = "HUMAN_GOLD" if key == "decision" else "attacker-dataset"
    changed = tmp_path / f"{section}-value.json"
    changed.write_text(json.dumps(manifest), encoding="utf-8")
    monkeypatch.setattr(r2, "MANIFEST_PATH", changed)
    with pytest.raises(ValueError, match="metadata|authoring|decision|baseDataset"):
        r2.validate_manifest()


def test_r2_manifest_rejects_artifact_and_schema_nested_metadata_attacks(monkeypatch, tmp_path):
    original = json.loads(r2.MANIFEST_PATH.read_text(encoding="utf-8"))
    manifest = json.loads(json.dumps(original))
    manifest["artifacts"][0]["role"] = "bogus"
    manifest["schemas"][0]["name"] = "bogus"
    changed = tmp_path / "nested-role-name.json"
    changed.write_text(json.dumps(manifest), encoding="utf-8")
    monkeypatch.setattr(r2, "MANIFEST_PATH", changed)
    with pytest.raises(ValueError, match="artifact metadata binding"):
        r2.validate_manifest()

    manifest = json.loads(json.dumps(original))
    manifest["artifacts"][0]["bogus"] = True
    changed = tmp_path / "artifact-extra.json"
    changed.write_text(json.dumps(manifest), encoding="utf-8")
    monkeypatch.setattr(r2, "MANIFEST_PATH", changed)
    with pytest.raises(ValueError, match=r"artifact\[0\] key closure"):
        r2.validate_manifest()

    manifest = json.loads(json.dumps(original))
    manifest["schemas"][0]["bogus"] = True
    changed = tmp_path / "schema-extra.json"
    changed.write_text(json.dumps(manifest), encoding="utf-8")
    monkeypatch.setattr(r2, "MANIFEST_PATH", changed)
    with pytest.raises(ValueError, match=r"schema\[0\] key closure"):
        r2.validate_manifest()
