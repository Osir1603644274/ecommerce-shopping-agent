"""Chat-completion shaped bridge to fresh, tool-isolated Codex subscription turns.

Business tools execute in the real Agent, never inside the CLI. Native Codex
turn usage includes host overhead and is not claimed to be provider wire usage.
"""
from __future__ import annotations

import asyncio
from copy import deepcopy
import json
import os
import re
from pathlib import Path
import subprocess
import tempfile
import time
import tomllib
from types import SimpleNamespace
import uuid

import jsonschema
from openai.types.chat import ChatCompletion
import tiktoken

from agent.evaluation.context_codex_abc_pilot_v1_20260904 import pilot
from .artifacts import append, canonical, file_sha, now, sha, write_new

ENVELOPE = {
    "type": "object", "additionalProperties": False,
    "properties": {
        "content": {"type": ["string", "null"]},
        "toolCalls": {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "properties": {"name": {"type": "string"}, "arguments": {"type": "string"}},
            "required": ["name", "arguments"]}},
    },
    "required": ["content", "toolCalls"],
}
KNOWN_WARNING = "Code Mode is unavailable because code-mode host is disabled. Code mode will fail closed; enable `features.code_mode_host` and install `codex-code-mode-host`."


def isolated_overrides(workdir):
    # Freeze the experiment model, not the user's current interactive model.
    # Read only server identifiers to disable external surfaces; never export config.
    user = tomllib.loads(pilot.USER_CONFIG.read_text(encoding="utf-8"))
    values = {"model": pilot.MODEL, "model_provider": "openai",
        "model_reasoning_effort": pilot.EFFORT, "service_tier": "default",
        "approval_policy": "never", "sandbox_mode": "read-only", "forced_login_method": "chatgpt",
        "project_doc_max_bytes": 0, "project_doc_fallback_filenames": [], "developer_instructions": "",
        "model_instructions_file": str(pilot.HERE / "inputs/base_instructions.txt"),
        "memories.use_memories": False, "memories.generate_memories": False, "agents.enabled": False,
        "web_search": "disabled", "tools.view_image": False, "history.persistence": "none",
        "model_auto_compact_token_limit": 1000000, "features.skip_host_skill_discovery": False,
        "suppress_unstable_features_warning": True, "log_dir": str(workdir / "logs"), "notify": []}
    values.update({"features." + key: False for key in pilot.DISABLED})
    for key in user.get("mcp_servers", {}):
        if not key.replace("_", "").replace("-", "").isalnum():
            raise ValueError("invalid_mcp_identifier")
        values[f"mcp_servers.{key}.enabled"] = False
    skill_files = sorted((Path(os.environ["USERPROFILE"]) / ".codex/skills").rglob("SKILL.md"))
    values["skills.config"] = [{"path": str(path.parent), "enabled": False} for path in skill_files]
    return values


def envelope_schema(request):
    schema = deepcopy(ENVELOPE)
    tools = request.get("tools") or []
    names = [item["function"]["name"] for item in tools]
    choice = request.get("tool_choice", "auto")
    if not names or choice == "none":
        schema["properties"]["toolCalls"]["maxItems"] = 0
        schema["properties"]["content"] = {"type": "string", "minLength": 1}
    elif isinstance(choice, dict):
        schema["properties"]["toolCalls"].update({"minItems": 1, "maxItems": 1})
        schema["properties"]["toolCalls"]["items"]["properties"]["name"]["enum"] = [choice["function"]["name"]]
    elif names:
        schema["properties"]["toolCalls"]["items"]["properties"]["name"]["enum"] = names
        if choice == "required":
            schema["properties"]["toolCalls"]["minItems"] = 1
    if request.get("parallel_tool_calls") is False:
        schema["properties"]["toolCalls"]["maxItems"] = 1 if names and choice != "none" else 0
    return schema


