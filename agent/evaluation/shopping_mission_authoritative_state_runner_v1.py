"""One-call authoritative MissionState runner and deterministic compiler."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence
from urllib.parse import urlparse

from jsonschema import Draft202012Validator

from agent.evaluation import shopping_mission_baseline_runner_v1 as base


PROFILE = "AUTHORITATIVE_STATE_COMPILER"
EVALUATION_DIR = Path(__file__).resolve().parent
STATE_SCHEMA_PATH = EVALUATION_DIR / "schemas" / "shopping_mission_authoritative_state_v1.schema.json"
SCORE_SCHEMA_PATH = EVALUATION_DIR / "schemas" / "shopping_mission_authoritative_state_score_v1.schema.json"
PREREGISTRATION_PATH = Path(__file__).resolve().parents[2] / "docs" / "analysis-briefs" / "shopping-mission-authoritative-state-compiler-2026-08-23.md"


STATE_PROMPT = f"""
只返回一个 JSON object，不要 Markdown，不要最终 Mission Graph。它必须严格匹配 Authoritative
MissionState：missionFamily, currentGoal, actions, dependencies, requirements, blockingUnknowns,
requiredOutputModes，不能增加其它字段。
missionFamily 只能是：{base.MISSION_FAMILIES}。
actions 每项必须有 ref(小写 snake_case，至少2字符)、kind、purchaseDisposition、requiredEvidence、
observationDependent；只有 QUERY_PRODUCTS 可以带 category，且必须带，category 只能是：
{base.CATEGORIES}。其它 action 禁止 category。非商品 action 的 purchaseDisposition 必须是
NOT_APPLICABLE，商品 action 只能 REQUIRED/MAYBE。kind/evidence 枚举沿用公开 Mission Graph 合同。
observationDependent 只在必须看到天气、库存、价格、优惠、政策或外部证据后才能决定下一步时为 true。
dependencies 使用 beforeRef/afterRef/reason；requirements 使用 scopeRef(GLOBAL 或 action ref)、key、
operator、value、priority(hard/soft)、polarity。统一 key 词表：{base.CONSTRAINT_KEYS}。
blockingUnknowns 最多一个，只能是：{base.UNKNOWN_CLASSES}。存在 blocking unknown 时必须有且只有一个
CLARIFY action；不存在时禁止 CLARIFY。requiredOutputModes 使用公开枚举。
忠实保留共享/局部预算、硬软负向条件、已有物品、撤回需求、收礼人作用域、外部研究、非购买行动、
比较与最小必要澄清。不要为了填字段添加用户没说的条件。
""".strip()


class AuthoritativeStateError(ValueError):
    """Invalid state or deterministic compilation boundary."""


def create_kwargs(*, model: str, messages: Sequence[Mapping[str, str]], max_tokens: int = 6000) -> dict[str, Any]:
    return {
        "model": model, "messages": list(messages), "temperature": 0,
        "max_tokens": max_tokens, "response_format": {"type": "json_object"},
        "extra_body": {"thinking": {"type": "disabled"}},
    }


def _messages(conversation: str) -> list[dict[str, str]]:
    return [
        {"role": "system", "content": STATE_PROMPT},
        {"role": "user", "content": f"购物对话：\n{conversation}\n\n请生成权威 MissionState。"},
    ]


def _state_validator() -> Draft202012Validator:
    schema = json.loads(STATE_SCHEMA_PATH.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


def validate_state(value: Any) -> dict[str, Any]:
    if type(value) is not dict:
        raise AuthoritativeStateError("state must be a JSON object")
    errors = sorted(_state_validator().iter_errors(value), key=lambda item: list(item.path))
    if errors:
        raise AuthoritativeStateError(f"state schema mismatch: {errors[0].message}")
    state = json.loads(json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")))
    actions = state["actions"]
    refs = [action["ref"] for action in actions]
    if len(refs) != len(set(refs)):
        raise AuthoritativeStateError("duplicate action ref")
    action_by_ref = {action["ref"]: action for action in actions}
    for action in actions:
        query = action["kind"] == "QUERY_PRODUCTS"
        if query != ("category" in action):
            raise AuthoritativeStateError("QUERY_PRODUCTS/category contract mismatch")
        if query and action["purchaseDisposition"] == "NOT_APPLICABLE":
            raise AuthoritativeStateError("product query must be purchasable")
        if not query and action["purchaseDisposition"] != "NOT_APPLICABLE":
            raise AuthoritativeStateError("non-product action cannot be purchasable")
    canonical_dependencies = [base._canonical_bytes(item) for item in state["dependencies"]]
    if len(canonical_dependencies) != len(set(canonical_dependencies)):
        raise AuthoritativeStateError("duplicate dependency")
    outgoing = {ref: [] for ref in refs}
    indegree = {ref: 0 for ref in refs}
    for edge in state["dependencies"]:
        before, after = edge["beforeRef"], edge["afterRef"]
        if before not in action_by_ref or after not in action_by_ref or before == after:
            raise AuthoritativeStateError("invalid dependency endpoint")
        outgoing[before].append(after)
        indegree[after] += 1
    queue = [ref for ref, degree in indegree.items() if degree == 0]
    visited = 0
    while queue:
        current = queue.pop()
        visited += 1
        for target in outgoing[current]:
            indegree[target] -= 1
            if indegree[target] == 0:
                queue.append(target)
    if visited != len(refs):
        raise AuthoritativeStateError("state dependency graph contains a cycle")
    canonical_requirements = [base._canonical_bytes(item) for item in state["requirements"]]
    if len(canonical_requirements) != len(set(canonical_requirements)):
        raise AuthoritativeStateError("duplicate requirement")
    for requirement in state["requirements"]:
        if requirement["scopeRef"] != "GLOBAL" and requirement["scopeRef"] not in action_by_ref:
            raise AuthoritativeStateError("requirement scope is not an action ref")
    clarify_count = sum(action["kind"] == "CLARIFY" for action in actions)
    if clarify_count != (1 if state["blockingUnknowns"] else 0):
        raise AuthoritativeStateError("clarification action/blocking unknown mismatch")
    return state


def _route(state: Mapping[str, Any]) -> str:
    if state["blockingUnknowns"]:
        return "CLARIFY"
    if any(action["observationDependent"] for action in state["actions"]):
        return "BOUNDED_REACT"
    if len(state["actions"]) == 1 and state["actions"][0]["kind"] == "QUERY_PRODUCTS":
        return "DIRECT"
    if all(action["kind"] in {"RESEARCH", "NON_PURCHASE_ACTION"} for action in state["actions"]):
        return "ANSWER_ONLY"
    return "PLANNED"


def compile_state(state: Mapping[str, Any], scenario_id: str) -> dict[str, Any]:
    validated = validate_state(state)
    ref_to_node: dict[str, str] = {}
    nodes = []
    for index, action in enumerate(validated["actions"], start=1):
        node_key = f"node_{index:03d}_{action['kind'].lower()}"
        ref_to_node[action["ref"]] = node_key
        node = {
            "nodeKey": node_key, "kind": action["kind"],
            "purchaseDisposition": action["purchaseDisposition"],
            "requiredEvidence": action["requiredEvidence"],
        }
        if "category" in action:
            node["category"] = action["category"]
        nodes.append(node)
    dependencies = [{
        "before": ref_to_node[edge["beforeRef"]], "after": ref_to_node[edge["afterRef"]],
        "reason": edge["reason"],
    } for edge in validated["dependencies"]]
    constraints = []
    for requirement in validated["requirements"]:
        constraint = dict(requirement)
        scope = constraint.pop("scopeRef")
        constraint["scope"] = "GLOBAL" if scope == "GLOBAL" else ref_to_node[scope]
        constraints.append(constraint)
    unknowns = validated["blockingUnknowns"]
    graph = {
        "goalKey": f"mission_{scenario_id.rsplit('-', 1)[-1].lower()}",
        "routeClass": _route(validated), "nodes": nodes,
        "dependencies": dependencies, "constraints": constraints,
        "blockingUnknowns": unknowns,
        "clarification": {
            "required": bool(unknowns), "focus": unknowns[0] if unknowns else None,
            "maxQuestions": 1 if unknowns else 0,
        },
        "requiredOutputModes": validated["requiredOutputModes"],
    }
    return base._validate_model_graph({"missionFamily": validated["missionFamily"], "predictedGraph": graph}, scenario_id)


async def _run_case(row, run_id: str, call_json):
    scenario_id = row["scenarioId"]
    result, call_receipt = await base._recorded_call(
        call_json, "STATE_EXTRACT", _messages(base._conversation(row)), 6000,
    )
    prediction = None
    error_code = None
    digest = call_receipt["responseDigest"]
    try:
        if result is None:
            raise AuthoritativeStateError(call_receipt["errorCode"] or "model_call_failed")
        prediction = compile_state(json.loads(result.content), scenario_id)
    except Exception as exc:
        error_code = str(exc)[:120] if isinstance(exc, AuthoritativeStateError) else type(exc).__name__
    status = "COMPLETED" if prediction is not None else "FAILED"
    case = {
        "scenarioId": scenario_id, "runId": run_id, "profile": PROFILE, "status": status,
        "prediction": prediction, "errorCode": error_code, "rawResponseDigest": digest,
    }
    receipt = {
        "scenarioId": scenario_id, "runId": run_id, "profile": PROFILE, "status": status,
        "modelCalls": 1, "inputTokens": call_receipt["inputTokens"],
        "outputTokens": call_receipt["outputTokens"], "latencyMs": call_receipt["latencyMs"],
        "usageTrust": "API_REPORTED", "calls": [call_receipt],
    }
    return case, receipt


async def run_profile(*, run_id: str, output_dir: Path, model: str, endpoint: str, call_json, concurrency: int = 3):
    parsed = urlparse(endpoint)
    if parsed.scheme != "https" or (parsed.hostname or "").casefold() != "api.deepseek.com" or parsed.port not in {None, 443}:
        raise AuthoritativeStateError("model endpoint must be pinned")
    if output_dir.exists():
        raise AuthoritativeStateError("output directory already exists")
    rows = base._validate_public_preconditions()
    semaphore = asyncio.Semaphore(max(1, concurrency))

    async def limited(row):
        async with semaphore:
            return await _run_case(row, run_id, call_json)

    started = datetime.now(timezone.utc)
    results = await asyncio.gather(*(limited(row) for row in rows))
    completed_at = datetime.now(timezone.utc)
    cases = [result[0] for result in results]
    receipts = [result[1] for result in results]
    case_bytes = b"".join(base._canonical_bytes(item) for item in cases)
    receipt_bytes = b"".join(base._canonical_bytes(item) for item in receipts)
    manifest = {
        "schemaVersion": "shopping-mission-authoritative-state-run-manifest-v1",
        "datasetId": base.DATASET_ID, "runId": run_id, "profile": PROFILE,
        "model": model, "endpointOrigin": "https://api.deepseek.com", "temperature": 0,
        "startedAt": started.isoformat(), "completedAt": completed_at.isoformat(),
        "scenarioCount": 18, "completedCount": sum(item["status"] == "COMPLETED" for item in cases),
        "failedCount": sum(item["status"] == "FAILED" for item in cases),
        "publicSha256": base._sha256_path(base.PUBLIC_PATH),
        "humanLanguageEvidenceSha256": base.EXPECTED_LANGUAGE_EVIDENCE_SHA,
        "humanSemanticEvidenceSha256": base.EXPECTED_SEMANTIC_EVIDENCE_SHA,
        "frozenBaselineRunnerSha256": base._sha256_path(Path(base.__file__).resolve()),
        "runnerSha256": base._sha256_path(Path(__file__).resolve()),
        "stateSchemaSha256": base._sha256_path(STATE_SCHEMA_PATH),
        "predictionSchemaSha256": base._sha256_path(base.PREDICTION_SCHEMA_PATH),
        "preregistrationSha256": base._sha256_path(PREREGISTRATION_PATH),
        "casesSha256": base._sha256_bytes(case_bytes), "receiptsSha256": base._sha256_bytes(receipt_bytes),
        "compilerType": "DETERMINISTIC", "modelRepairCalls": 0, "toolCalls": 0,
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


ModelCall = base.ModelCall
