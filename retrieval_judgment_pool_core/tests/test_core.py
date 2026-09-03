from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from retrieval_judgment_pool_core import PoolError, PoolService
from retrieval_judgment_pool_core.core import read_json, read_jsonl


REPO_ROOT = Path(__file__).resolve().parents[2]
PUBLIC_FIXTURE = REPO_ROOT / "evaluation" / "retrieval-judgment-pool-v1-20260901" / "fixtures" / "public_tiny_v1"


def _workspace(tmp_path: Path) -> tuple[Path, Path]:
    data_root = tmp_path / "data"
    shutil.copytree(PUBLIC_FIXTURE, data_root / "public-tiny-v1")
    return data_root, tmp_path / "runs"


def _ready(tmp_path: Path) -> tuple[PoolService, str, Path]:
    data_root, run_root = _workspace(tmp_path)
    service = PoolService(data_root, run_root)
    created = service.create_run("public-tiny-v1")
    service.build_pool(created["run_id"])
    return service, created["run_id"], run_root / created["run_id"]


def test_public_fixture_builds_ready_unjudged_multiretriever_pool(tmp_path: Path) -> None:
    service, run_id, run_dir = _ready(tmp_path)
    verification = service.verify_run(run_id)
    assert verification["status"] == "READY"
    assert verification["query_count"] == 3
    assert verification["document_count"] == 14
    assert verification["candidate_pair_count"] == 30
    assert verification["all_candidates_unjudged"] is True
    assert verification["qrels_read"] is False
    assert verification["sealed_or_hidden_data_read"] is False
    analysis = read_json(run_dir / "analysis.json")
    assert analysis["retriever_ids"] == ["bm25f-v1", "char-fuzzy-v1", "semantic-hash-v1", "structured-v1"]
    assert sum(len(values) for query in analysis["queries"] for values in query["unique_by_retriever"].values()) > 0
    assert sum(len(query["disagreement"]) for query in analysis["queries"]) > 0
    pool = read_jsonl(run_dir / "candidate_pool.audit.jsonl")
    assert all(any("structured_conflict_hard_negative" in candidate["selection_reasons"] for candidate in row["candidates"]) for row in pool)


def test_blind_packet_hides_ranking_and_selection_provenance(tmp_path: Path) -> None:
    service, run_id, run_dir = _ready(tmp_path)
    blind = read_jsonl(run_dir / "blind_packet.jsonl")
    assert service.export_blind_packet(run_id)["label_status"] == "UNJUDGED"
    for row in blind:
        for candidate in row["candidates"]:
            assert set(candidate) == {"document_id", "entity_type", "display", "label"}
            assert candidate["label"] == "UNJUDGED"


def test_same_inputs_are_byte_deterministic_across_run_roots(tmp_path: Path) -> None:
    data_root, _ = _workspace(tmp_path)
    service_a = PoolService(data_root, tmp_path / "runs-a")
    service_b = PoolService(data_root, tmp_path / "runs-b")
    run_a = service_a.create_run("public-tiny-v1")["run_id"]
    run_b = service_b.create_run("public-tiny-v1")["run_id"]
    service_a.build_pool(run_a)
    service_b.build_pool(run_b)
    assert run_a == run_b
    root_a = tmp_path / "runs-a" / run_a
    root_b = tmp_path / "runs-b" / run_b
    for relative in (
        "manifest.json",
        "analysis.json",
        "reranker_scores.jsonl",
        "candidate_pool.audit.jsonl",
        "blind_packet.jsonl",
        "receipt.json",
        "SHA256SUMS.txt",
    ):
        assert (root_a / relative).read_bytes() == (root_b / relative).read_bytes()


