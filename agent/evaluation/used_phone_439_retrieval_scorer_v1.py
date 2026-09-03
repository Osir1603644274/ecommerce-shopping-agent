"""Strict intake and executable macro-metric gates for the 439 retrieval study."""

from __future__ import annotations

import argparse
import copy
import json
import math
import random
import shutil
from pathlib import Path
from typing import Any, Iterable, Mapping

from agent.evaluation import used_phone_439_retrieval_grid_v1 as grid
from agent.evaluation import used_phone_439_retrieval_review_package_v3 as package


ALLOWED_EVIDENCE = {
    "title_claim", "attribute_fact", "brand_fact", "synthetic_price", "constraint_conflict"
}
BASELINE = ("standard", "es_1_dense_0")


def validate_review_submission(pack_rows: list[dict[str, Any]], review_rows: list[dict[str, Any]]) -> None:
    pack = {row["blindQueryId"]: row for row in pack_rows}
    reviews = {row.get("blindQueryId"): row for row in review_rows}
    if len(pack) != len(pack_rows) or len(reviews) != len(review_rows) or set(pack) != set(reviews):
        raise ValueError("review blindQueryId set is not exact")
    for blind_id, source in pack.items():
        row = reviews[blind_id]
        if set(row) != {"blindQueryId", "reviews"} or not isinstance(row["reviews"], list):
            raise ValueError("review row schema mismatch")
        expected = {candidate["candidateToken"] for candidate in source["candidates"]}
        actual = [review.get("candidateToken") for review in row["reviews"]]
        if len(actual) != len(set(actual)) or set(actual) != expected:
            raise ValueError("candidate reviews are not an exact token set")
        for review in row["reviews"]:
            if set(review) != {"candidateToken", "relevance", "reason", "evidenceBasis"}:
                raise ValueError("candidate review schema mismatch")
            relevance = review["relevance"]
            if not (relevance == "unknown" or type(relevance) is int and relevance in {0, 1, 2, 3}):
                raise ValueError("invalid relevance")
            if not isinstance(review["reason"], str) or not review["reason"].strip():
                raise ValueError("review reason missing")
            basis = review["evidenceBasis"]
            if not isinstance(basis, list) or not basis or not all(item in ALLOWED_EVIDENCE for item in basis):
                raise ValueError("review evidenceBasis invalid")
            if len(basis) != len(set(basis)):
                raise ValueError("review evidenceBasis duplicated")


def _validate_access_attestation(
    path: Path, reviewer: str, input_dir: Path, *, role: str = "reviewer"
) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    expected_inputs = ["contract.json", "blind_pack.jsonl", "receipt.json"]
    if value != {
        "schemaVersion": "used-phone-439-codex-review-access-attestation-v1",
        "reviewer": reviewer,
        "role": role,
        "authority": "CODEX_ASSISTED_NOT_HUMAN_GOLD",
        "selfAttested": True,
        "taskIdentity": f"attempt012-{reviewer}",
        "inputsRead": expected_inputs,
        "blindPackSha256": grid.sha256(input_dir / "blind_pack.jsonl"),
        "contractSha256": grid.sha256(input_dir / "contract.json"),
        "distributionReceiptSha256": grid.sha256(input_dir / "receipt.json"),
        "mappingSeen": False,
        "otherReviewerSeen": False,
        "repositoryContextSeen": False,
    }:
        raise ValueError("reviewer access self-attestation mismatch")
    return value


def intake_review(input_dir: Path, submission_dir: Path, output_dir: Path, reviewer: str) -> Path:
    if reviewer not in package.REVIEWERS:
        raise ValueError("unknown reviewer")
    pack_path = input_dir / "blind_pack.jsonl"
    receipt = json.loads((input_dir / "receipt.json").read_text(encoding="utf-8"))
    if receipt["reviewer"] != reviewer or receipt["artifacts"]["blind_pack.jsonl"]["sha256"] != grid.sha256(pack_path):
        raise ValueError("reviewer input receipt mismatch")
    submission = submission_dir / "reviewed.jsonl"
    submitted_attestation = submission_dir / "access_attestation.json"
    _validate_access_attestation(submitted_attestation, reviewer, input_dir)
    pack_rows = grid.read_jsonl(pack_path)
    review_rows = grid.read_jsonl(submission)
    validate_review_submission(pack_rows, review_rows)
    if output_dir.exists():
        raise FileExistsError("refusing to overwrite review intake")
    output_dir.mkdir(parents=True)
    normalized = output_dir / "reviewed.jsonl"
    shutil.copy2(submission, normalized)
    attestation = output_dir / "access_attestation.json"
    shutil.copy2(submitted_attestation, attestation)
    result_receipt = {
        "schemaVersion": "used-phone-439-codex-review-intake-receipt-v1",
        "reviewer": reviewer,
        "inputPackSha256": grid.sha256(pack_path),
        "submissionSha256": grid.sha256(submission),
        "submittedAttestationSha256": grid.sha256(submitted_attestation),
        "artifacts": {
            "reviewed.jsonl": {"rows": len(review_rows), "sha256": grid.sha256(normalized), "bytes": normalized.stat().st_size},
            "access_attestation.json": {"sha256": grid.sha256(attestation), "bytes": attestation.stat().st_size},
        },
    }
    (output_dir / "receipt.json").write_text(grid.canonical(result_receipt) + "\n", encoding="utf-8")
    return output_dir


