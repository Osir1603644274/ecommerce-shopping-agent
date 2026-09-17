"""Deterministically recompute integrity checks, independently of report text."""
import json
from collections import Counter
from xml.etree import ElementTree as ET

from .common import HERE, ROOT, check_freeze, file_sha, json_new, manifest_check, rows, sha


def main():
    artifact_check = manifest_check(HERE / "SHA256SUMS.txt")
    source = check_freeze()
    ledger = rows(HERE / "provider_ledger.jsonl")
    start = [r for r in ledger if r["event"] == "START"]
    end = [r for r in ledger if r["event"] == "END"]
    case_rows = rows(HERE / "p2/attempt001/cases.jsonl")
    reqs = rows(HERE / "p2/attempt001/private_requests.jsonl")
    resps = rows(HERE / "p2/attempt001/private_responses.jsonl")
    req = {r["requestId"]: r for r in reqs}
    resp = {r["requestId"]: r for r in resps}
    final = json.loads((HERE / "FINAL_DECISION.json").read_text(encoding="utf-8"))
    p2 = json.loads((HERE / "p2/attempt001/result.json").read_text(encoding="utf-8"))
    usage = sum(r["usage"]["total_tokens"] for r in end)
    suite = list(ET.parse(HERE / "p8/full001/pytest-full.xml").getroot().iter("testsuite"))
    root_cases = list(ET.parse(HERE / "p8/full001/pytest-full.xml").getroot().iter("testcase"))
    checks = {
        "artifactHashes": not artifact_check["mismatches"],
        "requestLedgerIdsUnique": len({r["requestId"] for r in start}) == len(start),
        "responseLedgerIdsUnique": len({r["requestId"] for r in end}) == len(end),
        "requestResponseIdentitySets": {r["requestId"] for r in start} == set(req) == set(resp) == {r["requestId"] for r in end},
        "privateRecordsOneToOne": len(reqs) == len(resps) == len(start),
        "requestHashEdges": all(sha(req[r["requestId"]]["request"]) == r["requestSha256"] for r in start),
        "responseHashEdges": all(sha(resp[r["requestId"]]["response"]) == r["responseSha256"] for r in end),
        "actualRequestTokenCaps": all(r["request"]["max_tokens"] == 4096 for r in reqs),
        "actualStrictSetting": all(bool(r["request"]["tools"][0]["function"].get("strict")) == r["binding"]["strict"] for r in reqs),
        "allResponsesSingleToolCall": all(len(c["message"].get("tool_calls") or []) == 1 for r in resps for c in r["response"]["choices"]),
        "allAssignedCasesPresent": len(case_rows) == len({(r["caseId"],r["block"],r["strict"]) for r in case_rows}) == 96,
        "reportedPrimaryCounts": all(dict(Counter(r["status"] for r in case_rows if r["strict"] == mode)) == p2["summary"][str(mode)] for mode in (False, True)),
        "reportedCallsAndUsage": final["providerRequests"] == len(start) and final["providerTokens"] == usage,
        "repairsFullyCounted": len(start) == len(case_rows) + sum(r["repairUsed"] for r in case_rows),
        "noDependentProviderPhases": {r["phase"] for r in start} == {"P2"},
        "notPromotedToMainAB": final["plannedMainExperimentsComplete"] is False and final["allP0P8Passed"] is False,
        "fullJUnitSummaryMatches": all(final["fullSuite"][k] == sum(int(s.attrib.get(k,0)) for s in suite) for k in ("tests", "failures", "errors", "skipped")),
        "junitCaseCount": len(root_cases) == final["fullSuite"]["tests"],
        "oldManifest": not manifest_check(ROOT / "agent/evaluation/context_program_v1_20260904/FINAL_SHA256SUMS_v2.txt")["mismatches"],
        "repairManifest": not manifest_check(ROOT / "agent/evaluation/context_failure_repair_20260904/SHA256SUMS.txt")["mismatches"],
    }
    result = {"status": "PASS_ARTIFACT_INTEGRITY_NOT_PRODUCT_ACCEPTANCE" if all(checks.values()) else "FAIL",
        "checks": checks, "artifacts": artifact_check["checked"], "sourceFiles": len(source["sources"]),
        "manifestSha256": file_sha(HERE / "SHA256SUMS.txt"), "requests": len(start), "tokens": usage}
    json_new(HERE / "verification.json", result)
    print(json.dumps(result), flush=True)


if __name__ == "__main__": main()