def decode_answer(answer, request):
    payload = json.loads(answer)
    jsonschema.validate(payload, envelope_schema(request))
    tools = {item["function"]["name"]: item["function"] for item in request.get("tools", [])}
    calls = payload["toolCalls"]
    choice = request.get("tool_choice", "auto")
    if choice == "none" and calls:
        raise ValueError("forbidden_tool_call")
    if choice == "required" and not calls:
        raise ValueError("required_tool_missing")
    if isinstance(choice, dict):
        required = choice["function"]["name"]
        if len(calls) != 1 or calls[0]["name"] != required:
            raise ValueError("named_tool_choice_mismatch")
    for call in calls:
        if call["name"] not in tools:
            raise ValueError("unknown_business_tool")
        arguments = json.loads(call["arguments"])
        jsonschema.validate(arguments, tools[call["name"]]["parameters"])
    if not calls and not (payload["content"] or "").strip():
        raise ValueError("empty_model_response")
    return payload


def recoverable_transport_event(event):
    """Only observed retry/fallback diagnostics, never terminal/host failures."""
    if event.get("type") == "error":
        return bool(re.fullmatch(
            r"Reconnecting\.\.\. [1-5]/5 \((?:stream disconnected before completion: tls handshake eof|Connection failed: error sending request)\)",
            event.get("message", "")))
    return (event.get("type") == "item.completed" and event.get("item", {}).get("type") == "error"
        and event["item"].get("message") == "Falling back from WebSockets to HTTPS transport. stream disconnected before completion: tls handshake eof")


def native_terminal_usage(lines):
    """Read an unambiguous completed usage receipt independently of acceptance.

    A failed process or rejected host event still incurs any proven native
    cost. Returning usage here never authorizes an answer or tool call.
    """
    events = [json.loads(line) for line in lines.splitlines() if line.strip()]
    terminals = [e for e in events if e.get("type") in {"turn.completed", "turn.failed"}]
    threads = [e for e in events if e.get("type") == "thread.started"]
    if len(terminals) != 1 or terminals[0]["type"] != "turn.completed" or len(threads) != 1:
        raise ValueError("native_usage_not_unambiguously_completed")
    usage = terminals[0].get("usage")
    if not isinstance(usage, dict) or any(type(usage.get(key)) is not int or usage[key] < 0
                                           for key in ("input_tokens", "output_tokens")):
        raise ValueError("missing_native_usage")
    for key, parent in (("cached_input_tokens", "input_tokens"), ("reasoning_output_tokens", "output_tokens")):
        subset = usage.get(key)
        if subset is not None and (type(subset) is not int or not 0 <= subset <= usage[parent]):
            raise ValueError("invalid_native_usage_subset")
    return usage, threads[0]["thread_id"]


def parse_events(lines, exit_code):
    events = [json.loads(line) for line in lines.splitlines() if line.strip()]
    terminal = [event for event in events if event.get("type") == "turn.completed"]
    threads = [event["thread_id"] for event in events if event.get("type") == "thread.started"]
    messages = [event["item"]["text"] for event in events
                if event.get("type") == "item.completed" and event.get("item", {}).get("type") == "agent_message"]
    for event in events:
        item = event.get("item", {})
        if recoverable_transport_event(event):
            continue  # A unique completed turn and zero exit are still required.
        if event.get("type") in {"error", "turn.failed"}:
            raise ValueError("codex_turn_failed")
        if event.get("type", "").startswith("item.") and item.get("type") not in {"reasoning", "agent_message"}:
            if item.get("type") != "error" or item.get("message") != KNOWN_WARNING:
                raise ValueError("unexpected_host_tool_or_event:" + str(item.get("type")))
    if exit_code or len(terminal) != 1 or len(threads) != 1 or not messages:
        raise ValueError("incomplete_codex_turn")
    usage, _ = native_terminal_usage(lines)
    return messages[-1], usage, threads[0]


