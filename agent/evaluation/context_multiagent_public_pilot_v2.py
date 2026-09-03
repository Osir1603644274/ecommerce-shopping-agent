"""Versioned provider-compatibility adapter for the frozen public pilot.

V1 attempt001 is immutable.  V2 changes only the DeepSeek structured-tool
request adapter: thinking is disabled when tools are present so the already
frozen named ``tool_choice`` remains valid.  Dataset, prompts, model, budgets,
deadlines, randomization, scorers, and gates remain owned by the V1 runner.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from openai import AsyncOpenAI as _AsyncOpenAI

from evaluation import context_multiagent_public_pilot_v1 as v1


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_MANIFEST = ROOT / "evaluation/context-multiagent-public-pilot-v2.json"
DEFAULT_OUTPUT = (
    ROOT / "agent/evaluation/runs/context_multiagent_public_pilot_v2_attempt002"
)
DEFAULT_SMOKE_OUTPUT = (
    ROOT / "agent/evaluation/runs/context_multiagent_provider_compat_v2_smoke001"
)


class _CompletionsProxy:
    def __init__(self, delegate: Any) -> None:
        self._delegate = delegate

    async def create(self, *args: Any, **kwargs: Any) -> Any:
        if kwargs.get("tools"):
            extra_body = dict(kwargs.get("extra_body") or {})
            configured = extra_body.get("thinking")
            disabled = {"type": "disabled"}
            if configured not in (None, disabled):
                raise RuntimeError("tool request attempted a conflicting thinking mode")
            extra_body["thinking"] = disabled
            kwargs["extra_body"] = extra_body
        return await self._delegate.create(*args, **kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._delegate, name)


class _ChatProxy:
    def __init__(self, delegate: Any) -> None:
        self._delegate = delegate
        self.completions = _CompletionsProxy(delegate.completions)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._delegate, name)


class DeepSeekToolCompatibleClient:
    """AsyncOpenAI proxy that changes only tool-bearing DeepSeek requests."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self._client = _AsyncOpenAI(*args, **kwargs)
        self.chat = _ChatProxy(self._client.chat)

    async def close(self) -> None:
        await self._client.close()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._client, name)


def _validate_manifest(manifest_path: Path) -> tuple[dict[str, Any], bytes]:
    raw = manifest_path.read_bytes()
    manifest = json.loads(raw.decode("utf-8"))
    if manifest.get("status") != "FROZEN_BEFORE_EXECUTION":
        raise RuntimeError("public pilot V2 manifest is not frozen")
    for relative, expected in manifest["sourceFiles"].items():
        actual = v1.file_hash(ROOT / relative)
        if actual != expected:
            raise RuntimeError(
                f"source hash mismatch for {relative}: expected {expected}, got {actual}"
            )
    return manifest, raw


