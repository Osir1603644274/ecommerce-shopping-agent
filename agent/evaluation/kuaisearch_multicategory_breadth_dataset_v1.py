"""Freeze the approved KuaiSearch breadth portfolio without upgrading sparse qrels to gold."""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable


SCHEMA_VERSION = "kuaisearch-multicategory-breadth-dataset-v1"
DATASET_VERSION = "kuaisearch-multicategory-breadth-v1-20260824-human-r1"
REVIEW_ID = "kuaisearch-multicategory-targeted-human-review-008"
REVIEW_VERSION = "kuaisearch-multicategory-human-review-20260824-r1"
AUDIT_VERSION = "kuaisearch-multicategory-breadth-audit-20260824-r1"

EXPECTED_SHA = {
    "documents": "6b8f55f94fb292e9ff946014221244e07fa338e22cfa29de39c42f8c2d1e245f",
    "queries": "a168134f2626d106e93b438c95d02ea8949629c4d5278793afb17b12b611f738",
    "qrels": "9461f44894021c43617bcd0b81859a8197e2f55d791e736232faaca92eca00dc",
    "evidence": "9e8f9e78ab6a630985dbfb3b202bc06ab010120cfc9bce3b61c042750db669b3",
    "audit_manifest": "4e853f543238fcd6274df8152ba84bc8efba7b0320357255c8b50b7813546e5b",
    "candidates": "768364c4277e2db906059ed05be10f7a666aa9353e2cf397b4289d1048d562ed",
    "samples": "c386778127380acbb546371424a22cc5b88727adb04473e8c3ceedfac0c4badc",
    "review_workbook": "ade186c5e4504bf957af30d0851bb24b3c54ff24f652b727b928887aeab4cb24",
}

