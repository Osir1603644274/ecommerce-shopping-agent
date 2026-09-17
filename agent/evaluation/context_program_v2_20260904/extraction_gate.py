"""P2: 24 fixed cases x two modes x two blocks. No state persistence."""
import argparse
import asyncio
import json
from collections import Counter

from openai import AsyncOpenAI
from agent.app import llm
from agent.app.domains.ecommerce.shopping_state_authority import bind_authoritative_write
from .common import HERE, RecordedClient, BudgetStop, append, check_freeze, file_sha, json_new, now
from .datasets import check_requirements, synthetic_state


async def run():
    check_freeze()
    out = HERE / "p2/attempt001"
    out.mkdir(parents=True, exist_ok=False)
    cases_path = HERE / "p1/extraction_cases.json"
    cases = json.loads(cases_path.read_text(encoding="utf-8"))
    json_new(out / "started.json", {"at": now(), "pid": __import__("os").getpid(),
        "casesSha256": file_sha(cases_path), "runnerSha256": file_sha(__file__),
        "baseCalls": 96, "cap": 120, "dataClass": "synthetic shopping constraints",
        "provider": "DeepSeek official beta", "businessToolCalls": 0, "stateWrites": 0})
    results = []
    llm.settings.deepseek_base_url = "https://api.deepseek.com/beta"
    llm.settings.task_state_extraction_max_tokens = 4096
    try:
        async with AsyncOpenAI(api_key=llm.settings.deepseek_api_key, base_url=llm.settings.deepseek_base_url,
                               timeout=45, max_retries=0) as provider:
            client = RecordedClient(provider, phase="P2", output=out)
            for block in (1, 2):
                for index, case in enumerate(cases):
                    modes = (False, True) if (index + block) % 2 else (True, False)
                    for strict in modes:
                        llm.settings.task_state_extraction_strict_enabled = strict
                        client.binding = {"caseId": case["caseId"], "block": block, "strict": strict}
                        state = synthetic_state(case["caseId"])
                        messages = [{"role": "system", "content": llm.TASK_STATE_PLANNING_PROMPT},
                            {"role": "system", "content": "当前已持久化状态：" + state.model_dump_json(by_alias=True)},
                            {"role": "user", "content": case["message"]}]
                        result = {**client.binding, "familyId": case["familyId"], "status": "FAILED", "repairUsed": False}
                        try:
                            call, response = await llm._submit_task_state_extraction(client, messages)
                            try:
                                args = llm._parse_task_state_arguments(call, response=response, strict=strict)
                                payload, _ = llm._build_validated_task_state_payload(state, args, message=case["message"], require_status=True)
                            except llm.TaskStatePayloadValidationError as exc:
                                result["initialValidationError"] = getattr(exc, "code", type(exc).__name__)
                                if call is None: raise
                                result["repairUsed"] = True
                                args, _ = await llm._repair_task_state_payload(client, planning_messages=messages,
                                    original_call=call, validation_error=exc)
                                payload, _ = llm._build_validated_task_state_payload(state, args, message=case["message"], require_status=True)
                            domain = {**state.domain_state, **payload.get("domainStatePatch", {})}
                            domain = {k: v for k, v in domain.items() if v is not None}
                            bound = bind_authoritative_write(domain, task_id=state.task_id, task_revision=state.revision+1,
                                goal=payload.get("goal", state.goal), unknowns=payload.get("addUnknowns", state.unknowns),
                                pending_questions=payload.get("pendingQuestions", state.pending_questions))
                            result["oracleErrors"] = check_requirements(bound["shoppingGuide"], case)
                            result["effectiveGuide"] = bound["shoppingGuide"]
                            result["status"] = "PASS" if not result["oracleErrors"] else "SEMANTIC_FAILURE"
                        except BudgetStop:
                            raise
                        except Exception as exc:
                            result.update({"errorType": type(exc).__name__, "errorCode": getattr(exc, "code", None)})
                        results.append(result)
                        append(out / "cases.jsonl", result)
                        print(f"P2 {len(results)}/96 strict={strict} {case['caseId']} {result['status']}", flush=True)
                        if client.halted:
                            raise BudgetStop("three_consecutive_transport_errors")
    except Exception as exc:
        json_new(out / "stop.json", {"at": now(), "errorType": type(exc).__name__, "code": str(exc)[:180], "completed": len(results)})
    check_freeze()
    summaries = {str(mode): dict(Counter(r["status"] for r in results if r["strict"] == mode)) for mode in (False, True)}
    eligible = [mode for mode in (True, False) if len([r for r in results if r["strict"] == mode]) == 48
                and all(r["status"] == "PASS" for r in results if r["strict"] == mode)]
    result = {"status": "PASS" if eligible else "HOLD_EXTRACTION_SEMANTICS", "completed": len(results),
        "summary": summaries, "selectedStrict": eligible[0] if eligible else None,
        "selectionPolicy": "48/48 required; strict preferred when both pass; no failed-case deletion",
        "businessToolCalls": 0, "stateWrites": 0, "productionDefaultsChanged": False}
    json_new(out / "result.json", result)
    print(json.dumps(result), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--execute", action="store_true", required=True)
    parser.parse_args()
    asyncio.run(run())