def test_pool_budget_sensitivity_changes_identity_and_bounded_output(tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    shutil.copytree(PUBLIC_FIXTURE, data_root / "budget-10")
    shutil.copytree(PUBLIC_FIXTURE, data_root / "budget-8")
    config_path = data_root / "budget-8" / "config.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config["selection"]["final_pool_budget_per_query"] = 8
    config_path.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    service = PoolService(data_root, tmp_path / "runs")
    run_10 = service.create_run("budget-10")["run_id"]
    run_8 = service.create_run("budget-8")["run_id"]
    assert run_10 != run_8
    ready_10 = service.build_pool(run_10)["verification"]
    ready_8 = service.build_pool(run_8)["verification"]
    assert ready_10["candidate_pair_count"] == 30
    assert ready_8["candidate_pair_count"] == 24
    assert ready_10["all_candidates_unjudged"] is True
    assert ready_8["all_candidates_unjudged"] is True


def test_interrupted_build_resumes_only_hash_matching_stage(tmp_path: Path) -> None:
    data_root, run_root = _workspace(tmp_path)
    service = PoolService(data_root, run_root)
    run_id = service.create_run("public-tiny-v1")["run_id"]
    with pytest.raises(PoolError, match="injected") as exc:
        service.build_pool(run_id, inject_failure_after="RETRIEVE")
    assert exc.value.code == "INJECTED_FAILURE"
    status = service.get_run_status(run_id)
    assert "RETRIEVE" in status["completed_stages"]
    retrieve_bytes = (run_root / run_id / "runs" / "bm25f-v1.jsonl").read_bytes()
    assert service.build_pool(run_id)["status"] == "READY"
    assert (run_root / run_id / "runs" / "bm25f-v1.jsonl").read_bytes() == retrieve_bytes


@pytest.mark.parametrize("stage", ["NORMALIZE", "RETRIEVE", "ANALYZE", "CE_SCORE", "SELECT", "PACKAGE", "VERIFY"])
def test_every_state_stage_has_fail_closed_exact_hash_recovery(tmp_path: Path, stage: str) -> None:
    data_root, run_root = _workspace(tmp_path)
    service = PoolService(data_root, run_root)
    if stage == "NORMALIZE":
        with pytest.raises(PoolError) as failure:
            service.create_run("public-tiny-v1", inject_failure_after="NORMALIZE")
        assert failure.value.code == "INJECTED_FAILURE"
        run_id = service.create_run("public-tiny-v1")["run_id"]
    else:
        run_id = service.create_run("public-tiny-v1")["run_id"]
        with pytest.raises(PoolError) as failure:
            service.build_pool(run_id, inject_failure_after=stage)
        assert failure.value.code == "INJECTED_FAILURE"
        assert stage in service.get_run_status(run_id)["completed_stages"]
    assert service.build_pool(run_id)["status"] == "READY"
    assert service.verify_run(run_id)["status"] == "READY"


def test_data_drift_and_completed_artifact_tamper_fail_closed(tmp_path: Path) -> None:
    data_root, run_root = _workspace(tmp_path)
    service = PoolService(data_root, run_root)
    run_id = service.create_run("public-tiny-v1")["run_id"]
    documents = data_root / "public-tiny-v1" / "documents.jsonl"
    documents.write_text(documents.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    with pytest.raises(PoolError) as drift:
        service.build_pool(run_id)
    assert drift.value.code == "INPUT_DRIFT"

    service2, run_id2, run_dir2 = _ready(tmp_path / "tamper")
    (run_dir2 / "analysis.json").write_text("{}\n", encoding="utf-8")
    with pytest.raises(PoolError) as tamper:
        service2.verify_run(run_id2)
    assert tamper.value.code == "ARTIFACT_TAMPERED"


def test_label_fields_sensitive_refs_and_path_traversal_are_rejected(tmp_path: Path) -> None:
    data_root, run_root = _workspace(tmp_path)
    service = PoolService(data_root, run_root)
    with pytest.raises(PoolError) as traversal:
        service.create_run("../public-tiny-v1")
    assert traversal.value.code == "PATH_OUT_OF_SCOPE"
    with pytest.raises(PoolError) as qrels:
        service.create_run("qrels-public")
    assert qrels.value.code == "SENSITIVE_INPUT_FORBIDDEN"
    query_path = data_root / "public-tiny-v1" / "queries.jsonl"
    rows = [json.loads(line) for line in query_path.read_text(encoding="utf-8").splitlines() if line]
    rows[0]["relevance"] = 3
    query_path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")
    with pytest.raises(PoolError) as leaked:
        service.create_run("public-tiny-v1")
    assert leaked.value.code == "LABEL_INPUT_FORBIDDEN"


def test_repeat_calls_are_idempotent_and_concurrent_lock_is_fail_closed(tmp_path: Path) -> None:
    data_root, run_root = _workspace(tmp_path)
    service = PoolService(data_root, run_root)
    first = service.create_run("public-tiny-v1")
    second = service.create_run("public-tiny-v1")
    assert first["run_id"] == second["run_id"]
    assert second["idempotent_reuse"] is True
    lock = run_root / first["run_id"] / ".mutate.lock"
    lock.write_text("other-caller", encoding="utf-8")
    with pytest.raises(PoolError) as busy:
        service.build_pool(first["run_id"])
    assert busy.value.code == "RUN_BUSY"
    lock.unlink()
    assert service.build_pool(first["run_id"])["status"] == "READY"
    assert service.build_pool(first["run_id"])["idempotent_reuse"] is True


def test_external_retriever_requires_valid_complete_submission(tmp_path: Path) -> None:
    data_root, run_root = _workspace(tmp_path)
    config_path = data_root / "public-tiny-v1" / "config.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config["retrievers"].append({
        "retriever_id": "external-demo-v1",
        "kind": "external",
        "depth": 2,
        "model": "external-demo",
        "revision": "v1",
    })
    config_path.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    service = PoolService(data_root, run_root)
    run_id = service.create_run("public-tiny-v1")["run_id"]
    with pytest.raises(PoolError) as missing:
        service.build_pool(run_id)
    assert missing.value.code == "EXTERNAL_RUN_REQUIRED"
    queries = [json.loads(line) for line in (data_root / "public-tiny-v1" / "queries.jsonl").read_text(encoding="utf-8").splitlines() if line]
    submission = data_root / "public-tiny-v1" / "submissions" / "external-demo.jsonl"
    submission.parent.mkdir()
    submission.write_text("".join(json.dumps({"query_id": row["query_id"], "document_id": "product:p001", "rank": 1, "score": 1.0}) + "\n" for row in queries), encoding="utf-8")
    accepted = service.submit_retrieval_run(run_id, "external-demo-v1", "submissions/external-demo.jsonl")
    assert accepted["status"] == "ACCEPTED"
    assert service.build_pool(run_id)["status"] == "READY"