def _load_frozen_review(intake_dir: Path, input_dir: Path, reviewer: str) -> list[dict[str, Any]]:
    receipt = json.loads((intake_dir / "receipt.json").read_text(encoding="utf-8"))
    reviewed = intake_dir / "reviewed.jsonl"
    attestation = intake_dir / "access_attestation.json"
    expected_receipt = {
        "schemaVersion": "used-phone-439-codex-review-intake-receipt-v1",
        "reviewer": reviewer,
        "inputPackSha256": grid.sha256(input_dir / "blind_pack.jsonl"),
        "submissionSha256": grid.sha256(reviewed),
        "submittedAttestationSha256": grid.sha256(attestation),
        "artifacts": {
            "reviewed.jsonl": {"rows": 24, "sha256": grid.sha256(reviewed), "bytes": reviewed.stat().st_size},
            "access_attestation.json": {"sha256": grid.sha256(attestation), "bytes": attestation.stat().st_size},
        },
    }
    if receipt != expected_receipt or {path.name for path in intake_dir.iterdir()} != {
        "reviewed.jsonl", "access_attestation.json", "receipt.json"
    }:
        raise ValueError("review intake receipt artifact mismatch")
    _validate_access_attestation(attestation, reviewer, input_dir)
    rows = grid.read_jsonl(reviewed)
    validate_review_submission(grid.read_jsonl(input_dir / "blind_pack.jsonl"), rows)
    return rows


def _real_reviews(
    run_dir: Path, intake_root: Path
) -> tuple[dict[str, dict[tuple[str, int], dict[str, Any]]], dict[tuple[str, int], dict[str, Any]]]:
    mapping = json.loads((run_dir / "private_evaluator/sealed_mapping_v3.json").read_text(encoding="utf-8"))
    all_reviews: dict[str, dict[tuple[str, int], dict[str, Any]]] = {}
    source_candidates: dict[tuple[str, int], dict[str, Any]] = {}
    evaluator = grid.read_jsonl(run_dir / "private_evaluator/evaluator_pool_v3.jsonl")
    for row in evaluator:
        for candidate in row["candidates"]:
            source_candidates[(row["queryId"], int(candidate["productId"]))] = candidate
    for reviewer in package.REVIEWERS:
        input_dir = run_dir / "distribution" / reviewer
        rows = _load_frozen_review(intake_root / reviewer, input_dir, reviewer)
        by_blind = {row["blindQueryId"]: row for row in rows}
        real: dict[tuple[str, int], dict[str, Any]] = {}
        for blind_query_id, item in mapping["reviewers"][reviewer].items():
            review_by_token = {
                row["candidateToken"]: row for row in by_blind[blind_query_id]["reviews"]
            }
            for token, product_id in item["candidates"].items():
                real[(item["queryId"], int(product_id))] = review_by_token[token]
        if set(real) != set(source_candidates):
            raise ValueError("unblinded review set mismatch")
        all_reviews[reviewer] = real
    return all_reviews, source_candidates


def _make_adjudication_material(
    run_dir: Path,
    reviews: Mapping[str, Mapping[tuple[str, int], Mapping[str, Any]]],
    candidates: Mapping[tuple[str, int], Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, Any], int]:
    evaluator = {
        row["queryId"]: row
        for row in grid.read_jsonl(run_dir / "private_evaluator/evaluator_pool_v3.jsonl")
    }
    intents = {
        row["queryId"]: row
        for row in grid.read_jsonl(run_dir / "private_evaluator/frozen_taskstate_intents_v3.jsonl")
    }
    disputes: dict[str, list[tuple[int, Mapping[str, Any], Mapping[str, Any]]]] = {}
    for key in sorted(candidates):
        first = reviews["reviewer01"][key]
        second = reviews["reviewer02"][key]
        if first["relevance"] != second["relevance"] or first["relevance"] == "unknown":
            disputes.setdefault(key[0], []).append((key[1], first, second))
    public_rows = []
    private_mapping: dict[str, Any] = {
        "schemaVersion": "used-phone-439-adjudication-mapping-v1",
        "visibility": "PRIVATE_EVALUATOR_ONLY",
        "queries": {},
    }
    for index, query_id in enumerate(sorted(disputes), 1):
        blind_query = f"AQ-{index:02d}-{package.v2._token('attempt012-adjudication', query_id)[:6]}"
        public_candidates = []
        token_map = {}
        for product_id, first, second in disputes[query_id]:
            token = f"AC-{package.v2._token('attempt012-adjudication', query_id, product_id)}"
            token_map[token] = product_id
            source = candidates[(query_id, product_id)]
            public_candidates.append({
                "candidateToken": token,
                "title": source["title"],
                "brand": source["brand"],
                "syntheticReferencePriceMinor": source["syntheticReferencePriceMinor"],
                "priceDisclosureZh": source["priceDisclosureZh"],
                "controlledAttributes": source["controlledAttributes"],
                "rawAttributeText": source["attributeText"],
                "priorReviews": [
                    {key: first[key] for key in ("relevance", "reason", "evidenceBasis")},
                    {key: second[key] for key in ("relevance", "reason", "evidenceBasis")},
                ],
            })
        intent = intents[query_id]
        public_rows.append({
            "schemaVersion": "used-phone-439-adjudication-blind-item-v1",
            "blindQueryId": blind_query,
            "query": evaluator[query_id]["query"],
            "intent": {
                "supportedHardRequirements": intent["supportedHardRequirements"],
                "explicitSoftPreferences": intent["explicitSoftPreferences"],
                "unsupportedStructuredEvidence": intent["unsupportedStructuredEvidence"],
                "policies": intent["policies"],
            },
            "candidates": public_candidates,
        })
        private_mapping["queries"][blind_query] = {"queryId": query_id, "candidates": token_map}
    return public_rows, private_mapping, sum(len(rows) for rows in disputes.values())


