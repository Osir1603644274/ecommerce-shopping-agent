# ContextCompiler Provider Paired V4 — Final Evidence and Decision

- Date: 2026-09-01
- Formal verdict: `HOLD_CONTEXT_PROVIDER_PAIR`
- Production default: unchanged; ContextCompiler remains default-off/HOLD
- Formal attempts executed for V4: exactly one (`attempt001`)
- Scope: bounded provider context fidelity plus A2 reference projection; not ecommerce task success

## Why V4 existed

V3 completed all 130 provider calls and showed lower prompt-token usage, but
failed exact fidelity in 11 arm/case outputs. Every failure was confined to
`recentReference`: the provider was asked to infer the latest relevant user
history item.

V4 did not delete or replace those cases. It retained all 24 clusters / 65 turn
cases and gave both arms the same server-resolved `referenceContextState`, bound
to task, revision, source and—when applicable—CandidateScope presentation,
focus and compared IDs. Four additional cases came from the sanitized A2
four-turn real-browser chain. The only arm difference remained the history
policy: `preserve` versus `query_focused`.

## Frozen data and deterministic safety gates

- Dataset: 25 clusters / 69 turn cases.
- Legacy V3 cases preserved: 65/65.
- A2 sanitized real-browser cases: 4/4.
- Protected-field equality: 69/69.
- Typed reference binding validation: 69/69.
- Serialize/reload/recompile recovery: 138/138.
- Forged/stale/cross-task/cross-scope mutation matrix: 8/8 failed closed.
- Treatment had a lower deterministic estimate in 51/69 cases.
- Non-scored provider compatibility smoke: 1/1 exact.

All deterministic gates passed before the sole formal attempt.

## Formal provider results

Both arms used `deepseek-v4-flash`, temperature 0, forced structured output,
zero retries and randomized within-pair arm order.

| Metric | CTX1a preserve | CTX1b query-focused |
| --- | ---: | ---: |
| Calls succeeded | 69/69 | 69/69 |
| Usage present | 69/69 | 69/69 |
| Exact full-output fidelity | 69/69 | 69/69 |
| Every individual output field exact | 69/69 | 69/69 |
| Prompt tokens total | 72,775 | 69,259 |
| Prompt tokens P50 / P95 | 1,052 / 1,117 | 984 / 1,117 |
| Total tokens | 87,659 | 84,143 |
| Total tokens P50 / P95 | 1,264 / 1,351.6 | 1,198 / 1,351.6 |
| Latency P50 ms | 1,734.828 | 1,868.523 |
| Latency P95 ms | 2,514.292 | 3,121.585 |

Observed treatment effects:

- prompt-token total: `-4.8313%` (passes preregistered `>=3%` reduction);
- total-token total: `-4.0110%` (passes preregistered `>=2%` reduction);
- treatment/control P95 latency ratio: `1.241536`.

The preregistered latency ceiling was `1.20`. The observed ratio exceeded it,
so the conjunctive acceptance gate failed. No case, arm, outlier or provider
call was removed, and the attempt was not rerun.

## Gate result

| Gate | Result |
| --- | --- |
| Deterministic preflight | PASS |
| 138/138 provider calls | PASS |
| 138/138 observed usage | PASS |
| Exact fidelity in both arms | PASS |
| Every output field exact | PASS |
| Prompt-token reduction >=3% | PASS |
| Total-token reduction >=2% | PASS |
| Treatment P95 latency <=1.20x control | **FAIL** (`1.241536x`) |
| Zero retries | PASS |

Final formal verdict: `HOLD_CONTEXT_PROVIDER_PAIR`.

## Interpretation and claim boundary

V4 closes the bounded V3 reference-fidelity defect: the provider no longer
guesses `recentReference`, and the A2 focus/compared-ID state survived both
context policies exactly. It also confirms a real provider-observed token
reduction in this frozen corpus.

It does **not** pass the complete adoption gate because treatment P95 latency
exceeded the frozen limit. Therefore it does not authorize enabling
ContextCompiler by default. It also does not measure ecommerce recommendation
quality, complete Agent task success, production ContextPack wiring, browser
E2E deployment, process/checkpoint recovery or production readiness.

Allowed wording:

> In a frozen 69-case paired provider evaluation, a server-resolved typed
> reference contract achieved 69/69 exact fidelity in both arms and reduced
> prompt tokens by 4.83%; the overall experiment remained HOLD because the
> treatment P95 latency ratio (1.2415x) exceeded the preregistered 1.20 ceiling.

Forbidden wording includes “ContextCompiler production accepted”, “quality and
latency both improved”, “full Agent evaluation passed” or “production default
was switched”.

## Reproducible evidence

- `preregistration.md`
- `manifest.json`
- `data-lineage.json`
- `scenarios.jsonl`
- `compatibility-smoke001/smoke.json`
- `attempt001/started.json`
- `attempt001/traces.jsonl`
- `attempt001/result.json`
- `attempt001/receipt.json`
- `attempt001/SHA256SUMS.txt`
- `verification.json`
- `verify_package.py`
- `pytest-context-v4.xml` (40 passed)
- `SOURCE_HASHES.sha256`
- `SHA256SUMS.txt`

