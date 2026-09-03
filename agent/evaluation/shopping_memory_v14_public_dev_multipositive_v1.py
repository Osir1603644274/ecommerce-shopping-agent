"""Build public-dev multi-positive Shopping Memory V14 candidates and qrels."""
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from evaluation.shopping_memory_v14_retrieval_dev_v2 import (
    CATEGORY_SHA256,
    DEV_SHA256,
    INDEX_SHA256,
    RetrievalDevError,
    fts_query,
    read_jsonl,
    sha256,
    shopping_query,
)


ASPECT_SHA256 = "9441f80fc6c2ca246c192f22a2375b38bd3e6533881b01dc9d6fdd0c0334b78b"
CANDIDATE_DEPTH = 50
MINIMUM_TUPLE_SUPPORT = 5


@dataclass(frozen=True, slots=True)
class Candidate:
    product_rowid: int
    product_id: str
    raw_bm25: float | None
    source: str


def relevance_grade(matched: int, active: int) -> int:
    if active < 1 or matched < 0 or matched > active:
        raise RetrievalDevError("invalid relevance inputs")
    if matched == active:
        return 3
    if matched * 2 >= active:
        return 2
    if matched > 0:
        return 1
    return 0


class FullCatalogPool:
    def __init__(self, index_path: Path, category_path: Path, aspect_path: Path) -> None:
        index_uri = index_path.resolve().as_uri() + "?mode=ro&immutable=1"
        self.connection = sqlite3.connect(index_uri, uri=True)
        category_uri = category_path.resolve().as_uri() + "?mode=ro&immutable=1"
        aspect_uri = aspect_path.resolve().as_uri() + "?mode=ro&immutable=1"
        self.connection.execute("ATTACH DATABASE ? AS category_db", (category_uri,))
        self.connection.execute("ATTACH DATABASE ? AS aspect_db", (aspect_uri,))
        self.connection.execute(
            "CREATE TEMP TABLE candidate_rowid(product_rowid INTEGER PRIMARY KEY)"
        )
        self._support_cache: dict[tuple[str, str, str], int] = {}

    def close(self) -> None:
        self.connection.close()

    def candidates(
        self,
        query: str,
        category_id: str,
        historical_product_id: str,
        *,
        limit: int = CANDIDATE_DEPTH,
    ) -> list[Candidate]:
        rows = self.connection.execute(
            "SELECT product_fts.rowid,product_fts.product_id,bm25(product_fts) AS score "
            "FROM product_fts JOIN category_db.product_category AS category "
            "ON category.product_rowid=product_fts.rowid "
            "WHERE product_fts MATCH ? AND category.category_id=? "
            "ORDER BY score ASC,product_fts.product_id ASC LIMIT ?",
            (query, category_id, limit + 25),
        ).fetchall()
        output: list[Candidate] = []
        seen: set[str] = {historical_product_id}
        for product_rowid, product_id, score in rows:
            normalized_id = str(product_id)
            if normalized_id in seen:
                continue
            seen.add(normalized_id)
            output.append(Candidate(int(product_rowid), normalized_id, float(score), "fts"))
            if len(output) == limit:
                return output
        if len(output) < limit:
            fallback = self.connection.execute(
                "SELECT locator.rowid,locator.product_id "
                "FROM category_db.product_category AS category "
                "JOIN product_locator AS locator ON locator.rowid=category.product_rowid "
                "WHERE category.category_id=? ORDER BY locator.product_id ASC",
                (category_id,),
            )
            for product_rowid, product_id in fallback:
                normalized_id = str(product_id)
                if normalized_id in seen:
                    continue
                seen.add(normalized_id)
                output.append(Candidate(int(product_rowid), normalized_id, None, "category_fallback"))
                if len(output) == limit:
                    break
        return output

    def historical_aspects(self, product_id: str) -> set[tuple[str, str]]:
        rows = self.connection.execute(
            "SELECT aspect.attribute_key,aspect.normalized_value "
            "FROM product_locator AS locator JOIN aspect_db.product_aspect AS aspect "
            "ON aspect.product_rowid=locator.rowid WHERE locator.product_id=?",
            (product_id,),
        ).fetchall()
        return {(str(key), str(value)) for key, value in rows}

    def tuple_support(self, category_id: str, key: str, value: str) -> int:
        identity = (category_id, key, value)
        if identity not in self._support_cache:
            count = self.connection.execute(
                "SELECT count(*) FROM aspect_db.product_aspect AS aspect "
                "JOIN category_db.product_category AS category "
                "ON category.product_rowid=aspect.product_rowid "
                "WHERE aspect.attribute_key=? AND aspect.normalized_value=? "
                "AND category.category_id=?",
                (key, value, category_id),
            ).fetchone()[0]
            self._support_cache[identity] = int(count)
        return self._support_cache[identity]

    def candidate_matches(
        self,
        candidates: Sequence[Candidate],
        preferences: Sequence[tuple[str, str]],
    ) -> dict[int, set[tuple[str, str]]]:
        self.connection.execute("DELETE FROM candidate_rowid")
        self.connection.executemany(
            "INSERT INTO candidate_rowid(product_rowid) VALUES(?)",
            ((candidate.product_rowid,) for candidate in candidates),
        )
        clauses = " OR ".join(
            "(aspect.attribute_key=? AND aspect.normalized_value=?)" for _ in preferences
        )
        parameters = [item for pair in preferences for item in pair]
        rows = self.connection.execute(
            "SELECT aspect.product_rowid,aspect.attribute_key,aspect.normalized_value "
            "FROM candidate_rowid AS candidate JOIN aspect_db.product_aspect AS aspect "
            "ON aspect.product_rowid=candidate.product_rowid WHERE " + clauses,
            parameters,
        ).fetchall()
        output: dict[int, set[tuple[str, str]]] = {
            candidate.product_rowid: set() for candidate in candidates
        }
        for product_rowid, key, value in rows:
            output[int(product_rowid)].add((str(key), str(value)))
        return output


