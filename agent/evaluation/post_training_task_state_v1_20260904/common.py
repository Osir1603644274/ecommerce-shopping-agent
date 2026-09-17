from __future__ import annotations

import hashlib
import json
import math
import re
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


PACKAGE_DIR = Path(__file__).resolve().parent
AGENT_DIR = PACKAGE_DIR.parents[1]
REPO_DIR = AGENT_DIR.parent
if str(AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(AGENT_DIR))

SCHEMA_VERSION = "shopping-taskstate-posttraining-v1"
DATASET_ID = "shopping-taskstate-posttraining-v1-20260904"
MODEL_ID = "Qwen/Qwen2.5-1.5B-Instruct"
MODEL_REVISION = "775b11afaf83e0dc75bd5abaf90133e47b3ec082"
MODEL_LICENSE = "Apache-2.0"
MODEL_WEIGHTS_SHA256 = "dd924a11b4c220f385b51ffa522daea7c9f3d850e31b162bb5661df483c6d3ee"

FORBIDDEN_SOURCE_MARKERS = (
    "sealed",
    "validation",
    "attempt",
    "private_evaluator",
    "state_oracle.private",
)

SYSTEM_PROMPT = """你是电商 Agent 的 Shopping TaskState Interpreter。
你只把本轮用户原话转成 update_task_state 的参数 JSON，不回答用户，不输出 Markdown，不输出解释。
顶层只允许 status、goal、upsertFacts、removeFactKeys、upsertConstraints、removeConstraintKeys、addUnknowns、resolveUnknowns、pendingQuestions、optionalShoppingQuestions、domainStatePatch。
每次必须显式给出 status。可执行时 status=ready、pendingQuestions=[]、addUnknowns=[]；真正缺少目标品类或比较对象时才 collecting_information，并同时给出 addUnknowns 与一句 pendingQuestions。
电商导购写入 domainStatePatch.shoppingGuide。初始状态使用 mode、category、requirements；已有 requirements 的后续轮只用 upsertRequirements/removeRequirementKeys。不得写 useCases、candidateIds、comparedIds、evidenceStatus、brandAvoidances 等服务端字段。
requirements 每项只能包含 key、operator、value、unit、priority、source。用户明确条件 priority=hard、source=user。明确排除使用 not_in 数组。不要虚构事实或候选商品。
category 只能是 phone、laptop、headphones。输出必须是单个合法 JSON 对象。"""


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_text(value: str) -> str:
    return sha256_bytes(value.encode("utf-8"))


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            value = json.loads(line)
            if not isinstance(value, dict):
                raise ValueError(f"{path}:{line_number} is not an object")
            rows.append(value)
    return rows


def write_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(canonical_json(row) + "\n")


def compact_state(state: dict[str, Any]) -> dict[str, Any]:
    domain = state.get("domainState") or {}
    return {
        "taskType": state["taskType"],
        "status": state["status"],
        "goal": state["goal"],
        "facts": state.get("facts", []),
        "constraints": state.get("constraints", []),
        "unknowns": state.get("unknowns", []),
        "pendingQuestions": state.get("pendingQuestions", []),
        "shoppingGuide": domain.get("shoppingGuide"),
    }


def render_user_prompt(record: dict[str, Any]) -> str:
    payload = {
        "currentTaskState": compact_state(record["state"]),
        "userMessage": record["userMessage"],
    }
    return "请提交本轮 update_task_state 参数：\n" + json.dumps(
        payload, ensure_ascii=False, sort_keys=True
    )


def render_messages(record: dict[str, Any]) -> list[dict[str, str]]:
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": render_user_prompt(record)},
    ]


def make_task_state(raw: dict[str, Any]):
    from app.task_state import TaskState

    return TaskState.model_validate(raw)


def validate_arguments(
    record: dict[str, Any], arguments: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, Any]]:
    from app.llm import _build_validated_task_state_payload

    return _build_validated_task_state_payload(
        make_task_state(record["state"]),
        arguments,
        message=record["userMessage"],
        require_status=True,
        allow_auto_ready=False,
    )


def _apply_keyed(
    existing: list[dict[str, Any]],
    upserts: list[dict[str, Any]],
    removals: list[str],
) -> list[dict[str, Any]]:
    values = {str(item["key"]): item for item in existing}
    for key in removals:
        values.pop(str(key), None)
    for item in upserts:
        values[str(item["key"])] = item
    return [values[key] for key in sorted(values)]


