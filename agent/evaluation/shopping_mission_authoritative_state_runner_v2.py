"""Prompt-contract repair for one-call authoritative MissionState compilation."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from agent.evaluation import shopping_mission_baseline_runner_v1 as base
from agent.evaluation import shopping_mission_authoritative_state_runner_v1 as v1


PROFILE = v1.PROFILE
STATE_SCHEMA_PATH = v1.STATE_SCHEMA_PATH
SCORE_SCHEMA_PATH = v1.SCORE_SCHEMA_PATH
PREREGISTRATION_PATH = Path(__file__).resolve().parents[2] / "docs" / "analysis-briefs" / "shopping-mission-authoritative-state-prompt-repair-2026-08-23.md"

ACTION_KINDS = "RESEARCH, QUERY_PRODUCTS, CHECK_OWNED, NON_PURCHASE_ACTION, CLARIFY, COMPARE, BASKET_VALIDATE"
EVIDENCE_KINDS = "USER_STATE, CATALOG_FACT, BACKEND_DYNAMIC, EXTERNAL_STABLE, EXTERNAL_DYNAMIC, POLICY_DOCUMENT"
OUTPUT_MODES = "PREPARATION_CHECKLIST, PRODUCT_RECOMMENDATIONS, COMPARISON, BASKET_SUMMARY, RESEARCH_SUMMARY, CLARIFICATION_QUESTION, NO_PURCHASE_GUIDANCE"

STATE_PROMPT = f"""
只返回一个 JSON object，不要 Markdown，不要最终 Mission Graph，也不要增加字段。顶层必须且只能有：
missionFamily, currentGoal, actions, dependencies, requirements, blockingUnknowns, requiredOutputModes。

完整合法枚举：
- missionFamily: {base.MISSION_FAMILIES}
- action.kind: {ACTION_KINDS}
- action.requiredEvidence 每项: {EVIDENCE_KINDS}
- category: {base.CATEGORIES}
- requirement.key: {base.CONSTRAINT_KEYS}
- blockingUnknowns 每项: {base.UNKNOWN_CLASSES}
- requiredOutputModes 每项: {OUTPUT_MODES}

actions 每项必须有 ref(小写 snake_case)、kind、purchaseDisposition、requiredEvidence(array)、
observationDependent(boolean)。只有 QUERY_PRODUCTS 必须带 category；其它 kind 禁止 category。
QUERY_PRODUCTS 的 purchaseDisposition 只能 REQUIRED/MAYBE；其它 kind 必须 NOT_APPLICABLE。
observationDependent 只在必须先看到天气、库存、价格、优惠、政策或外部证据才能决定下一步时为 true。
dependencies 每项只能有 beforeRef, afterRef, reason；reason 只能 INFORMS/REQUIRES/FILTERS/AGGREGATES。
requirements 每项只能有 scopeRef, key, operator, value, priority, polarity；operator 只能
eq/lte/gte/in/not_in/prefer/sum_lte，priority 只能 hard/soft，polarity 只能 positive/negative。
blockingUnknowns 最多一个；非空时必须有且只有一个 CLARIFY action，空时禁止 CLARIFY。

合法形态示例（只示范格式，不要复制语义）：
{{"missionFamily":"CONTRACT_RED","currentGoal":"找通勤鞋","actions":[{{"ref":"query_shoes","kind":"QUERY_PRODUCTS","category":"sneakers","purchaseDisposition":"REQUIRED","requiredEvidence":["CATALOG_FACT"],"observationDependent":false}}],"dependencies":[],"requirements":[{{"scopeRef":"query_shoes","key":"budget_cny","operator":"lte","value":1000,"priority":"hard","polarity":"positive"}}],"blockingUnknowns":[],"requiredOutputModes":["PRODUCT_RECOMMENDATIONS"]}}

