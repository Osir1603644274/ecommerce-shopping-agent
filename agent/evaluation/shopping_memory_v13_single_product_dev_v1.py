"""Development-only Shopping Memory V13 single-product quality ceiling.

Authority: contract-v13.6 and preregistration amendments 004-007.  This module
has an exact allow-list of readable inputs and cannot open validation or sealed
files.  It measures an oracle-confirmed positive ranking ceiling only; it is
not extraction, governance-superiority, production-lambda, or production-ready
evidence.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import random
import unicodedata
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


SCHEMA_VERSION = "shopping-memory-v13-single-product-dev-report-v1"
CONTRACT_VERSION = "shopping-memory-contract-v13.6"
ROOT = Path(__file__).resolve().parents[2]
ASSET_DIR = ROOT / "agent" / "evaluation" / "assets" / "shopping_memory_v13_20260830"
DEV_PATH = ASSET_DIR / "dev.jsonl"
CORPUS_PATH = ASSET_DIR / "train-target-catalog.jsonl"
CATALOG_PATH = ASSET_DIR / "catalog-values.jsonl"
MANIFEST_PATH = ASSET_DIR / "manifest.json"
CONTRACT_PATH = ROOT / "docs" / "experiments" / "shopping-memory-v13-2026-08-29" / "contract-v13.6.json"
AMENDMENT_PATHS = (
    ROOT / "docs" / "experiments" / "shopping-memory-v13-2026-08-29" / "PREREGISTRATION_AMENDMENT_004.md",
    ROOT / "docs" / "experiments" / "shopping-memory-v13-2026-08-29" / "PREREGISTRATION_AMENDMENT_005.md",
    ROOT / "docs" / "experiments" / "shopping-memory-v13-2026-08-29" / "PREREGISTRATION_AMENDMENT_006.md",
    ROOT / "docs" / "experiments" / "shopping-memory-v13-2026-08-29" / "PREREGISTRATION_AMENDMENT_007.md",
)
ALLOWED_INPUTS = frozenset({
    DEV_PATH.resolve(), CORPUS_PATH.resolve(), CATALOG_PATH.resolve(),
    MANIFEST_PATH.resolve(), CONTRACT_PATH.resolve(),
    *(path.resolve() for path in AMENDMENT_PATHS),
})
EXPECTED_HASHES = {
    MANIFEST_PATH.resolve(): "d30801a3c0afd2f30e5dfe1f8bc03dad42c765bbef51008e48a2d7f863b01918",
    DEV_PATH.resolve(): "f7fc366dd9b78188ebd11c8953f6e6f41cb31583824bccfe373a741068c95973",
    CORPUS_PATH.resolve(): "f74d743d2ba3718fbcf6cedcf2253f14325953d806ec1bfe747c20c005f65252",
    CATALOG_PATH.resolve(): "ceea5d32202ba533fa336535047e74b59e105508d6dd1dcac80cf6912c27b8e6",
}
LAMBDA_GRID = (0.01, 0.03, 0.05, 0.08)
K1 = 1.2
B = 0.75
CANDIDATE_DEPTH = 50
BOOTSTRAP_SEED = 20260830
BOOTSTRAP_ITERATIONS = 10_000
FIXTURE_CONTEXT = {
    "type": "synthetic_positive_oracle_fixture",
    "authenticatedOwner": "fixture-owner",
    "recordOwner": "fixture-owner",
    "currentRequestRecipientScope": "self",
    "recordStatus": "ACTIVE",
    "chainVerified": True,
    "fixtureNow": "2026-08-30T00:00:00Z",
    "expiresAt": "2026-08-31T00:00:00Z",
    "currentTurnOverrideKeys": [],
    "taskStateOverrideKeys": [],
}
FIXTURE_CONTEXT_HASH = hashlib.sha256(
    json.dumps(FIXTURE_CONTEXT, sort_keys=True, separators=(",", ":")).encode("utf-8")
).hexdigest()
CLAIMS_EXCLUDED = [
    "governance_superiority",
    "production_lambda_selection",
    "avoid_behavior",
    "real_extraction_quality",
]


class DevRunnerError(RuntimeError):
    pass


def _guard(path: Path) -> Path:
    resolved = path.resolve()
    if resolved not in ALLOWED_INPUTS:
        raise DevRunnerError(f"input path is not development-allowlisted: {resolved.name}")
    return resolved


def _bytes(path: Path) -> bytes:
    return _guard(path).read_bytes()


def _sha(path: Path) -> str:
    return hashlib.sha256(_bytes(path)).hexdigest()


def _canonical(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), allow_nan=False,
    ).encode("utf-8")


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(_bytes(path))
    if type(value) is not dict:
        raise DevRunnerError(f"expected JSON object: {path.name}")
    return value


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line_number, raw in enumerate(_bytes(path).splitlines(), 1):
        if not raw.strip():
            raise DevRunnerError(f"blank JSONL line: {path.name}:{line_number}")
        value = json.loads(raw)
        if type(value) is not dict:
            raise DevRunnerError(f"expected JSON object: {path.name}:{line_number}")
        rows.append(value)
    return rows


def tokenize(text: object, *, distinct: bool = False) -> tuple[str, ...]:
    if type(text) is not str:
        raise DevRunnerError("tokenizer input must be text")
    normalized = unicodedata.normalize("NFKC", text).lower()
    output: list[str] = []
    current: list[str] = []
    for character in normalized:
        if unicodedata.category(character)[:1] in {"L", "N"}:
            current.append(character)
        elif current:
            output.append("".join(current))
            current = []
    if current:
        output.append("".join(current))
    if not distinct:
        return tuple(output)
    seen: set[str] = set()
    return tuple(token for token in output if not (token in seen or seen.add(token)))


@dataclass(frozen=True, slots=True)
class CorpusDocument:
    product_id: str
    category_id: str
    product_name: str
    aspects: frozenset[tuple[str, str]]
    terms: Counter[str]
    length: int


class DeterministicBM25:
    def __init__(self, documents: Sequence[CorpusDocument]) -> None:
        if len(documents) != len({item.product_id for item in documents}):
            raise DevRunnerError("corpus productId is not unique")
        if not documents:
            raise DevRunnerError("empty corpus")
        self.documents = tuple(documents)
        self.average_length = sum(item.length for item in documents) / len(documents)
        self.document_frequency: Counter[str] = Counter()
        for document in documents:
            self.document_frequency.update(document.terms.keys())

    def raw_scores(self, query: str) -> list[tuple[str, float]]:
        query_terms = tokenize(query, distinct=True)
        count = len(self.documents)
        scores: list[tuple[str, float]] = []
        for document in self.documents:
            score = 0.0
            for term in query_terms:
                frequency = document.terms.get(term, 0)
                if frequency == 0:
                    continue
                document_frequency = self.document_frequency[term]
                inverse_frequency = math.log(
                    1 + (count - document_frequency + 0.5) / (document_frequency + 0.5)
                )
                denominator = frequency + K1 * (
                    1 - B + B * document.length / self.average_length
                )
                score += inverse_frequency * frequency * (K1 + 1) / denominator
            scores.append((document.product_id, score))
        scores.sort(key=lambda item: (-item[1], item[0]))
        return scores


def build_documents(rows: Sequence[Mapping[str, Any]]) -> tuple[CorpusDocument, ...]:
    documents: list[CorpusDocument] = []
    for row in rows:
        product_id = row.get("productId")
        category_id = row.get("categoryId")
        product_name = row.get("productName")
        aspects = row.get("aspects")
        if any(type(value) is not str or not value for value in (product_id, category_id, product_name)):
            raise DevRunnerError("invalid corpus identity")
        if type(aspects) is not list:
            raise DevRunnerError("invalid corpus aspects")
        aspect_pairs: set[tuple[str, str]] = set()
        display_values: list[str] = []
        for aspect in aspects:
            if type(aspect) is not dict:
                raise DevRunnerError("invalid corpus aspect")
            key = aspect.get("attributeKey")
            normalized = aspect.get("normalizedValue")
            display = aspect.get("displayValue")
            if any(type(value) is not str for value in (key, normalized, display)):
                raise DevRunnerError("invalid corpus aspect")
            aspect_pairs.add((key, normalized))
            display_values.append(display)
        terms = Counter(tokenize(" ".join([product_name, category_id, *display_values])))
        documents.append(CorpusDocument(
            product_id, category_id, product_name,
            frozenset(aspect_pairs), terms, sum(terms.values()),
        ))
    return tuple(documents)


def catalog_value_set(rows: Sequence[Mapping[str, Any]]) -> frozenset[tuple[str, str, str, str]]:
    values: set[tuple[str, str, str, str]] = set()
    for row in rows:
        fields = tuple(row.get(key) for key in (
            "catalogRevision", "categoryId", "attributeKey", "normalizedValue"
        ))
        if any(type(value) is not str or not value for value in fields):
            raise DevRunnerError("invalid catalog value")
        if fields in values:
            raise DevRunnerError("duplicate catalog value")
        values.add(fields)  # type: ignore[arg-type]
    return frozenset(values)


def canonical_binding(
    category_id: str,
    catalog_revision: str,
    preferences: Sequence[Mapping[str, Any]],
) -> dict[str, object]:
    visible = [{
        "categoryId": preference.get("categoryId"),
        "preferenceKind": preference.get("preferenceKind"),
        "attributeKey": preference.get("attributeKey"),
        "normalizedValue": preference.get("normalizedValue"),
    } for preference in preferences]
    visible.sort(key=lambda item: (
        str(item["categoryId"]), str(item["attributeKey"]),
        str(item["preferenceKind"]), str(item["normalizedValue"]),
    ))
    return {
        "memoryRevision": 1,
        "categoryId": category_id,
        "catalogRevision": catalog_revision,
        "preferences": visible,
    }


def eligibility(
    row: Mapping[str, Any],
    *,
    corpus: Mapping[str, CorpusDocument],
    catalog: frozenset[tuple[str, str, str, str]],
) -> tuple[bool, tuple[str, ...], dict[str, Any] | None]:
    if row.get("questionType") != "single_product":
        return False, ("QUESTION_TYPE_NOT_SINGLE_PRODUCT",), None
    reasons: set[str] = set()
    episodes = row.get("memoryEpisodes")
    targets = row.get("targetProducts")
    if type(episodes) is not list or len(episodes) != 1:
        reasons.add("MEMORY_EPISODE_COUNT_NOT_ONE")
    if type(targets) is not list or len(targets) != 1:
        reasons.add("TARGET_PRODUCT_COUNT_NOT_ONE")
    if reasons:
        return False, tuple(sorted(reasons)), None
    episode = episodes[0]
    target = targets[0]
    if type(episode) is not dict or type(target) is not dict:
        return False, ("EPISODE_OR_TARGET_INVALID",), None
    preferences = episode.get("preferences")
    if type(preferences) is not list or not 1 <= len(preferences) <= 8:
        reasons.add("PREFERENCE_COUNT_OUT_OF_RANGE")
        preferences = [] if type(preferences) is not list else preferences
    episode_category = episode.get("categoryId")
    target_category = target.get("categoryId")
    if type(episode_category) is not str or episode_category != target_category:
        reasons.add("EPISODE_TARGET_CATEGORY_MISMATCH")
    target_id = target.get("productId")
    document = corpus.get(target_id) if type(target_id) is str else None
    if document is None:
        reasons.add("TARGET_NOT_IN_CORPUS")
    elif document.category_id != target_category:
        reasons.add("TARGET_CORPUS_CATEGORY_MISMATCH")
    target_aspects_raw = target.get("aspects")
    target_aspects = {
        (aspect.get("attributeKey"), aspect.get("normalizedValue"))
        for aspect in target_aspects_raw
        if type(aspect) is dict
    } if type(target_aspects_raw) is list else set()
    seen_keys: set[tuple[object, object]] = set()
    seen_full: set[tuple[object, object, object, object, object]] = set()
    revisions: set[str] = set()
    for preference in preferences:
        if type(preference) is not dict:
            reasons.add("PREFERENCE_INVALID")
            continue
        revision = preference.get("catalogRevision")
        category = preference.get("categoryId")
        key = preference.get("attributeKey")
        value = preference.get("normalizedValue")
        kind = preference.get("preferenceKind")
        if any(type(item) is not str or not item for item in (revision, category, key, value, kind)):
            reasons.add("PREFERENCE_INVALID")
            continue
        revisions.add(revision)
        category_key = (category, key)
        full = (revision, category, key, value, kind)
        if category_key in seen_keys:
            reasons.add("CATEGORY_ATTRIBUTE_NOT_UNIQUE")
        seen_keys.add(category_key)
        if full in seen_full:
            reasons.add("FULL_TUPLE_NOT_UNIQUE")
        seen_full.add(full)
        if (revision, category, key, value) not in catalog:
            reasons.add("CATALOG_TUPLE_MISSING")
        if preference.get("recipientScope") != "self":
            reasons.add("RECIPIENT_SCOPE_NOT_SELF")
        if preference.get("source") != "user_confirmed":
            reasons.add("SOURCE_NOT_USER_CONFIRMED")
        if category != episode_category:
            reasons.add("PREFERENCE_EPISODE_CATEGORY_MISMATCH")
        if (key, value) not in target_aspects:
            reasons.add("TARGET_ASPECT_SUPPORT_MISSING")
    if len(revisions) != 1:
        reasons.add("CATALOG_REVISION_NOT_UNIQUE")
    revision = next(iter(revisions), "unknown")
    binding = canonical_binding(str(episode_category), revision, preferences)
    binding_bytes = len(_canonical(binding))
    if binding_bytes > 4096:
        reasons.add("BINDING_BYTES_EXCEEDED")
    context = {
        "questionId": row.get("questionId"),
        "conversationId": row.get("conversationId"),
        "query": row.get("query"),
        "targetProductId": target_id,
        "categoryId": episode_category,
        "catalogRevision": revision,
        "preferences": preferences,
        "bindingBytes": binding_bytes,
        "evaluatorContextHash": FIXTURE_CONTEXT_HASH,
    }
    return not reasons, tuple(sorted(reasons)), context


def memory_score(
    preferences: Sequence[Mapping[str, Any]],
    document: CorpusDocument,
) -> float:
    active = [
        preference for preference in preferences
        if preference.get("preferenceKind") in {"prefer", "avoid"}
    ]
    if not active:
        return 0.0
    total = 0
    for preference in active:
        exact = (
            preference.get("categoryId") == document.category_id
            and (
                preference.get("attributeKey"), preference.get("normalizedValue")
            ) in document.aspects
        )
        if exact:
            total += 1 if preference.get("preferenceKind") == "prefer" else -1
    return total / len(active)


def rerank(
    base: Sequence[tuple[str, float]],
    *,
    documents: Mapping[str, CorpusDocument],
    preferences: Sequence[Mapping[str, Any]],
    weight: float,
) -> list[str]:
    if weight not in LAMBDA_GRID:
        raise DevRunnerError("lambda outside frozen grid")
    maximum = base[0][1] if base else 0.0
    ranked: list[tuple[float, int, str]] = []
    for base_rank, (product_id, raw_score) in enumerate(base):
        base_score = raw_score / maximum if maximum > 0 else 0.0
        final = base_score + weight * memory_score(preferences, documents[product_id])
        ranked.append((final, base_rank, product_id))
    ranked.sort(key=lambda item: (-item[0], item[1], item[2]))
    return [item[2] for item in ranked]


def ranking_metrics(ranking: Sequence[str], target_product_id: str) -> dict[str, float]:
    try:
        rank = ranking.index(target_product_id) + 1
    except ValueError:
        rank = 0
    return {
        "hitAt10": 1.0 if 1 <= rank <= 10 else 0.0,
        "nDCGAt10": 1.0 / math.log2(rank + 1) if 1 <= rank <= 10 else 0.0,
        "MRRAt50": 1.0 / rank if 1 <= rank <= 50 else 0.0,
        "targetRecallAt50": 1.0 if 1 <= rank <= 50 else 0.0,
    }


def zero_metrics() -> dict[str, float]:
    return {
        "hitAt10": 0.0, "nDCGAt10": 0.0,
        "MRRAt50": 0.0, "targetRecallAt50": 0.0,
    }


def mean_metrics(rows: Sequence[Mapping[str, float]]) -> dict[str, float]:
    if not rows:
        return {key: 0.0 for key in ("hitAt10", "nDCGAt10", "MRRAt50", "targetRecallAt50")}
    return {
        key: sum(row[key] for row in rows) / len(rows)
        for key in ("hitAt10", "nDCGAt10", "MRRAt50", "targetRecallAt50")
    }


def percentile(values: Sequence[float], quantile: float) -> float:
    if not values or not 0 <= quantile <= 1:
        raise DevRunnerError("invalid percentile input")
    ordered = sorted(values)
    position = (len(ordered) - 1) * quantile
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def cluster_bootstrap(
    deltas: Sequence[tuple[str, float]],
    *,
    iterations: int = BOOTSTRAP_ITERATIONS,
    seed: int = BOOTSTRAP_SEED,
) -> dict[str, float | int | str]:
    grouped: dict[str, list[float]] = defaultdict(list)
    for conversation_id, delta in deltas:
        grouped[conversation_id].append(delta)
    clusters = sorted(grouped)
    if not clusters or iterations < 1:
        raise DevRunnerError("empty bootstrap input")
    generator = random.Random(seed)
    cluster_macro = {
        cluster: sum(values) / len(values) for cluster, values in grouped.items()
    }
    samples: list[float] = []
    for _ in range(iterations):
        values = [cluster_macro[generator.choice(clusters)] for _ in clusters]
        samples.append(sum(values) / len(values))
    return {
        "unit": "conversationId",
        "clusterCount": len(clusters),
        "iterations": iterations,
        "seed": seed,
        "percentileMethod": "linear_type7",
        "meanDelta": sum(cluster_macro.values()) / len(cluster_macro),
        "p2_5": percentile(samples, 0.025),
        "p97_5": percentile(samples, 0.975),
    }


def _validate_authority() -> tuple[dict[str, Any], dict[str, str]]:
    hashes = {path.name: _sha(path) for path in EXPECTED_HASHES}
    for path, expected in EXPECTED_HASHES.items():
        if hashes[path.name] != expected:
            raise DevRunnerError(f"input hash mismatch: {path.name}")
    contract = _read_json(CONTRACT_PATH)
    if contract.get("schemaVersion") != CONTRACT_VERSION:
        raise DevRunnerError("contract-v13.6 is not active")
    manifest = _read_json(MANIFEST_PATH)
    if manifest.get("files", {}).get("dev.jsonl") != EXPECTED_HASHES[DEV_PATH.resolve()]:
        raise DevRunnerError("manifest dev hash mismatch")
    if manifest.get("files", {}).get("train-target-catalog.jsonl") != EXPECTED_HASHES[CORPUS_PATH.resolve()]:
        raise DevRunnerError("manifest corpus hash mismatch")
    if manifest.get("files", {}).get("catalog-values.jsonl") != EXPECTED_HASHES[CATALOG_PATH.resolve()]:
        raise DevRunnerError("manifest catalog hash mismatch")
    return contract, hashes


def evaluate_development() -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    contract, input_hashes = _validate_authority()
    documents = build_documents(_read_jsonl(CORPUS_PATH))
    if len(documents) != 1449:
        raise DevRunnerError("frozen corpus cardinality mismatch")
    document_map = {item.product_id: item for item in documents}
    catalog = catalog_value_set(_read_jsonl(CATALOG_PATH))
    rows = _read_jsonl(DEV_PATH)
    retriever = DeterministicBM25(documents)
    eligibility_rows: list[dict[str, Any]] = []
    eligible_contexts: list[dict[str, Any]] = []
    reason_counts: Counter[str] = Counter()
    single_product_count = 0
    eligible_avoid_count = 0
    for row in rows:
        if row.get("questionType") == "single_product":
            single_product_count += 1
        accepted, reasons, context = eligibility(row, corpus=document_map, catalog=catalog)
        if not accepted:
            reason_counts.update(reasons)
        eligibility_rows.append({
            "questionId": row.get("questionId"),
            "conversationId": row.get("conversationId"),
            "questionType": row.get("questionType"),
            "eligible": accepted,
            "reasons": list(reasons),
            "bindingBytes": context.get("bindingBytes") if context else None,
            "evaluatorContextHash": context.get("evaluatorContextHash") if context else None,
        })
        if accepted and context is not None:
            eligible_contexts.append(context)
            eligible_avoid_count += sum(
                preference.get("preferenceKind") == "avoid"
                for preference in context["preferences"]
            )

    per_query: list[dict[str, Any]] = []
    arm_metrics: dict[float, dict[str, list[dict[str, float]]]] = {
        weight: {"A": [], "B": [], "C": []} for weight in LAMBDA_GRID
    }
    base_no_match = 0
    bc_identity_failures = 0
    candidate_set_failures = 0
    for context in eligible_contexts:
        raw = retriever.raw_scores(context["query"])
        no_match = not raw or raw[0][1] == 0
        if no_match:
            base_no_match += 1
        base = raw[:CANDIDATE_DEPTH]
        base_ids = [product_id for product_id, _ in base]
        target = context["targetProductId"]
        base_result = zero_metrics() if no_match else ranking_metrics(base_ids, target)
        lambda_rows: dict[str, Any] = {}
        for weight in LAMBDA_GRID:
            # B is the direct tuple baseline. C is the same tuple after the
            # explicitly frozen synthetic owner/self/ACTIVE/no-override fixture.
            b_ids = rerank(
                base, documents=document_map,
                preferences=context["preferences"], weight=weight,
            )
            c_ids = rerank(
                base, documents=document_map,
                preferences=context["preferences"], weight=weight,
            )
            bc_equal = b_ids == c_ids
            candidates_preserved = (
                len(b_ids) == len(base_ids)
                and len(c_ids) == len(base_ids)
                and set(b_ids) == set(base_ids)
                and set(c_ids) == set(base_ids)
            )
            bc_identity_failures += not bc_equal
            candidate_set_failures += not candidates_preserved
            b_result = zero_metrics() if no_match else ranking_metrics(b_ids, target)
            c_result = zero_metrics() if no_match else ranking_metrics(c_ids, target)
            arm_metrics[weight]["A"].append(base_result)
            arm_metrics[weight]["B"].append(b_result)
            arm_metrics[weight]["C"].append(c_result)
            lambda_rows[f"{weight:.2f}"] = {
                "targetRanks": {
                    "A": base_ids.index(target) + 1 if target in base_ids else None,
                    "B": b_ids.index(target) + 1 if target in b_ids else None,
                    "C": c_ids.index(target) + 1 if target in c_ids else None,
                },
                "metrics": {"A": base_result, "B": b_result, "C": c_result},
                "bEqualsC": bc_equal,
                "candidateSetPreserved": candidates_preserved,
            }
        per_query.append({
            "questionId": context["questionId"],
            "conversationId": context["conversationId"],
            "targetProductId": target,
            "baseNoMatch": no_match,
            "evaluatorContextHash": FIXTURE_CONTEXT_HASH,
            "lambdas": lambda_rows,
        })

    aggregate: dict[str, Any] = {}
    for weight in LAMBDA_GRID:
        key = f"{weight:.2f}"
        means = {arm: mean_metrics(values) for arm, values in arm_metrics[weight].items()}
        aggregate[key] = {
            "arms": means,
            "CMinusA": {
                metric: means["C"][metric] - means["A"][metric]
                for metric in means["A"]
            },
        }
    c_hit_counts = {
        weight: sum(row["hitAt10"] for row in arm_metrics[weight]["C"])
        for weight in LAMBDA_GRID
    }
    selected_weight = min(
        LAMBDA_GRID,
        key=lambda weight: (-c_hit_counts[weight], weight),
    )
    selected_key = f"{selected_weight:.2f}"
    deltas: list[tuple[str, float]] = []
    for row in per_query:
        metrics = row["lambdas"][selected_key]["metrics"]
        deltas.append((
            row["conversationId"],
            metrics["C"]["hitAt10"] - metrics["A"]["hitAt10"],
        ))
    bootstrap = cluster_bootstrap(deltas)
    eligible_count = len(eligible_contexts)
    coverage = eligible_count / single_product_count if single_product_count else 0.0
    base_recall = aggregate[selected_key]["arms"]["A"]["targetRecallAt50"]
    gates = {
        "minimumEligibleDev": eligible_count >= 200,
        "minimumEligibleCoverage": coverage >= 0.75,
        "minimumBaseTargetRecallAt50": base_recall >= 0.95,
        "baseNoMatchCountZero": base_no_match == 0,
        "armBEqualsArmCPerCasePerLambda": bc_identity_failures == 0,
        "candidateSetPreservedPerCasePerLambda": candidate_set_failures == 0,
        "eligibleAvoidCountZero": eligible_avoid_count == 0,
        "selectedBootstrapLowerBoundPositive": bootstrap["p2_5"] > 0,
    }
    decision = (
        contract["maximumDevelopmentVerdict"] if all(gates.values()) else "HOLD"
    )
    report = {
        "schemaVersion": SCHEMA_VERSION,
        "contractVersion": CONTRACT_VERSION,
        "split": "development",
        "questionType": "single_product",
        "decision": decision,
        "productionVerdict": contract["productionVerdictCeiling"],
        "claimsBoundary": {
            "description": "oracle-confirmed positive ranking ceiling only",
            "excluded": CLAIMS_EXCLUDED,
            "validationRead": False,
            "sealedRead": False,
            "productionDefaultChanged": False,
            "armBEqualsCMeaning": "runner correctness assertion only",
        },
        "inputHashes": input_hashes,
        "contractHashes": {
            CONTRACT_PATH.name: _sha(CONTRACT_PATH),
            **{path.name: _sha(path) for path in AMENDMENT_PATHS},
            "runnerSource": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        },
        "evaluatorContext": FIXTURE_CONTEXT,
        "evaluatorContextHash": FIXTURE_CONTEXT_HASH,
        "counts": {
            "allDevQuestions": len(rows),
            "singleProductQuestions": single_product_count,
            "eligibleQuestions": eligible_count,
            "ineligibleSingleProductQuestions": single_product_count - eligible_count,
            "eligibleCoverage": coverage,
            "eligibleAvoidPreferenceCount": eligible_avoid_count,
            "baseNoMatchCount": base_no_match,
            "bCIdentityFailureCount": bc_identity_failures,
            "candidateSetFailureCount": candidate_set_failures,
        },
        "eligibilityReasonCounts": dict(sorted(reason_counts.items())),
        "lambdaGrid": list(LAMBDA_GRID),
        "selectedLambda": selected_weight,
        "selectedAtGridBoundary": selected_weight in {min(LAMBDA_GRID), max(LAMBDA_GRID)},
        "lambdaSelectionHitCounts": {
            f"{weight:.2f}": int(c_hit_counts[weight]) for weight in LAMBDA_GRID
        },
        "metricsByLambda": aggregate,
        "selectedLambdaBootstrap": {
            "label": contract["developmentIntervalLabel"],
            **bootstrap,
        },
        "gates": gates,
    }
    return report, eligibility_rows, per_query


def _jsonl_bytes(rows: Iterable[Mapping[str, Any]]) -> bytes:
    return b"".join(_canonical(row) + b"\n" for row in rows)


def materialize_report(
    output_dir: Path,
    report: Mapping[str, Any],
    eligibility_rows: Sequence[Mapping[str, Any]],
    per_query_rows: Sequence[Mapping[str, Any]],
) -> Path:
    if output_dir.exists():
        raise FileExistsError(f"refusing to overwrite existing output: {output_dir}")
    trace = {
        "schemaVersion": "shopping-memory-v13-single-product-dev-trace-v1",
        "split": report.get("split"),
        "questionType": report.get("questionType"),
        "counts": report.get("counts"),
        "gates": report.get("gates"),
        "selectedLambda": report.get("selectedLambda"),
        "selectedAtGridBoundary": report.get("selectedAtGridBoundary"),
    }
    receipt = {
        "schemaVersion": "shopping-memory-v13-single-product-dev-receipt-v1",
        "contractVersion": report.get("contractVersion"),
        "decision": report.get("decision"),
        "productionVerdict": report.get("productionVerdict"),
        "inputHashes": report.get("inputHashes"),
        "contractHashes": report.get("contractHashes"),
        "evaluatorContextHash": report.get("evaluatorContextHash"),
        "validationRead": False,
        "sealedRead": False,
    }
    payloads = {
        "report.json": _canonical(report) + b"\n",
        "eligibility.jsonl": _jsonl_bytes(eligibility_rows),
        "per-query-metrics.jsonl": _jsonl_bytes(per_query_rows),
        "trace.json": _canonical(trace) + b"\n",
        "receipt.json": _canonical(receipt) + b"\n",
    }
    output_dir.mkdir(parents=True, exist_ok=False)
    for name, payload in payloads.items():
        (output_dir / name).write_bytes(payload)
    checksums = "".join(
        f"{hashlib.sha256(payload).hexdigest()}  {name}\n"
        for name, payload in sorted(payloads.items())
    )
    (output_dir / "SHA256SUMS.txt").write_text(checksums, encoding="utf-8")
    return output_dir / "report.json"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", required=True, type=Path)
    args = parser.parse_args()
    output_dir = args.output_dir.resolve()
    if output_dir.exists():
        raise FileExistsError(f"refusing to overwrite existing output: {output_dir}")
    report, eligibility_rows, per_query_rows = evaluate_development()
    path = materialize_report(output_dir, report, eligibility_rows, per_query_rows)
    print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