def semantic_effect(record: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    """Normalize one validated patch to its deterministic state effect.

    This removes harmless differences such as omitted empty arrays while retaining
    every user/model-owned field. Server receipts and identity clocks are excluded.
    """

    state = record["state"]
    domain_before = state.get("domainState") or {}
    domain_patch = payload.get("domainStatePatch") or {}
    unknowns = [
        value
        for value in state.get("unknowns", [])
        if value not in set(payload.get("resolveUnknowns", []))
    ]
    for value in payload.get("addUnknowns", []):
        if value not in unknowns:
            unknowns.append(value)
    guide = domain_patch.get("shoppingGuide", domain_before.get("shoppingGuide"))
    return {
        "status": payload.get("status", state["status"]),
        "goal": payload.get("goal", state["goal"]),
        "facts": _apply_keyed(
            state.get("facts", []),
            payload.get("upsertFacts", []),
            payload.get("removeFactKeys", []),
        ),
        "constraints": _apply_keyed(
            state.get("constraints", []),
            payload.get("upsertConstraints", []),
            payload.get("removeConstraintKeys", []),
        ),
        "unknowns": unknowns,
        "pendingQuestions": payload.get(
            "pendingQuestions", state.get("pendingQuestions", [])
        ),
        "shoppingGuide": guide,
        "optionalShoppingQuestions": domain_patch.get(
            "optionalShoppingQuestions", []
        ),
    }


def parse_json_object(text: str) -> tuple[dict[str, Any] | None, bool, str | None]:
    stripped = text.strip()
    strict = False
    try:
        value = json.loads(stripped)
        if isinstance(value, dict):
            strict = canonical_json(value) == canonical_json(json.loads(stripped))
            return value, strict, None
        return None, False, "json_root_not_object"
    except json.JSONDecodeError as exc:
        direct_error = f"json_decode:{exc.msg}"

    fenced = re.sub(r"^```(?:json)?\s*|\s*```$", "", stripped, flags=re.I)
    decoder = json.JSONDecoder()
    for match in re.finditer(r"\{", fenced):
        try:
            value, _ = decoder.raw_decode(fenced[match.start() :])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value, False, None
    return None, False, direct_error


def requirement_lanes(guide: Any) -> set[str]:
    if not isinstance(guide, dict):
        return set()
    lanes = set()
    for item in guide.get("requirements", []):
        if not isinstance(item, dict):
            continue
        polarity = "exclude" if item.get("operator") == "not_in" else "include"
        lanes.add(canonical_json({
            "key": item.get("key"),
            "polarity": polarity,
            "operator": item.get("operator"),
            "value": item.get("value"),
            "unit": item.get("unit"),
            "priority": item.get("priority"),
        }))
    return lanes


def score_rows(rows: list[dict[str, Any]]) -> dict[str, Any]:
    total = len(rows)
    valid = sum(bool(row["contractValid"]) for row in rows)
    exact = sum(bool(row["semanticExact"]) for row in rows)
    status_exact = sum(bool(row["statusExact"]) for row in rows)
    guide_exact = sum(bool(row["guideExact"]) for row in rows)
    strict_json = sum(bool(row["strictJson"]) for row in rows)
    parsed_json = sum(bool(row["parsedJson"]) for row in rows)
    false_ready = sum(bool(row["falseReady"]) for row in rows)
    false_clarify = sum(bool(row["falseClarify"]) for row in rows)
    gold_lanes = sum(int(row["goldRequirementLaneCount"]) for row in rows)
    predicted_lanes = sum(int(row["predictedRequirementLaneCount"]) for row in rows)
    matched_lanes = sum(int(row["matchedRequirementLaneCount"]) for row in rows)
    precision = matched_lanes / predicted_lanes if predicted_lanes else (1.0 if not gold_lanes else 0.0)
    recall = matched_lanes / gold_lanes if gold_lanes else 1.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    errors = Counter(
        row["errorCode"] for row in rows if isinstance(row.get("errorCode"), str)
    )
    latencies = [float(row["latencyMs"]) for row in rows]
    return {
        "rowCount": total,
        "strictJsonRate": strict_json / total if total else 0.0,
        "parsedJsonRate": parsed_json / total if total else 0.0,
        "contractValidRate": valid / total if total else 0.0,
        "semanticExactRate": exact / total if total else 0.0,
        "statusAccuracy": status_exact / total if total else 0.0,
        "guideAccuracy": guide_exact / total if total else 0.0,
        "requirementMicroPrecision": precision,
        "requirementMicroRecall": recall,
        "requirementMicroF1": f1,
        "falseReadyRate": false_ready / total if total else 0.0,
        "falseClarifyRate": false_clarify / total if total else 0.0,
        "meanLatencyMs": sum(latencies) / len(latencies) if latencies else None,
        "p95LatencyMs": (
            sorted(latencies)[max(0, math.ceil(0.95 * len(latencies)) - 1)]
            if latencies
            else None
        ),
        "errorCounts": dict(sorted(errors.items())),
    }


def fixed_timestamp() -> str:
    return datetime(2026, 9, 4, tzinfo=timezone.utc).isoformat()

