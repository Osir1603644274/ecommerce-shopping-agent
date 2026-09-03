from __future__ import annotations
import hashlib,json
from pathlib import Path

P=Path(__file__).resolve().parent
ROOT=P.parents[2]
PID="real_user_multiturn_replay_20260903_v7"
def canon(x): return json.dumps(x,ensure_ascii=False,sort_keys=True,separators=(",",":"))
def sha(p): return hashlib.sha256(p.read_bytes()).hexdigest()
def main():
    cfg=json.loads((P/"execution_config_snapshot.json").read_text(encoding="utf-8"))
    auth=json.loads((P/"execution_authority.json").read_text(encoding="utf-8"))
    assert cfg["packageId"]==auth["packageId"]==PID
    assert auth["configSnapshot"]["sha256"]==sha(P/"execution_config_snapshot.json")
    fm={"controlledWorldHash":"controlledWorld","modelConfigurationHash":"modelConfiguration","toolConfigurationHash":"toolConfiguration","budgetConfigurationHash":"budgetConfiguration","policyConfigurationHash":"policyConfiguration"}
    for field,component in fm.items():
        assert auth["expectedTraceHashes"][field]==hashlib.sha256(canon(cfg["components"][component]).encode()).hexdigest()
        for src in cfg["components"][component]["sourceFiles"]:
            assert sha(ROOT/src["path"])==src["sha256"]
    conversations=[json.loads(x) for x in (P/"conversations.jsonl").read_text(encoding="utf-8").splitlines() if x]
    assert len(conversations)==8 and sum(len(x["turns"]) for x in conversations)==21
    sums={line.split("  ",1)[1]:line.split("  ",1)[0] for line in (P/"SHA256SUMS.txt").read_text().splitlines() if line}
    assert all(sha(P/name)==digest for name,digest in sums.items())
    result={"status":"PASS","packageId":PID,"conversationCount":8,"turnCount":21,"scheduledArmTurns":42,"sourceHashFailures":0,"checksumFailures":0,"formalAttempts":0}
    (P/"verification.json").write_text(json.dumps(result,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    print(json.dumps(result,ensure_ascii=False))
    return 0
if __name__=="__main__": raise SystemExit(main())
