"""Post-run accounting and supplementary semantic warnings, without rescoring."""
import json
from collections import Counter, defaultdict
from importlib.metadata import version, PackageNotFoundError
import platform

from .common import HERE, ROOT, file_sha, json_new, rows, sha


def main():
    source = HERE / "p2/attempt001"
    cases = rows(source / "cases.jsonl")
    ledger = rows(HERE / "provider_ledger.jsonl")
    requests = rows(source / "private_requests.jsonl")
    responses = rows(source / "private_responses.jsonl")
    starts = [r for r in ledger if r["event"] == "START"]
    ends = [r for r in ledger if r["event"] == "END"]
    by_request = {r["requestId"]: r for r in requests}
    by_response = {r["requestId"]: r for r in responses}
    by_end = {r["requestId"]: r for r in ends}
    call_count = Counter((r["binding"]["caseId"], r["binding"]["block"], r["binding"]["strict"]) for r in starts)
    groups = defaultdict(list)
    marker_warnings = []
    for r in starts:
        key = (r["binding"]["caseId"], r["binding"]["block"], r["binding"]["strict"])
        groups[key].append(r["requestId"])
        for choice in by_response[r["requestId"]]["response"]["choices"]:
            for call in choice["message"].get("tool_calls") or []:
                payload = json.loads(call["function"]["arguments"])
                if payload.get("goal") == "null":
                    marker_warnings.append({"requestId": r["requestId"], "binding": r["binding"], "field": "goal",
                        "warning": "STRING_NULL_IS_VALID_STRING_BUT_NOT_NULL_OMISSION"})
    checks = {
        "all96AssignedExecutionsRecorded": len(cases) == 96 and len(call_count) == 96,
        "uniqueRequestIds": len({r["requestId"] for r in starts}) == len(starts),
        "oneEndPerStart": len(ends) == len(starts) and len(by_end) == len(starts),
        "allRequestHashesMatch": all(sha(by_request[r["requestId"]]["request"]) == r["requestSha256"] for r in starts),
        "allResponseHashesMatch": all(sha(by_response[r["requestId"]]["response"]) == r["responseSha256"] for r in ends),
        "allWireBudgets4096": all(r["effectiveMaxTokens"] == 4096 for r in starts),
        "allThinkingDisabled": all(r["thinking"] == {"type": "disabled"} for r in starts),
        "allSDKRetriesZero": all(r["sdkRetries"] == 0 for r in starts),
        "maxTwoCallsPerExecution": max(call_count.values()) <= 2,
        "repairDeclarationsMatchRequests": all(call_count[(r["caseId"],r["block"],r["strict"])] == 1 + int(r["repairUsed"]) for r in cases),
        "withinP2Cap120": len(starts) <= 120,
        "noUnknownUsage": all(r.get("usage") is not None for r in ends),
    }
    usage = {k: sum((r.get("usage") or {}).get(k, 0) or 0 for r in ends)
             for k in ("prompt_tokens", "completion_tokens", "total_tokens")}
    by_mode = {}
    for mode in (False, True):
        ids = {r["requestId"] for r in starts if r["binding"]["strict"] == mode}
        mode_cases = [r for r in cases if r["strict"] == mode]
        by_mode[str(mode)] = {"executions": len(mode_cases), "passed": sum(r["status"] == "PASS" for r in mode_cases),
            "repairExecutions": sum(r["repairUsed"] for r in mode_cases), "requests": len(ids),
            "totalTokens": sum(by_end[i]["usage"]["total_tokens"] for i in ids),
            "families": {f:dict(Counter(r["status"] for r in mode_cases if r["familyId"]==f)) for f in sorted({r["familyId"] for r in mode_cases})}}
    warning_ids = {r["requestId"] for r in marker_warnings}
    successful_final_markers = [r for r in cases if r["status"] == "PASS" and groups[(r["caseId"],r["block"],r["strict"])][-1] in warning_ids]
    json_new(HERE / "p8/call_accounting.json", {"status": "PASS" if all(checks.values()) else "FAIL", "checks": checks,
        "requests": len(starts), "usage": usage, "byMode": by_mode,
        "finishReasons": dict(Counter(r.get("finishReason") for r in ends)),
        "supplementaryGoalMarkerWarnings": marker_warnings, "goalMarkerInPrimaryPassedFinalResponses": len(successful_final_markers),
        "warningPolicy": "SUPPLEMENTARY_AUDIT_NOT_RETROACTIVE_PRIMARY_RESCORING", "modelCallsForAudit": 0})
    packages = {}
    for package in ("openai", "pydantic", "pydantic-settings", "langgraph", "redis", "pytest", "jsonschema"):
        try: packages[package] = version(package)
        except PackageNotFoundError: packages[package] = "UNAVAILABLE"
    json_new(HERE / "p8/environment.json", {"platform": platform.platform(), "python": platform.python_version(),
        "packages": packages, "providerModel": starts[0]["model"], "providerEndpoint": starts[0]["endpoint"]})
    print(json.dumps({"checks": checks, "usage": usage, "requests": len(starts),
        "supplementaryGoalMarkerWarnings": len(marker_warnings), "goalMarkerInPrimaryPassedFinalResponses": len(successful_final_markers)}), flush=True)


if __name__ == "__main__": main()
