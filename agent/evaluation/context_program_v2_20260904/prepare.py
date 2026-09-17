"""P0/P1 preparation. No provider calls, no production mutations."""
import json
import subprocess
from datetime import datetime, timedelta, timezone
from xml.etree import ElementTree as ET

from .common import HERE, ROOT, CAPS, check_freeze, file_sha, json_new, manifest_check, sha
from .datasets import conversations, extraction_cases


def main():
    from agent.app.settings import settings
    if (HERE / "p0").exists():
        raise RuntimeError("P0_already_exists_no_overwrite")
    repair = ROOT / "agent/evaluation/context_failure_repair_20260904"
    old = ROOT / "agent/evaluation/context_program_v1_20260904"
    reports = [manifest_check(repair / "SHA256SUMS.txt"), manifest_check(old / "FINAL_SHA256SUMS_v2.txt")]
    if any(r["mismatches"] for r in reports):
        raise RuntimeError("historical_evidence_drift")
    changes = json.loads((repair / "source-changes.json").read_text(encoding="utf-8"))
    if any(file_sha(r["path"]) != r["afterSha256"] for r in changes["sources"]):
        raise RuntimeError("repair_source_drift")
    suites = []
    for name in ("pytest-targeted-final.xml", "pytest-durable-context-final.xml"):
        for suite in ET.parse(repair / name).getroot().iter("testsuite"):
            suites.append(dict(suite.attrib))
    if sum(int(s["tests"]) for s in suites) != 445 or any(int(s[k]) for s in suites for k in ("failures", "errors", "skipped")):
        raise RuntimeError("repair_gate_not_passed")
    instant = datetime.now(timezone.utc)
    json_new(HERE / "p0/contract.json", {"programId": HERE.name, "startedAt": instant.isoformat(),
        "deadlineAt": (instant + timedelta(hours=12)).isoformat(), "phaseRequestCaps": CAPS,
        "requestCap": 3000, "tokenCap": 10_000_000, "sdkRetries": 0,
        "maxStructuredRepair": 1, "formalRetryPolicy": "NO_AUTOMATIC_RETRY",
        "endpoint": "https://api.deepseek.com/beta", "model": settings.deepseek_model,
        "extractionMaxTokens": 4096, "thinking": "disabled", "productionDefaultsChanged": False,
        "successFloor": .98, "noninferiorityMargin": .03, "tokenRatio": .9,
        "p95Ratio": 1.2, "zeroHardRequirementErrors": True})
    json_new(HERE / "p0/repair_and_history.json", {"manifests": reports, "sourceChecks": changes["sources"], "testSuites": suites})
    paths = sorted((ROOT / "agent/app").rglob("*.py"))
    paths += sorted(HERE.glob("*.py"))
    paths += [p for p in (ROOT / ".env", ROOT / "agent/.env") if p.is_file()]
    paths += [ROOT / "agent/evaluation/real_user_multiturn_ab_executor_20260902_v2/lane_runtime.py",
              ROOT / "agent/evaluation/real_user_multiturn_ab_executor_20260902_v2/runner.py"]
    status = subprocess.check_output(["git", "status", "--porcelain=v1", "-z"], cwd=ROOT)
    json_new(HERE / "p0/source_freeze.json", {"sources": [
        {"path": p.relative_to(ROOT).as_posix(), "sha256": file_sha(p)} for p in paths],
        "head": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
        "preexistingWorktreeStatusSha256": __import__("hashlib").sha256(status).hexdigest(),
        "settings": {"model": settings.deepseek_model, "context": settings.agent_context_mode,
            "strict": settings.task_state_extraction_strict_enabled, "maxTokens": settings.task_state_extraction_max_tokens,
            "multiAgent": settings.multi_agent_v2_enabled, "memory": settings.memory_projection_client_enabled,
            "authority": settings.shopping_state_authority, "runtime": settings.agent_control_runtime}})
    historical = old / "p1/conversations.jsonl"
    json_new(HERE / "p1/history.json", {"path": str(historical), "sha256": file_sha(historical), "role": "REGRESSION_ONLY"})
    json_new(HERE / "p1/extraction_cases.json", extraction_cases())
    dev, confirm = conversations("dev", 16), conversations("confirm", 64)
    json_new(HERE / "p1/development.json", dev)
    json_new(HERE / "p1/confirmation.json", confirm)
    json_new(HERE / "p1/dataset_manifest.json", {"datasets": [
        {"path": str(p.relative_to(HERE)), "sha256": file_sha(p)} for p in sorted((HERE / "p1").glob("*.json"))],
        "developmentConversations": 16, "confirmationConversations": 64,
        "developmentFamilies": len({x["familyId"] for x in dev}),
        "confirmationFamilies": len({x["familyId"] for x in confirm}),
        "familyDisjoint": not ({x["familyId"].split(":", 1)[1] for x in dev} & {x["familyId"].split(":", 1)[1] for x in confirm}),
        "independentHumanHoldout": False,
        "confirmatoryStatisticalPower": "INSUFFICIENT_FOR_GENERAL_POPULATION_NONINFERIORITY; only four generator families; bounded engineering confirmation only"})
    check_freeze()
    json_new(HERE / "p0/result.json", {"status": "PASS", "repairTests": 445,
        "repairManifestEntries": 7, "historicalManifestEntries": 22, "frozenSources": len(paths), "modelCalls": 0})
    print("P0 PASS; P1 synthetic development=16 confirmation=64; only 2/4 independent template families; no broad noninferiority claim", flush=True)


if __name__ == "__main__":
    main()
