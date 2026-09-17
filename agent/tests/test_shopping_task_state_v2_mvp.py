"""Contract tests for the manually authored Shopping Task State V2 MVP."""

from __future__ import annotations

import inspect
import json
from collections import Counter

from jsonschema import Draft202012Validator

from agent.evaluation import shopping_task_state_v2_mvp as dataset
from agent.evaluation import shopping_task_state_v2_public as public_loader


def test_dataset_is_aligned_and_replay_valid():
    public_rows, private_rows = dataset.load_and_validate()
    assert len(public_rows) == len(private_rows) == 14
    assert sum(len(row["turns"]) for row in public_rows) == 56
    assert {row["scenarioId"] for row in public_rows} == {
        row["scenarioId"] for row in private_rows
    }


def test_public_loader_has_no_oracle_dependency():
    rows = public_loader.load_scenarios()
    source = inspect.getsource(public_loader).casefold()
    assert len(rows) == 14
    assert "state_oracle" not in source
    assert "oracle_private" not in source
    assert "private_path" not in source


def test_schemas_are_draft_202012_meta_valid():
    for path in (dataset.PUBLIC_SCHEMA_PATH, dataset.PRIVATE_SCHEMA_PATH):
        schema = json.loads(path.read_text(encoding="utf-8"))
        Draft202012Validator.check_schema(schema)


def test_manifest_binds_public_private_and_schema_bytes():
    manifest = dataset.validate_manifest()
    assert manifest["humanLanguageReviewStatus"] == "PENDING"
    assert manifest["productOracleStatus"] == "NOT_BOUND"
    assert manifest["strategyRunStatus"] == "NOT_RUN"


def test_manual_language_surface_is_unique_and_not_schema_register():
    rows = public_loader.load_scenarios()
    texts = [turn["text"] for row in rows for turn in row["turns"]]
    assert len(texts) == len(set(texts)) == 56
    forbidden = (
        "status=", "priority=", "memoryscope", "routeclass",
        "scenarioid", "requirementid", "activerequirementids",
    )
    assert not any(token in text.casefold() for token in forbidden for text in texts)
    assert all(any("\u4e00" <= char <= "\u9fff" for char in text) for text in texts)


def test_route_and_memory_operation_matrix_is_present():
    _, private_rows = dataset.load_and_validate()
    routes = Counter(
        turn["routeClass"]
        for row in private_rows
        for turn in row["turnAnnotations"]
    )
    memory_ops = Counter(
        event["op"]
        for row in private_rows
        for turn in row["turnAnnotations"]
        for event in turn["memoryEvents"]
    )
    assert set(routes) == {"CLARIFY", "FAST", "PAE", "BOUNDED_REACT", "ANSWER_ONLY"}
    assert {"write", "read", "update", "revoke", "suppress", "do_not_write"} <= set(memory_ops)


def test_required_semantic_and_long_term_memory_coverage_is_complete():
    summary = dataset.dataset_summary()
    assert summary["scenarioCount"] == 14
    assert summary["turnCount"] == 56
    assert summary["sessionCount"] == 18
    assert dataset.REQUIRED_COVERAGE <= set(summary["coverageTags"])