def _adjudication_contract() -> dict[str, Any]:
    return {
        "schemaVersion": "used-phone-439-adjudication-contract-v1",
        "authority": "CODEX_ASSISTED_NOT_HUMAN_GOLD",
        "output": "exact blind query/candidate coverage; numeric relevance 0..3 only; non-empty reason/evidenceBasis",
        "mappingForbidden": True,
        "rankingAndMetricsForbidden": True,
        "evidencePriority": package.review_contract()["adjudication"]["evidencePriority"],
        "accessAttestation": {
            "schemaVersion": "used-phone-439-codex-review-access-attestation-v1",
            "reviewer": "adjudicator",
            "role": "adjudicator",
            "taskIdentity": "attempt012-adjudicator",
            "mustBindHashesOf": ["blind_pack.jsonl", "contract.json", "receipt.json"],
            "mappingSeen": False,
            "otherReviewerSeen": False,
            "repositoryContextSeen": False,
        },
    }


def build_adjudication_package(run_dir: Path, intake_root: Path, output_root: Path) -> Path:
    package.validate(run_dir)
    if output_root.exists():
        raise FileExistsError("refusing to overwrite adjudication package")
    reviews, candidates = _real_reviews(run_dir, intake_root)
    public_rows, private_mapping, dispute_count = _make_adjudication_material(
        run_dir, reviews, candidates
    )
    distribution = output_root / "distribution"
    private = output_root / "private"
    distribution.mkdir(parents=True)
    private.mkdir()
    contract = _adjudication_contract()
    grid.write_jsonl(distribution / "blind_pack.jsonl", public_rows)
    (distribution / "contract.json").write_text(grid.canonical(contract) + "\n", encoding="utf-8")
    distribution_receipt = {
        "schemaVersion": "used-phone-439-adjudication-distribution-receipt-v1",
        "reviewer": "adjudicator",
        "artifacts": {
            "blind_pack.jsonl": {"rows": len(public_rows), "sha256": grid.sha256(distribution / "blind_pack.jsonl"), "bytes": (distribution / "blind_pack.jsonl").stat().st_size},
            "contract.json": {"sha256": grid.sha256(distribution / "contract.json"), "bytes": (distribution / "contract.json").stat().st_size},
        },
    }
    (distribution / "receipt.json").write_text(grid.canonical(distribution_receipt) + "\n", encoding="utf-8")
    private_mapping["packHash"] = grid.sha256(distribution / "blind_pack.jsonl")
    (private / "mapping.json").write_text(grid.canonical(private_mapping) + "\n", encoding="utf-8")
    chain = {
        reviewer: grid.sha256(intake_root / reviewer / "receipt.json")
        for reviewer in package.REVIEWERS
    }
    (private / "receipt.json").write_text(grid.canonical({
        "schemaVersion": "used-phone-439-adjudication-private-receipt-v1",
        "reviewIntakeReceiptHashes": chain,
        "mappingSha256": grid.sha256(private / "mapping.json"),
        "distributionReceiptSha256": grid.sha256(distribution / "receipt.json"),
        "disputeCount": dispute_count,
    }) + "\n", encoding="utf-8")
    return output_root


def validate_adjudication_submission(pack_rows: list[dict[str, Any]], review_rows: list[dict[str, Any]]) -> None:
    validate_review_submission(pack_rows, review_rows)
    if any(
        review["relevance"] == "unknown"
        for row in review_rows for review in row["reviews"]
    ):
        raise ValueError("adjudication must resolve every item to a numeric grade")


