import json

import pytest

from agent.evaluation.product_retrieval_nonsealed_bundle_v1 import (
    QUERY_IDS,
    SEALED_QUERY_IDS,
    materialize,
    validate_bundle,
)


def test_nonsealed_bundle_excludes_every_sealed_identity(tmp_path):
    materialize(tmp_path)
    validate_bundle(tmp_path)
    serialized = "\n".join(path.read_text(encoding="utf-8") for path in tmp_path.iterdir())
    assert all(query_id not in serialized for query_id in SEALED_QUERY_IDS)
    queries = [json.loads(line) for line in (tmp_path / "queries_public.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [row["queryId"] for row in queries] == list(QUERY_IDS)


def test_nonsealed_bundle_rejects_qrel_tampering(tmp_path):
    materialize(tmp_path)
    path = tmp_path / "qrels_evaluator.jsonl"
    path.write_text(path.read_text(encoding="utf-8") + '{"queryId":"uphq-008"}\n', encoding="utf-8")
    with pytest.raises(ValueError, match="sealed|binding|coverage"):
        validate_bundle(tmp_path)
