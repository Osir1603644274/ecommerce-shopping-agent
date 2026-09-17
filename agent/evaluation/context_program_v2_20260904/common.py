"""Immutable artifacts and a single-writer, write-ahead provider budget ledger."""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
CAPS = {"P2": 120, "P3": 240, "P4": 1800, "P5": 600, "P6": 40, "P7": 200}


def now():
    return datetime.now(timezone.utc).isoformat()


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def sha(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def file_sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def json_new(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        stream.write(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n")
        stream.flush()
        os.fsync(stream.fileno())


def append(path, value):
    with Path(path).open("a", encoding="utf-8", newline="\n") as stream:
        stream.write(canonical(value) + "\n")
        stream.flush()
        os.fsync(stream.fileno())


def rows(path):
    return [json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines() if line.strip()]


def manifest_check(path):
    path = Path(path)
    results = []
    for line in path.read_text(encoding="utf-8").splitlines():
        expected, relative = line.split("  ", 1)
        target = (path.parent / relative).resolve()
        if not target.is_relative_to(path.parent.resolve()):
            raise RuntimeError("manifest_path_escape")
        results.append({"path": str(target), "expected": expected, "actual": file_sha(target)})
    return {"checked": len(results), "mismatches": [r for r in results if r["expected"] != r["actual"]], "rows": results}


def check_freeze():
    frozen = json.loads((HERE / "p0/source_freeze.json").read_text(encoding="utf-8"))
    for item in frozen["sources"]:
        if file_sha(ROOT / item["path"]) != item["sha256"]:
            raise RuntimeError("source_drift:" + item["path"])
    return frozen


class BudgetStop(RuntimeError):
    pass


class RecordedClient:
    """No hidden retries, no 1024 override; one in-flight call per writer.

    Reservation uses UTF-8 request byte length plus generous framing allowance
    and the full output cap. This deliberately overestimates input tokens.
    Request START is fsynced before dispatch; unknown calls remain charged.
    """
    def __init__(self, provider, *, phase, output, ledger=None):
        self.provider = provider
        self.phase = phase
        self.output = Path(output)
        self.ledger = Path(ledger) if ledger else HERE / "provider_ledger.jsonl"
        self.binding: dict[str, Any] = {}
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))
        self.lock = asyncio.Lock()
        self.transport_failures = 0
        self.halted = False

    def usage_state(self):
        events = rows(self.ledger) if self.ledger.exists() else []
        starts = {e["requestId"]: e for e in events if e["event"] == "START"}
        ends = {e["requestId"]: e for e in events if e["event"] == "END"}
        charged = sum(
            (ends[k].get("usage") or {}).get("total_tokens", s["reservedTokens"])
            for k, s in starts.items()
        )
        return starts, charged

    async def create(self, **kwargs):
        async with self.lock:
            if self.halted:
                raise BudgetStop("provider_dispatch_halted")
            kwargs = dict(kwargs)
            kwargs["temperature"] = 0
            kwargs.setdefault("max_tokens", 4096)
            if not isinstance(kwargs["max_tokens"], int) or not 1 <= kwargs["max_tokens"] <= 4096:
                raise BudgetStop("unregistered_output_cap")
            extra = dict(kwargs.get("extra_body") or {})
            extra["thinking"] = {"type": "disabled"}
            kwargs["extra_body"] = extra
            starts, charged = self.usage_state()
            reserved = len(canonical(kwargs).encode()) + 8192 + kwargs["max_tokens"]
            deadline = json.loads((HERE / "p0/contract.json").read_text(encoding="utf-8"))["deadlineAt"]
            if (datetime.now(timezone.utc) >= datetime.fromisoformat(deadline)
                or len(starts) >= 3000
                or sum(s["phase"] == self.phase for s in starts.values()) >= CAPS[self.phase]
                or charged + reserved > 10_000_000):
                self.halted = True
                raise BudgetStop("request_token_or_time_budget_exhausted")
            request_id = f"ctxv2-call-{len(starts)+1:05d}"
            entry = {"event": "START", "requestId": request_id, "phase": self.phase,
                     "at": now(), "binding": self.binding, "requestSha256": sha(kwargs),
                     "reservedTokens": reserved, "effectiveMaxTokens": kwargs["max_tokens"],
                     "thinking": extra["thinking"], "model": kwargs["model"],
                     "endpoint": str(self.provider.base_url), "sdkRetries": self.provider.max_retries}
            append(self.output / "private_requests.jsonl", {**entry, "request": kwargs})
            append(self.ledger, entry)
            clock = asyncio.get_running_loop().time()
            try:
                response = await self.provider.chat.completions.create(**kwargs)
            except Exception as exc:
                self.transport_failures += 1
                append(self.ledger, {"event": "END", "requestId": request_id, "at": now(),
                    "status": "FAILED_OR_UNKNOWN", "errorType": type(exc).__name__,
                    "httpStatus": getattr(exc, "status_code", None), "usage": None})
                if self.transport_failures >= 3:
                    self.halted = True
                raise
            self.transport_failures = 0
            elapsed = (asyncio.get_running_loop().time() - clock) * 1000
            body = response.model_dump()
            append(self.output / "private_responses.jsonl", {"requestId": request_id, "response": body})
            append(self.ledger, {"event": "END", "requestId": request_id, "at": now(),
                "status": "SUCCEEDED", "usage": response.usage.model_dump() if response.usage else None,
                "finishReason": response.choices[0].finish_reason, "durationMs": elapsed,
                "responseSha256": sha(body)})
            return response
