"""Public-only runners for Shopping Mission baseline comparison V1.

The runner never opens the private oracle and never repairs model output.  It
owns scenario identity, records API-reported usage, and writes immutable run
directories.  Scoring is a separate process boundary.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from time import perf_counter_ns
from typing import Any, Awaitable, Callable, Mapping, Sequence
from urllib.parse import urlparse

from jsonschema import Draft202012Validator


DATASET_ID = "shopping-mission-benchmark-v1-mvp-20260823"
PROFILES = frozenset({"DIRECT_ONE_SHOT", "STATEFUL_CONTEXT"})
EVALUATION_DIR = Path(__file__).resolve().parent
ASSET_DIR = EVALUATION_DIR / "assets" / DATASET_ID.replace("-", "_")
PUBLIC_PATH = ASSET_DIR / "public" / "scenarios.jsonl"
REVIEW_DIR = ASSET_DIR / "reviews"
SCHEMA_DIR = EVALUATION_DIR / "schemas"
PREDICTION_SCHEMA_PATH = SCHEMA_DIR / "shopping_mission_prediction_v1.schema.json"
MODEL_OUTPUT_SCHEMA_PATH = SCHEMA_DIR / "shopping_mission_baseline_model_output_v1.schema.json"
PREREGISTRATION_PATH = Path(__file__).resolve().parents[2] / "docs" / "analysis-briefs" / "shopping-mission-baseline-comparison-2026-08-23.md"
EXPECTED_LANGUAGE_EVIDENCE_SHA = "59d564a95b633dc14c6f8e8674be057e03280500ef735101e8c1db7353f0abf0"
EXPECTED_SEMANTIC_EVIDENCE_SHA = "40275bd6b52332e8ad966284cacef8c9d5f78b4fe63aec6ffd69623cccb7f35e"


class BaselineRunnerError(RuntimeError):
    """Fail-closed public runner error."""


@dataclass(frozen=True)
class ModelCall:
    content: str
    input_tokens: int
    output_tokens: int


CallJson = Callable[[str, Sequence[Mapping[str, str]], int], Awaitable[ModelCall]]


MISSION_FAMILIES = (
    "BASKET_COMPOSITION, EVENT_PREPARATION, GIFT_OR_PROXY, "
    "REPLACEMENT_OR_UPGRADE, COMPATIBILITY_ECOSYSTEM, "
    "HOUSEHOLD_OR_LEARNING_PROJECT, REPLENISHMENT, "
    "COMPARISON_DECISION, CONTRACT_RED"
)
CATEGORIES = (
    "used_phone, phone_accessory, earbuds, smartwatch, tshirt, jeans, sneakers, "
    "handbag, drinkware, cleaning, stationery, books, unsupported_catalog"
)
CONSTRAINT_KEYS = (
    "audience_age, battery_days, bundle_total_cny, buy_only_shortage, buyer_personal_preferences, "
    "camping_requirements, capacity_ml, cart_total_cny, compatible_platform, cushioning, "
    "coupon_subtotal_scope, cross_item_color_match, decorative_items, equipment_status, "
    "exclude_already_owned, fit, fit_stability, fits_laptop_inches, folders_to_buy, genres, "
    "health_features, item_types, language, leakproof, luggage_mode, merchant_identity, owned, "
    "only_if_incompatible, page_count, platform, price_cny, primary_goal, priority_order, "
    "product_recommendations, quantity, size, sole_grip, sport, storage_gb, strap_comfort, "
    "strong_odor, surfaces, sweat_resistant, swim_frequency_weekly, topics, trip_duration_days, "
    "unsafe_chemical_mixing, waist_size, water_resistance, weight, winner_without_waterproof_evidence"
)
UNKNOWN_CLASSES = "exact_date, location, usage_rate, current_condition, product_identity, size, other"

GRAPH_CONTRACT = f"""
只返回一个 JSON object，不要 Markdown，不要解释。对象必须有 missionFamily 和 predictedGraph。
missionFamily 只能是：{MISSION_FAMILIES}。
predictedGraph 必须有：goalKey, routeClass, nodes, dependencies, constraints,
blockingUnknowns, clarification, requiredOutputModes。
routeClass 只能是 DIRECT, PLANNED, BOUNDED_REACT, CLARIFY, ANSWER_ONLY。
node 的 kind 只能是 RESEARCH, QUERY_PRODUCTS, CHECK_OWNED, NON_PURCHASE_ACTION,
CLARIFY, COMPARE, BASKET_VALIDATE；每个 node 都有本地 nodeKey、purchaseDisposition
(REQUIRED/MAYBE/NOT_APPLICABLE)、requiredEvidence 数组。只有 QUERY_PRODUCTS 可以有 category，
category 只能是：{CATEGORIES}。requiredEvidence 只能取 USER_STATE, CATALOG_FACT,
BACKEND_DYNAMIC, EXTERNAL_STABLE, EXTERNAL_DYNAMIC, POLICY_DOCUMENT。
dependencies 使用 before/after 本地 nodeKey，reason 只能是 INFORMS, REQUIRES, FILTERS, AGGREGATES。
constraint 必须有 scope、key、operator、value、priority、polarity。scope 为 GLOBAL 或本地 nodeKey；
operator 只能是 eq, lte, gte, in, not_in, prefer, sum_lte；priority 为 hard/soft；polarity 为
positive/negative。统一 key 词表为：{CONSTRAINT_KEYS}。不要为了凑词表添加用户没说的条件。
blockingUnknowns 只使用这些公开小类：{UNKNOWN_CLASSES}。只有未知信息确实阻塞下一步时才加入。
clarification 为 required(boolean), focus(string 或 null), maxQuestions(0 或 1)；focus 与 blockingUnknowns
使用相同小类。requiredOutputModes 只能取 PREPARATION_CHECKLIST, PRODUCT_RECOMMENDATIONS,
COMPARISON, BASKET_SUMMARY, RESEARCH_SUMMARY, CLARIFICATION_QUESTION, NO_PURCHASE_GUIDANCE。
goalKey/nodeKey 只是图内本地 snake_case 标识，不要求特定措辞。不要把所有准备事项都商品化；已有物品、
被撤销需求、代购对象、共享/局部预算、动态证据和最小必要澄清必须正确处理。
""".strip()

STATE_CONTRACT = f"""
只返回 JSON object，不要 Markdown，不要最终 Mission Graph。你是购物任务状态编译器，请忠实整理：
currentGoal(string), missionFamily(从 {MISSION_FAMILIES} 选择), purchaseNeeds(array),
researchNeeds(array), ownedItems(array), nonPurchaseActions(array), comparisonNeeds(array),
requirements(array), blockingUnknowns(array), revokedNeeds(array), requiredOutputModes(array)。
purchaseNeeds 每项写 ref、category(从 {CATEGORIES} 选择)、disposition(REQUIRED/MAYBE)；requirements
每项写 scopeRef(GLOBAL 或 purchase ref)、key、operator、value、priority、polarity。key 优先使用：
{CONSTRAINT_KEYS}。blockingUnknowns 只使用：{UNKNOWN_CLASSES}，非阻塞信息不要追问。
不要加入用户未说的事实；把撤回、已有、不需购买和收礼人/购买者作用域显式分开。
""".strip()


def _canonical_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_path(path: Path) -> str:
    return _sha256_bytes(path.read_bytes())


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    raw = path.read_bytes()
    if not raw or not raw.endswith(b"\n"):
        raise BaselineRunnerError(f"invalid public JSONL framing: {path}")
    rows = [json.loads(line) for line in raw.decode("utf-8").splitlines()]
    if any(type(row) is not dict for row in rows):
        raise BaselineRunnerError("public rows must be JSON objects")
    return rows


def _validate_public_preconditions() -> list[dict[str, Any]]:
    language_path = REVIEW_DIR / "human_language_review_completed_attempt001.json"
    semantic_path = REVIEW_DIR / "human_semantic_review_completed_attempt001.json"
    if _sha256_path(language_path) != EXPECTED_LANGUAGE_EVIDENCE_SHA:
        raise BaselineRunnerError("human language evidence digest drift")
    if _sha256_path(semantic_path) != EXPECTED_SEMANTIC_EVIDENCE_SHA:
        raise BaselineRunnerError("human semantic evidence digest drift")
    for path in (language_path, semantic_path):
        evidence = json.loads(path.read_text(encoding="utf-8"))
        if evidence.get("decision") != "ALL_PASS" or evidence.get("reviewedRowCount") != 18:
            raise BaselineRunnerError("human review gate is not complete")
    rows = _read_jsonl(PUBLIC_PATH)
    if len(rows) != 18 or len({row.get("scenarioId") for row in rows}) != 18:
        raise BaselineRunnerError("public scenario closure mismatch")
    return rows


def _messages_for_graph(conversation: str, state: Mapping[str, Any] | None = None) -> list[dict[str, str]]:
    user = f"购物对话：\n{conversation}\n\n请生成最终 Mission Graph。"
    if state is not None:
        projection = json.dumps(state, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        user = f"购物对话：\n{conversation}\n\n可信 MissionState 投影：\n{projection}\n\n请生成最终 Mission Graph。"
    return [{"role": "system", "content": GRAPH_CONTRACT}, {"role": "user", "content": user}]


def _messages_for_state(conversation: str) -> list[dict[str, str]]:
    return [
        {"role": "system", "content": STATE_CONTRACT},
        {"role": "user", "content": f"购物对话：\n{conversation}\n\n请生成 MissionState。"},
    ]


def _conversation(row: Mapping[str, Any]) -> str:
    return "\n".join(f"{turn['turnId']}: {turn['text']}" for turn in row["turns"])


def _prediction_validator() -> Draft202012Validator:
    schema = json.loads(PREDICTION_SCHEMA_PATH.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


def _validate_model_graph(value: Any, scenario_id: str) -> dict[str, Any]:
    if type(value) is not dict or set(value) != {"missionFamily", "predictedGraph"}:
        raise BaselineRunnerError("model output top-level contract mismatch")
    prediction = {
        "scenarioId": scenario_id,
        "schemaVersion": "shopping-mission-prediction-v1-mvp",
        "missionFamily": value["missionFamily"],
        "predictedGraph": value["predictedGraph"],
    }
    errors = sorted(_prediction_validator().iter_errors(prediction), key=lambda item: list(item.path))
    if errors:
        raise BaselineRunnerError(f"prediction schema mismatch: {errors[0].message}")
    return prediction


def _validate_state(value: Any) -> dict[str, Any]:
    required = {
        "currentGoal", "missionFamily", "purchaseNeeds", "researchNeeds", "ownedItems",
        "nonPurchaseActions", "comparisonNeeds", "requirements", "blockingUnknowns",
        "revokedNeeds", "requiredOutputModes",
    }
    if type(value) is not dict or set(value) != required:
        raise BaselineRunnerError("mission state top-level contract mismatch")
    if not isinstance(value["currentGoal"], str) or value["missionFamily"] not in MISSION_FAMILIES.split(", "):
        raise BaselineRunnerError("mission state identity mismatch")
    for key in required - {"currentGoal", "missionFamily"}:
        if type(value[key]) is not list:
            raise BaselineRunnerError(f"mission state field must be an array: {key}")
    # Plain JSON round-trip freezes subclasses and rejects NaN.
    return json.loads(json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")))


async def _recorded_call(
    call_json: CallJson,
    phase: str,
    messages: Sequence[Mapping[str, str]],
    max_tokens: int,
) -> tuple[ModelCall | None, dict[str, Any]]:
    request_digest = _sha256_bytes(_canonical_bytes({"phase": phase, "messages": list(messages), "maxTokens": max_tokens}))
    started = perf_counter_ns()
    try:
        result = await call_json(phase, messages, max_tokens)
        latency_ms = max(0, (perf_counter_ns() - started) // 1_000_000)
        digest = _sha256_bytes(result.content.encode("utf-8"))
        receipt = {
            "phase": phase, "ok": True, "latencyMs": latency_ms,
            "inputTokens": result.input_tokens, "outputTokens": result.output_tokens,
            "requestDigest": request_digest, "responseDigest": digest, "errorCode": None,
        }
        return result, receipt
    except Exception as exc:
        latency_ms = max(0, (perf_counter_ns() - started) // 1_000_000)
        error_code = type(exc).__name__
        receipt = {
            "phase": phase, "ok": False, "latencyMs": latency_ms,
            "inputTokens": 0, "outputTokens": 0, "requestDigest": request_digest,
            "responseDigest": None, "errorCode": error_code,
        }
        return None, receipt


async def _run_case(row: Mapping[str, Any], profile: str, run_id: str, call_json: CallJson) -> tuple[dict[str, Any], dict[str, Any]]:
    scenario_id = str(row["scenarioId"])
    conversation = _conversation(row)
    calls: list[dict[str, Any]] = []
    prediction: dict[str, Any] | None = None
    error_code: str | None = None
    last_digest: str | None = None
    try:
        if profile == "DIRECT_ONE_SHOT":
            result, receipt = await _recorded_call(call_json, "DIRECT_GRAPH", _messages_for_graph(conversation), 6000)
            calls.append(receipt)
            if result is None:
                raise BaselineRunnerError(receipt["errorCode"] or "model_call_failed")
            last_digest = receipt["responseDigest"]
            prediction = _validate_model_graph(json.loads(result.content), scenario_id)
        else:
            state_result, state_receipt = await _recorded_call(call_json, "STATE_EXTRACT", _messages_for_state(conversation), 3000)
            calls.append(state_receipt)
            if state_result is None:
                raise BaselineRunnerError(state_receipt["errorCode"] or "state_call_failed")
            state = _validate_state(json.loads(state_result.content))
            graph_result, graph_receipt = await _recorded_call(call_json, "STATE_TO_GRAPH", _messages_for_graph(conversation, state), 6000)
            calls.append(graph_receipt)
            if graph_result is None:
                raise BaselineRunnerError(graph_receipt["errorCode"] or "graph_call_failed")
            last_digest = graph_receipt["responseDigest"]
            prediction = _validate_model_graph(json.loads(graph_result.content), scenario_id)
    except Exception as exc:
        error_code = type(exc).__name__ if not isinstance(exc, BaselineRunnerError) else str(exc)[:96]

    status = "COMPLETED" if prediction is not None else "FAILED"
    case = {
        "scenarioId": scenario_id, "runId": run_id, "profile": profile, "status": status,
        "prediction": prediction, "errorCode": error_code, "rawResponseDigest": last_digest,
    }
    receipt = {
        "scenarioId": scenario_id, "runId": run_id, "profile": profile, "status": status,
        "modelCalls": len(calls),
        "inputTokens": sum(int(item["inputTokens"]) for item in calls),
        "outputTokens": sum(int(item["outputTokens"]) for item in calls),
        "latencyMs": sum(int(item["latencyMs"]) for item in calls),
        "usageTrust": "API_REPORTED", "calls": calls,
    }
    return case, receipt


async def run_profile(
    *, profile: str, run_id: str, output_dir: Path, model: str,
    endpoint: str, call_json: CallJson, concurrency: int = 3,
) -> dict[str, Any]:
    if profile not in PROFILES:
        raise BaselineRunnerError(f"unknown profile: {profile}")
    parsed = urlparse(endpoint)
    if parsed.scheme != "https" or (parsed.hostname or "").casefold() != "api.deepseek.com" or parsed.port not in {None, 443}:
        raise BaselineRunnerError("model endpoint must be pinned to https://api.deepseek.com")
    if output_dir.exists():
        raise BaselineRunnerError("output directory already exists")
    rows = _validate_public_preconditions()
    semaphore = asyncio.Semaphore(max(1, concurrency))

    async def limited(row: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        async with semaphore:
            return await _run_case(row, profile, run_id, call_json)

    started = datetime.now(timezone.utc)
    results = await asyncio.gather(*(limited(row) for row in rows))
    completed = datetime.now(timezone.utc)
    cases = [item[0] for item in results]
    receipts = [item[1] for item in results]
    case_bytes = b"".join(_canonical_bytes(item) for item in cases)
    receipt_bytes = b"".join(_canonical_bytes(item) for item in receipts)
    manifest = {
        "schemaVersion": "shopping-mission-baseline-run-manifest-v1",
        "datasetId": DATASET_ID, "runId": run_id, "profile": profile,
        "model": model, "endpointOrigin": "https://api.deepseek.com", "temperature": 0,
        "startedAt": started.isoformat(), "completedAt": completed.isoformat(),
        "scenarioCount": len(cases),
        "completedCount": sum(item["status"] == "COMPLETED" for item in cases),
        "failedCount": sum(item["status"] == "FAILED" for item in cases),
        "publicSha256": _sha256_path(PUBLIC_PATH),
        "humanLanguageEvidenceSha256": EXPECTED_LANGUAGE_EVIDENCE_SHA,
        "humanSemanticEvidenceSha256": EXPECTED_SEMANTIC_EVIDENCE_SHA,
        "runnerSha256": _sha256_path(Path(__file__).resolve()),
        "predictionSchemaSha256": _sha256_path(PREDICTION_SCHEMA_PATH),
        "preregistrationSha256": _sha256_path(PREREGISTRATION_PATH),
        "casesSha256": _sha256_bytes(case_bytes), "receiptsSha256": _sha256_bytes(receipt_bytes),
        "modelRepairCalls": 0, "toolCalls": 0,
    }
    parent = output_dir.parent
    parent.mkdir(parents=True, exist_ok=True)
    temp_dir = Path(tempfile.mkdtemp(prefix=f".{output_dir.name}-", dir=parent))
    try:
        (temp_dir / "cases.jsonl").write_bytes(case_bytes)
        (temp_dir / "receipts.jsonl").write_bytes(receipt_bytes)
        (temp_dir / "manifest.json").write_bytes(_canonical_bytes(manifest))
        os.replace(temp_dir, output_dir)
    except Exception:
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise
    return manifest