async def compatibility_smoke(manifest_path: Path, output_dir: Path) -> int:
    """Run one explicitly non-scored provider-contract probe."""

    manifest, manifest_bytes = _validate_manifest(manifest_path)
    output_dir.mkdir(parents=True, exist_ok=False)
    tool_name = "submit_compatibility_probe"
    tool = {
        "type": "function",
        "function": {
            "name": tool_name,
            "description": "Return the fixed compatibility marker.",
            "parameters": {
                "type": "object",
                "additionalProperties": False,
                "required": ["marker"],
                "properties": {"marker": {"const": "TOOL_COMPATIBLE"}},
            },
        },
    }
    started_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    started = time.perf_counter()
    response = None
    error: Exception | None = None
    arguments: dict[str, Any] = {}
    client = DeepSeekToolCompatibleClient(
        api_key=v1.settings.deepseek_api_key,
        base_url=v1.settings.deepseek_base_url,
        timeout=float(manifest["model"]["timeoutSeconds"]),
        max_retries=0,
    )
    try:
        response = await client.chat.completions.create(
            model=manifest["model"]["name"],
            messages=[
                {
                    "role": "system",
                    "content": "Only call submit_compatibility_probe with the fixed marker.",
                },
                {"role": "user", "content": "Run the compatibility probe."},
            ],
            tools=[tool],
            tool_choice={"type": "function", "function": {"name": tool_name}},
            temperature=0,
            max_tokens=128,
        )
        arguments = v1.parse_tool_arguments(response, tool_name)
    except Exception as exc:  # preserved in the smoke receipt
        error = exc
    finally:
        await client.close()
    passed = error is None and arguments == {"marker": "TOOL_COMPATIBLE"}
    usage = getattr(response, "usage", None)
    receipt = {
        "schemaVersion": "context-multiagent-provider-compatibility-smoke-v2",
        "status": "PASS" if passed else "FAIL",
        "scored": False,
        "startedAt": started_at,
        "completedAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "durationMs": (time.perf_counter() - started) * 1000.0,
        "manifestSha256": hashlib.sha256(manifest_bytes).hexdigest(),
        "model": manifest["model"]["name"],
        "maxRetries": 0,
        "thinkingModeForTools": "disabled",
        "forcedToolChoice": tool_name,
        "arguments": arguments,
        "providerResponseId": getattr(response, "id", None),
        "inputTokens": getattr(usage, "prompt_tokens", None),
        "outputTokens": getattr(usage, "completion_tokens", None),
        "errorCode": type(error).__name__ if error is not None else None,
        "errorMessage": str(error)[:500] if error is not None else None,
    }
    receipt["receiptHash"] = v1.sha256_json(
        {key: value for key, value in receipt.items() if key != "completedAt"}
    )
    (output_dir / "smoke.json").write_text(
        json.dumps(receipt, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    print(json.dumps(receipt, ensure_ascii=False, indent=2), flush=True)
    return 0 if passed else 1


def _augment_v2_result(output_dir: Path) -> None:
    """Add non-conflated diagnostics without changing the frozen V1 gate."""

    summary_path = output_dir / "summary.json"
    if not summary_path.exists():
        return
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    traces = v1.load_jsonl(output_dir / "traces.jsonl")
    research = [row for row in traces if str(row.get("routeGold", "")).startswith("RESEARCH_")]
    execution_failures = sum(row.get("status") != "ok" for row in research)
    raw_leaks = sum(
        row.get("arm") == "MA1"
        and row.get("status") == "ok"
        and row.get("rawChildObservationLeak") is not False
        for row in research
    )
    legacy = dict(summary)
    summary["schemaVersion"] = "context-multiagent-public-pilot-result-v2"
    summary["providerCompatibilityAdapter"] = {
        "thinkingDisabledOnlyForToolRequests": True,
        "forcedToolChoicePreserved": True,
        "maxRetries": 0,
    }
    summary["diagnostics"] = {
        "armExecutionFailureCount": execution_failures,
        "rawChildObservationLeakCount": raw_leaks,
        "contractSafetyFailureCount": raw_leaks,
        "legacyCombinedSafetyFailureCount": legacy.get("safetyFailureCount"),
        "note": "diagnostic separation only; the preregistered V1 engineering gate is unchanged",
    }
    stable = {
        key: value
        for key, value in summary.items()
        if key not in {"completedAt", "resultHash"}
    }
    summary["resultHash"] = v1.sha256_json(stable)
    summary_path.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    attempt_path = output_dir / "attempt.json"
    attempt = json.loads(attempt_path.read_text(encoding="utf-8"))
    attempt["schemaVersion"] = "context-multiagent-public-pilot-attempt-v2"
    attempt["resultHash"] = summary["resultHash"]
    attempt["summarySha256"] = v1.file_hash(summary_path)
    attempt_path.write_text(
        json.dumps(attempt, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )


async def execute(manifest_path: Path, output_dir: Path, attempt_id: str) -> int:
    v1.AsyncOpenAI = DeepSeekToolCompatibleClient
    code = await v1.execute(manifest_path, output_dir, attempt_id)
    _augment_v2_result(output_dir)
    return code


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument(
        "--attempt-id", default="context-multiagent-public-pilot-v2-attempt002"
    )
    parser.add_argument("--compatibility-smoke", action="store_true")
    args = parser.parse_args()
    manifest = args.manifest if args.manifest.is_absolute() else ROOT / args.manifest
    output = args.output_dir if args.output_dir.is_absolute() else ROOT / args.output_dir
    if args.compatibility_smoke:
        if args.output_dir == DEFAULT_OUTPUT:
            output = DEFAULT_SMOKE_OUTPUT
        return asyncio.run(compatibility_smoke(manifest, output))
    return asyncio.run(execute(manifest, output, args.attempt_id))


if __name__ == "__main__":
    raise SystemExit(main())
