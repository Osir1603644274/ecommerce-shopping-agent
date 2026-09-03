"""Build auditable candidate pools for human used-phone qrel annotation.

The module does not assign relevance grades.  Existing sparse qrels and manual
coverage seeds may add documents to a review pool, but never become labels.
Production code must not import this offline-only module or its artifacts.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from copy import deepcopy
from collections import defaultdict
from pathlib import Path
from typing import Any, Callable, Iterable

from agent.app.domains.ecommerce.models import bm25_rank, expand_product_query


SCHEMA_VERSION = "used-phone-human-qrel-review-v1"
DEFAULT_POOL_DEPTH = 12
DEFAULT_CROSS_ENCODER_INPUT_DEPTH = 50
DEFAULT_CONFUSION_AUDIT_DEPTH = 2
DEFAULT_CROSS_ENCODER_MODEL = "BAAI/bge-reranker-base"
DEFAULT_CROSS_ENCODER_REVISION = "2cfc18c9415c912f9d8155881c133215df768a70"
SYNTHETIC_PRICE_SCHEMA_VERSION = "used-phone-synthetic-reference-price-v1"
SYNTHETIC_PRICE_POLICY = "budget_and_ranking"

# These roles deliberately separate static text retrieval from scenarios which
# require a constraint gate, retained turn state, or a capability answer.
# Keeping the mapping here as well as in the seed asset makes a pool revision
# fail closed if somebody accidentally changes only one copy.
EVALUATION_ROLE_BY_QUERY_ID = {
    "uphq-001": ("primary_retrieval", "main_retrieval"),
    "uphq-002": ("primary_retrieval", "main_retrieval"),
    "uphq-003": ("constraint_smoke", "hard_constraint_smoke"),
    "uphq-004": ("multi_turn_e2e", "multi_turn_e2e"),
    "uphq-005": ("primary_retrieval", "main_retrieval"),
    "uphq-006": ("capability_boundary", "capability_boundary"),
    "uphq-007": ("primary_retrieval", "main_retrieval"),
    "uphq-008": ("primary_retrieval", "main_retrieval"),
    "uphq-009": ("primary_retrieval", "main_retrieval"),
    "uphq-010": ("primary_retrieval", "main_retrieval"),
    "uphq-011": ("no_answer_constraint", "no_answer_constraint"),
    "uphq-012": ("primary_retrieval", "main_retrieval"),
}
PRIMARY_RETRIEVAL_ROLE = "primary_retrieval"
NON_STATIC_RETRIEVAL_ROLES = {
    "constraint_smoke", "multi_turn_e2e", "capability_boundary",
    "no_answer_constraint",
}

Ranker = Callable[[str, list[dict[str, Any]]], list[int]]
ScoredRanker = Callable[
    [str, list[dict[str, Any]]],
    list[tuple[int, float]],
]


class LocalCrossEncoder:
    """Pinned, offline-capable Transformers Cross-Encoder adapter."""

    def __init__(
        self,
        *,
        model_name: str = DEFAULT_CROSS_ENCODER_MODEL,
        revision: str = DEFAULT_CROSS_ENCODER_REVISION,
        cache_dir: Path,
        model_path: Path | None = None,
        local_files_only: bool = True,
        batch_size: int = 32,
        max_length: int = 256,
    ) -> None:
        if batch_size < 1 or max_length < 16:
            raise ValueError("invalid Cross-Encoder batch size or max length")
        import torch
        from transformers import AutoModelForSequenceClassification, AutoTokenizer

        self.model_name = model_name
        self.revision = revision
        self.cache_dir = cache_dir.resolve()
        self.model_path = model_path.resolve() if model_path is not None else None
        self.batch_size = batch_size
        self.max_length = max_length
        self.device = "cuda" if torch.cuda.is_available() else "cpu"
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        load_identifier = str(self.model_path) if self.model_path else model_name
        load_options = {
            "cache_dir": str(self.cache_dir),
            "local_files_only": local_files_only or self.model_path is not None,
            "trust_remote_code": False,
        }
        if self.model_path is None:
            load_options["revision"] = revision
        self.tokenizer = AutoTokenizer.from_pretrained(load_identifier, **load_options)
        self.model = AutoModelForSequenceClassification.from_pretrained(
            load_identifier,
            use_safetensors=True,
            **load_options,
        )
        if self.device == "cuda":
            self.model = self.model.half()
        self.model.to(self.device)
        self.model.eval()

    def provenance(self) -> dict[str, Any]:
        provenance = {
            "model": self.model_name,
            "revision": self.revision,
            "device": self.device,
            "batchSize": self.batch_size,
            "maxLength": self.max_length,
            "documentFields": ["item_title"],
            "localOnlyAfterProvisioning": True,
        }
        if self.model_path is not None:
            weights = self.model_path / "model.safetensors"
            provenance["localArtifact"] = {
                "directoryName": self.model_path.name,
                "weightsSha256": file_sha256(weights),
                "weightsBytes": weights.stat().st_size,
            }
        return provenance

    def rank(
        self,
        query: str,
        documents: list[dict[str, Any]],
    ) -> list[tuple[int, float]]:
        import torch

        scored: list[tuple[int, float]] = []
        with torch.inference_mode():
            for start in range(0, len(documents), self.batch_size):
                batch = documents[start:start + self.batch_size]
                encoded = self.tokenizer(
                    [query] * len(batch),
                    [str(item.get("title") or "") for item in batch],
                    padding=True,
                    truncation=True,
                    max_length=self.max_length,
                    return_tensors="pt",
                )
                encoded = {key: value.to(self.device) for key, value in encoded.items()}
                logits = self.model(**encoded).logits.reshape(-1).float().cpu().tolist()
                scored.extend(
                    (int(document["id"]), float(score))
                    for document, score in zip(batch, logits, strict=True)
                )
        return sorted(scored, key=lambda item: (-item[1], item[0]))


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"invalid JSONL {path}:{line_number}") from exc
            if not isinstance(row, dict):
                raise ValueError(f"JSONL row is not an object {path}:{line_number}")
            rows.append(row)
    return rows


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _display_path(path: Path) -> str:
    """Prefer repository-relative, slash-stable manifest paths."""

    resolved = path.resolve()
    try:
        return resolved.relative_to(Path(os.getcwd()).resolve()).as_posix()
    except ValueError:
        return str(resolved)


def _runtime_documents(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "id": int(row["product_id"]),
            "title": str(row.get("item_title") or ""),
            "brand": str(row.get("brand") or ""),
            "attributeText": str(row.get("attr_value") or ""),
        }
        for row in rows
    ]


def validate_synthetic_prices(
    rows: list[dict[str, Any]],
    documents: list[dict[str, Any]],
) -> dict[int, dict[str, Any]]:
    """Validate a complete, identity-bound synthetic-price sidecar."""

    catalog_ids = {int(row["product_id"]) for row in documents}
    prices: dict[int, dict[str, Any]] = {}
    for row in rows:
        if row.get("schemaVersion") != SYNTHETIC_PRICE_SCHEMA_VERSION:
            raise ValueError("unexpected synthetic price schemaVersion")
        try:
            product_id = int(row["itemId"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("invalid synthetic price itemId") from exc
        if product_id in prices:
            raise ValueError(f"duplicate synthetic price itemId: {product_id}")
        price_minor = row.get("referencePriceMinor")
        if type(price_minor) is not int or price_minor < 0:
            raise ValueError(f"invalid synthetic price: {product_id}")
        if (
            row.get("currency") != "CNY"
            or row.get("dataNature") != "synthetic"
            or row.get("priceStatus") != "synthetic"
            or not str(row.get("labelZh") or "").strip()
            or not str(row.get("disclosureZh") or "").strip()
        ):
            raise ValueError(f"invalid synthetic price provenance: {product_id}")
        prices[product_id] = {
            "referencePriceMinor": price_minor,
            "currency": "CNY",
            "dataNature": "synthetic",
            "priceStatus": "synthetic",
            "labelZh": str(row["labelZh"]),
            "disclosureZh": str(row["disclosureZh"]),
            "policy": SYNTHETIC_PRICE_POLICY,
            "sourceSchemaVersion": SYNTHETIC_PRICE_SCHEMA_VERSION,
        }
    missing = sorted(catalog_ids - prices.keys())
    extra = sorted(prices.keys() - catalog_ids)
    if missing or extra:
        raise ValueError(
            "synthetic prices do not exactly match catalog identities: "
            f"missing={missing[:5]}, extra={extra[:5]}"
        )
    return prices


def validate_synthetic_price_manifest(
    manifest: dict[str, Any],
    *,
    price_path: Path,
    price_rows: list[dict[str, Any]],
) -> None:
    if (
        manifest.get("schemaVersion") != SYNTHETIC_PRICE_SCHEMA_VERSION
        or manifest.get("dataNature") != "synthetic"
        or manifest.get("priceStatus") != "synthetic"
    ):
        raise ValueError("invalid synthetic price manifest identity")
    output = manifest.get("output")
    boundary = manifest.get("policyBoundary")
    if not isinstance(output, dict) or not isinstance(boundary, dict):
        raise ValueError("synthetic price manifest lacks provenance")
    if (
        output.get("sha256") != file_sha256(price_path)
        or output.get("rowCount") != len(price_rows)
        or boundary.get("maySupportBudgetFilteringOnlyWhenPolicy")
        != SYNTHETIC_PRICE_POLICY
        or boundary.get("mayPopulateVerifiedSnapshot") is not False
    ):
        raise ValueError("synthetic price manifest does not match sidecar/policy")


def _validate_seed_queries(rows: list[dict[str, Any]]) -> None:
    identities: set[str] = set()
    for row in rows:
        if row.get("schemaVersion") != "used-phone-human-qrel-query-v1":
            raise ValueError("unexpected seed query schemaVersion")
        query_id = str(row.get("queryId") or "")
        if not query_id.startswith("uphq-") or query_id in identities:
            raise ValueError(f"invalid or duplicate queryId: {query_id!r}")
        identities.add(query_id)
        if not str(row.get("rawQuery") or "").strip():
            raise ValueError(f"missing rawQuery: {query_id}")
        if not str(row.get("retrievalQuery") or "").strip():
            raise ValueError(f"missing retrievalQuery: {query_id}")
        expected_role = EVALUATION_ROLE_BY_QUERY_ID.get(query_id)
        if expected_role is None:
            raise ValueError(f"query has no registered evaluation role: {query_id}")
        if (
            row.get("evaluationRole") != expected_role[0]
            or row.get("stratum") != expected_role[1]
        ):
            raise ValueError(
                f"evaluation role/stratum mismatch for {query_id}: "
                f"expected={expected_role}"
            )
        source = row.get("source")
        if not isinstance(source, dict) or source.get("humanAuthored") is not True:
            raise ValueError(f"query is not explicitly human-authored: {query_id}")
        audit_probes = row.get("confusionAuditQueries", [])
        if not isinstance(audit_probes, list):
            raise ValueError(f"confusionAuditQueries must be a list: {query_id}")
        seen_probes: set[str] = set()
        for probe in audit_probes:
            if not isinstance(probe, dict):
                raise ValueError(f"invalid confusion audit probe: {query_id}")
            probe_query = str(probe.get("query") or "").strip()
            rationale = str(probe.get("rationale") or "").strip()
            if not probe_query or not rationale or probe_query in seen_probes:
                raise ValueError(f"invalid or duplicate confusion audit probe: {query_id}")
            seen_probes.add(probe_query)


def build_review_rows(
    *,
    seed_queries: list[dict[str, Any]],
    documents: list[dict[str, Any]],
    sparse_qrels: list[dict[str, Any]],
    pool_depth: int = DEFAULT_POOL_DEPTH,
    cross_encoder_input_depth: int = DEFAULT_CROSS_ENCODER_INPUT_DEPTH,
    confusion_audit_depth: int = DEFAULT_CONFUSION_AUDIT_DEPTH,
    vector_ranker: Ranker | None = None,
    cross_encoder_ranker: ScoredRanker | None = None,
    retrieval_metadata: dict[str, Any] | None = None,
    synthetic_prices: dict[int, dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Create unjudged, multi-system candidate pools for human review."""

    _validate_seed_queries(seed_queries)
    if pool_depth < 5:
        raise ValueError("pool_depth must be at least 5")
    if cross_encoder_input_depth < pool_depth:
        raise ValueError("cross_encoder_input_depth must be >= pool_depth")
    if not 1 <= confusion_audit_depth <= 10:
        raise ValueError("confusion_audit_depth must be between 1 and 10")
    products = _runtime_documents(documents)
    runtime_by_id = {int(row["id"]): row for row in products}
    by_product_id = {int(row["product_id"]): row for row in documents}
    if len(by_product_id) != len(documents):
        raise ValueError("catalog contains duplicate product_id")
    sparse_by_query: dict[str, list[int]] = defaultdict(list)
    for row in sparse_qrels:
        if int(row.get("relevance", 0)) > 0:
            sparse_by_query[str(row["query_id"])].append(int(row["product_id"]))

    output: list[dict[str, Any]] = []
    for seed in seed_queries:
        query = str(seed["retrievalQuery"])
        expanded_query, expansion_terms = expand_product_query(query)
        full_rankings = {
            "bm25_title": bm25_rank(
                query, products, field_weights={"title": 1.0}
            )[:cross_encoder_input_depth],
            "bm25_fields": bm25_rank(query, products)[:cross_encoder_input_depth],
            "bm25_expanded_fields": bm25_rank(
                expanded_query, products
            )[:cross_encoder_input_depth],
        }
        if vector_ranker is not None:
            full_rankings["vector_title"] = vector_ranker(
                query, products
            )[:cross_encoder_input_depth]

        legacy_query_id = str(seed.get("source", {}).get("legacyQueryId") or "")
        legacy_products = sparse_by_query.get(legacy_query_id, [])
        coverage_products = [int(item) for item in seed.get("mustIncludeProductIds", [])]
        for product_id in coverage_products:
            if product_id not in by_product_id:
                raise ValueError(
                    f"coverage seed product is outside catalog: "
                    f"{seed['queryId']}:{product_id}"
                )

        ordinary_pool_ids: set[int] = {
            int(product_id)
            for ranking in full_rankings.values()
            for product_id in ranking[:pool_depth]
        }
        ordinary_pool_ids.update(legacy_products)
        ordinary_pool_ids.update(coverage_products)
        audit_product_ids: list[int] = []
        confusion_audit_probes: list[dict[str, Any]] = []
        for probe in seed.get("confusionAuditQueries", []):
            probe_query = str(probe["query"])
            selected_for_probe: list[int] = []
            probe_ranking = bm25_rank(
                probe_query,
                products,
                field_weights={"title": 1.0},
            )
            for product_id in probe_ranking:
                if product_id in ordinary_pool_ids or product_id in audit_product_ids:
                    continue
                audit_product_ids.append(product_id)
                selected_for_probe.append(product_id)
                if len(selected_for_probe) == confusion_audit_depth:
                    break
            confusion_audit_probes.append({
                "query": probe_query,
                "rationale": str(probe["rationale"]),
                "selectionMethod": "bm25_title_first_unseen",
                "selectedProductIds": selected_for_probe,
            })

        cross_scores: dict[int, float] = {}
        cross_input: list[int] = []
        if cross_encoder_ranker is not None:
            for ranking in full_rankings.values():
                for product_id in ranking:
                    if product_id not in cross_input:
                        cross_input.append(product_id)
            for product_id in (
                *legacy_products,
                *coverage_products,
                *audit_product_ids,
            ):
                if product_id not in cross_input:
                    cross_input.append(product_id)
            scored = cross_encoder_ranker(
                query,
                [runtime_by_id[product_id] for product_id in cross_input],
            )
            scored_ids = [int(item[0]) for item in scored]
            if len(scored_ids) != len(cross_input) or set(scored_ids) != set(cross_input):
                raise ValueError(
                    f"Cross-Encoder must return a complete candidate permutation: "
                    f"{seed['queryId']}"
                )
            cross_scores = {int(product_id): float(score) for product_id, score in scored}
            full_rankings["cross_encoder_title"] = scored_ids

        rankings = {
            system_name: ranking[:pool_depth]
            for system_name, ranking in full_rankings.items()
        }
        selected: list[int] = []
        selection_sources: dict[int, list[str]] = defaultdict(list)
        rank_positions: dict[int, dict[str, int]] = defaultdict(dict)
        for system_name, ranking in rankings.items():
            for rank, product_id in enumerate(ranking, 1):
                rank_positions[product_id][system_name] = rank
                if product_id not in selected:
                    selected.append(product_id)
                selection_sources[product_id].append(system_name)

        for product_id in legacy_products:
            if product_id not in selected:
                selected.append(product_id)
            selection_sources[product_id].append("legacy_sparse_qrel_pool_only")
        for product_id in coverage_products:
            if product_id not in selected:
                selected.append(product_id)
            selection_sources[product_id].append("curated_coverage_pool_only")
        for product_id in audit_product_ids:
            if product_id not in selected:
                selected.append(product_id)
            selection_sources[product_id].append("confusion_audit_pool_only")

        candidates = []
        for product_id in selected:
            document = by_product_id[product_id]
            candidates.append({
                "productId": product_id,
                "itemTitle": str(document.get("item_title") or ""),
                "brand": str(document.get("brand") or ""),
                "attrValue": str(document.get("attr_value") or ""),
                **(
                    {"syntheticReferencePrice": deepcopy(synthetic_prices[product_id])}
                    if synthetic_prices is not None else {}
                ),
                "retrievalRanks": rank_positions.get(product_id, {}),
                "retrievalScores": (
                    {"cross_encoder_title": round(cross_scores[product_id], 8)}
                    if product_id in cross_scores else {}
                ),
                "selectionSources": sorted(set(selection_sources[product_id])),
                "judgment": {
                    "grade": None,
                    "label": "unjudged",
                    "evidenceBasis": [],
                    "reason": "",
                    "reviewStatus": "pending_review",
                    "reviewerId": "",
                    "reviewedAt": None,
                },
            })
        output.append({
            "schemaVersion": SCHEMA_VERSION,
            "queryId": seed["queryId"],
            "rawQuery": seed["rawQuery"],
            "retrievalQuery": query,
            "source": seed["source"],
            "intent": seed.get("intent", {}),
            "split": seed["split"],
            "evaluationRole": seed["evaluationRole"],
            "stratum": seed["stratum"],
            "reviewStatus": "pending_review",
            "humanConfirmed": False,
            "reviewerId": "",
            "reviewedAt": None,
            "pool": {
                "method": (
                    "union_bm25_vector_cross_encoder_plus_coverage_confusion_audit_v3"
                    if cross_encoder_ranker is not None and confusion_audit_probes
                    else "union_bm25_vector_cross_encoder_plus_coverage_v2"
                    if cross_encoder_ranker is not None
                    else "union_bm25_vector_plus_coverage_confusion_audit_v3"
                    if vector_ranker is not None and confusion_audit_probes
                    else "union_bm25_vector_plus_coverage_v2"
                    if vector_ranker is not None
                    else "union_bm25_title_fields_expanded_plus_coverage_confusion_audit_v2"
                    if confusion_audit_probes
                    else "union_bm25_title_fields_expanded_plus_coverage_v1"
                ),
                "depthPerSystem": pool_depth,
                "crossEncoderInputDepth": cross_encoder_input_depth,
                "crossEncoderInputCount": len(cross_input),
                "systems": list(rankings),
                "retrievalMetadata": deepcopy(retrieval_metadata or {}),
                "queryExpansionTerms": expansion_terms,
                "confusionAudit": {
                    "enabled": bool(confusion_audit_probes),
                    "depthPerProbe": confusion_audit_depth,
                    "probes": confusion_audit_probes,
                    "poolSourcesAreNotLabels": True,
                },
                "candidateCount": len(candidates),
                "unjudgedOutsidePool": True,
                "poolSourcesAreNotLabels": True,
            },
            "candidates": candidates,
        })
    return output


