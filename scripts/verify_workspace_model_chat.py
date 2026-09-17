"""Opt-in local browser-BFF chat check. No account or transaction writes."""
import json
from pathlib import Path
import uuid
import httpx

base = "http://127.0.0.1:8000"
target = Path(__file__).resolve().parent.parent / "docs/acceptance/unified-commerce-20260908-evidence/model-chat.json"
if target.exists():
    raise RuntimeError("Evidence already exists; choose a new attempt explicitly before making any request")
with httpx.Client(base_url=base, headers={"Origin": base}, timeout=190) as client:
    workspace = client.get("/api/commerce-demo/workspace")
    workspace.raise_for_status()
    csrf = workspace.json()["csrfToken"]
    question = "买二手手机前，我想弄清电池循环次数和电池健康度有什么区别，应该怎样判断？"
    response = client.post("/api/commerce-demo/workspace/chat",
                           headers={"X-CSRF-Token": csrf},
                           json={"message": question, "requestId": uuid.uuid4().hex})
    response.raise_for_status()
    events = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")]
    complete = next((event for event in events if event["type"] == "complete"), None)
    assert complete, "No completed chat event"
    answer = complete["workspace"]["messages"][-1]["content"]
    target.write_text(json.dumps({"question": question, "answer": answer,
                                  "deltaEvents": sum(e["type"] == "delta" for e in events),
                                  "modelCallsVerified": False,
                                  "note": "Confirm actual model calls from the matching server trace, not HTTP status."},
                                 ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"completed": True, "answerCharacters": len(answer), "evidence": str(target)}, ensure_ascii=False))