def active_preferences(episode: Mapping[str, Any]) -> tuple[tuple[str, str], ...]:
    raw = episode.get("preferences")
    if type(raw) is not list:
        raise RetrievalDevError("preferences are not a list")
    output: list[tuple[str, str]] = []
    for preference in raw:
        if type(preference) is not dict or preference.get("preferenceKind") != "prefer":
            continue
        key = preference.get("attributeKey")
        value = preference.get("normalizedValue")
        if type(key) is not str or not key or type(value) is not str or not value:
            raise RetrievalDevError("invalid active preference")
        output.append((key, value))
    return tuple(output)


def evaluate(
    index_path: Path,
    category_path: Path,
    aspect_path: Path,
    dev_path: Path,
) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    expected = (
        (index_path, INDEX_SHA256, "index"),
        (category_path, CATEGORY_SHA256, "category"),
        (aspect_path, ASPECT_SHA256, "aspect"),
        (dev_path, DEV_SHA256, "public dev"),
    )
    for path, digest, label in expected:
        if sha256(path) != digest:
            raise RetrievalDevError(f"{label} hash mismatch")
    all_rows = read_jsonl(dev_path)
    rows = [row for row in all_rows if row.get("questionType") == "single_product"]
    if len(all_rows) != 600 or len(rows) != 300:
        raise RetrievalDevError("public dev cardinality mismatch")

    pool = FullCatalogPool(index_path, category_path, aspect_path)
    dataset: list[dict[str, Any]] = []
    eligibility: list[dict[str, Any]] = []
    try:
        for row in rows:
            reasons: set[str] = set()
            episode = row.get("memoryEpisodes", [None])[0]
            historical = row.get("targetProducts", [None])[0]
            if type(episode) is not dict or type(historical) is not dict:
                raise RetrievalDevError("invalid episode or historical product")
            category_id = episode.get("categoryId")
            historical_id = historical.get("productId")
            if type(category_id) is not str or category_id != historical.get("categoryId"):
                reasons.add("CATEGORY_MISMATCH")
            if type(historical_id) is not str or not historical_id:
                raise RetrievalDevError("invalid historical product id")
            preferences = active_preferences(episode)
            keys = [key for key, _ in preferences]
            if not 1 <= len(preferences) <= 8:
                reasons.add("ACTIVE_PREFERENCE_COUNT_OUT_OF_RANGE")
            if len(keys) != len(set(keys)):
                reasons.add("ACTIVE_ATTRIBUTE_NOT_UNIQUE")
            historical_aspects = pool.historical_aspects(historical_id)
            if not set(preferences).issubset(historical_aspects):
                reasons.add("HISTORICAL_PRODUCT_DOES_NOT_SUPPORT_PREFERENCE")
            support = {
                f"{key}={value}": pool.tuple_support(str(category_id), key, value)
                for key, value in preferences
            }
            if any(count < MINIMUM_TUPLE_SUPPORT for count in support.values()):
                reasons.add("PREFERENCE_TUPLE_SUPPORT_BELOW_FIVE")

            query = fts_query(row.get("query"))
            candidates = pool.candidates(query, str(category_id), historical_id)
            if len(candidates) != CANDIDATE_DEPTH:
                reasons.add("CANDIDATE_DEPTH_NOT_FIFTY")
            if historical_id in {candidate.product_id for candidate in candidates}:
                reasons.add("HISTORICAL_PRODUCT_LEAK")
            matches = pool.candidate_matches(candidates, preferences) if preferences else {}
            max_relevance = max(
                (-candidate.raw_bm25 if candidate.raw_bm25 is not None else 0.0)
                for candidate in candidates
            ) if candidates else 0.0
            candidate_rows: list[dict[str, Any]] = []
            exact_alternatives = 0
            for rank, candidate in enumerate(candidates, 1):
                matched = len(matches.get(candidate.product_rowid, set()))
                grade = relevance_grade(matched, len(preferences)) if preferences else 0
                exact_alternatives += grade == 3
                relevance = -candidate.raw_bm25 if candidate.raw_bm25 is not None else 0.0
                candidate_rows.append({
                    "productId": candidate.product_id,
                    "baseRank": rank,
                    "baseScore": relevance / max_relevance if max_relevance > 0 else 0.0,
                    "candidateSource": candidate.source,
                    "matchedPreferenceCount": matched,
                    "activePreferenceCount": len(preferences),
                    "relevanceGrade": grade,
                })
            if exact_alternatives == 0:
                reasons.add("NO_EXACT_ALTERNATIVE_IN_FIXED_CANDIDATES")
            accepted = not reasons
            eligibility.append({
                "questionId": row.get("questionId"),
                "conversationId": row.get("conversationId"),
                "eligible": accepted,
                "reasons": sorted(reasons),
                "activePreferenceCount": len(preferences),
                "tupleSupport": support,
                "exactAlternativeCount": exact_alternatives,
            })
            dataset.append({
                "questionId": row.get("questionId"),
                "conversationId": row.get("conversationId"),
                "queryText": shopping_query(row.get("query")),
                "categoryId": category_id,
                "historicalProductId": historical_id,
                "activePreferences": [
                    {"attributeKey": key, "normalizedValue": value}
                    for key, value in preferences
                ],
                "eligible": accepted,
                "candidates": candidate_rows,
            })
    finally:
        pool.close()

    eligible = [row for row in eligibility if row["eligible"]]
    reason_counts: dict[str, int] = {}
    for row in eligibility:
        for reason in row["reasons"]:
            reason_counts[reason] = reason_counts.get(reason, 0) + 1
    coverage = len(eligible) / len(rows)
    gates = {
        "publicDevSingleProductCountExact300": len(rows) == 300,
        "candidateDepthExact50All": all(len(row["candidates"]) == 50 for row in dataset),
        "historicalProductExcludedAll": all(
            row["historicalProductId"] not in {item["productId"] for item in row["candidates"]}
            for row in dataset
        ),
        "minimumEligible200": len(eligible) >= 200,
        "minimumEligibleCoverage075": coverage >= 0.75,
    }
    report = {
        "schemaVersion": "shopping-memory-v14-public-dev-multipositive-report-v1",
        "decision": "PUBLIC_DEV_MULTIPOSITIVE_DATA_ACCEPT" if all(gates.values()) else "HOLD_PUBLIC_DEV_MULTIPOSITIVE_DATA",
        "split": "public-development",
        "counts": {
            "allRows": len(all_rows), "singleProductRows": len(rows),
            "eligibleRows": len(eligible), "eligibleCoverage": coverage,
        },
        "relevance": {
            "grade3": "all active prefer tuples match",
            "grade2": "at least half but not all active prefer tuples match",
            "grade1": "at least one but less than half active prefer tuples match",
            "grade0": "no active prefer tuple matches",
            "historicalProductPolicy": "always excluded",
            "indifferentPolicy": "not scored",
        },
        "eligibilityReasonCounts": dict(sorted(reason_counts.items())),
        "gates": gates,
        "inputs": {
            "indexSha256": INDEX_SHA256, "categorySha256": CATEGORY_SHA256,
            "aspectSha256": ASPECT_SHA256, "devSha256": DEV_SHA256,
        },
        "explicitBoundaries": {
            "memoryRerank": False, "lambdaSelection": False,
            "validationRead": False, "sealedRead": False,
            "productionSwitchAuthority": False,
        },
    }
    return report, dataset, eligibility