class SubscriptionClient:
    def __init__(self, output: Path, *, timeout_seconds=300, max_calls=12, application_input_budget=None):
        self.application_input_budget = application_input_budget
        # Native CLI runs in an empty isolated cwd, so artifact/schema paths
        # must not remain relative to the caller's repository directory.
        self.output = Path(output).resolve()
        self.output.mkdir(parents=True, exist_ok=False)
        self.timeout_seconds = timeout_seconds
        self.max_calls = max_calls
        self.calls = []
        self.turn_start = None
        self.turn_limit = None
        self.workdir = Path(tempfile.mkdtemp(prefix="context-strategy-codex-"))
        self.overrides = isolated_overrides(self.workdir)
        # Keep validated host isolation settings and auth location. Never read credentials.
        self.env = pilot.clean_env()
        login = subprocess.run([pilot.CODEX, "login", "status"], env=self.env, capture_output=True,
                               text=True, encoding="utf-8", timeout=30,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if login.returncode or "Logged in using ChatGPT" not in login.stdout + login.stderr:
            raise RuntimeError("chatgpt_subscription_auth_required")
        write_new(self.output / "runtime.json", {"at": now(), "model": pilot.MODEL,
            "effort": pilot.EFFORT, "auth": "ChatGPT", "overrides": self.overrides,
            "workdir": str(self.workdir), "adapterSha256": file_sha(__file__),
            "isolationHelperSha256": file_sha(pilot.__file__),
            "cliVersion": subprocess.check_output([pilot.CODEX, "--version"], text=True).strip(),
            "usageScope": "CODEX_TURN_INCLUDING_HOST_OVERHEAD", "providerWireRequestCount": None,
            "applicationInputBudgetEveryCall": application_input_budget})
        self.chat = SimpleNamespace(completions=self)

    def begin_turn(self, max_calls=12):
        if type(max_calls) is not int or max_calls < 1:
            raise ValueError("invalid_turn_call_budget")
        self.turn_start, self.turn_limit = len(self.calls), max_calls

    async def create(self, **request):
        if request.get("stream"):
            raise ValueError("streaming_not_supported_no_fake_ttft")
        request_tokens = len(tiktoken.get_encoding("o200k_base").encode(canonical(request)))
        if self.application_input_budget is not None and request_tokens > self.application_input_budget:
            write_new(self.output / ("preflight-rejected-" + uuid.uuid4().hex + ".json"), {
                "code": "application_input_budget_exceeded", "applicationRequestTokens": request_tokens,
                "budget": self.application_input_budget, "modelCalled": False, "requestSha256": sha(request), "request": request})
            raise ValueError("every_call_application_input_budget_exceeded:" + str(request_tokens))
        if len(self.calls) >= self.max_calls:
            raise RuntimeError("development_model_call_ceiling")
        if self.turn_start is not None and len(self.calls) - self.turn_start >= self.turn_limit:
            raise RuntimeError("development_turn_model_call_ceiling")
        ordinal = len(self.calls) + 1
        record = {"ordinal": ordinal, "at": now(), "status": "STARTED", "usage": None}
        self.calls.append(record)
        out = self.output / f"call-{ordinal:03d}"
        out.mkdir(exist_ok=False)
        prompt = ("你正在替代一次购物 Agent 的模型调用，业务工具由外部 Agent 执行。"
            "严格按下列 messages 的角色和先后理解任务。只能返回 JSON envelope："
            "content 是回答正文或 null；toolCalls 是请求的业务函数列表。"
            "arguments 必须是该函数参数对象的合法 JSON 字符串，满足原始函数 schema。"
            "不要执行任何宿主工具，不要虚构业务工具已经执行。遵守 tool_choice。"
            "如果需要函数，仅返回调用；不需要则 toolCalls=[]。\n<model_request>\n"
            + canonical(request) + "\n</model_request>")
        schema = envelope_schema(request)
        write_new(out / "request.json", request)
        write_new(out / "schema.json", schema)
        with (out / "prompt.txt").open("x", encoding="utf-8") as stream:
            stream.write(prompt)
        args = [pilot.CODEX, *pilot.cli_options(self.overrides), "exec", "--ephemeral",
                "--skip-git-repo-check", "--json", "--color", "never",
                "--output-schema", str(out / "schema.json"), "-"]
        record.update({"requestSha256": sha(request), "promptSha256": sha(prompt),
                       "applicationPromptTokens": len(tiktoken.get_encoding("o200k_base").encode(prompt))})
        append(self.output / "ledger.jsonl", dict(record))
        started = time.perf_counter()
        process = None
        try:
            with (out / "events.jsonl").open("x", encoding="utf-8") as stdout, (out / "stderr.txt").open("x", encoding="utf-8") as stderr:
                process = subprocess.Popen(args, cwd=self.workdir, env=self.env,
                    stdin=subprocess.PIPE, stdout=stdout, stderr=stderr,
                    text=True, encoding="utf-8", creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                write_new(out / "process.json", {"pid": process.pid, "at": now(), "timeoutSeconds": self.timeout_seconds})
                await asyncio.to_thread(process.communicate, prompt, timeout=self.timeout_seconds)
            record["processExitCode"] = process.returncode
            event_text = (out / "events.jsonl").read_text(encoding="utf-8")
            answer, usage, thread_id = parse_events(event_text, process.returncode)
            record["recoveredTransportDiagnostics"] = sum(recoverable_transport_event(json.loads(line))
                for line in event_text.splitlines() if line.strip())
            # Record native cost even when output fails application validation.
            record.update({"usage": usage, "threadId": thread_id, "rawAnswer": answer})
            value = decode_answer(answer, request)
            record.update({"status": "COMPLETED", "answer": value})
            tool_calls = [{"id": "call_" + uuid.uuid4().hex, "type": "function",
                           "function": call} for call in value["toolCalls"]]
            return ChatCompletion.model_validate({"id": "codex_" + thread_id, "object": "chat.completion",
                "created": int(time.time()), "model": pilot.MODEL,
                "choices": [{"index": 0, "finish_reason": "tool_calls" if tool_calls else "stop",
                             "message": {"role": "assistant", "content": value["content"],
                                         "tool_calls": tool_calls or None}}],
                "usage": {"prompt_tokens": usage["input_tokens"], "completion_tokens": usage["output_tokens"],
                          "total_tokens": usage["input_tokens"] + usage["output_tokens"],
                          "prompt_tokens_details": {"cached_tokens": usage.get("cached_input_tokens", 0)}}})
        except BaseException as exc:
            record.update({"status": "FAILED", "failureType": type(exc).__name__, "failure": str(exc)[:1000]})
            if process is not None and process.poll() is None:
                process.kill()
                await asyncio.to_thread(process.wait, timeout=10)
            raise
        finally:
            if process is not None:
                record["processExitCode"] = process.returncode
            if record.get("usage") is None and (out / "events.jsonl").exists():
                try:
                    usage, thread_id = native_terminal_usage((out / "events.jsonl").read_text(encoding="utf-8"))
                    record.update({"usage": usage, "threadId": thread_id,
                        "usageRecoveredFromCompletedTerminal": True})
                except (OSError, ValueError, KeyError, TypeError) as usage_error:
                    record["terminalUsageUnavailable"] = type(usage_error).__name__
            record["durationMs"] = (time.perf_counter() - started) * 1000
            write_new(out / "result.json", record)
            append(self.output / "ledger.jsonl", dict(record))


async def smoke(output):
    client = SubscriptionClient(output, max_calls=2)
    tool = {"type": "function", "function": {"name": "record_budget", "description": "Record CNY minor units",
        "parameters": {"type": "object", "properties": {"budgetMinor": {"type": "integer"}},
                       "required": ["budgetMinor"], "additionalProperties": False}}}
    result = await client.create(model=pilot.MODEL,
        messages=[{"role": "user", "content": "预算1500元，请调用 record_budget 记录人民币分。"}],
        tools=[tool], tool_choice={"type": "function", "function": {"name": "record_budget"}})
    arguments = json.loads(result.choices[0].message.tool_calls[0].function.arguments)
    if arguments != {"budgetMinor": 150000}:
        raise AssertionError("unit_conversion_failed")
    write_new(Path(output) / "smoke.json", {"status": "SUBSCRIPTION_TOOL_CONTRACT_PASS", "calls": client.calls,
                                           "fullAgentIntegration": "NOT_YET_TESTED"})
    print(canonical({"status": "SUBSCRIPTION_TOOL_CONTRACT_PASS", "usage": client.calls[0]["usage"]}), flush=True)


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("output", type=Path)
    asyncio.run(smoke(parser.parse_args().output))
