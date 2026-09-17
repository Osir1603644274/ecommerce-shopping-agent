"""Isolated in-process BFF acceptance against real Java/Redis/model services.

The SDK observer records response metadata only. It does not substitute a response,
alter a request, change routing/settings, or modify the running server process.
Run inside the existing Agent container, once per explicit evidence destination.
"""
import asyncio
import json
import sys
import uuid
from pathlib import Path

import httpx
from openai.resources.chat.completions import AsyncCompletions

sys.path.insert(0, "/app")
target = Path(sys.argv[1])
if target.exists():
    raise RuntimeError("Evidence exists; no request made")
receipts = []
original = AsyncCompletions.create


async def observe(self, *args, **kwargs):
    response = await original(self, *args, **kwargs)
    receipt = {"modelRequested": kwargs.get("model"), "stream": bool(kwargs.get("stream")), "completed": False}
    receipts.append(receipt)
    def record(value):
        receipt["providerResponseId"] = getattr(value, "id", None)
        receipt["modelReturned"] = getattr(value, "model", None)
        if getattr(value, "usage", None):
            receipt["usage"] = value.usage.model_dump()
        if getattr(value, "choices", None):
            reasons = [c.finish_reason for c in value.choices if c.finish_reason]
            if reasons:
                receipt["finishReasons"] = reasons
                receipt["completed"] = True
    if kwargs.get("stream"):
        async def chunks():
            async for chunk in response:
                record(chunk)
                yield chunk
        return chunks()
    record(response)
    return response


async def main():
    from app.main import app
    AsyncCompletions.create = observe
    try:
        origin = "http://127.0.0.1:8000"
        turns = []
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=origin,
                                   headers={"Origin": origin}, timeout=180) as client:
            boot = await client.get("/api/commerce-demo/workspace")
            boot.raise_for_status()
            csrf = boot.json()["csrfToken"]
            for question in ["推荐2000元以内的二手手机", "对比第一款和第二款，哪些信息已经确认，哪些还需要我核实？"]:
                begin = len(receipts)
                response = await client.post("/api/commerce-demo/workspace/chat", headers={"X-CSRF-Token": csrf},
                                             json={"message": question, "requestId": uuid.uuid4().hex})
                response.raise_for_status()
                events = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")]
                final = next((e["workspace"] for e in events if e.get("type") == "complete"), None)
                turns.append({"question": question, "completed": final is not None,
                              "answer": final["messages"][-1]["content"] if final else None,
                              "providerCalls": receipts[begin:]})
                if not final:
                    break
        verified = any(r["completed"] and r.get("providerResponseId") for r in receipts)
        evidence = {"scope": "Real BFF handlers via ASGI; real dependencies and unmodified SDK requests. Not a live browser capture.",
                    "modelCallsVerified": bool(verified), "turns": turns}
        target.write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({"modelCallsVerified": bool(verified), "calls": len(receipts), "evidence": str(target)}))
        assert verified and all(t["completed"] for t in turns)
    finally:
        AsyncCompletions.create = original


asyncio.run(main())