def build_final_qrels(
    run_dir: Path,
    intake_root: Path,
    adjudication_root: Path,
    adjudication_submission_dir: Path,
    output_dir: Path,
) -> Path:
    package.validate(run_dir)
    if output_dir.exists():
        raise FileExistsError("refusing to overwrite final qrel bundle")
    reviews, candidates = _real_reviews(run_dir, intake_root)
    adjudication_pack = grid.read_jsonl(adjudication_root / "distribution/blind_pack.jsonl")
    adjudication_rows = grid.read_jsonl(adjudication_submission_dir / "reviewed.jsonl")
    _validate_access_attestation(
        adjudication_submission_dir / "access_attestation.json",
        "adjudicator",
        adjudication_root / "distribution",
        role="adjudicator",
    )
    validate_adjudication_submission(adjudication_pack, adjudication_rows)
    mapping_path = adjudication_root / "private/mapping.json"
    adjudication_mapping = json.loads(mapping_path.read_text(encoding="utf-8"))
    if adjudication_mapping["packHash"] != grid.sha256(adjudication_root / "distribution/blind_pack.jsonl"):
        raise ValueError("adjudication mapping/pack mismatch")
    private_receipt = json.loads((adjudication_root / "private/receipt.json").read_text(encoding="utf-8"))
    expected_review_hashes = {
        reviewer: grid.sha256(intake_root / reviewer / "receipt.json")
        for reviewer in package.REVIEWERS
    }
    if (
        private_receipt["reviewIntakeReceiptHashes"] != expected_review_hashes
        or private_receipt["mappingSha256"] != grid.sha256(mapping_path)
        or private_receipt["distributionReceiptSha256"] != grid.sha256(adjudication_root / "distribution/receipt.json")
    ):
        raise ValueError("adjudication private receipt chain mismatch")
    adjudicated: dict[tuple[str, int], dict[str, Any]] = {}
    rows_by_blind = {row["blindQueryId"]: row for row in adjudication_rows}
    for blind_query, item in adjudication_mapping["queries"].items():
        review_by_token = {row["candidateToken"]: row for row in rows_by_blind[blind_query]["reviews"]}
        for token, product_id in item["candidates"].items():
            adjudicated[(item["queryId"], int(product_id))] = review_by_token[token]
    final = []
    for key in sorted(candidates):
        first, second = reviews["reviewer01"][key], reviews["reviewer02"][key]
        if first["relevance"] == second["relevance"] and first["relevance"] != "unknown":
            grade, provenance = int(first["relevance"]), "exact_two_reviewer_agreement"
        else:
            if key not in adjudicated:
                raise ValueError("disagreement or unknown lacks adjudication")
            grade, provenance = int(adjudicated[key]["relevance"]), "blind_third_adjudication"
        final.append({"queryId": key[0], "productId": key[1], "grade": grade, "provenance": provenance})
    output_dir.mkdir(parents=True)
    qrel_path = output_dir / "final_qrels.jsonl"
    grid.write_jsonl(qrel_path, final)
    frozen_adjudication = output_dir / "adjudication_reviewed.jsonl"
    frozen_adjudication_attestation = output_dir / "adjudication_access_attestation.json"
    shutil.copy2(adjudication_submission_dir / "reviewed.jsonl", frozen_adjudication)
    shutil.copy2(
        adjudication_submission_dir / "access_attestation.json",
        frozen_adjudication_attestation,
    )
    review_receipts = {
        reviewer: grid.sha256(intake_root / reviewer / "receipt.json")
        for reviewer in package.REVIEWERS
    }
    receipt = {
        "schemaVersion": "used-phone-439-final-qrel-receipt-v1",
        "authority": "CODEX_ASSISTED_QREL_NOT_FORMAL_HUMAN_GOLD",
        "packageReceiptSha256": grid.sha256(run_dir / "receipt_v3.json"),
        "reviewIntakeReceiptHashes": review_receipts,
        "adjudicationPrivateReceiptSha256": grid.sha256(adjudication_root / "private/receipt.json"),
        "adjudicationOutputSha256": grid.sha256(frozen_adjudication),
        "adjudicationAttestationSha256": grid.sha256(frozen_adjudication_attestation),
        "finalQrels": {"rows": len(final), "sha256": grid.sha256(qrel_path), "bytes": qrel_path.stat().st_size},
        "sources": {
            Path(__file__).resolve().relative_to(grid.ROOT).as_posix(): grid.sha256(Path(__file__).resolve()),
            Path(package.__file__).resolve().relative_to(grid.ROOT).as_posix(): grid.sha256(Path(package.__file__).resolve()),
        },
    }
    (output_dir / "receipt.json").write_text(grid.canonical(receipt) + "\n", encoding="utf-8")
    return output_dir


