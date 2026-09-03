"""Repair and blind the 439-item retrieval experiment review package.

The raw ranking grid remains owned by ``used_phone_439_retrieval_grid_v1``.
This companion freezes TaskState intent semantics, adds price/evidence fields,
creates two independently shuffled blind review packs, records replayable dense
latencies, and performs strict Cartesian-grid/hash/statistic validation.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
from pathlib import Path
from typing import Any, Iterable, Mapping

import httpx

from agent.evaluation import used_phone_439_retrieval_grid_v1 as grid


ROOT = grid.ROOT
DEFAULT_RUN_DIR = (
    ROOT / "agent/evaluation/runs/used_phone_439_retrieval_grid_v1_20260828_attempt002"
)
REVIEWER_SEEDS = {
    "reviewer01": "used-phone-439-reviewer01-20260828-v2",
    "reviewer02": "used-phone-439-reviewer02-20260828-v2",
}


def _req(
    key: str,
    operator: str,
    value: object,
    unit: str,
    source_text: str,
) -> dict[str, Any]:
    return {
        "key": key,
        "operator": operator,
        "value": value,
        "unit": unit,
        "priority": "hard",
        "source": "user",
        "sourceText": source_text,
        "unknownPolicy": "not_eligible",
    }


HARD_REQUIREMENTS: dict[str, list[dict[str, Any]]] = {
    "uphqv2-002": [_req("price_minor", "lte", 50000, "CNY_MINOR", "500元以内")],
    "uphqv2-003": [_req("price_minor", "lte", 200000, "CNY_MINOR", "两千以内")],
    "uphqv2-004": [],
    "uphqv2-005": [],
    "uphqv2-006": [
        _req("price_minor", "gte", 20000, "CNY_MINOR", "200到300"),
        _req("price_minor", "lte", 30000, "CNY_MINOR", "200到300"),
    ],
    "uphqv2-007": [
        _req("brand", "eq", "vivo", "text", "vivo"),
        _req("screen_originality", "eq", "original", "enum", "原装屏幕"),
    ],
    "uphqv2-008": [
        _req("brand", "eq", "apple", "text", "苹果手机"),
        _req("os", "eq", "ios", "enum", "苹果手机"),
    ],
    "uphqv2-009": [_req("brand", "eq", "redmi", "text", "红米")],
    "uphqv2-010": [],
    "uphqv2-011": [_req("brand", "eq", "huawei", "text", "华为")],
    "uphqv2-012": [],
    "uphqv2-013": [_req("brand", "eq", "vivo", "text", "vivo")],
    "uphqv2-014": [],
    "uphqv2-015": [_req("os", "eq", "android", "enum", "安卓")],
    "uphqv2-016": [_req("brand", "eq", "apple", "text", "苹果")],
    "uphqv2-017": [],
    "uphqv2-018": [_req("brand", "eq", "iqoo", "text", "iqoo")],
    "uphqv2-019": [_req("price_minor", "lte", 30000, "CNY_MINOR", "推荐300")],
    "uphqv2-020": [],
    "uphqv2-021": [],
    "uphqv2-022": [],
    "uphqv2-023": [_req("price_minor", "lte", 80000, "CNY_MINOR", "800元以内")],
    "uphqv2-024": [_req("brand", "eq", "xiaomi", "text", "小米")],
    "uphqv2-025": [],
}


SOFT_PREFERENCES: dict[str, list[dict[str, Any]]] = {
    "uphqv2-002": [{"key": "form_factor", "value": "small_screen", "sourceText": "小屏"}],
    "uphqv2-003": [{"key": "value", "value": "high", "sourceText": "性价比最高"}],
    "uphqv2-004": [
        {"key": "use_case", "value": "cashier", "sourceText": "收银"},
        {"key": "use_case", "value": "camera", "sourceText": "拍照好"},
        {"key": "use_case", "value": "gaming", "sourceText": "打游戏流畅"},
    ],
    "uphqv2-005": [{"key": "audience", "value": "college_student", "sourceText": "大学生"}],
    "uphqv2-006": [{"key": "use_case", "value": "gaming", "sourceText": "游戏手机"}],
    "uphqv2-007": [{"key": "model_text", "value": "x80", "sourceText": "x80"}],
    "uphqv2-008": [{"key": "price_target_minor", "value": 10000, "sourceText": "100元左右"}],
    "uphqv2-009": [
        {"key": "model_text", "value": "k50 pro", "sourceText": "k50pro"},
        {"key": "use_case", "value": "gaming", "sourceText": "游戏手机"},
    ],
    "uphqv2-010": [{"key": "display", "value": "high_refresh", "sourceText": "高帧"}],
    "uphqv2-011": [
        {"key": "value", "value": "high", "sourceText": "性价比"},
        {"key": "use_case", "value": "gaming", "sourceText": "打游戏"},
    ],
    "uphqv2-012": [
        {"key": "price_target_minor", "value": 100000, "sourceText": "1000左右"},
        {"key": "use_case", "value": "gaming", "sourceText": "游戏"},
    ],
    "uphqv2-013": [
        {"key": "price_target_minor", "value": 100000, "sourceText": "千元"},
        {"key": "value", "value": "high", "sourceText": "性价比"},
        {"key": "negative_use_case", "value": "gaming", "sourceText": "不玩游戏"},
    ],
    "uphqv2-014": [
        {"key": "use_case", "value": "backup", "sourceText": "备用机"},
        {"key": "battery", "value": "large_capacity", "sourceText": "大容量电池"},
        {"key": "audience", "value": "student", "sourceText": "学生党"},
    ],
    "uphqv2-015": [{"key": "audience", "value": "student", "sourceText": "学生党"}],
    "uphqv2-016": [
        {"key": "model_text", "value": "11", "sourceText": "苹果11"},
        {"key": "use_case", "value": "backup", "sourceText": "备用机"},
    ],
    "uphqv2-017": [
        {"key": "audio", "value": "loud", "sourceText": "声音大"},
        {"key": "audio", "value": "good_quality", "sourceText": "音质好"},
    ],
    "uphqv2-018": [
        {"key": "use_case", "value": "gaming", "sourceText": "游戏手机"},
        {"key": "price_target_minor", "value": 100000, "sourceText": "1000左右"},
    ],
    "uphqv2-019": [{"key": "battery", "value": "long_life", "sourceText": "耗电慢"}],
    "uphqv2-020": [
        {"key": "price_phrase", "value": "1500_plus", "sourceText": "1500多"},
        {"key": "camera", "value": "good_pixels", "sourceText": "像素好"},
    ],
    "uphqv2-021": [
        {"key": "use_case", "value": "backup", "sourceText": "备用机"},
        {"key": "audience", "value": "student", "sourceText": "学生党"},
        {"key": "price", "value": "cheap", "sourceText": "便宜"},
        {"key": "memory", "value": "large", "sourceText": "内存大"},
        {"key": "reviews", "value": "many_positive", "sourceText": "好评多"},
    ],
    "uphqv2-022": [
        {"key": "memory_text", "value": "12gb", "sourceText": "12gb"},
        {"key": "storage_text", "value": "512gb", "sourceText": "512gb"},
    ],
    "uphqv2-023": [
        {"key": "value", "value": "high", "sourceText": "性价比最高"},
        {"key": "use_case", "value": "gaming", "sourceText": "游戏"},
    ],
    "uphqv2-024": [{"key": "value", "value": "high", "sourceText": "性价比"}],
    "uphqv2-025": [{"key": "chipset_text", "value": "snapdragon_8_gen_1", "sourceText": "骁龙8gen1"}],
}


UNSUPPORTED_STRUCTURED_EVIDENCE = {
    "uphqv2-002": ["form_factor"],
    "uphqv2-004": ["cashier_performance", "camera_performance", "gaming_fluency"],
    "uphqv2-005": ["audience_suitability"],
    "uphqv2-006": ["gaming_performance"],
    "uphqv2-007": ["model_identity"],
    "uphqv2-009": ["model_identity", "gaming_performance"],
    "uphqv2-010": ["verified_refresh_rate_or_frame_rate"],
    "uphqv2-011": ["value_for_money", "gaming_performance"],
    "uphqv2-012": ["approximate_price", "gaming_performance"],
    "uphqv2-013": ["approximate_price", "value_for_money"],
    "uphqv2-014": ["battery_capacity", "audience_suitability"],
    "uphqv2-015": ["audience_suitability"],
    "uphqv2-016": ["model_identity", "backup_suitability"],
    "uphqv2-017": ["speaker_volume", "audio_quality"],
    "uphqv2-018": ["approximate_price", "gaming_performance"],
    "uphqv2-019": ["measured_battery_life"],
    "uphqv2-020": ["approximate_price", "camera_performance"],
    "uphqv2-021": ["memory_capacity", "review_count", "audience_suitability"],
    "uphqv2-022": ["memory_capacity", "storage_capacity"],
    "uphqv2-023": ["value_for_money", "gaming_performance"],
    "uphqv2-024": ["value_for_money"],
    "uphqv2-025": ["chipset_identity"],
}


def _file_pin(path: Path, *, rows: int | None = None) -> dict[str, Any]:
    pin: dict[str, Any] = {"bytes": path.stat().st_size, "sha256": grid.sha256(path)}
    if rows is not None:
        pin["rows"] = rows
    return pin


def _token(seed: str, *parts: object) -> str:
    value = "|".join((seed, *(str(part) for part in parts)))
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


def _taskstate_rows(queries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    ids = {str(row["scenarioId"]) for row in queries}
    if ids != set(HARD_REQUIREMENTS) or ids != set(SOFT_PREFERENCES):
        raise ValueError("frozen intent map does not cover exactly 24 queries")
    rows = []
    for query in queries:
        query_id = str(query["scenarioId"])
        rows.append({
            "schemaVersion": "used-phone-439-frozen-taskstate-intent-v2",
            "queryId": query_id,
            "query": query["rawQuery"],
            "split": query["split"],
            "category": "phone",
            "supportedHardRequirements": HARD_REQUIREMENTS[query_id],
            "explicitSoftPreferences": SOFT_PREFERENCES[query_id],
            "unsupportedStructuredEvidence": UNSUPPORTED_STRUCTURED_EVIDENCE.get(query_id, []),
            "policies": {
                "within": "hard_lte",
                "numericRange": "hard_gte_and_lte",
                "aroundOrPlus": "soft_only_no_silent_hard_ceiling",
                "hardUnknown": "not_eligible",
                "titleClaims": "retrieval_relevance_only_not_answer_fact",
                "price": "frozen_synthetic_reference_price_disclosed_not_market_price",
            },
            "status": "FROZEN_EXPERIMENT_INPUT",
        })
    return rows


def _review_contract() -> dict[str, Any]:
    return {
        "schemaVersion": "used-phone-439-retrieval-review-contract-v2",
        "status": "FROZEN_BEFORE_HUMAN_REVIEW",
        "independence": {
            "minimumReviewers": 2,
            "reviewersMustNotSeeMapping": True,
            "reviewersMustNotSeeEachOther": True,
            "modelPrelabelsForbidden": True,
            "adjudication": "exact_agreement_kept_else_project_owner_human_adjudicates",
        },
        "labels": {
            "3": "All supported hard requirements are known/pass and direct source evidence strongly matches the central query need.",
            "2": "All supported hard requirements are known/pass and the product is materially but only partially relevant.",
            "1": "Related phone, but relevance is weak, ambiguous, or supported only by an unverified seller-title claim.",
            "0": "Any supported hard requirement fails/is unknown, or the product is wrong-brand/model/configuration or unrelated.",
        },
        "evidencePolicy": {
            "controlledAttributes": "may_support_hard_eligibility",
            "syntheticReferencePrice": "may_support_frozen_demo_budget_only_with_disclosure",
            "titleAndRawAttributeText": "may_support retrieval relevance but never measured performance facts",
            "outsidePool": "UNJUDGED_NOT_NEGATIVE",
        },
        "metrics": {
            "names": ["pooledRecallAt50", "pooledNdcgAt10", "pooledHitAt3", "hardViolationRate", "p50Ms", "p95Ms"],
            "directoryRecallClaimForbidden": True,
            "unitOfAnalysis": "query",
        },
        "selection": {
            "development": (
                "Choose the arm with maximum mean pooledNdcgAt10 subject to zero hard violations; "
                "ties break by pooledRecallAt50, pooledHitAt3, lower p95, then fixed arm name."
            ),
            "validationGate": {
                "pooledNdcgAt10DeltaVsStandardEs": ">=0.02",
                "pooledRecallAt50DeltaVsStandardEs": ">=0",
                "pooledHitAt3DeltaVsStandardEs": ">=0",
                "hardViolationRate": 0,
                "p95Ms": "<=100",
                "pairedBootstrap95CiLowerForNdcgDelta": ">0",
            },
            "sealedTest": "one_shot_descriptive_confirmation_no_retuning",
            "productionDefault": (
                "HOLD unless validation passes, sealed has no safety regression, and two-human qrels plus adjudication are complete."
            ),
            "smallSampleWarning": "24 queries and 4 sealed queries cannot establish general superiority.",
        },
    }


def _price_and_catalog() -> tuple[dict[int, dict[str, Any]], dict[int, dict[str, Any]]]:
    prices = {int(row["itemId"]): row for row in grid.read_jsonl(grid.PRICE_PATH)}
    catalog = {int(row["itemId"]): row for row in grid.read_jsonl(grid.CATALOG_PATH)}
    if len(prices) != 439 or len(catalog) != 439 or set(prices) != set(catalog):
        raise ValueError("price/catalog 439 identity mismatch")
    return prices, catalog


def _enrich_pool(
    raw_pool: list[dict[str, Any]],
    prices: Mapping[int, Mapping[str, Any]],
    catalog: Mapping[int, Mapping[str, Any]],
) -> list[dict[str, Any]]:
    rows = []
    for row in raw_pool:
        candidates = []
        for candidate in row["candidates"]:
            product_id = int(candidate["productId"])
            price = prices[product_id]
            product = catalog[product_id]
            attributes = product.get("attributes") or {}
            candidates.append({
                **candidate,
                "syntheticReferencePriceMinor": int(price["referencePriceMinor"]),
                "priceStatus": "synthetic",
                "priceDisclosureZh": price["disclosureZh"],
                "controlledAttributes": {
                    key: {
                        "status": value.get("status"),
                        "value": value.get("value"),
                    }
                    for key, value in sorted(attributes.items())
                },
            })
        rows.append({**row, "candidates": candidates})
    return rows


def _blind_packs(
    enriched_pool: list[dict[str, Any]],
    taskstate_rows: list[dict[str, Any]],
) -> tuple[dict[str, list[dict[str, Any]]], dict[str, Any]]:
    intent_by_id = {row["queryId"]: row for row in taskstate_rows}
    packs: dict[str, list[dict[str, Any]]] = {}
    mapping: dict[str, Any] = {
        "schemaVersion": "used-phone-439-retrieval-blind-mapping-v2",
        "visibility": "PRIVATE_EVALUATOR_ONLY",
        "reviewers": {},
    }
    for reviewer, seed in REVIEWER_SEEDS.items():
        query_rows = list(enriched_pool)
        random.Random(_token(seed, "query-order")).shuffle(query_rows)
        public_rows = []
        reviewer_map: dict[str, Any] = {}
        for query_index, row in enumerate(query_rows, 1):
            query_id = str(row["queryId"])
            blind_query_id = f"BQ-{query_index:02d}-{_token(seed, query_id)[:6]}"
            candidates = list(row["candidates"])
            random.Random(_token(seed, query_id, "candidate-order")).shuffle(candidates)
            public_candidates = []
            candidate_map = {}
            for candidate in candidates:
                product_id = int(candidate["productId"])
                candidate_token = f"C-{_token(seed, query_id, product_id)}"
                candidate_map[candidate_token] = product_id
                public_candidates.append({
                    "candidateToken": candidate_token,
                    "title": candidate["title"],
                    "brand": candidate["brand"],
                    "syntheticReferencePriceMinor": candidate["syntheticReferencePriceMinor"],
                    "priceStatus": candidate["priceStatus"],
                    "priceDisclosureZh": candidate["priceDisclosureZh"],
                    "controlledAttributes": candidate["controlledAttributes"],
                    "rawAttributeText": candidate["attributeText"],
                    "review": {"relevance": None, "reason": None},
                })
            intent = intent_by_id[query_id]
            public_rows.append({
                "schemaVersion": "used-phone-439-retrieval-blind-item-v2",
                "blindQueryId": blind_query_id,
                "query": row["query"],
                "intent": {
                    "supportedHardRequirements": intent["supportedHardRequirements"],
                    "explicitSoftPreferences": intent["explicitSoftPreferences"],
                    "unsupportedStructuredEvidence": intent["unsupportedStructuredEvidence"],
                    "policies": intent["policies"],
                },
                "candidates": public_candidates,
            })
            reviewer_map[blind_query_id] = {
                "queryId": query_id,
                "split": row["split"],
                "candidates": candidate_map,
            }
        packs[reviewer] = public_rows
        mapping["reviewers"][reviewer] = reviewer_map
    return packs, mapping


def _dense_latency_rows(
    documents: list[dict[str, Any]], queries: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    rows = []
    for repeat in range(1, grid.REPEATS + 1):
        _, latency = grid.dense_rankings(documents, queries)
        for query_id, duration in latency["queryMs"].items():
            rows.append({
                "schemaVersion": "used-phone-439-dense-latency-trace-v2",
                "repeat": repeat,
                "queryId": query_id,
                "queryMs": round(float(duration), 3),
                "catalogBuildMs": round(float(latency["buildMs"]), 3),
            })
    return rows


def _runtime_snapshot() -> dict[str, Any]:
    state_path = ROOT / ".runtime/used-phone-demo/state.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    agent_state = state.get("agent") if isinstance(state.get("agent"), dict) else state
    web_status = None
    runtime: dict[str, Any] = {}
    index_count = None
    errors: list[str] = []
    with httpx.Client(timeout=5.0) as client:
        try:
            page = client.get("http://127.0.0.1:18000/")
            web_status = page.status_code
            runtime = client.get("http://127.0.0.1:18000/agent/runtime-status").json()
        except httpx.HTTPError as exc:
            errors.append(f"web:{type(exc).__name__}")
        try:
            index = client.get(
                "http://127.0.0.1:19281/_cat/indices/used-phone-demo-products-v1",
                params={"format": "json"},
            ).json()
            index_count = int(index[0]["docs.count"])
        except (httpx.HTTPError, IndexError, KeyError, TypeError, ValueError) as exc:
            errors.append(f"webElasticsearch:{type(exc).__name__}")
    return {
        "schemaVersion": "used-phone-439-retrieval-web-runtime-snapshot-v2",
        "webReachable": web_status == 200,
        "webHttpStatus": web_status,
        "controlPolicy": runtime.get("controlPolicy") or agent_state.get("controlRuntime"),
        "reactLive": runtime.get("reactLive"),
        "rollbackPolicy": runtime.get("rollbackPolicy"),
        "backendPort": agent_state.get("backendPort"),
        "retrievalMode": agent_state.get("retrievalMode"),
        "vectorBackend": agent_state.get("vectorBackend"),
        "stateAuthority": (
            "live_runtime_and_state" if web_status == 200
            else "last_recorded_state_not_live_runtime_evidence"
        ),
        "isolationPins": {
            "agent/app/settings.py": grid.sha256(ROOT / "agent/app/settings.py"),
            "scripts/used-phone-demo.ps1": grid.sha256(ROOT / "scripts/used-phone-demo.ps1"),
            ".runtime/used-phone-demo/state.json": grid.sha256(state_path),
        },
        "webIndex": "used-phone-demo-products-v1",
        "webIndexDocumentCount": index_count,
        "observationErrors": errors,
        "experimentElasticsearchUrl": grid.ELASTICSEARCH_URL,
        "experimentContainer": grid.ELASTICSEARCH_CONTAINER,
        "productionDefaultChangedByPackageBuilder": False,
    }


def _strict_raw_validation(run_dir: Path) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    raw_report = json.loads((run_dir / "report.json").read_text(encoding="utf-8"))
    raw_receipt = json.loads((run_dir / "receipt.json").read_text(encoding="utf-8"))
    traces = grid.read_jsonl(run_dir / "trace.jsonl")
    documents, queries = grid.load_inputs()
    query_ids = [str(row["scenarioId"]) for row in queries]
    expected = {
        (query_id, analyzer, arm, repeat)
        for query_id in query_ids
        for analyzer in grid.INDEX_NAMES
        for arm in grid.WEIGHT_ARMS
        for repeat in range(1, grid.REPEATS + 1)
    }
    actual = [
        (row["queryId"], row["analyzer"], row["arm"], int(row["repeat"]))
        for row in traces
    ]
    if len(actual) != len(set(actual)) or set(actual) != expected:
        raise ValueError("raw trace Cartesian grid mismatch")
    stable_cells = 0
    for query_id in query_ids:
        for analyzer in grid.INDEX_NAMES:
            for arm in grid.WEIGHT_ARMS:
                rows = [
                    row for row in traces
                    if row["queryId"] == query_id
                    and row["analyzer"] == analyzer
                    and row["arm"] == arm
                ]
                if len({tuple(row["rankedIds"]) for row in rows}) != 1:
                    raise ValueError("unstable repeated ranking")
                if any(
                    len(row["esTop50"]) != 50
                    or len(row["denseTop50"]) != 50
                    or len(row["rankedIds"]) != 50
                    or len(row["scores"]) != 50
                    for row in rows
                ):
                    raise ValueError("Top-50 trace shape mismatch")
                stable_cells += 1
    if raw_report["execution"]["traceRows"] != len(traces):
        raise ValueError("raw report trace count mismatch")
    if raw_report["execution"]["stableRankingCells"] != stable_cells:
        raise ValueError("raw report stable cell count mismatch")
    for relative, pin in raw_report["inputPins"].items():
        path = ROOT / relative
        if path.stat().st_size != pin["bytes"] or grid.sha256(path) != pin["sha256"]:
            raise ValueError(f"raw input pin mismatch: {relative}")
    source = raw_receipt["source"]
    source_path = ROOT / source["path"]
    if grid.sha256(source_path) != source["sha256"]:
        raise ValueError("raw source pin mismatch")
    for name, pin in raw_receipt["artifacts"].items():
        path = run_dir / name
        if grid.sha256(path) != pin["sha256"]:
            raise ValueError(f"raw artifact pin mismatch: {name}")
        if "rows" in pin and len(grid.read_jsonl(path)) != pin["rows"]:
            raise ValueError(f"raw artifact row count mismatch: {name}")
    es_samples: dict[tuple[str, str, int], set[float]] = {}
    for row in traces:
        key = (row["analyzer"], row["queryId"], int(row["repeat"]))
        es_samples.setdefault(key, set()).add(float(row["latencyMs"]["es"]))
    if any(len(values) != 1 for values in es_samples.values()):
        raise ValueError("ES latency differs across arms for one query execution")
    es_stats = {}
    for analyzer in grid.INDEX_NAMES:
        values = [
            next(iter(sample))
            for (sample_analyzer, _query_id, _repeat), sample in es_samples.items()
            if sample_analyzer == analyzer
        ]
        es_stats[analyzer] = {
            "p50": grid.percentile(values, 0.50),
            "p95": grid.percentile(values, 0.95),
        }
    reported_es = raw_report["execution"]["latencyMs"]["es"]
    if any(
        abs(es_stats[analyzer][metric] - reported_es[analyzer][metric]) > 0.001
        for analyzer in grid.INDEX_NAMES for metric in ("p50", "p95")
    ):
        raise ValueError("raw ES latency statistics do not replay")
    return traces, {"documents": documents, "queries": queries, "stableCells": stable_cells}


def build(run_dir: Path = DEFAULT_RUN_DIR) -> Path:
    traces, raw = _strict_raw_validation(run_dir)
    documents = raw["documents"]
    queries = raw["queries"]
    prices, catalog = _price_and_catalog()
    raw_pool = grid.read_jsonl(run_dir / "human_review_pool.jsonl")
    enriched = _enrich_pool(raw_pool, prices, catalog)
    taskstate = _taskstate_rows(queries)
    contract = _review_contract()
    packs, mapping = _blind_packs(enriched, taskstate)
    dense_latency = _dense_latency_rows(documents, queries)
    runtime = _runtime_snapshot()

    taskstate_path = run_dir / "frozen_taskstate_intents_v2.jsonl"
    enriched_path = run_dir / "evaluator_pool_v2.jsonl"
    contract_path = run_dir / "review_contract_v2.json"
    dense_path = run_dir / "dense_latency_trace_v2.jsonl"
    runtime_path = run_dir / "web_runtime_snapshot_v2.json"
    reviewer_paths = {
        reviewer: run_dir / f"blind_{reviewer}_v2.jsonl"
        for reviewer in REVIEWER_SEEDS
    }
    private_dir = run_dir / "private"
    private_dir.mkdir(exist_ok=True)
    mapping_path = private_dir / "sealed_mapping_v2.json"
    grid.write_jsonl(taskstate_path, taskstate)
    grid.write_jsonl(enriched_path, enriched)
    contract_path.write_text(grid.canonical(contract) + "\n", encoding="utf-8")
    grid.write_jsonl(dense_path, dense_latency)
    runtime_path.write_text(grid.canonical(runtime) + "\n", encoding="utf-8")
    for reviewer, rows in packs.items():
        grid.write_jsonl(reviewer_paths[reviewer], rows)
    mapping["packHashes"] = {
        reviewer: grid.sha256(path) for reviewer, path in reviewer_paths.items()
    }
    mapping_path.write_text(grid.canonical(mapping) + "\n", encoding="utf-8")

    dense_values = [row["queryMs"] for row in dense_latency]
    fusion_values = [row["latencyMs"]["fusion"] for row in traces]
    final_report = {
        "schemaVersion": "used-phone-439-retrieval-review-package-report-v2",
        "status": "HOLD_PENDING_TWO_HUMAN_REVIEWS_AND_ADJUDICATION",
        "decision": {
            "determinismGate": "ACCEPT",
            "blindReviewPackageGate": "ACCEPT",
            "hardConstraintSafetyGate": "NOT_EVALUATED_PENDING_QRELS",
            "relevanceWinner": "NOT_EVALUATED",
            "productionDefaultSwitch": "HOLD",
        },
        "rawGrid": {
            "traceRows": len(traces),
            "stableRankingCells": raw["stableCells"],
            "queryCount": len(queries),
            "analyzers": list(grid.INDEX_NAMES),
            "arms": list(grid.WEIGHT_ARMS),
            "repeats": grid.REPEATS,
        },
        "humanReview": {
            "queryCount": len(enriched),
            "candidatePairs": sum(row["candidateCount"] for row in enriched),
            "reviewerCount": 2,
            "blindFieldsHidden": ["queryId", "split", "productId", "analyzer", "arm", "rank", "weights", "poolWitnesses"],
            "mappingVisibility": "PRIVATE_EVALUATOR_ONLY",
            "metricRecallName": "pooledRecallAt50",
            "directoryRecallClaimForbidden": True,
        },
        "latencyMs": {
            "denseQuery": {
                "samples": len(dense_values),
                "p50": grid.percentile(dense_values, 0.50),
                "p95": grid.percentile(dense_values, 0.95),
            },
            "fusion": {
                "samples": len(fusion_values),
                "p50": grid.percentile(fusion_values, 0.50),
                "p95": grid.percentile(fusion_values, 0.95),
            },
        },
        "callLayering": {
            "embeddingCatalogBuilds": grid.REPEATS,
            "embeddingQueryCalls": len(queries) * grid.REPEATS,
            "llmDecisionCalls": 0,
            "taskManagerCalls": 0,
            "finalAnswerCalls": 0,
            "promptTokens": 0,
            "completionTokens": 0,
        },
        "runtime": runtime,
        "limitations": [
            "No winner or quality metric exists before completed independent human qrels.",
            "Recall@50 is pooled recall over the all-arm Top-50 union, not directory-level recall.",
            "Unsupported structured evidence remains visible as a capability boundary and cannot become a factual answer claim.",
            "The small query count prevents a general superiority claim even after descriptive scoring.",
        ],
    }
    final_report_path = run_dir / "review_package_report_v2.json"
    final_report_path.write_text(grid.canonical(final_report) + "\n", encoding="utf-8")

    artifacts = {
        "trace.jsonl": _file_pin(run_dir / "trace.jsonl", rows=len(traces)),
        "report.json": _file_pin(run_dir / "report.json"),
        "receipt.json": _file_pin(run_dir / "receipt.json"),
        "human_review_pool.jsonl": _file_pin(run_dir / "human_review_pool.jsonl", rows=len(raw_pool)),
        "frozen_taskstate_intents_v2.jsonl": _file_pin(taskstate_path, rows=len(taskstate)),
        "evaluator_pool_v2.jsonl": _file_pin(enriched_path, rows=len(enriched)),
        "review_contract_v2.json": _file_pin(contract_path),
        "dense_latency_trace_v2.jsonl": _file_pin(dense_path, rows=len(dense_latency)),
        "web_runtime_snapshot_v2.json": _file_pin(runtime_path),
        "blind_reviewer01_v2.jsonl": _file_pin(reviewer_paths["reviewer01"], rows=len(packs["reviewer01"])),
        "blind_reviewer02_v2.jsonl": _file_pin(reviewer_paths["reviewer02"], rows=len(packs["reviewer02"])),
        "private/sealed_mapping_v2.json": _file_pin(mapping_path),
        "review_package_report_v2.json": _file_pin(final_report_path),
    }
    receipt = {
        "schemaVersion": "used-phone-439-retrieval-review-package-receipt-v2",
        "status": final_report["status"],
        "artifacts": artifacts,
        "sources": {
            Path(grid.__file__).resolve().relative_to(ROOT).as_posix(): grid.sha256(Path(grid.__file__).resolve()),
            Path(__file__).resolve().relative_to(ROOT).as_posix(): grid.sha256(Path(__file__).resolve()),
        },
    }
    receipt_path = run_dir / "review_package_receipt_v2.json"
    receipt_path.write_text(grid.canonical(receipt) + "\n", encoding="utf-8")
    validate(run_dir)
    return run_dir


def _keys(value: object) -> Iterable[str]:
    if isinstance(value, Mapping):
        for key, child in value.items():
            yield str(key)
            yield from _keys(child)
    elif isinstance(value, list):
        for child in value:
            yield from _keys(child)


def validate(run_dir: Path = DEFAULT_RUN_DIR) -> None:
    traces, raw = _strict_raw_validation(run_dir)
    receipt = json.loads((run_dir / "review_package_receipt_v2.json").read_text(encoding="utf-8"))
    for relative, pin in receipt["artifacts"].items():
        path = run_dir / relative
        if path.stat().st_size != pin["bytes"] or grid.sha256(path) != pin["sha256"]:
            raise ValueError(f"v2 artifact pin mismatch: {relative}")
        if "rows" in pin and len(grid.read_jsonl(path)) != pin["rows"]:
            raise ValueError(f"v2 artifact row mismatch: {relative}")
    for relative, expected_hash in receipt["sources"].items():
        if grid.sha256(ROOT / relative) != expected_hash:
            raise ValueError(f"v2 source pin mismatch: {relative}")
    intents = grid.read_jsonl(run_dir / "frozen_taskstate_intents_v2.jsonl")
    query_ids = {str(row["scenarioId"]) for row in raw["queries"]}
    if {row["queryId"] for row in intents} != query_ids or len(intents) != 24:
        raise ValueError("TaskState intent coverage mismatch")
    enriched = grid.read_jsonl(run_dir / "evaluator_pool_v2.jsonl")
    if any(
        candidate.get("syntheticReferencePriceMinor") is None
        or candidate.get("priceDisclosureZh") != "AI 合成，非真实报价"
        for row in enriched for candidate in row["candidates"]
    ):
        raise ValueError("evaluator pool lacks disclosed price evidence")
    packs = {
        reviewer: grid.read_jsonl(run_dir / f"blind_{reviewer}_v2.jsonl")
        for reviewer in REVIEWER_SEEDS
    }
    forbidden = {"queryId", "split", "productId", "analyzer", "arm", "rank", "weights", "poolWitnesses"}
    for reviewer, rows in packs.items():
        if len(rows) != 24 or forbidden & set(_keys(rows)):
            raise ValueError(f"blind leakage in {reviewer}")
        if any(
            candidate.get("syntheticReferencePriceMinor") is None
            for row in rows for candidate in row["candidates"]
        ):
            raise ValueError(f"blind price missing in {reviewer}")
    mapping = json.loads((run_dir / "private/sealed_mapping_v2.json").read_text(encoding="utf-8"))
    for reviewer, path_hash in mapping["packHashes"].items():
        if grid.sha256(run_dir / f"blind_{reviewer}_v2.jsonl") != path_hash:
            raise ValueError("blind pack/mapping hash mismatch")
    for reviewer in REVIEWER_SEEDS:
        mapped = mapping["reviewers"][reviewer]
        if len(mapped) != 24:
            raise ValueError("blind query mapping incomplete")
        if sum(len(item["candidates"]) for item in mapped.values()) != sum(
            row["candidateCount"] for row in enriched
        ):
            raise ValueError("blind candidate mapping incomplete")
    latency = grid.read_jsonl(run_dir / "dense_latency_trace_v2.jsonl")
    expected_latency = {
        (str(row["scenarioId"]), repeat)
        for row in raw["queries"] for repeat in range(1, grid.REPEATS + 1)
    }
    actual_latency = {(row["queryId"], int(row["repeat"])) for row in latency}
    if len(latency) != len(actual_latency) or actual_latency != expected_latency:
        raise ValueError("dense latency trace grid mismatch")
    report = json.loads((run_dir / "review_package_report_v2.json").read_text(encoding="utf-8"))
    dense_values = [row["queryMs"] for row in latency]
    fusion_values = [row["latencyMs"]["fusion"] for row in traces]
    if report["latencyMs"]["denseQuery"] != {
        "samples": len(dense_values),
        "p50": grid.percentile(dense_values, 0.50),
        "p95": grid.percentile(dense_values, 0.95),
    }:
        raise ValueError("dense latency report does not replay")
    if report["latencyMs"]["fusion"] != {
        "samples": len(fusion_values),
        "p50": grid.percentile(fusion_values, 0.50),
        "p95": grid.percentile(fusion_values, 0.95),
    }:
        raise ValueError("fusion latency report does not replay")
    runtime = json.loads((run_dir / "web_runtime_snapshot_v2.json").read_text(encoding="utf-8"))
    if runtime["productionDefaultChangedByPackageBuilder"] is not False:
        raise ValueError("web default isolation snapshot mismatch")
    if (
        runtime["backendPort"] != 18082
        or runtime["retrievalMode"] != "hybrid"
        or runtime["vectorBackend"] != "local"
    ):
        raise ValueError("recorded web retrieval identity mismatch")
    for relative, expected_hash in runtime["isolationPins"].items():
        if grid.sha256(ROOT / relative) != expected_hash:
            raise ValueError(f"web isolation pin drift: {relative}")
    if runtime["webReachable"] and (
        runtime["webHttpStatus"] != 200
        or runtime["webIndexDocumentCount"] != 252
    ):
        raise ValueError("reachable web runtime identity mismatch")
    if report["decision"] != {
        "determinismGate": "ACCEPT",
        "blindReviewPackageGate": "ACCEPT",
        "hardConstraintSafetyGate": "NOT_EVALUATED_PENDING_QRELS",
        "relevanceWinner": "NOT_EVALUATED",
        "productionDefaultSwitch": "HOLD",
    }:
        raise ValueError("review package decision boundary mismatch")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-dir", type=Path, default=DEFAULT_RUN_DIR)
    parser.add_argument("--validate-only", action="store_true")
    args = parser.parse_args()
    if args.validate_only:
        validate(args.run_dir)
    else:
        build(args.run_dir)
    print(grid.canonical({"status": "ACCEPT", "runDir": str(args.run_dir)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
