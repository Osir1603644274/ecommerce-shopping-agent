from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from agent.evaluation import kuaisearch_g2_phase_a_full_qrels_v1 as full


def _rows(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_build_full_records_preserves_review_depth_and_unknowns():
    records, counts = full.build_full_records()
    assert counts == {
        "rows": 508,
        "queries": 48,
        "pairedAdjudicatedRows": 124,
        "primaryOnlyRows": 384,
        "reviewerARepeatRows": 48,
        "unknownRows": 83,
    }
    assert len({(row["queryId"], row["blindCandidateId"]) for row in records}) == 508
    assert sum(row["resolutionSource"] == "R2_PAIRED_ADJUDICATION" for row in records) == 124
    assert sum(row["resolutionSource"] == "REVIEWER_A_PRIMARY_ONLY" for row in records) == 384
    assert all(row["relevance"] in {"0", "1", "2", "3", "U"} for row in records)


def test_materialized_bundle_replays_and_is_non_overwriting(tmp_path):
    output = tmp_path / "r3"
    full.materialize(output)
    full.validate_bundle(output)
    assert {path.name for path in output.iterdir()} == {
        "full_qrels.jsonl",
        "manifest.json",
        "receipt.json",
    }
    with pytest.raises(FileExistsError):
        full.materialize(output)


def test_validation_rejects_qrel_tampering(tmp_path):
    output = tmp_path / "r3"
    full.materialize(output)
    path = output / "full_qrels.jsonl"
    records = _rows(path)
    records[0]["relevance"] = "3" if records[0]["relevance"] != "3" else "0"
    path.write_text("".join(full.canonical(row) + "\n" for row in records), encoding="utf-8")
    with pytest.raises(ValueError, match="replay mismatch|artifact binding"):
        full.validate_bundle(output)


def test_repeat_inconsistency_fails_closed():
    original = full._xlsx_sheet_rows(full.REVIEWER_A_SNAPSHOT, 2)
    mutated = [dict(row) for row in original]
    groups = {}
    target = None
    for index, row in enumerate(mutated[1:], start=1):
        key = full.reviewer_key(row)
        if key in groups:
            target = index
            break
        groups[key] = index
    assert target is not None
    mutated[target]["G"] = "0" if full.normalize_grade(mutated[target]["G"]) != "0" else "3"
    with patch.object(full, "_xlsx_sheet_rows", return_value=mutated):
        with pytest.raises(ValueError, match="repeat inconsistency"):
            full.build_full_records()