def validate_final_qrel_bundle(run_dir: Path, intake_root: Path, adjudication_root: Path, bundle: Path) -> list[dict[str, Any]]:
    package.validate(run_dir)
    reviews, candidates = _real_reviews(run_dir, intake_root)
    expected_pack, expected_mapping, expected_disputes = _make_adjudication_material(
        run_dir, reviews, candidates
    )
    actual_pack = grid.read_jsonl(adjudication_root / "distribution/blind_pack.jsonl")
    if actual_pack != expected_pack:
        raise ValueError("adjudication blind pack does not replay frozen disputes")
    actual_contract = json.loads(
        (adjudication_root / "distribution/contract.json").read_text(encoding="utf-8")
    )
    if actual_contract != _adjudication_contract():
        raise ValueError("adjudication contract does not replay frozen source")
    mapping_path = adjudication_root / "private/mapping.json"
    actual_mapping = json.loads(mapping_path.read_text(encoding="utf-8"))
    expected_mapping["packHash"] = grid.sha256(adjudication_root / "distribution/blind_pack.jsonl")
    if actual_mapping != expected_mapping:
        raise ValueError("adjudication mapping does not replay frozen disputes")
    distribution_receipt_path = adjudication_root / "distribution/receipt.json"
    distribution_receipt = json.loads(distribution_receipt_path.read_text(encoding="utf-8"))
    expected_distribution_receipt = {
        "schemaVersion": "used-phone-439-adjudication-distribution-receipt-v1",
        "reviewer": "adjudicator",
        "artifacts": {
            "blind_pack.jsonl": {
                "rows": len(actual_pack),
                "sha256": grid.sha256(adjudication_root / "distribution/blind_pack.jsonl"),
                "bytes": (adjudication_root / "distribution/blind_pack.jsonl").stat().st_size,
            },
            "contract.json": {
                "sha256": grid.sha256(adjudication_root / "distribution/contract.json"),
                "bytes": (adjudication_root / "distribution/contract.json").stat().st_size,
            },
        },
    }
    if distribution_receipt != expected_distribution_receipt:
        raise ValueError("adjudication distribution receipt mismatch")
    private_receipt_path = adjudication_root / "private/receipt.json"
    private_receipt = json.loads(private_receipt_path.read_text(encoding="utf-8"))
    expected_private_receipt = {
        "schemaVersion": "used-phone-439-adjudication-private-receipt-v1",
        "reviewIntakeReceiptHashes": {
            reviewer: grid.sha256(intake_root / reviewer / "receipt.json")
            for reviewer in package.REVIEWERS
        },
        "mappingSha256": grid.sha256(mapping_path),
        "distributionReceiptSha256": grid.sha256(distribution_receipt_path),
        "disputeCount": expected_disputes,
    }
    if private_receipt != expected_private_receipt:
        raise ValueError("adjudication private receipt does not replay")
    receipt = json.loads((bundle / "receipt.json").read_text(encoding="utf-8"))
    expected_source_paths = {
        Path(__file__).resolve().relative_to(grid.ROOT).as_posix(),
        Path(package.__file__).resolve().relative_to(grid.ROOT).as_posix(),
    }
    if set(receipt) != {
        "schemaVersion", "authority", "packageReceiptSha256", "reviewIntakeReceiptHashes",
        "adjudicationPrivateReceiptSha256", "adjudicationOutputSha256",
        "adjudicationAttestationSha256", "finalQrels", "sources"
    } or receipt["schemaVersion"] != "used-phone-439-final-qrel-receipt-v1" or set(receipt["sources"]) != expected_source_paths:
        raise ValueError("final qrel receipt schema mismatch")
    if {path.name for path in bundle.iterdir()} != {
        "final_qrels.jsonl", "adjudication_reviewed.jsonl",
        "adjudication_access_attestation.json", "receipt.json"
    }:
        raise ValueError("final qrel bundle disk manifest mismatch")
    if receipt["packageReceiptSha256"] != grid.sha256(run_dir / "receipt_v3.json"):
        raise ValueError("final qrel package receipt mismatch")
    for reviewer in package.REVIEWERS:
        if receipt["reviewIntakeReceiptHashes"][reviewer] != grid.sha256(intake_root / reviewer / "receipt.json"):
            raise ValueError("final qrel review chain mismatch")
    if receipt["adjudicationPrivateReceiptSha256"] != grid.sha256(private_receipt_path):
        raise ValueError("final qrel adjudication chain mismatch")
    if (
        receipt["adjudicationOutputSha256"] != grid.sha256(bundle / "adjudication_reviewed.jsonl")
        or receipt["adjudicationAttestationSha256"] != grid.sha256(bundle / "adjudication_access_attestation.json")
    ):
        raise ValueError("final qrel frozen adjudication artifact mismatch")
    _validate_access_attestation(
        bundle / "adjudication_access_attestation.json",
        "adjudicator",
        adjudication_root / "distribution",
        role="adjudicator",
    )
    adjudication_rows = grid.read_jsonl(bundle / "adjudication_reviewed.jsonl")
    validate_adjudication_submission(
        grid.read_jsonl(adjudication_root / "distribution/blind_pack.jsonl"), adjudication_rows
    )
    for relative, expected in receipt["sources"].items():
        if grid.sha256(grid.ROOT / relative) != expected:
            raise ValueError("final qrel source pin mismatch")
    qrels = bundle / "final_qrels.jsonl"
    pin = receipt["finalQrels"]
    if pin != {"rows": len(grid.read_jsonl(qrels)), "sha256": grid.sha256(qrels), "bytes": qrels.stat().st_size}:
        raise ValueError("final qrel artifact mismatch")
    qrel_rows = grid.read_jsonl(qrels)
    adjudication_mapping = actual_mapping
    adjudication_by_blind = {row["blindQueryId"]: row for row in adjudication_rows}
    adjudicated: dict[tuple[str, int], int] = {}
    for blind_query, item in adjudication_mapping["queries"].items():
        by_token = {
            row["candidateToken"]: row for row in adjudication_by_blind[blind_query]["reviews"]
        }
        for token, product_id in item["candidates"].items():
            adjudicated[(item["queryId"], int(product_id))] = int(by_token[token]["relevance"])
    expected_rows = []
    for key in sorted(candidates):
        first, second = reviews["reviewer01"][key], reviews["reviewer02"][key]
        if first["relevance"] == second["relevance"] and first["relevance"] != "unknown":
            grade, provenance = int(first["relevance"]), "exact_two_reviewer_agreement"
        else:
            if key not in adjudicated:
                raise ValueError("final qrel lacks required adjudication")
            grade, provenance = adjudicated[key], "blind_third_adjudication"
        expected_rows.append({"queryId": key[0], "productId": key[1], "grade": grade, "provenance": provenance})
    if qrel_rows != expected_rows:
        raise ValueError("final qrel does not replay review/adjudication chain")
    return qrel_rows