def preserve_existing_human_judgments(
    rows: list[dict[str, Any]],
    existing_rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Merge candidate-pool revisions without losing confirmed human work."""

    # A v1 pool predates role fields.  The mapping is registry-owned above, so
    # adding those derived fields does not alter any human judgment; it merely
    # permits safe preservation during the one-time contract migration.
    existing_rows = deepcopy(existing_rows)
    for existing_row in existing_rows:
        expected_role = EVALUATION_ROLE_BY_QUERY_ID.get(existing_row.get("queryId"))
        if expected_role is not None:
            existing_row.setdefault("evaluationRole", expected_role[0])
            existing_row.setdefault("stratum", expected_role[1])
    validate_review_rows(existing_rows)
    existing_by_query = {row["queryId"]: row for row in existing_rows}
    for row in rows:
        old_row = existing_by_query.get(row["queryId"])
        if old_row is None:
            continue
        new_by_product = {
            int(candidate["productId"]): candidate
            for candidate in row["candidates"]
        }
        for old_candidate in old_row["candidates"]:
            judgment = old_candidate.get("judgment", {})
            if judgment.get("reviewStatus") != "human_confirmed":
                continue
            product_id = int(old_candidate["productId"])
            new_candidate = new_by_product.get(product_id)
            if new_candidate is None:
                new_candidate = deepcopy(old_candidate)
                new_candidate.setdefault("retrievalScores", {})
                new_candidate["retrievalRanks"] = {}
                new_candidate["selectionSources"] = sorted(set(
                    [*new_candidate.get("selectionSources", []),
                     "prior_human_judgment_retained"]
                ))
                row["candidates"].append(new_candidate)
                new_by_product[product_id] = new_candidate
            else:
                new_candidate["judgment"] = deepcopy(judgment)
        row["pool"]["candidateCount"] = len(row["candidates"])
        all_reviewed = all(
            candidate["judgment"].get("reviewStatus") == "human_confirmed"
            for candidate in row["candidates"]
        )
        if all_reviewed and old_row.get("humanConfirmed") is True:
            row["reviewStatus"] = "human_confirmed"
            row["humanConfirmed"] = True
            row["reviewerId"] = old_row["reviewerId"]
            row["reviewedAt"] = old_row["reviewedAt"]
    validate_review_rows(rows)
    return rows


def validate_review_rows(rows: list[dict[str, Any]]) -> None:
    seen: set[str] = set()
    allowed_labels = {
        None: None,
        0: "not_relevant",
        1: "marginal",
        2: "relevant",
        3: "highly_relevant",
    }
    for row in rows:
        if row.get("schemaVersion") != SCHEMA_VERSION:
            raise ValueError("unexpected review schemaVersion")
        query_id = str(row.get("queryId") or "")
        if not query_id or query_id in seen:
            raise ValueError(f"duplicate review queryId: {query_id!r}")
        seen.add(query_id)
        expected_role = EVALUATION_ROLE_BY_QUERY_ID.get(query_id)
        if expected_role is None or (
            row.get("evaluationRole"), row.get("stratum")
        ) != expected_role:
            raise ValueError(f"review row has invalid evaluation role/stratum: {query_id}")
        candidates = row.get("candidates")
        if not isinstance(candidates, list) or not candidates:
            raise ValueError(f"empty candidate pool: {query_id}")
        product_ids: set[int] = set()
        for candidate in candidates:
            product_id = int(candidate["productId"])
            if product_id in product_ids:
                raise ValueError(f"duplicate candidate: {query_id}:{product_id}")
            product_ids.add(product_id)
            price = candidate.get("syntheticReferencePrice")
            if price is not None:
                if not isinstance(price, dict) or (
                    type(price.get("referencePriceMinor")) is not int
                    or price["referencePriceMinor"] < 0
                    or price.get("currency") != "CNY"
                    or price.get("dataNature") != "synthetic"
                    or price.get("priceStatus") != "synthetic"
                    or price.get("policy") != SYNTHETIC_PRICE_POLICY
                    or price.get("sourceSchemaVersion")
                    != SYNTHETIC_PRICE_SCHEMA_VERSION
                    or not str(price.get("labelZh") or "").strip()
                    or not str(price.get("disclosureZh") or "").strip()
                ):
                    raise ValueError(
                        f"invalid candidate synthetic price: {query_id}:{product_id}"
                    )
            judgment = candidate.get("judgment", {})
            grade = judgment.get("grade")
            if grade not in allowed_labels:
                raise ValueError(f"invalid grade: {query_id}:{product_id}:{grade}")
            expected = allowed_labels[grade]
            if expected is None:
                if judgment.get("label") not in {"unjudged", "unknown"}:
                    raise ValueError(f"invalid empty-grade label: {query_id}:{product_id}")
            elif judgment.get("label") != expected:
                raise ValueError(f"grade/label mismatch: {query_id}:{product_id}")
            reviewed = judgment.get("reviewStatus") == "human_confirmed"
            if judgment.get("label") == "unjudged":
                if reviewed or judgment.get("reviewerId") or judgment.get("reviewedAt"):
                    raise ValueError(
                        f"unjudged candidate has review provenance: {query_id}:{product_id}"
                    )
            else:
                if not reviewed or not judgment.get("reviewerId") or not judgment.get("reviewedAt"):
                    raise ValueError(
                        f"judged candidate lacks human provenance: {query_id}:{product_id}"
                    )
                if not str(judgment.get("reason") or "").strip():
                    raise ValueError(
                        f"reviewed candidate lacks reason: {query_id}:{product_id}"
                    )
        if row.get("humanConfirmed") is True:
            if row.get("reviewStatus") != "human_confirmed":
                raise ValueError(f"confirmed row has wrong status: {query_id}")
            if not row.get("reviewerId") or not row.get("reviewedAt"):
                raise ValueError(f"confirmed row lacks reviewer identity/time: {query_id}")
            for candidate in candidates:
                judgment = candidate["judgment"]
                if judgment.get("label") == "unjudged":
                    raise ValueError(
                        f"confirmed row still has unjudged candidate: "
                        f"{query_id}:{candidate['productId']}"
                    )
                if judgment.get("reviewStatus") != "human_confirmed":
                    raise ValueError(
                        f"confirmed row has candidate without human provenance: "
                        f"{query_id}:{candidate['productId']}"
                    )


def freeze_human_qrels(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Emit numeric qrels only after every pooled candidate is human reviewed."""

    validate_review_rows(rows)
    pending = [row["queryId"] for row in rows if row.get("humanConfirmed") is not True]
    if pending:
        raise ValueError(f"cannot freeze qrels with pending queries: {pending}")
    qrels: list[dict[str, Any]] = []
    for row in rows:
        for candidate in row["candidates"]:
            judgment = candidate["judgment"]
            grade = judgment.get("grade")
            if grade is None:
                # A reviewed unknown remains outside qrels; it is not a negative.
                continue
            qrels.append({
                "schemaVersion": "used-phone-human-qrel-v1",
                "queryId": row["queryId"],
                "productId": int(candidate["productId"]),
                "relevance": int(grade),
                "labelSource": "human",
                "reviewerId": row["reviewerId"],
                "reviewedAt": row["reviewedAt"],
                "evidenceBasis": list(judgment.get("evidenceBasis") or []),
                "reason": str(judgment["reason"]),
                "split": row["split"],
            })
    return sorted(qrels, key=lambda item: (item["split"], item["queryId"], item["productId"]))


def freeze_role_aware_human_qrels(
    rows: list[dict[str, Any]],
    *,
    include_sealed_test: bool = False,
) -> dict[str, Any]:
    """Freeze only confirmed main-retrieval rows for static ranking metrics.

    Unknown judgments are intentionally absent from qrels.  Constraint smoke,
    multi-turn, capability and no-answer rows remain auditable in readiness but
    cannot silently alter Hit/Recall/NDCG denominators.
    """

    # This module does not yet have an adjudication reader for the independent
    # second-human sealed-review package.  A first-human confirmation alone is
    # deliberately insufficient to release the preregistered sealed rows.
    # Keep the parameter for an explicit, fail-closed compatibility boundary;
    # a future release must add and validate that separate adjudication asset.
    if include_sealed_test:
        raise ValueError(
            "sealed-test qrels require completed independent second-human "
            "review and adjudication; this offline phase cannot release them"
        )

    validate_review_rows(rows)
    primary_rows = [
        row for row in rows if row["evaluationRole"] == PRIMARY_RETRIEVAL_ROLE
    ]
    pending_primary = [
        row["queryId"] for row in primary_rows if not row.get("humanConfirmed")
    ]
    if pending_primary:
        raise ValueError(f"cannot freeze pending primary retrieval queries: {pending_primary}")
    eligible_rows = [
        row for row in primary_rows
        if include_sealed_test or row["split"] != "sealed_test"
    ]
    qrels = []
    unknown_count = 0
    for row in eligible_rows:
        for candidate in row["candidates"]:
            judgment = candidate["judgment"]
            if judgment.get("grade") is None:
                unknown_count += 1
                continue
            qrels.append({
                "schemaVersion": "used-phone-human-qrel-v1",
                "queryId": row["queryId"],
                "productId": int(candidate["productId"]),
                "relevance": int(judgment["grade"]),
                "labelSource": "human",
                "reviewerId": str(judgment["reviewerId"]),
                "reviewedAt": str(judgment["reviewedAt"]),
                "evidenceBasis": list(judgment.get("evidenceBasis") or []),
                "reason": str(judgment["reason"]),
                "split": row["split"],
                "evaluationRole": row["evaluationRole"],
                "stratum": row["stratum"],
            })
    role_rows: dict[str, list[str]] = defaultdict(list)
    for row in rows:
        role_rows[str(row["evaluationRole"])].append(str(row["queryId"]))
    return {
        "schemaVersion": "used-phone-human-qrel-role-aware-freeze-v1",
        "status": "PRELIMINARY_NOT_GOLD",
        "staticMetricRole": PRIMARY_RETRIEVAL_ROLE,
        "staticMetricQueryIds": [row["queryId"] for row in eligible_rows],
        "withheldSealedTestQueryIds": [
            row["queryId"] for row in primary_rows if row["split"] == "sealed_test"
        ],
        "excludedFromStaticMetrics": {
            role: sorted(query_ids)
            for role, query_ids in role_rows.items()
            if role != PRIMARY_RETRIEVAL_ROLE
        },
        "unknownJudgmentCountExcludedNotNegative": unknown_count,
        "qrels": sorted(qrels, key=lambda item: (item["split"], item["queryId"], item["productId"])),
        "readiness": {
            "primaryRetrievalReady": not pending_primary,
            "capabilityBoundaryPending": any(
                row["evaluationRole"] == "capability_boundary" and not row.get("humanConfirmed")
                for row in rows
            ),
            "noAnswerIsSeparateFromRanking": True,
            "outsidePoolIsUnjudgedNotNegative": True,
        },
    }


def write_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")))
            handle.write("\n")


def build_bundle(
    *,
    seed_path: Path,
    document_path: Path,
    sparse_qrel_path: Path,
    output_path: Path,
    manifest_path: Path,
    pool_depth: int = DEFAULT_POOL_DEPTH,
    cross_encoder_input_depth: int = DEFAULT_CROSS_ENCODER_INPUT_DEPTH,
    confusion_audit_depth: int = DEFAULT_CONFUSION_AUDIT_DEPTH,
    include_vector: bool = False,
    cross_encoder: LocalCrossEncoder | None = None,
    existing_review_path: Path | None = None,
    synthetic_price_path: Path | None = None,
    synthetic_price_manifest_path: Path | None = None,
) -> dict[str, Any]:
    seed_queries = read_jsonl(seed_path)
    documents = read_jsonl(document_path)
    sparse_qrels = read_jsonl(sparse_qrel_path)
    synthetic_prices: dict[int, dict[str, Any]] | None = None
    synthetic_price_rows: list[dict[str, Any]] = []
    if synthetic_price_path is not None:
        if synthetic_price_manifest_path is None:
            raise ValueError("synthetic price manifest path is required")
        synthetic_price_rows = read_jsonl(synthetic_price_path)
        synthetic_price_manifest = json.loads(
            synthetic_price_manifest_path.read_text(encoding="utf-8")
        )
        if not isinstance(synthetic_price_manifest, dict):
            raise ValueError("synthetic price manifest is not an object")
        validate_synthetic_price_manifest(
            synthetic_price_manifest,
            price_path=synthetic_price_path,
            price_rows=synthetic_price_rows,
        )
        synthetic_prices = validate_synthetic_prices(synthetic_price_rows, documents)
    vector_ranker: Ranker | None = None
    retrieval_metadata: dict[str, Any] = {}
    started = time.perf_counter()
    if include_vector:
        from agent.evaluation.used_phone_real_query_retrieval_v1 import (
            build_vector_ranker,
        )

        vector_ranker = build_vector_ranker(_runtime_documents(documents))
        retrieval_metadata["vector"] = {
            "model": "BAAI/bge-small-zh-v1.5",
            "documentFields": ["item_title"],
            "implementation": "FastEmbed cosine",
        }
    if cross_encoder is not None:
        retrieval_metadata["crossEncoder"] = cross_encoder.provenance()
    rows = build_review_rows(
        seed_queries=seed_queries,
        documents=documents,
        sparse_qrels=sparse_qrels,
        pool_depth=pool_depth,
        cross_encoder_input_depth=cross_encoder_input_depth,
        confusion_audit_depth=confusion_audit_depth,
        vector_ranker=vector_ranker,
        cross_encoder_ranker=(cross_encoder.rank if cross_encoder else None),
        retrieval_metadata=retrieval_metadata,
        synthetic_prices=synthetic_prices,
    )
    preserved_human_judgments = 0
    if existing_review_path is not None and existing_review_path.exists():
        existing_rows = read_jsonl(existing_review_path)
        rows = preserve_existing_human_judgments(rows, existing_rows)
        preserved_human_judgments = sum(
            candidate["judgment"].get("reviewStatus") == "human_confirmed"
            for row in rows
            for candidate in row["candidates"]
        )
    if synthetic_prices is not None:
        for row in rows:
            for candidate in row["candidates"]:
                candidate["syntheticReferencePrice"] = deepcopy(
                    synthetic_prices[int(candidate["productId"])]
                )
    validate_review_rows(rows)
    write_jsonl(output_path, rows)
    human_judgments = sum(
        candidate["judgment"].get("reviewStatus") == "human_confirmed"
        for row in rows
        for candidate in row["candidates"]
    )
    human_confirmed_queries = sum(row.get("humanConfirmed") is True for row in rows)
    partially_reviewed_queries = sum(
        row.get("humanConfirmed") is not True
        and any(
            candidate["judgment"].get("reviewStatus") == "human_confirmed"
            for candidate in row["candidates"]
        )
        for row in rows
    )
    manifest = {
        "schemaVersion": "used-phone-human-qrel-manifest-v1",
        "status": "PENDING_HUMAN_REVIEW_NOT_GOLD",
        "queryCount": len(rows),
        "candidatePairCount": sum(len(row["candidates"]) for row in rows),
        "humanConfirmedQueryCount": human_confirmed_queries,
        "partiallyReviewedQueryCount": partially_reviewed_queries,
        "humanJudgmentCount": human_judgments,
        "evaluationRoles": {
            role: sorted(row["queryId"] for row in rows if row["evaluationRole"] == role)
            for role in sorted({row["evaluationRole"] for row in rows})
        },
        "strata": {
            stratum: sorted(row["queryId"] for row in rows if row["stratum"] == stratum)
            for stratum in sorted({row["stratum"] for row in rows})
        },
        "catalogProductCount": len(documents),
        "poolDepthPerSystem": pool_depth,
        "crossEncoderInputDepth": cross_encoder_input_depth,
        "confusionAuditDepthPerProbe": confusion_audit_depth,
        "retrieval": {
            "vectorEnabled": include_vector,
            "crossEncoderEnabled": cross_encoder is not None,
            "metadata": retrieval_metadata,
            "buildDurationMs": round((time.perf_counter() - started) * 1000, 2),
        },
        "inputs": {
            "seedQueries": {"path": _display_path(seed_path), "sha256": file_sha256(seed_path)},
            "documents": {"path": _display_path(document_path), "sha256": file_sha256(document_path)},
            "sparseQrels": {"path": _display_path(sparse_qrel_path), "sha256": file_sha256(sparse_qrel_path)},
            **(
                {
                    "syntheticPrices": {
                        "path": _display_path(synthetic_price_path),
                        "sha256": file_sha256(synthetic_price_path),
                        "manifestPath": _display_path(synthetic_price_manifest_path),
                        "manifestSha256": file_sha256(synthetic_price_manifest_path),
                        "rowCount": len(synthetic_price_rows),
                    }
                }
                if synthetic_price_path is not None
                and synthetic_price_manifest_path is not None
                else {}
            ),
        },
        "output": {"path": _display_path(output_path), "sha256": file_sha256(output_path)},
        "safety": {
            "existingQrelGradesCopied": False,
            "poolSourcesAreLabels": False,
            "unjudgedOutsidePoolIsNegative": False,
            "productionUseForbidden": True,
            "existingHumanJudgmentsPreserved": preserved_human_judgments,
            "syntheticPricePolicy": (
                {
                    "enabled": True,
                    "policy": SYNTHETIC_PRICE_POLICY,
                    "dataNature": "synthetic",
                    "mayPopulateVerifiedSnapshot": False,
                    "disclosureZh": "模拟参考价 / AI 合成，非真实报价",
                }
                if synthetic_prices is not None else {"enabled": False}
            ),
        },
    }
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return manifest


__all__ = [
    "DEFAULT_POOL_DEPTH",
    "DEFAULT_CROSS_ENCODER_INPUT_DEPTH",
    "DEFAULT_CONFUSION_AUDIT_DEPTH",
    "DEFAULT_CROSS_ENCODER_MODEL",
    "DEFAULT_CROSS_ENCODER_REVISION",
    "EVALUATION_ROLE_BY_QUERY_ID",
    "PRIMARY_RETRIEVAL_ROLE",
    "LocalCrossEncoder",
    "SCHEMA_VERSION",
    "build_bundle",
    "build_review_rows",
    "freeze_human_qrels",
    "freeze_role_aware_human_qrels",
    "preserve_existing_human_judgments",
    "read_jsonl",
    "validate_review_rows",
]
