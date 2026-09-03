"""V3 provider adapter: disable thinking only for tool-bearing requests."""

from __future__ import annotations

import argparse
import asyncio
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from openai import AsyncOpenAI as _AsyncOpenAI

from agent.evaluation.context_compiler_provider_paired_v1_20260901_v1 import (
    runner as base,
)


PACKAGE_DIR = Path(__file__).resolve().parent
DEFAULT_MANIFEST = PACKAGE_DIR / "manifest.json"
DEFAULT_OUTPUT = PACKAGE_DIR / "attempt001"
DEFAULT_SMOKE_OUTPUT = PACKAGE_DIR / "compatibility-smoke001"

base.SYSTEM_PROMPT = """You are a deterministic context-fidelity checker.
Call record_context_fidelity exactly once. Copy currentGoal, candidateIds and
evidenceRefs exactly from goal, candidateScopeState.rankedItemIds and
evidenceRefs. If referenceQuery is true, select only user history items whose
atTurn is a positive integer and copy the summary from the greatest atTurn;
if no such item exists return null. If referenceQuery is false return null.
Do not infer, translate, reorder, add or omit values."""


class _CompletionsProxy:
    def __init__(self, delegate: Any) -> None:
        self._delegate = delegate

    async def create(self, *args: Any, **kwargs: Any) -> Any:
        if kwargs.get("tools"):
            extra_body = dict(kwargs.get("extra_body") or {})
            configured = extra_body.get("thinking")
            disabled = {"type": "disabled"}
            if configured not in (None, disabled):
                raise RuntimeError("tool request attempted conflicting thinking mode")
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
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self._client = _AsyncOpenAI(*args, **kwargs)
        self.chat = _ChatProxy(self._client.chat)

    async def close(self) -> None:
        await self._client.close()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._client, name)


def deterministic_preflight(manifest_path: Path = DEFAULT_MANIFEST):
    return base.deterministic_preflight(manifest_path)


async def compatibility_smoke(manifest_path: Path, output_dir: Path) -> int:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    preflight = deterministic_preflight(manifest_path)
    if preflight["status"] != "PASS":
        raise RuntimeError("deterministic preflight failed")
    if output_dir.exists():
        raise RuntimeError("compatibility smoke output already exists")
    if not base.settings.deepseek_api_key:
        raise RuntimeError("DeepSeek API key is not configured")
    if base.settings.deepseek_model != manifest["provider"]["model"]:
        raise RuntimeError("configured model differs from frozen manifest")
    output_dir.mkdir(parents=True, exist_ok=False)
    case = base.load_jsonl(base.ROOT / manifest["dataset"]["path"])[0]
    compiled = base.compile_case(case, "CTX1b")
    client = DeepSeekToolCompatibleClient(
        api_key=base.settings.deepseek_api_key,
        base_url=base.settings.deepseek_base_url,
        timeout=float(manifest["provider"]["timeoutSeconds"]),
        max_retries=0,
    )
    started = time.perf_counter()
    error: Exception | None = None
    trace: dict[str, Any] | None = None
    try:
        trace = await base._call_provider(
            client,
            model=manifest["provider"]["model"],
            max_tokens=int(manifest["provider"]["maxTokens"]),
            case=case,
            arm="CTX1b",
            model_view=compiled.model_view,
        )
    except Exception as exc:
        error = exc
    finally:
        await client.close()
    passed = error is None and bool(trace and trace.get("exactFidelity"))
    receipt = {
        "schemaVersion": "context-compiler-provider-compatibility-smoke-v1",
        "status": "PASS" if passed else "FAIL",
        "scored": False,
        "completedAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "durationMs": (time.perf_counter() - started) * 1000.0,
        "thinkingDisabledOnlyForToolRequests": True,
        "maxRetries": 0,
        "caseId": case["caseId"],
        "trace": trace,
        "errorCode": type(error).__name__ if error is not None else None,
        "errorMessage": str(error)[:500] if error is not None else None,
    }
    (output_dir / "smoke.json").write_text(
        json.dumps(receipt, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    print(json.dumps(receipt, ensure_ascii=False, indent=2), flush=True)
    return 0 if passed else 1


async def execute(manifest_path: Path, output_dir: Path, attempt_id: str) -> int:
    base.AsyncOpenAI = DeepSeekToolCompatibleClient
    return await base.execute(manifest_path, output_dir, attempt_id)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument(
        "--attempt-id",
        default="context-compiler-provider-paired-v1-20260901-v3-attempt001",
    )
    parser.add_argument("--preflight", action="store_true")
    parser.add_argument("--compatibility-smoke", action="store_true")
    args = parser.parse_args()
    manifest = (
        args.manifest if args.manifest.is_absolute() else base.ROOT / args.manifest
    )
    output = (
        args.output_dir if args.output_dir.is_absolute() else base.ROOT / args.output_dir
    )
    if args.preflight:
        print(json.dumps(
            deterministic_preflight(manifest), ensure_ascii=False, indent=2
        ))
        return 0
    if args.compatibility_smoke:
        if args.output_dir == DEFAULT_OUTPUT:
            output = DEFAULT_SMOKE_OUTPUT
        return asyncio.run(compatibility_smoke(manifest, output))
    return asyncio.run(execute(manifest, output, args.attempt_id))


if __name__ == "__main__":
    raise SystemExit(main())