def _query_metrics(ranked: list[int], grades: Mapping[int, int]) -> dict[str, float] | None:
    relevant = {product_id for product_id, grade in grades.items() if grade >= 2}
    if not relevant:
        return None
    recall = len(set(ranked[:50]) & relevant) / len(relevant)
    hit = float(any(product_id in relevant for product_id in ranked[:3]))
    gains = [(2 ** grades.get(product_id, 0)) - 1 for product_id in ranked[:10]]
    dcg = sum(gain / math.log2(rank + 1) for rank, gain in enumerate(gains, 1))
    ideal = sorted(((2 ** grade) - 1 for grade in grades.values()), reverse=True)[:10]
    idcg = sum(gain / math.log2(rank + 1) for rank, gain in enumerate(ideal, 1))
    return {"pooledRecallAt50": recall, "pooledNdcgAt10": dcg / idcg if idcg else 0.0, "pooledHitAt3": hit}


def _macro(rows: Iterable[Mapping[str, float]]) -> dict[str, float]:
    values = list(rows)
    if not values:
        return {"pooledRecallAt50": 0.0, "pooledNdcgAt10": 0.0, "pooledHitAt3": 0.0}
    return {
        key: sum(row[key] for row in values) / len(values)
        for key in ("pooledRecallAt50", "pooledNdcgAt10", "pooledHitAt3")
    }


def paired_bootstrap_lower(deltas: list[float], *, samples: int = 10000, seed: int = 20260828) -> float:
    if not deltas:
        return float("-inf")
    rng = random.Random(seed)
    means = []
    for _ in range(samples):
        means.append(sum(rng.choice(deltas) for _ in deltas) / len(deltas))
    means.sort()
    return means[math.floor(0.025 * (len(means) - 1))]


def _latency_p95(
    traces: list[dict[str, Any]], dense_rows: list[dict[str, Any]], analyzer: str, arm: str
) -> float:
    dense = {(row["queryId"], int(row["repeat"])): float(row["queryMs"]) for row in dense_rows}
    es_weight, dense_weight = grid.WEIGHT_ARMS[arm]
    values = []
    for row in traces:
        if row["analyzer"] != analyzer or row["arm"] != arm:
            continue
        duration = float(row["latencyMs"]["fusion"])
        if es_weight > 0:
            duration += float(row["latencyMs"]["es"])
        if dense_weight > 0:
            duration += dense[(row["queryId"], int(row["repeat"]))]
        values.append(duration)
    return grid.percentile(values, 0.95)