def _canonical(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def materialize(
    output_dir: Path,
    report: Mapping[str, Any],
    dataset: Iterable[Mapping[str, Any]],
    eligibility: Iterable[Mapping[str, Any]],
    *, runner_path: Path, contract_path: Path,
) -> Path:
    if output_dir.exists():
        raise FileExistsError(f"refusing to overwrite {output_dir}")
    output_dir.mkdir(parents=True)
    report_path = output_dir / "report.json"
    dataset_path = output_dir / "dataset.jsonl"
    eligibility_path = output_dir / "eligibility.jsonl"
    report_path.write_bytes(_canonical(report))
    with dataset_path.open("wb") as stream:
        for row in dataset:
            stream.write(_canonical(row))
    with eligibility_path.open("wb") as stream:
        for row in eligibility:
            stream.write(_canonical(row))
    receipt = {
        "schemaVersion": "shopping-memory-v14-public-dev-multipositive-receipt-v1",
        "decision": report["decision"],
        "runnerSha256": sha256(runner_path), "contractSha256": sha256(contract_path),
        "reportSha256": sha256(report_path), "datasetSha256": sha256(dataset_path),
        "eligibilitySha256": sha256(eligibility_path),
        "validationRead": False, "sealedAccess": "NONE",
    }
    receipt_path = output_dir / "receipt.json"
    receipt_path.write_bytes(_canonical(receipt))
    files = (report_path, dataset_path, eligibility_path, receipt_path)
    (output_dir / "SHA256SUMS.txt").write_text(
        "".join(f"{sha256(path)}  {path.name}\n" for path in files), encoding="ascii"
    )
    return report_path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--index", type=Path, required=True)
    parser.add_argument("--categories", type=Path, required=True)
    parser.add_argument("--aspects", type=Path, required=True)
    parser.add_argument("--dev", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--contract", type=Path, required=True)
    args = parser.parse_args()
    report, dataset, eligibility = evaluate(args.index, args.categories, args.aspects, args.dev)
    print(materialize(
        args.output_dir, report, dataset, eligibility,
        runner_path=Path(__file__), contract_path=args.contract,
    ))


if __name__ == "__main__":
    main()