忠实保留共享/局部预算、硬软负向条件、已有物品、撤回需求、收礼人作用域、外部研究、非购买行动、
比较与最小必要澄清。不要发明用户没说的条件或枚举。
""".strip()


def create_kwargs(*, model, messages, max_tokens=6000):
    return v1.create_kwargs(model=model, messages=messages, max_tokens=max_tokens)


def _messages(conversation):
    return [
        {"role": "system", "content": STATE_PROMPT},
        {"role": "user", "content": f"购物对话：\n{conversation}\n\n请生成权威 MissionState。"},
    ]


async def _run_case(row, run_id, call_json):
    scenario_id = row["scenarioId"]
    result, call_receipt = await base._recorded_call(
        call_json, "STATE_EXTRACT", _messages(base._conversation(row)), 6000,
    )
    prediction = None
    error_code = None
    try:
        if result is None:
            raise v1.AuthoritativeStateError(call_receipt["errorCode"] or "model_call_failed")
        prediction = v1.compile_state(json.loads(result.content), scenario_id)
    except Exception as exc:
        error_code = str(exc)[:120] if isinstance(exc, v1.AuthoritativeStateError) else type(exc).__name__
    status = "COMPLETED" if prediction is not None else "FAILED"
    case = {
        "scenarioId": scenario_id, "runId": run_id, "profile": PROFILE, "status": status,
        "prediction": prediction, "errorCode": error_code,
        "rawResponseDigest": call_receipt["responseDigest"],
    }
    receipt = {
        "scenarioId": scenario_id, "runId": run_id, "profile": PROFILE, "status": status,
        "modelCalls": 1, "inputTokens": call_receipt["inputTokens"],
        "outputTokens": call_receipt["outputTokens"], "latencyMs": call_receipt["latencyMs"],
        "usageTrust": "API_REPORTED", "calls": [call_receipt],
    }
    return case, receipt


async def run_profile(*, run_id, output_dir, model, endpoint, call_json, concurrency=3):
    parsed = urlparse(endpoint)
    if parsed.scheme != "https" or (parsed.hostname or "").casefold() != "api.deepseek.com" or parsed.port not in {None, 443}:
        raise v1.AuthoritativeStateError("model endpoint must be pinned")
    if output_dir.exists():
        raise v1.AuthoritativeStateError("output directory already exists")
    rows = base._validate_public_preconditions()
    semaphore = asyncio.Semaphore(max(1, concurrency))

    async def limited(row):
        async with semaphore:
            return await _run_case(row, run_id, call_json)

    started = datetime.now(timezone.utc)
    results = await asyncio.gather(*(limited(row) for row in rows))
    completed_at = datetime.now(timezone.utc)
    cases = [item[0] for item in results]
    receipts = [item[1] for item in results]
    case_bytes = b"".join(base._canonical_bytes(item) for item in cases)
    receipt_bytes = b"".join(base._canonical_bytes(item) for item in receipts)
    manifest = {
        "schemaVersion": "shopping-mission-authoritative-state-run-manifest-v2",
        "datasetId": base.DATASET_ID, "runId": run_id, "profile": PROFILE,
        "model": model, "endpointOrigin": "https://api.deepseek.com", "temperature": 0,
        "startedAt": started.isoformat(), "completedAt": completed_at.isoformat(),
        "scenarioCount": 18, "completedCount": sum(item["status"] == "COMPLETED" for item in cases),
        "failedCount": sum(item["status"] == "FAILED" for item in cases),
        "publicSha256": base._sha256_path(base.PUBLIC_PATH),
        "humanLanguageEvidenceSha256": base.EXPECTED_LANGUAGE_EVIDENCE_SHA,
        "humanSemanticEvidenceSha256": base.EXPECTED_SEMANTIC_EVIDENCE_SHA,
        "frozenBaselineRunnerSha256": base._sha256_path(Path(base.__file__).resolve()),
        "parentRunnerSha256": base._sha256_path(Path(v1.__file__).resolve()),
        "runnerSha256": base._sha256_path(Path(__file__).resolve()),
        "stateSchemaSha256": base._sha256_path(STATE_SCHEMA_PATH),
        "predictionSchemaSha256": base._sha256_path(base.PREDICTION_SCHEMA_PATH),
        "preregistrationSha256": base._sha256_path(PREREGISTRATION_PATH),
        "casesSha256": base._sha256_bytes(case_bytes), "receiptsSha256": base._sha256_bytes(receipt_bytes),
        "promptVersion": "ENUMS_AND_SHAPE_EXPLICIT_V2", "compilerType": "DETERMINISTIC",
        "modelRepairCalls": 0, "toolCalls": 0,
    }
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    temp_dir = Path(tempfile.mkdtemp(prefix=f".{output_dir.name}-", dir=output_dir.parent))
    try:
        (temp_dir / "cases.jsonl").write_bytes(case_bytes)
        (temp_dir / "receipts.jsonl").write_bytes(receipt_bytes)
        (temp_dir / "manifest.json").write_bytes(base._canonical_bytes(manifest))
        os.replace(temp_dir, output_dir)
    except Exception:
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise
    return manifest


validate_state = v1.validate_state
compile_state = v1.compile_state
AuthoritativeStateError = v1.AuthoritativeStateError
ModelCall = base.ModelCall