def _evaluate_qrels(
    run_dir: Path, qrel_rows: list[dict[str, Any]]
) -> dict[str, Any]:
    chain_verified = False
    package.validate(run_dir)
    contract = package.review_contract()
    if (
        contract["relevanceMetrics"]["relevantCutoff"] != "grade>=2"
        or contract["relevanceMetrics"]["ndcgGain"] != "2^grade-1"
        or contract["minimumEligibleQueries"] != {"development": 9, "validation": 6, "sealed_test": 3}
        or contract["selection"]["validation"]["pairedBootstrap"]
        != {"samples": 10000, "seed": 20260828, "percentileCi": 0.95, "ndcgDeltaLower": ">0"}
    ):
        raise ValueError("scorer constants drift from frozen contract")
    private = run_dir / "private_evaluator"
    evaluator = grid.read_jsonl(private / "evaluator_pool_v3.jsonl")
    intents = grid.read_jsonl(private / "frozen_taskstate_intents_v3.jsonl")
    rankings = grid.read_jsonl(private / "production_aligned_rankings_v3.jsonl")
    traces = grid.read_jsonl(private / "trace.jsonl")
    dense_latency = grid.read_jsonl(private / "dense_latency_trace_v2.jsonl")
    prices, catalog = package.v2._price_and_catalog()
    intent_by_query = {row["queryId"]: row for row in intents}
    expected = {
        (row["queryId"], int(candidate["productId"]))
        for row in evaluator for candidate in row["candidates"]
    }
    actual = [(row.get("queryId"), int(row.get("productId"))) for row in qrel_rows]
    if len(actual) != len(set(actual)) or set(actual) != expected:
        raise ValueError("final qrel is not the exact evaluator pool")
    grades: dict[str, dict[int, int]] = {}
    incomplete: set[str] = set()
    for row in qrel_rows:
        query_id, product_id = str(row["queryId"]), int(row["productId"])
        grade = row.get("grade")
        if grade == "unknown":
            raise ValueError("final qrel cannot contain unresolved unknown")
        if type(grade) is not int or grade not in {0, 1, 2, 3}:
            raise ValueError("final qrel grade invalid")
        grades.setdefault(query_id, {})[product_id] = grade
    split_by_query = {row["queryId"]: row["split"] for row in intents}
    no_relevant = {
        query_id for query_id, query_grades in grades.items()
        if not any(grade >= 2 for grade in query_grades.values())
    }
    eligible = set(split_by_query) - incomplete - no_relevant
    ranking_by_cell = {
        (row["queryId"], row["analyzer"], row["arm"]): [item["productId"] for item in row["ranked"]]
        for row in rankings
    }
    hard_safety: dict[tuple[str, str], dict[str, int]] = {}
    for analyzer in grid.INDEX_NAMES:
        for arm in grid.WEIGHT_ARMS:
            checked = violations = 0
            for query_id in split_by_query:
                requirements = intent_by_query[query_id]["supportedHardRequirements"]
                for product_id in ranking_by_cell[(query_id, analyzer, arm)]:
                    checked += 1
                    if not package.hard_eligible(
                        catalog[int(product_id)],
                        int(prices[int(product_id)]["referencePriceMinor"]),
                        requirements,
                    ):
                        violations += 1
            hard_safety[(analyzer, arm)] = {"checked": checked, "violations": violations}
    per_cell: dict[tuple[str, str], dict[str, dict[str, dict[str, float]]]] = {}
    for analyzer in grid.INDEX_NAMES:
        for arm in grid.WEIGHT_ARMS:
            split_rows: dict[str, dict[str, dict[str, float]]] = {"development": {}, "validation": {}, "sealed_test": {}}
            for query_id in eligible:
                metrics = _query_metrics(ranking_by_cell[(query_id, analyzer, arm)], grades[query_id])
                if metrics is not None:
                    split_rows[split_by_query[query_id]][query_id] = metrics
            per_cell[(analyzer, arm)] = split_rows
    minimum = {"development": 9, "validation": 6, "sealed_test": 3}
    counts = {split: sum(query_id in eligible for query_id, actual_split in split_by_query.items() if actual_split == split) for split in minimum}
    sufficient = all(counts[split] >= required for split, required in minimum.items())
    def dev_key(cell: tuple[str, str]):
        macro = _macro(per_cell[cell]["development"].values())
        return (-macro["pooledNdcgAt10"], -macro["pooledRecallAt50"], -macro["pooledHitAt3"], _latency_p95(traces, dense_latency, *cell), cell)
    safe_cells = [cell for cell in per_cell if hard_safety[cell]["violations"] == 0]
    if not safe_cells:
        raise ValueError("no hard-safe development cell")
    winner = min(safe_cells, key=dev_key)
    winner_dev = _macro(per_cell[winner]["development"].values())
    validation_winner = _macro(per_cell[winner]["validation"].values())
    validation_base = _macro(per_cell[BASELINE]["validation"].values())
    validation_delta = {key: validation_winner[key] - validation_base[key] for key in validation_winner}
    paired = [
        per_cell[winner]["validation"][query_id]["pooledNdcgAt10"]
        - per_cell[BASELINE]["validation"][query_id]["pooledNdcgAt10"]
        for query_id in sorted(per_cell[winner]["validation"])
    ]
    ci_lower = paired_bootstrap_lower(paired)
    p95 = _latency_p95(traces, dense_latency, *winner)
    validation_accept = (
        sufficient
        and validation_delta["pooledNdcgAt10"] >= 0.02
        and validation_delta["pooledRecallAt50"] >= 0
        and validation_delta["pooledHitAt3"] >= 0
        and hard_safety[winner]["violations"] == 0
        and ci_lower > 0
        and p95 <= 100
    )
    sealed_winner = _macro(per_cell[winner]["sealed_test"].values())
    sealed_base = _macro(per_cell[BASELINE]["sealed_test"].values())
    sealed_delta = {key: sealed_winner[key] - sealed_base[key] for key in sealed_winner}
    winner_hits = sum(row["pooledHitAt3"] for row in per_cell[winner]["sealed_test"].values())
    base_hits = sum(row["pooledHitAt3"] for row in per_cell[BASELINE]["sealed_test"].values())
    sealed_accept = (
        sufficient
        and sealed_delta["pooledNdcgAt10"] >= -0.05
        and sealed_delta["pooledRecallAt50"] >= -0.05
        and base_hits - winner_hits < 2
        and hard_safety[winner]["violations"] == 0
        and p95 <= 100
    )
    gates_accept = validation_accept and sealed_accept
    return {
        "schemaVersion": "used-phone-439-retrieval-score-report-v1",
        "authority": "CODEX_ASSISTED_QREL_NOT_FORMAL_HUMAN_GOLD",
        "excluded": {"unknownQueries": sorted(incomplete), "noRelevantInPool": sorted(no_relevant)},
        "eligibleQueryCount": counts,
        "minimumEligibleGate": "ACCEPT" if sufficient else "HOLD_INSUFFICIENT_RELEVANT_QUERIES",
        "developmentWinner": {"analyzer": winner[0], "arm": winner[1], "metrics": winner_dev},
        "hardConstraintSafety": hard_safety[winner],
        "validation": {"winner": validation_winner, "baseline": validation_base, "delta": validation_delta, "bootstrap95CiLower": ci_lower, "p95Ms": p95, "gate": "ACCEPT" if validation_accept else "HOLD"},
        "sealed": {"winner": sealed_winner, "baseline": sealed_base, "delta": sealed_delta, "winnerHitQueries": winner_hits, "baselineHitQueries": base_hits, "gate": "ACCEPT" if sealed_accept else "HOLD"},
        "decision": {
            "metricGates": "ACCEPT" if gates_accept else "HOLD",
            "reviewChain": "ACCEPT" if chain_verified else "UNVERIFIED",
            "productionDefaultSwitch": "ACCEPT" if gates_accept and chain_verified else "HOLD",
            "reason": (
                "all frozen gates and mandatory review chain passed"
                if gates_accept and chain_verified
                else "metric gate failed or mandatory review chain was not verified"
            ),
        },
    }


