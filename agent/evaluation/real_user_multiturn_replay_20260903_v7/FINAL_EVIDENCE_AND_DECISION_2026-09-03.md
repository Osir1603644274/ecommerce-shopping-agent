# Real-user multi-turn Context A/B final evidence

## Decision

`BOUNDED_CONTEXT_SEMANTIC_FIDELITY_ACCEPT / HOLD_TOKEN_EFFICIENCY_AND_DEFAULT`

## Formal run

- Frozen input: 8 real-user sessions, 21 user turns, 13 scored follow-ups.
- Arms: complete same-arm raw dialogue versus current server-compiled Context with zero raw prior transcript.
- Fixed runtime: `react_v1`, frozen 439-product offline tools, DeepSeek `deepseek-v4-flash`, temperature 0, no automatic retry; Multi-Agent disabled equally to isolate Context.
- Durable identity: task/session remain stable per arm; every user turn receives a unique run ID; revision chains are continuous.
- Unique formal attempt: 42/42 arm turns succeeded; 19/19 tool calls per arm; no provider or contract failure.

## Quality and fidelity

- Final answers: 21/21 paired turns byte-identical.
- Complete same-arm dialogue: 21/21 paired turns byte-identical.
- Public evidence: 21/21 paired turns byte-identical.
- Two independent, mapping-blind AI judge runs each completed 13/13 follow-ups; both returned 13 ties and no A/B preference.
- Judge score details differed, so only the unanimous preference result is used; the reviewers are AI, not humans.

## Efficiency boundary

Both arms used deterministic fast paths for all 42 turns: observed model calls and provider Tokens were 0. Therefore this attempt cannot measure Context Token reduction.

Descriptive end-to-end latency was lower for the compiled arm (P50 `241.1421→208.1634 ms`, P95 `592.3295→496.5058 ms`), but the 21-pair local run does not establish a causal production latency gain.

## Preserved preformal failures

- V5: receipt `producer` did not equal the schema constant; no formal attempt.
- V6: receipt schema retained stale authority/configuration constants; no formal attempt.
- V7 repaired both contracts before the unique formal attempt. Neither failed package was overwritten or presented as a successful execution.

## Claim boundary

Allowed: current compiled Context preserved exact answers and public evidence against full same-arm dialogue across this 8-session/21-turn real-user set, with 13/13 blind follow-ups tied in both independent AI reviews.

Not allowed: measured Token reduction, universal Context superiority, human-review evidence, production readiness, or a global default switch. Existing provider V4 P95 `+24.15%` HOLD remains valid and is not overwritten.

