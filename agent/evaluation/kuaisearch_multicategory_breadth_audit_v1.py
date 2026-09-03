"""Deterministic KuaiSearch multi-category breadth audit.

This audit joins the frozen public query/qrel/document snapshot to the older
exact-category evidence audit through exact public product fields.  It uses
source relevance only to measure whether a category has query support; it does
not turn sparse qrels into exhaustive gold.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterable


SCHEMA_VERSION = "kuaisearch-multicategory-breadth-audit-v1"
DATASET_REVISION = "09807c773ce67360ed8df30842e372182fcf7ad9"
AUDIT_VERSION = "kuaisearch-multicategory-breadth-audit-20260824-r1"

EXPECTED_INPUTS = {
    "documents": ("6b8f55f94fb292e9ff946014221244e07fa338e22cfa29de39c42f8c2d1e245f", 46079),
    "queries": ("a168134f2626d106e93b438c95d02ea8949629c4d5278793afb17b12b611f738", 46306),
    "qrels": ("9461f44894021c43617bcd0b81859a8197e2f55d791e736232faaca92eca00dc", 46390),
    "evidence": ("9e8f9e78ab6a630985dbfb3b202bc06ab010120cfc9bce3b61c042750db669b3", 18591),
    "feasibility": ("c4c6e99eea5da3e51120c29898c8d92c7d21c7ae4a15dedd08f0d2c5f8e5cf11", 6843),
}

# A deliberately diverse portfolio, selected after the aggregate audit rather
# than simply taking the twelve largest apparel categories.
PORTFOLIO_KEYS = (
    "1/39/0",       # T-shirts
    "1/13/103",     # jeans
    "23/40/217",    # casual shoes
    "39/91/250",    # running shoes
    "10/75/82",     # household cleaning tools
    "15/23/21",     # western pastries
    "14/20/18",     # facial cleanser
    "17/181/508",   # school supplementary books
    "43/113/142",   # writing instruments
    "13/33/135",    # bracelets
    "29/55/367",    # stress-relief toys
    "30/59/57",     # phone cases (strategic conditional candidate)
)

CORE_GATE = {
    "minimumMatchedDocuments": 10,
    "minimumPositiveQueries": 10,
    "minimumEvidenceProducts": 25,
    "minimumDistinctAttributeSignatures": 20,
    "minimumPositiveQrelShare": 0.65,
    "minimumUniqueTitleRate": 0.85,
}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_line(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def _read_jsonl(path: Path) -> Iterable[dict[str, Any]]:
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                yield json.loads(line)


def _verify_input(name: str, path: Path) -> dict[str, Any]:
    expected_sha, expected_rows = EXPECTED_INPUTS[name]
    actual_sha = _sha256(path)
    if actual_sha != expected_sha:
        raise ValueError(f"{name} SHA mismatch: {actual_sha}")
    if name == "feasibility":
        rows = json.loads(path.read_text(encoding="utf-8"))
        actual_rows = len(rows)
    else:
        actual_rows = sum(1 for _ in _read_jsonl(path))
    if actual_rows != expected_rows:
        raise ValueError(f"{name} row count mismatch: {actual_rows}")
    return {"path": path.as_posix(), "sha256": actual_sha, "rows": actual_rows}


def _evidence_signature(row: dict[str, Any]) -> tuple[str, str, str, str]:
    refs = row.get("evidenceRefs")
    raw = refs[0].get("rawValue", "") if isinstance(refs, list) and refs and isinstance(refs[0], dict) else ""
    return (str(row.get("title", "")), str(raw), str(row.get("brand", "")), str(row.get("seller", "")))


def _document_signature(row: dict[str, Any]) -> tuple[str, str, str, str]:
    return (
        str(row.get("title", "")),
        str(row.get("attr_value", "")),
        str(row.get("brand", "")),
        str(row.get("seller_name", "")),
    )


def _write_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as handle:
        for row in rows:
            handle.write(_canonical_line(row))


def _gate(metric: dict[str, Any]) -> tuple[str, dict[str, bool]]:
    checks = {
        "matchedDocuments": metric["matchedDocumentCount"] >= CORE_GATE["minimumMatchedDocuments"],
        "positiveQueries": metric["positiveQueryCount"] >= CORE_GATE["minimumPositiveQueries"],
        "evidenceProducts": metric["evidenceProductCount"] >= CORE_GATE["minimumEvidenceProducts"],
        "attributeSignatures": metric["distinctAttributeSignatures"] >= CORE_GATE["minimumDistinctAttributeSignatures"],
        "positiveQrelShare": metric["positiveQrelShare"] >= CORE_GATE["minimumPositiveQrelShare"],
        "uniqueTitleRate": metric["uniqueTitleRate"] >= CORE_GATE["minimumUniqueTitleRate"],
    }
    if all(checks.values()):
        return "CORE_BREADTH_CANDIDATE", checks
    if metric["categoryKey"] == "30/59/57" and all(value for key, value in checks.items() if key != "positiveQueries") and metric["positiveQueryCount"] >= 8:
        return "CONDITIONAL_LOW_QUERY_SUPPORT", checks
    return "INSUFFICIENT_FOR_PORTFOLIO", checks


def _metric_score(metric: dict[str, Any]) -> float:
    return round(
        math.log1p(metric["positiveQueryCount"]) * 3
        + math.log1p(metric["matchedDocumentCount"]) * 2
        + math.log1p(metric["evidenceProductCount"])
        + math.log1p(metric["distinctAttributeSignatures"])
        + metric["positiveQrelShare"] * 3
        + metric["uniqueTitleRate"] * 2,
        6,
    )


def build_audit(
    *,
    documents_path: Path,
    queries_path: Path,
    qrels_path: Path,
    evidence_path: Path,
    feasibility_path: Path,
    output_dir: Path,
) -> dict[str, Any]:
    if output_dir.exists() and any(output_dir.iterdir()):
        raise FileExistsError(f"refusing to overwrite non-empty output: {output_dir}")
    inputs = {
        name: _verify_input(name, path)
        for name, path in (
            ("documents", documents_path),
            ("queries", queries_path),
            ("qrels", qrels_path),
            ("evidence", evidence_path),
            ("feasibility", feasibility_path),
        )
    }
    feasibility_rows = json.loads(feasibility_path.read_text(encoding="utf-8"))
    feasibility = {row["categoryKey"]: row for row in feasibility_rows}

    signatures: dict[tuple[str, str, str, str], set[str]] = defaultdict(set)
    evidence_product_ids: dict[str, set[str]] = defaultdict(set)
    for row in _read_jsonl(evidence_path):
        signatures[_evidence_signature(row)].add(str(row["categoryKey"]))
        evidence_product_ids[str(row["categoryKey"])].add(str(row.get("itemId", "")))

    doc_to_category: dict[str, str] = {}
    document_samples: dict[str, list[dict[str, Any]]] = defaultdict(list)
    doc_counts: Counter[str] = Counter()
    titles: dict[str, set[str]] = defaultdict(set)
    attrs: dict[str, set[str]] = defaultdict(set)
    brands: dict[str, set[str]] = defaultdict(set)
    sellers: dict[str, set[str]] = defaultdict(set)
    unmatched_documents = 0
    ambiguous_documents = 0
    document_lookup: dict[str, dict[str, Any]] = {}
    for row in _read_jsonl(documents_path):
        document_lookup[str(row["doc_id"])] = row
        categories = signatures.get(_document_signature(row), set())
        if not categories:
            unmatched_documents += 1
            continue
        if len(categories) != 1:
            ambiguous_documents += 1
            continue
        category = next(iter(categories))
        doc_id = str(row["doc_id"])
        doc_to_category[doc_id] = category
        doc_counts[category] += 1
        titles[category].add(str(row.get("title", "")))
        attrs[category].add(str(row.get("attr_value", "")))
        brands[category].add(str(row.get("brand", "")))
        sellers[category].add(str(row.get("seller_name", "")))
        document_samples[category].append({
            "docId": doc_id,
            "title": row.get("title"),
            "brand": row.get("brand"),
            "seller": row.get("seller_name"),
            "attrValue": row.get("attr_value"),
        })

    queries = {str(row["query_id"]): row for row in _read_jsonl(queries_path)}
    positive_queries: dict[str, set[str]] = defaultdict(set)
    linked_queries: dict[str, set[str]] = defaultdict(set)
    qrel_grades: dict[str, Counter[int]] = defaultdict(Counter)
    positive_samples: dict[str, list[dict[str, Any]]] = defaultdict(list)
    zero_samples: dict[str, list[dict[str, Any]]] = defaultdict(list)
    linked_qrels = 0
    for row in _read_jsonl(qrels_path):
        category = doc_to_category.get(str(row["doc_id"]))
        if category is None:
            continue
        query = queries.get(str(row["query_id"]))
        if query is None:
            raise ValueError("qrel references missing query")
        linked_qrels += 1
        query_id = str(row["query_id"])
        relevance = int(row["relevance"])
        linked_queries[category].add(query_id)
        qrel_grades[category][relevance] += 1
        sample = {
            "queryId": query_id,
            "query": query["query"],
            "split": query["split"],
            "sourceRelevance": relevance,
            "docId": row["doc_id"],
            "documentTitle": document_lookup[str(row["doc_id"])]["title"],
        }
        if relevance > 0:
            positive_queries[category].add(query_id)
            positive_samples[category].append(sample)
        else:
            zero_samples[category].append(sample)

    metrics: list[dict[str, Any]] = []
    for category in sorted(doc_counts):
        feasible = feasibility.get(category, {})
        all_qrels = sum(qrel_grades[category].values())
        positive_qrels = sum(count for grade, count in qrel_grades[category].items() if grade > 0)
        path = [
            str(feasible.get("categoryLevel1Name", "UNKNOWN")),
            str(feasible.get("categoryLevel2Name", "UNKNOWN")),
            str(feasible.get("categoryLevel3Name", "UNKNOWN")),
        ]
        metric = {
            "schemaVersion": "kuaisearch-multicategory-category-metric-v1",
            "categoryKey": category,
            "categoryPath": path,
            "matchedDocumentCount": doc_counts[category],
            "positiveQueryCount": len(positive_queries[category]),
            "linkedQueryCount": len(linked_queries[category]),
            "linkedQrelCount": all_qrels,
            "positiveQrelShare": round(positive_qrels / all_qrels, 6) if all_qrels else 0.0,
            "sourceGradeCounts": {str(key): qrel_grades[category][key] for key in sorted(qrel_grades[category])},
            "evidenceProductCount": int(feasible.get("evidenceProductCount") or len(evidence_product_ids[category])),
            "fullSourceItemCount": int(feasible.get("itemCount") or 0),
            "productsWithNonemptyAttr": int(feasible.get("productsWithNonemptyAttr") or 0),
            "distinctAttributeSignatures": int(feasible.get("distinctAttrSignatures") or 0),
            "uniqueMatchedTitleCount": len(titles[category]),
            "uniqueTitleRate": round(len(titles[category]) / doc_counts[category], 6),
            "uniqueMatchedAttrCount": len(attrs[category]),
            "distinctMatchedBrandCount": len(brands[category]),
            "distinctMatchedSellerCount": len(sellers[category]),
        }
        status, checks = _gate(metric)
        metric["gateStatus"] = status
        metric["gateChecks"] = checks
        metric["readinessScore"] = _metric_score(metric)
        metrics.append(metric)

    metrics_by_key = {row["categoryKey"]: row for row in metrics}
    candidates = []
    samples = []
    for key in PORTFOLIO_KEYS:
        if key not in metrics_by_key:
            raise ValueError(f"portfolio category lacks exact query/document join: {key}")
        metric = metrics_by_key[key]
        if metric["gateStatus"] == "INSUFFICIENT_FOR_PORTFOLIO":
            raise ValueError(f"portfolio category failed audit gate: {key}")
        candidate = {
            "schemaVersion": "kuaisearch-multicategory-breadth-candidate-v1",
            "auditVersion": AUDIT_VERSION,
            "categoryKey": key,
            "categoryPath": metric["categoryPath"],
            "role": "BREADTH",
            "status": metric["gateStatus"],
            "metrics": {name: metric[name] for name in (
                "matchedDocumentCount", "positiveQueryCount", "linkedQueryCount",
                "positiveQrelShare", "evidenceProductCount", "fullSourceItemCount",
                "distinctAttributeSignatures", "uniqueTitleRate", "readinessScore",
            )},
            "claimBoundary": "search_and_routing_breadth_only_not_deep_constraint_reasoning",
        }
        candidates.append(candidate)
        for sample_type, source_rows, limit in (
            ("positive_query", positive_samples[key], 3),
            ("grade_zero_query", zero_samples[key], 2),
            ("product", document_samples[key], 3),
        ):
            for row in sorted(source_rows, key=lambda item: json.dumps(item, ensure_ascii=False, sort_keys=True))[:limit]:
                samples.append({
                    "schemaVersion": "kuaisearch-multicategory-breadth-sample-v1",
                    "categoryKey": key,
                    "categoryPath": metric["categoryPath"],
                    "sampleType": sample_type,
                    "sample": row,
                })

    deep_anchor = {
        "schemaVersion": "kuaisearch-multicategory-breadth-candidate-v1",
        "auditVersion": AUDIT_VERSION,
        "categoryKey": "46/133/185",
        "categoryPath": ["二手", "二手手机通讯", "二手手机"],
        "role": "DEEP_ANCHOR",
        "status": "EXISTING_SEPARATE_439_GOLD_TRACK",
        "metrics": {
            "authoritativeRuntimeProducts": 439,
            "humanJudgedRealQueries": 24,
            "humanJudgments": 480,
        },
        "claimBoundary": "deep_used_phone_evidence_track_kept_separate_from_breadth_join",
    }

    output_dir.mkdir(parents=True, exist_ok=True)
    _write_jsonl(output_dir / "category_metrics.jsonl", sorted(metrics, key=lambda row: (-row["readinessScore"], row["categoryKey"])))
    _write_jsonl(output_dir / "candidates.jsonl", [deep_anchor, *candidates])
    _write_jsonl(output_dir / "samples.jsonl", samples)

    core_count = sum(row["status"] == "CORE_BREADTH_CANDIDATE" for row in candidates)
    conditional_count = sum(row["status"].startswith("CONDITIONAL") for row in candidates)
    report = [
        "# KuaiSearch multi-category breadth audit r1",
        "",
        "Status: `AUDIT_COMPLETE / DATASET_BUILD_NOT_STARTED`。",
        "",
        f"- Exact category join matched {len(doc_to_category):,}/{EXPECTED_INPUTS['documents'][1]:,} documents; "
        f"unmatched={unmatched_documents:,}, ambiguous={ambiguous_documents:,}。",
        f"- Linked source qrels={linked_qrels:,}; this measures query support only and is not exhaustive gold。",
        f"- Portfolio: 1 deep anchor + {core_count} core breadth + {conditional_count} conditional breadth category。",
        "",
        "| Role | Status | Category | Matched docs | Positive queries | Evidence products | Attr signatures |",
        "|---|---|---|---:|---:|---:|---:|",
        f"| DEEP | EXISTING | 二手/二手手机通讯/二手手机 | 439 runtime | 24 human-judged | 439 | 7 controlled fields |",
    ]
    for row in candidates:
        m = row["metrics"]
        report.append(
            f"| BREADTH | {row['status']} | {'/'.join(row['categoryPath'])} | "
            f"{m['matchedDocumentCount']} | {m['positiveQueryCount']} | {m['evidenceProductCount']} | "
            f"{m['distinctAttributeSignatures']} |"
        )
    report += [
        "",
        "## Boundary",
        "",
        "Breadth candidates prove category coverage, query routing and retrieval transfer only. "
        "They do not inherit the used-phone seven-field evidence contract. Source qrels are sparse and "
        "pool-external products remain unknown. Phone cases are conditional because only nine positive "
        "queries survive the exact join.",
        "",
    ]
    (output_dir / "report.md").write_text("\n".join(report), encoding="utf-8", newline="\n")

    artifacts = []
    for path in sorted(output_dir.iterdir(), key=lambda item: item.name):
        if path.name == "manifest.json":
            continue
        artifacts.append({
            "path": path.name,
            "sha256": _sha256(path),
            "bytes": path.stat().st_size,
            **({"rows": len(path.read_text(encoding="utf-8").splitlines())} if path.suffix == ".jsonl" else {}),
        })
    manifest = {
        "schemaVersion": SCHEMA_VERSION,
        "auditVersion": AUDIT_VERSION,
        "datasetRevision": DATASET_REVISION,
        "status": "AUDIT_COMPLETE_DATASET_BUILD_NOT_STARTED",
        "inputs": inputs,
        "join": {
            "method": "exact(title,attr_value,brand,seller)",
            "matchedDocuments": len(doc_to_category),
            "unmatchedDocuments": unmatched_documents,
            "ambiguousDocuments": ambiguous_documents,
            "linkedQrels": linked_qrels,
            "categoryCountWithExactJoin": len(metrics),
        },
        "selection": {
            "deepAnchors": 1,
            "breadthCandidates": len(candidates),
            "coreBreadthCandidates": core_count,
            "conditionalBreadthCandidates": conditional_count,
            "portfolioKeys": list(PORTFOLIO_KEYS),
            "gate": CORE_GATE,
        },
        "claimBoundary": {
            "sourceQrelIsExhaustiveGold": False,
            "breadthProvesDeepReasoning": False,
            "marketPriceAvailable": False,
        },
        "artifacts": artifacts,
    }
    (output_dir / "manifest.json").write_bytes(_canonical_line(manifest))
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--documents", type=Path, required=True)
    parser.add_argument("--queries", type=Path, required=True)
    parser.add_argument("--qrels", type=Path, required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--feasibility", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = build_audit(
        documents_path=args.documents.resolve(),
        queries_path=args.queries.resolve(),
        qrels_path=args.qrels.resolve(),
        evidence_path=args.evidence.resolve(),
        feasibility_path=args.feasibility.resolve(),
        output_dir=args.output.resolve(),
    )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()