def evaluate_qrels(run_dir: Path, qrel_rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Diagnostic-only scoring; it can never authorize a default switch."""
    return _evaluate_qrels(run_dir, qrel_rows)


def score_final_bundle(
    run_dir: Path,
    intake_root: Path,
    adjudication_root: Path,
    final_bundle: Path,
) -> dict[str, Any]:
    qrels = validate_final_qrel_bundle(
        run_dir, intake_root, adjudication_root, final_bundle
    )
    report = _evaluate_qrels(run_dir, qrels)
    report["decision"]["reviewChain"] = "ACCEPT"
    if report["decision"]["metricGates"] == "ACCEPT":
        report["decision"]["productionDefaultSwitch"] = "ACCEPT"
        report["decision"]["reason"] = "all frozen gates and mandatory review chain passed"
    return report


def _score_receipt_path(output: Path) -> Path:
    return output.with_name(output.name + ".receipt.json")


def write_score_report(
    report: Mapping[str, Any],
    run_dir: Path,
    intake_root: Path,
    adjudication_root: Path,
    final_bundle: Path,
    output: Path,
) -> Path:
    receipt_path = _score_receipt_path(output)
    if output.exists() or receipt_path.exists():
        raise FileExistsError("refusing to overwrite score report chain")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(grid.canonical(report) + "\n", encoding="utf-8")
    receipt = {
        "schemaVersion": "used-phone-439-score-report-receipt-v1",
        "packageReceiptSha256": grid.sha256(run_dir / "receipt_v3.json"),
        "finalBundleReceiptSha256": grid.sha256(final_bundle / "receipt.json"),
        "reviewIntakeReceiptHashes": {
            reviewer: grid.sha256(intake_root / reviewer / "receipt.json")
            for reviewer in package.REVIEWERS
        },
        "adjudicationPrivateReceiptSha256": grid.sha256(adjudication_root / "private/receipt.json"),
        "report": {"sha256": grid.sha256(output), "bytes": output.stat().st_size},
        "sources": {
            Path(__file__).resolve().relative_to(grid.ROOT).as_posix(): grid.sha256(Path(__file__).resolve()),
            Path(package.__file__).resolve().relative_to(grid.ROOT).as_posix(): grid.sha256(Path(package.__file__).resolve()),
        },
    }
    receipt_path.write_text(grid.canonical(receipt) + "\n", encoding="utf-8")
    validate_score_report(run_dir, intake_root, adjudication_root, final_bundle, output)
    return output


def validate_score_report(
    run_dir: Path,
    intake_root: Path,
    adjudication_root: Path,
    final_bundle: Path,
    output: Path,
) -> dict[str, Any]:
    receipt = json.loads(_score_receipt_path(output).read_text(encoding="utf-8"))
    expected_sources = {
        Path(__file__).resolve().relative_to(grid.ROOT).as_posix(): grid.sha256(Path(__file__).resolve()),
        Path(package.__file__).resolve().relative_to(grid.ROOT).as_posix(): grid.sha256(Path(package.__file__).resolve()),
    }
    expected = {
        "schemaVersion": "used-phone-439-score-report-receipt-v1",
        "packageReceiptSha256": grid.sha256(run_dir / "receipt_v3.json"),
        "finalBundleReceiptSha256": grid.sha256(final_bundle / "receipt.json"),
        "reviewIntakeReceiptHashes": {
            reviewer: grid.sha256(intake_root / reviewer / "receipt.json")
            for reviewer in package.REVIEWERS
        },
        "adjudicationPrivateReceiptSha256": grid.sha256(adjudication_root / "private/receipt.json"),
        "report": {"sha256": grid.sha256(output), "bytes": output.stat().st_size},
        "sources": expected_sources,
    }
    if receipt != expected:
        raise ValueError("score report receipt chain mismatch")
    report = json.loads(output.read_text(encoding="utf-8"))
    expected_report = score_final_bundle(
        run_dir, intake_root, adjudication_root, final_bundle
    )
    if report != expected_report:
        raise ValueError("score report does not replay frozen final bundle")
    return report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-dir", type=Path, default=package.DEFAULT_RUN_DIR)
    parser.add_argument("--intake-root", required=True, type=Path)
    parser.add_argument("--adjudication-root", required=True, type=Path)
    parser.add_argument("--final-bundle", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    report = score_final_bundle(
        args.run_dir, args.intake_root, args.adjudication_root, args.final_bundle
    )
    write_score_report(
        report,
        args.run_dir,
        args.intake_root,
        args.adjudication_root,
        args.final_bundle,
        args.output,
    )
    print(grid.canonical({"status": report["decision"]["productionDefaultSwitch"], "output": str(args.output)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
