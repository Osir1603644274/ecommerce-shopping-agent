"""Bounded, synthetic strict-provider smoke. No business tools or state writes.

Run with --execute --output <new-receipt.json>. At most four provider requests
(two cases, one repair each); automatic SDK retries are disabled. Not P4 A/B.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from openai import AsyncOpenAI

from app import llm
from app.domains.ecommerce.shopping_state_authority import bind_authoritative_write
from app.task_state import TaskState


def sha(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def synthetic_state():
    now = datetime.now(timezone.utc)
    return TaskState(
        taskId="task-context-repair-synthetic", taskType="ecommerce_guide",
        sessionId="session-context-repair-synthetic", revision=3, status="ready",
        goal="预算2200以内，只看iOS手机", unknowns=[], pendingQuestions=[],
        domainState={"shoppingGuide": {
            "mode": "recommend", "category": "phone", "useCases": [],
            "requirements": [
                {"key": "os", "operator": "eq", "value": "ios", "unit": "enum", "priority": "hard", "source": "user"},
                {"key": "price_minor", "operator": "lte", "value": 220000, "unit": "CNY_MINOR", "priority": "hard", "source": "user"},
            ], "candidateIds": [], "comparedIds": [], "evidenceStatus": "missing",
        }}, createdAt=now, updatedAt=now,
    )


async def run():
    receipts, cases = [], []
    old = (llm.settings.deepseek_base_url, llm.settings.task_state_extraction_strict_enabled)
    # This process only; do not edit .env or mutate another service's defaults.
    llm.settings.deepseek_base_url = "https://api.deepseek.com/beta"
    llm.settings.task_state_extraction_strict_enabled = True
    try:
        async with AsyncOpenAI(
            api_key=llm.settings.deepseek_api_key, base_url=llm.settings.deepseek_base_url,
            timeout=35, max_retries=0,
        ) as provider:
            async def create(**kwargs):
                if len(receipts) >= 4:
                    raise RuntimeError("provider_request_budget_exhausted")
                receipt = {"requestSha256": sha(kwargs), "maxTokens": kwargs.get("max_tokens"),
                           "strict": kwargs["tools"][0]["function"].get("strict"),
                           "thinking": kwargs.get("extra_body", {}).get("thinking")}
                receipts.append(receipt)
                try:
                    response = await provider.chat.completions.create(**kwargs)
                    receipt.update({"status": "SUCCEEDED", "finishReason": response.choices[0].finish_reason,
                                    "usage": response.usage.model_dump(), "responseSha256": sha(response.model_dump())})
                    return response
                except Exception as exc:
                    receipt.update({"status": "FAILED", "errorType": type(exc).__name__,
                                    "httpStatus": getattr(exc, "status_code", None)})
                    raise

            client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
            for case_id, message, expected_budget in [
                ("budget_override", "预算改为1600元，其他硬条件不变。", 160000),
                ("preserve_hard_requirements", "前面那些硬条件都别放宽，再把日常稳定性也考虑进去。", 220000),
            ]:
                state = synthetic_state()
                messages = [
                    {"role": "system", "content": llm.TASK_STATE_PLANNING_PROMPT},
                    {"role": "system", "content": "当前已持久化状态：" + state.model_dump_json(by_alias=True)},
                    {"role": "user", "content": message},
                ]
                case = {"caseId": case_id, "status": "FAILED", "repairUsed": False}
                cases.append(case)
                try:
                    call, response = await llm._submit_task_state_extraction(client, messages)
                    try:
                        args = llm._parse_task_state_arguments(call, response=response, strict=True)
                        payload, _ = llm._build_validated_task_state_payload(
                            state, args, message=message, require_status=True,
                        )
                    except llm.TaskStatePayloadValidationError as exc:
                        if call is None:
                            raise
                        case["repairUsed"] = True
                        args, _ = await llm._repair_task_state_payload(
                            client, planning_messages=messages, original_call=call, validation_error=exc,
                        )
                        payload, _ = llm._build_validated_task_state_payload(
                            state, args, message=message, require_status=True,
                        )
                    domain = {**state.domain_state, **payload["domainStatePatch"]}
                    domain = {key: value for key, value in domain.items() if value is not None}
                    bound = bind_authoritative_write(
                        domain, task_id=state.task_id, task_revision=state.revision + 1,
                        goal=payload.get("goal", state.goal), unknowns=payload.get("addUnknowns", []),
                        pending_questions=payload.get("pendingQuestions", []),
                    )
                    requirements = bound["shoppingGuide"]["requirements"]
                    values = {item["key"]: item["value"] for item in requirements}
                    # A native sparse patch omits an unchanged status after
                    # validation; the effective state remains ready.
                    assert payload.get("status", state.status) == "ready"
                    assert values["price_minor"] == expected_budget and values["os"] == "ios"
                    assert all(item["priority"] == "hard" for item in requirements if item["key"] in {"price_minor", "os"})
                    case.update({"status": "PASS", "decodedPatchSha256": sha(args),
                                 "boundStateSha256": sha(bound)})
                except Exception as exc:
                    case.update({"errorType": type(exc).__name__, "errorCode": getattr(exc, "code", None)})
                    break  # Fail closed; do not spend the remaining request budget.
    finally:
        llm.settings.deepseek_base_url, llm.settings.task_state_extraction_strict_enabled = old
    return {"schemaVersion": "context-failure-repair-smoke-v1", "kind": "synthetic_integration_smoke_not_ab",
            "runnerSha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            "status": "PASS" if len(cases) == 2 and all(c["status"] == "PASS" for c in cases) else "FAILED",
            "model": llm.settings.deepseek_model, "automaticRetries": 0, "maxProviderCalls": 4,
            "businessToolCalls": 0, "statePersists": 0, "calls": receipts, "cases": cases}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not args.execute:
        parser.error("--execute is required")
    # Exclusive creation before any API call; old receipts cannot be replaced.
    with args.output.open("x", encoding="utf-8") as stream:
        result = asyncio.run(run())
        json.dump(result, stream, ensure_ascii=False, indent=2)
    print(json.dumps({"status": result["status"], "providerCalls": len(result["calls"]), "cases": result["cases"]}))
    return 0 if result["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
