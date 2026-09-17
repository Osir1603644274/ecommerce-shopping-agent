"""Check whether saved textual-null goals survive pure business validation."""
import json
from openai.types.chat import ChatCompletion
from agent.app import llm
from .common import HERE, check_freeze, file_sha, json_new, rows
from .datasets import synthetic_state


def main():
    check_freeze()
    source = HERE / "p2/attempt001"
    requests = rows(source / "private_requests.jsonl")
    responses = {r["requestId"]: r["response"] for r in rows(source / "private_responses.jsonl")}
    last = {}
    for request in requests:
        b = request["binding"]
        last[(b["caseId"], b["block"], b["strict"])] = request
    cases = {c["caseId"]: c for c in json.loads((HERE / "p1/extraction_cases.json").read_text(encoding="utf-8"))}
    findings = []
    for outcome in rows(source / "cases.jsonl"):
        if outcome["status"] != "PASS": continue
        request = last[(outcome["caseId"], outcome["block"], outcome["strict"])]
        response = ChatCompletion.model_validate(responses[request["requestId"]])
        call = next(x for x in response.choices[0].message.tool_calls if x.function.name == llm.TASK_STATE_TOOL_NAME)
        args = llm._parse_task_state_arguments(call, response=response, strict=outcome["strict"])
        if args.get("goal") != "null": continue
        state = synthetic_state(outcome["caseId"])
        payload, _ = llm._build_validated_task_state_payload(state, args,
            message=cases[outcome["caseId"]]["message"], require_status=True)
        findings.append({"requestId": request["requestId"], "caseId": outcome["caseId"],
            "block": outcome["block"], "strict": outcome["strict"], "previousGoal": state.goal,
            "proposedGoal": payload.get("goal", state.goal), "textualNullSurvivesValidation": payload.get("goal") == "null"})
    json_new(HERE / "p2/goal_marker_validation.json", {"kind": "SUPPLEMENTARY_OFFLINE_SEMANTIC_AUDIT_NOT_PRIMARY_RESCORING",
        "modelCalls": 0, "statePersists": 0, "findingCount": len(findings),
        "survivesValidation": sum(r["textualNullSurvivesValidation"] for r in findings),
        "findings": findings, "validatorSourceSha256": file_sha(llm.__file__)})
    print(json.dumps({"findings": len(findings), "survivesValidation": sum(r["textualNullSurvivesValidation"] for r in findings), "modelCalls": 0}))


if __name__ == "__main__": main()
