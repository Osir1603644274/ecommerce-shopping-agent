# ContextCompiler provider-paired final evidence

- Date: 2026-09-01
- Final verdict: `HOLD_CONTEXT_PROVIDER_PAIR`
- Production default: unchanged (`ContextCompiler` remains default-off shadow)
- Scope: public context-fidelity evaluation only

## Evidence chain

1. V1 was rejected before any provider call because strict review found that a
   first-turn reference marker could select a synthetic old-history distractor.
2. V2 preserved that correction, but its formal attempt failed all 130 requests
   with the same `BadRequestError`: DeepSeek thinking mode rejected forced
   `tool_choice`. The failed attempt is preserved and has complete hashes.
3. V3 changed only the provider compatibility adapter: thinking is disabled for
   tool-bearing requests. A separate non-scored smoke passed 1/1 before the
   formal attempt.

## V3 deterministic gates

- Public source corpus: 24 conversation clusters / 65 turn cases.
- Protected Context fields equal: 65/65.
- Serialize, reload and recompile recovery: 130/130 exact across both arms.
- Query-focused treatment reduced the fixed estimate in 50/65 turns.
- Fixed-estimator P50: 412 → 307. This is not provider usage.

## V3 provider-observed results

Both arms used `deepseek-v4-flash`, temperature 0, forced structured output,
zero retries and randomized within-pair arm order.

| Metric | CTX1a preserve | CTX1b query-focused |
| --- | ---: | ---: |
| Calls succeeded | 65/65 | 65/65 |
| Usage present | 65/65 | 65/65 |
| Prompt tokens total | 50,539 | 47,029 |
| Prompt tokens P50 | 778 | 710 |
| Prompt tokens P95 | 828.6 | 819.4 |
| Exact fidelity | 60/65 | 59/65 |
| Latency P50 ms | 966.211 | 932.200 |
| Latency P95 ms | 1,360.518 | 1,399.916 |

Observed prompt tokens fell by 3,510 in total (`-6.94%`), and P50 fell by
`8.74%`. Latency is descriptive: treatment P50 was lower, while P95 was about
`2.90%` higher.

All 11 exact-fidelity failures were confined to `recentReference`; current goal,
candidate IDs and evidence references were 65/65 in both arms. Control missed 5
reference cases and treatment missed 6. Because the preregistered gate required
65/65 exact fidelity in both arms, the treatment cannot be declared quality
equivalent even though provider usage was lower.

## Decision boundary

The result proves a bounded provider-observed token reduction and deterministic
compile replay, but not ecommerce task success, recommendation quality,
checkpoint/process recovery, production readiness or a production-default
switch. It must not be rewritten as “Context quality unchanged” or as a fully
successful experiment. Further work should improve deterministic reference
resolution or the model-view contract before any new version is considered.

Authoritative artifacts:

- `manifest.json`
- `scenarios.jsonl`
- `compatibility-smoke001/smoke.json`
- `attempt001/attempt.json`
- `attempt001/traces.jsonl`
- `attempt001/summary.json`
- `attempt001/receipt.json`
- `attempt001/SHA256SUMS.txt`