EXPECTED_ROWS = {
    "documents": 46079,
    "queries": 46306,
    "qrels": 46390,
    "evidence": 18591,
    "candidates": 13,
    "samples": 90,
}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def canonical_line(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    with path.open(encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def write_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    with path.open("wb") as handle:
        for row in rows:
            handle.write(canonical_line(row))


def verify_file(name: str, path: Path, rows: int | None = None) -> dict[str, Any]:
    actual_sha = sha256(path)
    if actual_sha != EXPECTED_SHA[name]:
        raise ValueError(f"{name} SHA mismatch: {actual_sha}")
    actual_rows = None
    if rows is not None:
        actual_rows = len(read_jsonl(path))
        if actual_rows != rows:
            raise ValueError(f"{name} row mismatch: {actual_rows}")
    result: dict[str, Any] = {"path": path.as_posix(), "sha256": actual_sha}
    if actual_rows is not None:
        result["rows"] = actual_rows
    return result


def evidence_signature(row: dict[str, Any]) -> tuple[str, str, str, str]:
    refs = row.get("evidenceRefs")
    raw = refs[0].get("rawValue", "") if isinstance(refs, list) and refs and isinstance(refs[0], dict) else ""
    return str(row.get("title", "")), str(raw), str(row.get("brand", "")), str(row.get("seller", ""))


def document_signature(row: dict[str, Any]) -> tuple[str, str, str, str]:
    return (
        str(row.get("title", "")),
        str(row.get("attr_value", "")),
        str(row.get("brand", "")),
        str(row.get("seller_name", "")),
    )


def _ensure_empty(path: Path) -> None:
    if path.exists() and any(path.iterdir()):
        raise FileExistsError(f"refusing to overwrite non-empty output: {path}")


def _artifacts(directory: Path) -> list[dict[str, Any]]:
    records = []
    for path in sorted(directory.iterdir(), key=lambda item: item.name):
        if path.name == "manifest.json":
            continue
        record: dict[str, Any] = {"path": path.name, "sha256": sha256(path), "bytes": path.stat().st_size}
        if path.suffix == ".jsonl":
            record["rows"] = len(path.read_text(encoding="utf-8").splitlines())
        records.append(record)
    return records


def build_review_items(
    candidates: list[dict[str, Any]], samples: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    breadth = [row for row in candidates if row.get("role") == "BREADTH"]
    query_samples = [row for row in samples if row.get("sampleType") != "product"]
    product_samples = [row for row in samples if row.get("sampleType") == "product"]
    if (len(breadth), len(query_samples), len(product_samples)) != (12, 54, 36):
        raise ValueError("review source counts drifted")

    common = {
        "schemaVersion": "kuaisearch-multicategory-human-review-item-v1",
        "reviewId": REVIEW_ID,
        "reviewVersion": REVIEW_VERSION,
        "reviewerRole": "PROJECT_OWNER",
        "decisionMode": "BULK_CHAT_APPROVAL",
        "completedOn": "2026-08-24",
        "projectOwnerStatement": "通过",
        "itemByItemFormCompleted": False,
    }
    rows: list[dict[str, Any]] = []
    for index, row in enumerate(breadth, start=1):
        rows.append({
            **common,
            "reviewItemId": f"CAT-{index:03d}",
            "itemType": "CATEGORY",
            "decision": "INCLUDE_IN_BREADTH_DATASET",
            "sourceIdentity": {
                "categoryKey": row["categoryKey"],
                "categoryPath": row["categoryPath"],
                "auditCandidateStatus": row["status"],
            },
        })
    for index, row in enumerate(query_samples, start=1):
        sample = row["sample"]
        rows.append({
            **common,
            "reviewItemId": f"QUERY-{index:03d}",
            "itemType": "QUERY_LABEL",
            "decision": "APPROVE_SOURCE_RELEVANCE_LABEL",
            "sourceIdentity": {
                "categoryKey": row["categoryKey"],
                "categoryPath": row["categoryPath"],
                "sampleType": row["sampleType"],
                "queryId": sample["queryId"],
                "docId": sample["docId"],
                "sourceRelevance": int(sample["sourceRelevance"]),
            },
        })
    for index, row in enumerate(product_samples, start=1):
        sample = row["sample"]
        rows.append({
            **common,
            "reviewItemId": f"PROD-{index:03d}",
            "itemType": "PRODUCT_CATEGORY",
            "decision": "APPROVE_CATEGORY_ASSIGNMENT",
            "sourceIdentity": {
                "categoryKey": row["categoryKey"],
                "categoryPath": row["categoryPath"],
                "docId": sample["docId"],
            },
        })
    if len({row["reviewItemId"] for row in rows}) != 102:
        raise ValueError("review item identity collision")
    return rows


def build_dataset(
    *,
    documents_path: Path,
    queries_path: Path,
    qrels_path: Path,
    evidence_path: Path,
    audit_dir: Path,
    review_workbook_path: Path,
    review_output: Path,
    dataset_output: Path,
) -> dict[str, Any]:
    _ensure_empty(review_output)
    _ensure_empty(dataset_output)

    inputs = {
        "documents": verify_file("documents", documents_path, EXPECTED_ROWS["documents"]),
        "queries": verify_file("queries", queries_path, EXPECTED_ROWS["queries"]),
        "qrels": verify_file("qrels", qrels_path, EXPECTED_ROWS["qrels"]),
        "evidence": verify_file("evidence", evidence_path, EXPECTED_ROWS["evidence"]),
        "auditManifest": verify_file("audit_manifest", audit_dir / "manifest.json"),
        "candidates": verify_file("candidates", audit_dir / "candidates.jsonl", EXPECTED_ROWS["candidates"]),
        "samples": verify_file("samples", audit_dir / "samples.jsonl", EXPECTED_ROWS["samples"]),
        "reviewWorkbook": verify_file("review_workbook", review_workbook_path),
    }
    candidates = read_jsonl(audit_dir / "candidates.jsonl")
    samples = read_jsonl(audit_dir / "samples.jsonl")
    breadth = [row for row in candidates if row.get("role") == "BREADTH"]
    selected_keys = {str(row["categoryKey"]) for row in breadth}
    if len(selected_keys) != 12:
        raise ValueError("approved category count drifted")
    category_by_key = {str(row["categoryKey"]): row for row in breadth}

    review_items = build_review_items(candidates, samples)
    review_output.mkdir(parents=True, exist_ok=True)
    write_jsonl(review_output / "human_review_items.jsonl", review_items)
    review_report = [
        "# KuaiSearch multi-category human review r1",
        "",
        "Status: `HUMAN_BULK_REVIEW_COMPLETE`。",
        "",
        "- Project owner statement: `通过`。",
        "- Decision mode: one bulk chat approval; the spreadsheet was inspected but not filled item-by-item。",
        "- Approved: 12 categories, 54 query labels and 36 product category samples。",
        "- Boundary: this does not approve all source qrels or all evidence products as human gold。",
        "",
    ]
    (review_output / "report.md").write_text("\n".join(review_report), encoding="utf-8", newline="\n")
    review_manifest = {
        "schemaVersion": "kuaisearch-multicategory-human-review-manifest-v1",
        "reviewId": REVIEW_ID,
        "reviewVersion": REVIEW_VERSION,
        "status": "HUMAN_BULK_REVIEW_COMPLETE",
        "decisionMode": "BULK_CHAT_APPROVAL",
        "projectOwnerStatement": "通过",
        "completedOn": "2026-08-24",
        "counts": {"categories": 12, "queryLabels": 54, "productCategories": 36, "total": 102},
        "claimBoundary": {"itemByItemFormCompleted": False, "allSourceQrelsHumanApproved": False},
        "inputs": {name: inputs[name] for name in ("auditManifest", "candidates", "samples", "reviewWorkbook")},
        "artifacts": _artifacts(review_output),
    }
    (review_output / "manifest.json").write_bytes(canonical_line(review_manifest))

    selected_evidence: list[dict[str, Any]] = []
    signature_index: dict[tuple[str, str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in read_jsonl(evidence_path):
        category_key = str(row.get("categoryKey", ""))
        if category_key not in selected_keys:
            continue
        signature_index[evidence_signature(row)].append(row)
        refs = row.get("evidenceRefs") or []
        raw_attr = refs[0].get("rawValue", "") if refs else ""
        selected_evidence.append({
            "schemaVersion": "kuaisearch-multicategory-breadth-record-v1",
            "recordType": "EVIDENCE_PRODUCT",
            "datasetVersion": DATASET_VERSION,
            "itemId": str(row["itemId"]),
            "categoryKey": category_key,
            "categoryPath": row["categoryPath"],
            "title": row["title"],
            "brand": row["brand"],
            "seller": row["seller"],
            "rawAttrValue": raw_attr,
            "normalizedAttrValues": row.get("normalizedAttrValues", []),
            "marketPrice": None,
            "source": {"kind": "KUAISEARCH_EVIDENCE_AUDIT", "lineNumber": refs[0].get("lineNumber") if refs else None},
        })
    if len(selected_evidence) != 2040 or len(signature_index) != 2040:
        raise ValueError(f"selected evidence drift: {len(selected_evidence)}/{len(signature_index)}")

    documents: list[dict[str, Any]] = []
    document_category: dict[str, str] = {}
    for row in read_jsonl(documents_path):
        matches = signature_index.get(document_signature(row), [])
        if not matches:
            continue
        categories = {str(match["categoryKey"]) for match in matches}
        if len(categories) != 1 or len(matches) != 1:
            raise ValueError(f"ambiguous selected signature for doc {row['doc_id']}")
        category_key = next(iter(categories))
        document_category[str(row["doc_id"])] = category_key
        documents.append({
            "schemaVersion": "kuaisearch-multicategory-breadth-record-v1",
            "recordType": "DOCUMENT",
            "datasetVersion": DATASET_VERSION,
            "docId": str(row["doc_id"]),
            "evidenceItemId": str(matches[0]["itemId"]),
            "categoryKey": category_key,
            "categoryPath": category_by_key[category_key]["categoryPath"],
            "title": row["title"],
            "brand": row["brand"],
            "seller": row["seller_name"],
            "rawAttrValue": row["attr_value"],
            "joinMethod": "exact(title,attr_value,brand,seller)",
        })
    if len(documents) != 499:
        raise ValueError(f"selected document count drift: {len(documents)}")

    query_lookup = {str(row["query_id"]): row for row in read_jsonl(queries_path)}
    sample_query_ids = {
        (str(row["sample"]["queryId"]), str(row["sample"]["docId"]))
        for row in samples if row["sampleType"] != "product"
    }
    source_qrels: list[dict[str, Any]] = []
    linked_query_categories: dict[str, set[str]] = defaultdict(set)
    for row in read_jsonl(qrels_path):
        doc_id = str(row["doc_id"])
        category_key = document_category.get(doc_id)
        if category_key is None:
            continue
        query_id = str(row["query_id"])
        if query_id not in query_lookup:
            raise ValueError(f"missing query: {query_id}")
        linked_query_categories[query_id].add(category_key)
        source_qrels.append({
            "schemaVersion": "kuaisearch-multicategory-breadth-record-v1",
            "recordType": "SOURCE_QREL",
            "datasetVersion": DATASET_VERSION,
            "queryId": query_id,
            "docId": doc_id,
            "categoryKey": category_key,
            "sourceRelevance": int(row["relevance"]),
            "split": row["split"],
            "labelScope": "SPARSE_SOURCE_LABEL_NOT_EXHAUSTIVE_GOLD",
            "humanSampleApproved": (query_id, doc_id) in sample_query_ids,
        })
    if len(source_qrels) != 507 or len(linked_query_categories) != 507:
        raise ValueError(f"selected qrel/query count drift: {len(source_qrels)}/{len(linked_query_categories)}")
    if sum(row["humanSampleApproved"] for row in source_qrels) != 54:
        raise ValueError("human-approved query sample coverage drifted")

    queries = []
    for query_id in sorted(linked_query_categories):
        row = query_lookup[query_id]
        queries.append({
            "schemaVersion": "kuaisearch-multicategory-breadth-record-v1",
            "recordType": "QUERY",
            "datasetVersion": DATASET_VERSION,
            "queryId": query_id,
            "query": row["query"],
            "split": row["split"],
            "linkedCategoryKeys": sorted(linked_query_categories[query_id]),
            "source": "KUAISEARCH_RELEVANCE_SNAPSHOT",
        })

    category_review = {row["sourceIdentity"]["categoryKey"]: row for row in review_items if row["itemType"] == "CATEGORY"}
    categories = []
    for row in breadth:
        key = str(row["categoryKey"])
        categories.append({
            "schemaVersion": "kuaisearch-multicategory-breadth-record-v1",
            "recordType": "CATEGORY",
            "datasetVersion": DATASET_VERSION,
            "categoryKey": key,
            "categoryPath": row["categoryPath"],
            "auditCandidateStatus": row["status"],
            "humanDecision": category_review[key]["decision"],
            "claimBoundary": row["claimBoundary"],
        })

    review_by_id = {row["reviewItemId"]: row for row in review_items}
    human_sample_labels: list[dict[str, Any]] = []
    query_index = 0
    product_index = 0
    for row in samples:
        if row["sampleType"] == "product":
            product_index += 1
            review_item_id = f"PROD-{product_index:03d}"
            sample = row["sample"]
            human_sample_labels.append({
                "schemaVersion": "kuaisearch-multicategory-breadth-record-v1",
                "recordType": "HUMAN_SAMPLE_LABEL",
                "datasetVersion": DATASET_VERSION,
                "reviewItemId": review_item_id,
                "sampleKind": "PRODUCT_CATEGORY",
                "categoryKey": row["categoryKey"],
                "docId": sample["docId"],
                "humanDecision": review_by_id[review_item_id]["decision"],
                "decisionMode": "BULK_CHAT_APPROVAL",
            })
        else:
            query_index += 1
            review_item_id = f"QUERY-{query_index:03d}"
            sample = row["sample"]
            human_sample_labels.append({
                "schemaVersion": "kuaisearch-multicategory-breadth-record-v1",
                "recordType": "HUMAN_SAMPLE_LABEL",
                "datasetVersion": DATASET_VERSION,
                "reviewItemId": review_item_id,
                "sampleKind": "QUERY_LABEL",
                "categoryKey": row["categoryKey"],
                "queryId": sample["queryId"],
                "docId": sample["docId"],
                "sourceRelevance": int(sample["sourceRelevance"]),
                "sourceSampleType": row["sampleType"],
                "humanDecision": review_by_id[review_item_id]["decision"],
                "decisionMode": "BULK_CHAT_APPROVAL",
            })
    if len(human_sample_labels) != 90:
        raise ValueError("human sample label count drifted")

    dataset_output.mkdir(parents=True, exist_ok=True)
    write_jsonl(dataset_output / "categories.jsonl", categories)
    write_jsonl(dataset_output / "evidence_products.jsonl", sorted(selected_evidence, key=lambda row: (row["categoryKey"], row["itemId"])))
    write_jsonl(dataset_output / "documents.jsonl", sorted(documents, key=lambda row: row["docId"]))
    write_jsonl(dataset_output / "queries.jsonl", queries)
    write_jsonl(dataset_output / "source_qrels.jsonl", sorted(source_qrels, key=lambda row: (row["queryId"], row["docId"])))
    write_jsonl(dataset_output / "human_sample_labels.jsonl", human_sample_labels)
    dataset_report = [
        "# KuaiSearch multi-category breadth dataset v1",
        "",
        "Status: `DATASET_FROZEN / BASELINE_NOT_RUN`。",
        "",
        "- Approved breadth categories: 12 (11 core + 1 conditional phone-case category)。",
        "- Evidence products: 2,040; exact-joined documents: 499。",
        "- Source query/qrel pool: 507/507; source labels remain sparse and non-exhaustive。",
        "- Human-approved samples: 54 query labels + 36 document category labels。",
        "- No market prices, no deep constraint contract, no model or Agent result。",
        "",
    ]
    (dataset_output / "report.md").write_text("\n".join(dataset_report), encoding="utf-8", newline="\n")
    dataset_manifest = {
        "schemaVersion": SCHEMA_VERSION,
        "datasetVersion": DATASET_VERSION,
        "status": "DATASET_FROZEN_BASELINE_NOT_RUN",
        "inputs": inputs,
        "humanReview": {
            "reviewId": REVIEW_ID,
            "reviewVersion": REVIEW_VERSION,
            "manifestSha256": sha256(review_output / "manifest.json"),
            "decisionMode": "BULK_CHAT_APPROVAL",
            "counts": {"categories": 12, "queryLabels": 54, "productCategories": 36},
        },
        "counts": {
            "categories": len(categories),
            "evidenceProducts": len(selected_evidence),
            "documents": len(documents),
            "queries": len(queries),
            "sourceQrels": len(source_qrels),
            "humanSampleLabels": len(human_sample_labels),
        },
        "join": {
            "method": "exact(title,attr_value,brand,seller)",
            "selectedEvidenceSignatures": len(signature_index),
            "matchedDocuments": len(documents),
            "ambiguousSelectedSignatures": 0,
        },
        "claimBoundary": {
            "sourceQrelsAreExhaustiveGold": False,
            "allDatasetRowsHumanReviewed": False,
            "marketPriceAvailable": False,
            "breadthProvesDeepConstraintReasoning": False,
            "phoneCaseRetainsConditionalSourceStatus": True,
        },
        "artifacts": _artifacts(dataset_output),
    }
    (dataset_output / "manifest.json").write_bytes(canonical_line(dataset_manifest))
    return {"review": review_manifest, "dataset": dataset_manifest}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--documents", type=Path, required=True)
    parser.add_argument("--queries", type=Path, required=True)
    parser.add_argument("--qrels", type=Path, required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--audit-dir", type=Path, required=True)
    parser.add_argument("--review-workbook", type=Path, required=True)
    parser.add_argument("--review-output", type=Path, required=True)
    parser.add_argument("--dataset-output", type=Path, required=True)
    args = parser.parse_args()
    result = build_dataset(
        documents_path=args.documents.resolve(),
        queries_path=args.queries.resolve(),
        qrels_path=args.qrels.resolve(),
        evidence_path=args.evidence.resolve(),
        audit_dir=args.audit_dir.resolve(),
        review_workbook_path=args.review_workbook.resolve(),
        review_output=args.review_output.resolve(),
        dataset_output=args.dataset_output.resolve(),
    )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
