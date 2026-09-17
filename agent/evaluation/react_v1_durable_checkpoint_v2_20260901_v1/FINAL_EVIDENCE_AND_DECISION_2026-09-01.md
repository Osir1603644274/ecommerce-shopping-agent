# ReAct V1 Durable Checkpoint / ToolInbox V2 — final evidence

## Decision

- Combined bounded target: **ACCEPT WITH SEPARATE REMEDIATION**.
- Original formal `attempt001`: **FAIL, unchanged**. Its deterministic Layer A passed 130/130, but two predeclared real-model cases were unreachable because production intentionally resolves unsupported-evidence boundaries deterministically.
- Separately preregistered Layer B remediation: **ACCEPT**; three distinct stale-reference cases produced model-call counts `[1,1,1]`, zero provider failures and zero contract violations.
- Production/default switch: **HOLD — no configuration change**. Current code keeps `react_v1` as the web production default and `fixed_v1` as the explicit rollback/control path; this package does not authorize changing either role.

## Before-preregistration audit

- P0: pending writes lacked read-side count/hash/allowlist enforcement and could retain corrupt payloads.
- P0 discovered during preformal tamper: hard exit could land between pending-write `HSET` and `EXPIRE`, leaving a persistent hash.
- P1: restart accepted insufficient task/run/thread/policy binding; resolved clarification receipts lacked the full binding.
- P2: Redis and an external tool cannot provide cross-system atomicity. The effect-before-Inbox-complete window must stop as `UNKNOWN`; it is not exactly-once success.

## Implemented boundaries

- Pending writes now carry full SHA-256, byte count, aggregate budget and channel allowlist validation on load.
- Pending-write ordinal allocation, writes and TTL are one Redis Lua operation.
- Checkpoint/restart/resume require exact task, run, thread, session-owner, control-policy and policy-revision identity.
- Clarification and terminal replay receipts bind the same server-owned identity and reject conflicts.
- ToolInbox fencing uses a persistent fence sequence; the formal race uses distinct OS PIDs and rejects the old late result as `FENCED_OUT`.

## Formal evidence

| Layer | Result | Key evidence |
|---|---|---|
| A: deterministic real Redis/process | ACCEPT | 130/130; duplicate 0; missing 0; stale overwrite 0; tamper accepted 0; TTL missing 0; sensitive hit 0 |
| A recovery time | ACCEPT | in-process RTO p50 42.182 ms; p95 75.475 ms; cold process time retained separately in observations |
| B: original configured model | FAIL | `[0,1,0]`; B01/B03 were intentionally deterministic unsupported-evidence boundaries |
| B: preregistered remediation | ACCEPT | `[1,1,1]`; 0 provider failures; 5070 total tokens; latencies 5724.466/8093.413/4132.472 ms |
| C: targeted regression | ACCEPT | 131 passed, 1 skipped, 0 failed |

Tamper T01–T12 all failed closed. T09 (Inbox receipt mutation) and T10 (TaskState projection mutation) reached `STATE_DIVERGED`; T02 actually corrupted 28 pending writes and was rejected. Exact terminal replay produced one publication, three same-identity lookup hits and zero revision delta.

## Preserved evidence and limits

- Old run `evaluation/runs/checkpoint_graph_v2_durable_1x_20260825_attempt001` remains at its 2026-08-25 mtimes and was not overwritten.
- Original 2026-09-01 attempt remains `FAIL`; remediation is a separate identity, preregistration and new-only directory.
- Real local Redis and real multiprocess recovery were exercised. Docker was unavailable; no multi-host, Redis Cluster or Kubernetes claim is made.
- No claim is made for general ReAct quality, full-suite health, production readiness or cross-system exactly-once in the unavoidable `UNKNOWN` window.

## Authority paths

- Original score: `evaluation/runs/react_v1_durable_checkpoint_v2_20260901_attempt001/score.json`
- Remediation score: `evaluation/runs/react_v1_durable_checkpoint_v2_20260901_model_remediation_attempt001/score.json`
- Combined decision: `evaluation/react_v1_durable_checkpoint_v2_20260901_v1/combined-decision.json`
- Frozen contracts: `preregistration.json`, `scenario-matrix.json`, `IMPLEMENTATION_FREEZE_RECEIPT.json` in each package.
