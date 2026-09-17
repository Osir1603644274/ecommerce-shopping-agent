"""444 bounded native Codex executions with immutable paired inputs and ledgers."""
import argparse
import asyncio
from collections import Counter
import json
import os
import subprocess
import tempfile
import time
from .common import *
from .dataset import build_context, requirements

NOTICE = "Code Mode is unavailable because code-mode host is disabled. Code mode will fail closed; enable `features.code_mode_host` and install `codex-code-mode-host`."

def preflight(attempt):
    manifest = verify()
    gate=read(HERE / 'offline_gate.json')
    assert gate['passed']==5 and gate['selftestHash']==file_sha(HERE / 'selftest.py') and gate['manifestHash']==file_sha(HERE / 'inputs/manifest.json')
    out = HERE / attempt
    out.mkdir(exist_ok=False)
    cwd = Path(tempfile.mkdtemp(prefix="context-abc-study-"))
    overrides = p.effective_overrides(cwd)
    overrides["model_instructions_file"] = str(HERE / "inputs/base_instructions.txt")
    login = subprocess.run([p.CODEX, "login", "status"], capture_output=True, text=True, env=p.clean_env())
    if login.returncode or "Logged in using ChatGPT" not in login.stdout + login.stderr:
        raise RuntimeError("ChatGPT_auth_required_no_API_fallback")
    runtime = {"at": now(), "cwd": str(cwd), "overrides": overrides, "codex": p.CODEX,
        "cliVersion": subprocess.check_output([p.CODEX, "--version"], text=True).strip(), "auth": "ChatGPT",
        "cliBinaryHash": file_sha(p.CODEX),
        "proxy": {k:v for k,v in p.clean_env().items() if k in {"HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY"}},
        "codeFiles": {name: file_sha(HERE / name) for name in ("common.py", "dataset.py", "runner.py", "selftest.py", "README.md", "offline_gate.json")},
        "inputManifestHash": file_sha(HERE / "inputs/manifest.json"), "safety": "read-only; no tool actions; no business state persistence"}
    write_new(out / "runtime.json", runtime)
    for name in runtime["codeFiles"]:
        (out / name).write_bytes((HERE / name).read_bytes())
    probes = []
    for i in range(2):
        marker = f"ABC_STUDY_PROBE_{i}_ONLY_REPLY_OK"
        proc = subprocess.run([p.CODEX, *p.cli_options(overrides), "debug", "prompt-input", marker],
            cwd=cwd, env=p.clean_env(), capture_output=True, encoding="utf-8", timeout=120,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        write_new(out / f"prompt_probe_{i}.json", {"exitCode": proc.returncode, "stdout": proc.stdout, "stderr": proc.stderr})
        if proc.returncode: raise RuntimeError("prompt_export_failed")
        export = json.loads(proc.stdout)
        encoded = canonical(export)
        for forbidden in ("memory_summary", "MEMORY.md", "<response-annotations>", "0904.md", "<subagents>"):
            if forbidden in encoded: raise RuntimeError("forbidden_context:" + forbidden)
        if i == 1 and "ABC_STUDY_PROBE_0" in encoded: raise RuntimeError("prior_probe_leak")
        probes.append([{"role": e.get("role"), "content": e.get("content")} for e in export[:-1]])
    cases = [read(HERE / "inputs" / f"case-{i:03d}.json") for i in range(1,49)]
    write_new(out / "preflight.json", {"status": "READY_FOR_FIXED_STAGE_STUDY", "at": now(),
        "fixtureCount": len(cases), "nativeProbePrefixEqual": probes[0] == probes[1],
        "nativePrefixHashes": [sha(v) for v in probes], "commonHostContextPresent": True,
        "nativeProviderWireComplete": False, "sampledModelCalls": 0,
        "lengthBins": manifest["lengthBins"], "sameInputBC": manifest["sameInputBC"],
        "scope": "application text compression, AI answer quality, and Codex-workflow latency; not pure inference or native request token savings"})
    print(canonical(read(out / "preflight.json")), flush=True)

def check_code(runtime):
    if file_sha(runtime["codex"]) != runtime["cliBinaryHash"]: raise RuntimeError("cli_binary_drift")
    if file_sha(HERE / "inputs/manifest.json") != runtime["inputManifestHash"]: raise RuntimeError("manifest_drift")
    for name, digest in runtime["codeFiles"].items():
        if file_sha(HERE / name) != digest: raise RuntimeError("runner_source_drift:" + name)

def execute(prompt, runtime, directory, schema=None, timeout=300):
    directory.mkdir(exist_ok=False)
    (directory / "prompt.txt").write_text(prompt, encoding="utf-8")
    args = [runtime["codex"], *p.cli_options(runtime["overrides"]), "exec", "--ephemeral", "--skip-git-repo-check", "--json", "--color", "never"]
    if schema:
        write_new(directory / "output.schema.json", schema)
        args += ["--output-schema", str(directory / "output.schema.json")]
    args += ["-"]
    write_new(directory / "request.json", {"args": args, "promptHash": sha(prompt), "applicationTextTokens": tokens(prompt),
        "schemaHash": sha(schema) if schema else None, "schemaTextTokens": tokens(canonical(schema)) if schema else 0})
    started = time.monotonic()
    with (directory / "events.jsonl").open("x", encoding="utf-8") as output, (directory / "stderr.txt").open("x", encoding="utf-8") as error:
        proc = subprocess.Popen(args, cwd=runtime["cwd"], env=p.clean_env(), stdin=subprocess.PIPE,
            stdout=output, stderr=error, text=True, encoding="utf-8", creationflags=getattr(subprocess,"CREATE_NO_WINDOW",0))
        write_new(directory / "process.json", {"pid": proc.pid, "at": now(), "timeoutSeconds": timeout})
        try:
            proc.communicate(prompt, timeout=timeout)
            timed_out = False
        except subprocess.TimeoutExpired:
            print(canonical({"event": "HARD_TIMEOUT_TERMINATING_OWN_CHILD", "pid": proc.pid, "directory": directory.name}), flush=True)
            proc.kill(); proc.communicate(timeout=10); timed_out = True
    wall = (time.monotonic()-started)*1000
    events, malformed = [], []
    for line in (directory / "events.jsonl").read_text(encoding="utf-8").splitlines():
        try: events.append(json.loads(line))
        except ValueError:
            if line.strip(): malformed.append(line[:300])
    final = [e["item"]["text"] for e in events if e.get("type") == "item.completed" and e.get("item",{}).get("type") == "agent_message"]
    terminal = [e for e in events if e.get("type") == "turn.completed"]
    notices = [e for e in events if e.get("item",{}).get("type") == "error" and e["item"].get("message") == NOTICE]
    tool_events = [e for e in events if e.get("type", "").startswith("item.") and e.get("item",{}).get("type") not in {"agent_message", "reasoning", "error"}]
    errors = [e for e in events if e.get("type") in {"error", "turn.failed"} or (e.get("item",{}).get("type") == "error" and e not in notices)]
    usage = terminal[0].get("usage") if len(terminal) == 1 else None
    answer = final[-1] if final else ""
    success = proc.returncode == 0 and bool(answer) and usage is not None and not tool_events and not malformed
    return {"status": "COMPLETED_WITH_TRANSPORT_WARNINGS" if success and errors else "COMPLETED" if success else "FAILED",
        "exitCode": proc.returncode, "timeout": timed_out, "cliWallMs": wall, "answer": answer,
        "usage": usage, "usageScope": "CODEX_TURN_NOT_APPLICATION_TEXT", "nativeRequestCount": None,
        "threadIds": [e["thread_id"] for e in events if e.get("type") == "thread.started"],
        "errors": errors, "startupNotices": notices, "toolEvents": tool_events, "malformedLines": malformed,
        "terminalCount": len(terminal), "at": now()}

def validate(fixture, answer):
    if fixture["stage"] == "final":
        return {"textNonempty": bool(answer.strip()), "quality": "PENDING_BLIND_JUDGMENT"}
    llm, _, _, _, _, State, _, decoder, _ = p.load_app()
    args = decoder(json.loads(answer), llm.TASK_STATE_TOOL_SCHEMA)
    payload, _ = llm._build_validated_task_state_payload(State.model_validate(fixture["state"]), args,
        message=fixture["query"], require_status=True)
    guide = payload.get("domainStatePatch", {}).get("shoppingGuide", {})
    actual = {r["key"]: r["value"] for r in guide.get("requirements", []) if r.get("priority") == "hard"}
    expected = fixture["oracle"]["expectedHard"]
    tracked = {"price_minor", "storage_gb", "os"}
    actual_typed = [{k: r.get(k) for k in ("key", "operator", "unit", "value", "priority")}
        for r in guide.get("requirements", []) if r.get("priority") == "hard" and r.get("key") in tracked]
    expected_typed = [{k: r[k] for k in ("key", "operator", "unit", "value", "priority")} for r in requirements(expected)]
    correct = sorted(actual_typed, key=canonical) == sorted(expected_typed, key=canonical) and all(k not in actual for k in fixture["oracle"]["absentKeys"])
    return {"strictWireAccepted": True, "businessValidatorAccepted": True, "hardConditionsCorrect": correct,
        "actualHard": actual, "expectedHard": expected, "serverPayloadHash": sha(payload), "persisted": False}

def run(attempt):
    manifest = verify()
    out = HERE / attempt
    runtime = read(out / "runtime.json")
    check_code(runtime)
    assert read(out / "preflight.json")["status"] == "READY_FOR_FIXED_STAGE_STUDY"
    if (out / "ledger.jsonl").exists(): raise RuntimeError("existing_ledger_no_implicit_resume")
    # Warm local imports/tokenizer before measurement. No model warmup calls.
    p.load_app(); tokens("warm local tokenizer")
    start_batch = time.monotonic()
    errors_in_row = 0
    try:
        for row in read(HERE / "inputs/schedule.json"):
            check_code(runtime); verify()
            if time.monotonic()-start_batch > manifest["durationLimitSeconds"]: raise RuntimeError("batch_timeout")
            f = read(HERE / "inputs" / (row["caseId"] + ".json"))
            directory = out / "calls" / f'{row["ordinal"]:04d}-{row["caseId"]}-{row["arm"]}'
            directory.parent.mkdir(exist_ok=True)
            append(out / "ledger.jsonl", {"event": "START", **row, "at": now()})
            print(canonical({"event": "START", **row}), flush=True)
            start_stage = time.monotonic()
            prompt, context, receipt = asyncio.run(build_context(f, row["arm"]))
            build_ms = (time.monotonic()-start_stage)*1000
            if prompt != f["prompts"][row["arm"]]: raise RuntimeError("rebuilt_prompt_drift:" + f["id"])
            result = execute(prompt, runtime, directory, f["schema"])
            validation_start = time.monotonic()
            try: validation = validate(f, result["answer"]) if result["answer"] else {"error": "no_answer"}
            except Exception as exc: validation = {"errorType": type(exc).__name__, "error": str(exc)[:500]}
            validation_ms = (time.monotonic()-validation_start)*1000
            result.update({**row, "stage": f["stage"], "family": f["source"]["family"], "sourceKind": f["source"]["sourceKind"],
                "lengthBin": f["lengthBin"], "candidateCount": f["candidateCount"], "applicationInputTokens": tokens(prompt),
                "applicationOutputTokens": tokens(result["answer"]), "schemaTextTokens": tokens(canonical(f["schema"])) if f["schema"] else 0,
                "contextBuildMs": build_ms, "validationMs": validation_ms, "stageWallMs": (time.monotonic()-start_stage)*1000,
                "validation": validation, "contextHash": sha(context), "fixtureHash": file_sha(HERE / "inputs" / (f["id"] + ".json"))})
            if receipt: write_new(directory / "compiler_receipt.json", receipt)
            write_new(directory / "result.json", result)
            append(out / "ledger.jsonl", {"event": "END", **row, "at": now(), "status": result["status"],
                "resultHash": file_sha(directory / "result.json"), "requestHash": file_sha(directory / "request.json")})
            append(out / "progress.jsonl", {"completed": row["ordinal"], "total": 444, "status": result["status"],
                "caseId": f["id"], "arm": row["arm"], "cliSeconds": result["cliWallMs"]/1000, "at": now()})
            print(canonical({"event": "END", "ordinal": row["ordinal"], "status": result["status"],
                "appTokens": result["applicationInputTokens"], "cliSeconds": result["cliWallMs"]/1000, "validation": validation}), flush=True)
            if result["toolEvents"] or result["malformedLines"]: raise RuntimeError("execution_boundary_failure")
            errors_in_row = errors_in_row + 1 if result["status"] == "FAILED" else 0
            if errors_in_row >= 3: raise RuntimeError("three_consecutive_provider_failures")
            if any(x in canonical(result["errors"]).lower() for x in ("unauthorized", "usage limit", "quota exceeded", "insufficient_quota", "rate limit reached")):
                raise RuntimeError("account_or_quota_requires_attention_no_reset")
        write_new(out / "run_complete.json", {"at": now(), "status": "444_EXECUTIONS_FINISHED", "durationSeconds": time.monotonic()-start_batch})
    except Exception as exc:
        write_new(out / "run_stop.json", {"at": now(), "status": "STOPPED_WITH_EVIDENCE", "errorType": type(exc).__name__, "error": str(exc)})
        raise

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("phase", choices=["preflight", "run"])
    parser.add_argument("--attempt", default="attempt001")
    args = parser.parse_args()
    if not args.attempt.isalnum(): raise ValueError("invalid_attempt")
    (preflight if args.phase == "preflight" else run)(args.attempt)
