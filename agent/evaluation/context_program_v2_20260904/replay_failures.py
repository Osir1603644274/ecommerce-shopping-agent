"""Diagnose saved failed P2 responses offline; NEVER calls a provider."""
import json
from openai.types.chat import ChatCompletion
from agent.app import llm
from agent.app.domains.ecommerce.shopping_state_authority import bind_authoritative_write
from .common import HERE, append, check_freeze, file_sha, json_new, rows
from .datasets import check_requirements, synthetic_state


def main():
    check_freeze()
    source = HERE / "p2/attempt001"
    if not (source / "result.json").exists():
        raise RuntimeError("P2_must_finish_before_offline_diagnosis")
    out = HERE / "p2/offline_failure_analysis.json"
    if out.exists(): raise RuntimeError("refusing_overwrite")
    cases = {c["caseId"]: c for c in json.loads((HERE / "p1/extraction_cases.json").read_text(encoding="utf-8"))}
    failed = {(r["caseId"], r["block"], r["strict"]) for r in rows(source / "cases.jsonl") if r["status"] != "PASS"}
    requests = {r["requestId"]: r for r in rows(source / "private_requests.jsonl")}
    result = []
    for saved in rows(source / "private_responses.jsonl"):
        req = requests[saved["requestId"]]
        binding = req["binding"]
        if (binding["caseId"], binding["block"], binding["strict"]) not in failed: continue
        case = cases[binding["caseId"]]
        state = synthetic_state(case["caseId"])
        record = {"requestId": saved["requestId"], **binding, "message": case["message"]}
        try:
            response = ChatCompletion.model_validate(saved["response"])
            call = next((x for x in response.choices[0].message.tool_calls or [] if x.function.name == llm.TASK_STATE_TOOL_NAME), None)
            args = llm._parse_task_state_arguments(call, response=response, strict=binding["strict"])
            record["decodedArguments"] = args
            payload, _ = llm._build_validated_task_state_payload(state, args, message=case["message"], require_status=True)
            domain = {**state.domain_state, **payload.get("domainStatePatch", {})}
            bound = bind_authoritative_write({k: v for k,v in domain.items() if v is not None},
                task_id=state.task_id, task_revision=state.revision+1,
                goal=payload.get("goal", state.goal), unknowns=payload.get("addUnknowns", state.unknowns),
                pending_questions=payload.get("pendingQuestions", state.pending_questions))
            record["oracleErrors"] = check_requirements(bound["shoppingGuide"], case)
            record["offlineOutcome"] = "VALIDATED" if not record["oracleErrors"] else "SEMANTIC_FAILURE"
        except Exception as exc:
            record.update({"offlineOutcome": "REJECTED", "errorType": type(exc).__name__,
                "errorCode": getattr(exc, "code", None), "fieldPath": getattr(exc, "field_path", None),
                "errorMessage": str(exc)[:1500]})
        result.append(record)
    json_new(out, {"mode": "SAVED_RESPONSE_PURE_VALIDATION_REPLAY", "modelCalls": 0, "stateWrites": 0,
        "failedCaseExecutions": len(failed), "records": result,
        "inputs": {p.name: file_sha(p) for p in (source / "cases.jsonl", source / "private_requests.jsonl", source / "private_responses.jsonl")}})
    print(json.dumps([{k:v for k,v in r.items() if k != "decodedArguments"} for r in result], ensure_ascii=False, indent=2))


if __name__ == "__main__": main()
