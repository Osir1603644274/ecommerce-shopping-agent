from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys

import pytest

from agent.evaluation.used_phone_public_shadow_intake_v1 import (
    LABEL_BOUNDARY,
    ShadowIntakeError,
    canonical_bytes,
    freeze_bundle,
    materialize_bundle,
    sha256_bytes,
)


def _row(submission_id: str = "human-query-0001") -> dict:
    return {
        "schemaVersion": "used-phone-public-shadow-intake-v1",
        "submissionId": submission_id,
        "source": {
            "authorType": "self_declared_human",
            "relationship": "project_owner",
            "collectionMethod": "direct_entry",
            "capturedAt": "2026-08-14T06:00:00+08:00",
            "transformation": "verbatim",
        },
        "consent": {"localEvaluation": True, "modelProcessing": True},
        "privacy": {"reviewed": True, "containsDirectIdentifiers": False},
        "declarations": {
            "aiGenerated": False,
            "aiParaphrased": False,
            "benchmarkDerived": False,
            "expectedOutcomeIncluded": False,
            "labelsIncluded": False,
        },
        "turns": [{"turnId": "turn-1", "role": "user", "text": "我想挑一台二手手机，先说说该看什么"}],
    }


def _write_input(tmp_path: Path, rows: list[dict]) -> Path:
    path = tmp_path / "submissions.jsonl"
    path.write_bytes(b"".join(canonical_bytes(row) for row in rows))
    return path


def test_valid_unlabeled_rows_materialize_with_honest_boundary(tmp_path):
    source = _write_input(tmp_path, [_row()])
    cases, manifest = materialize_bundle(source)
    assert cases == [{
        "schemaVersion": "used-phone-public-shadow-case-v1",
        "caseId": "UPSH-H0001",
        "sourceSubmissionId": "human-query-0001",
        "sourceClass": "self_declared_human_original",
        "sourceRelationship": "project_owner",
        "transformation": "verbatim",
        "capturedAt": "2026-08-14T06:00:00+08:00",
        "labelBoundary": LABEL_BOUNDARY,
        "turns": _row()["turns"],
    }]
    assert manifest["containsLabels"] is False
    assert manifest["containsExpectedOutcomes"] is False
    assert manifest["independentlyVerifiedHumanIdentity"] is False
    assert manifest["input"]["sha256"] == sha256_bytes(source.read_bytes())


@pytest.mark.parametrize("field", [
    "aiGenerated", "aiParaphrased", "benchmarkDerived",
    "expectedOutcomeIncluded", "labelsIncluded",
])
def test_ai_benchmark_or_label_contamination_fails_closed(tmp_path, field):
    row = _row(); row["declarations"][field] = True
    with pytest.raises(ShadowIntakeError, match="schema error"):
        materialize_bundle(_write_input(tmp_path, [row]))


def test_direct_identifier_pattern_rejected_even_if_declaration_says_clean(tmp_path):
    row = _row(); row["turns"][0]["text"] = "手机号 13812345678，帮我挑二手手机"
    with pytest.raises(ShadowIntakeError, match="direct identifier"):
        materialize_bundle(_write_input(tmp_path, [row]))


def test_duplicate_content_and_noncontiguous_turns_rejected(tmp_path):
    duplicate = _row("human-query-0002")
    with pytest.raises(ShadowIntakeError, match="duplicates another turn sequence"):
        materialize_bundle(_write_input(tmp_path, [_row(), duplicate]))
    multi = _row(); multi["turns"].append({
        "turnId": "turn-3", "role": "user", "text": "第二轮条件有变化",
    })
    with pytest.raises(ShadowIntakeError, match="contiguous and ordered"):
        materialize_bundle(_write_input(tmp_path, [multi]))


def test_freeze_is_canonical_and_never_overwrites(tmp_path):
    source = _write_input(tmp_path, [_row()])
    output = tmp_path / "frozen"
    manifest = freeze_bundle(source, output)
    assert (output / "manifest.json").read_bytes() == canonical_bytes(manifest)
    case_payload = (output / "cases_public.jsonl").read_bytes()
    assert manifest["cases"]["sha256"] == sha256_bytes(case_payload)
    with pytest.raises(ShadowIntakeError, match="already exists"):
        freeze_bundle(source, output)


def test_extra_provenance_claim_is_rejected(tmp_path):
    row = deepcopy(_row())
    row["source"]["independentlyVerified"] = True
    with pytest.raises(ShadowIntakeError, match="schema error"):
        materialize_bundle(_write_input(tmp_path, [row]))


def test_cli_audit_then_freeze_and_refuse_overwrite(tmp_path):
    source = _write_input(tmp_path, [_row()])
    output = tmp_path / "frozen-cli"
    base = [
        sys.executable,
        "-m", "agent.scripts.freeze_used_phone_public_shadow_intake_v1",
        "--input", str(source),
    ]
    audit = subprocess.run(
        [*base, "--audit-only"],
        cwd=Path(__file__).resolve().parents[2],
        capture_output=True, text=True, encoding="utf-8", timeout=30,
    )
    assert audit.returncode == 0, audit.stderr
    audit_manifest = json.loads(audit.stdout)
    assert audit_manifest["caseCount"] == 1
    assert not output.exists()

    frozen = subprocess.run(
        [*base, "--output-dir", str(output)],
        cwd=Path(__file__).resolve().parents[2],
        capture_output=True, text=True, encoding="utf-8", timeout=30,
    )
    assert frozen.returncode == 0, frozen.stderr
    assert json.loads(frozen.stdout) == audit_manifest
    assert (output / "cases_public.jsonl").is_file()
    assert (output / "manifest.json").is_file()

    repeated = subprocess.run(
        [*base, "--output-dir", str(output)],
        cwd=Path(__file__).resolve().parents[2],
        capture_output=True, text=True, encoding="utf-8", timeout=30,
    )
    assert repeated.returncode != 0
    assert "already exists" in repeated.stderr
