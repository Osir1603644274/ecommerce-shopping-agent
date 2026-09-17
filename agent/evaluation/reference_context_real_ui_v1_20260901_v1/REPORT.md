# ReferenceContext real-web closure report (V1)

Generated: `2026-09-01T08:25:49Z`

## Decision

```text
REFERENCE_CONTEXT_EXPLICIT_ORDINAL: BOUNDED_ACCEPT
REFERENCE_CONTEXT_AMBIGUITY_GUARD: BOUNDED_ACCEPT
NEW_TASK_CROSS_TASK_REFERENCE_ISOLATION: BOUNDED_ACCEPT
REFERENCE_CONTEXT_FOCUSED_PLUS_EXPLICIT_ORDINAL_UI: HOLD_P1
POST_COMPARE_SAME_SCOPE_RECOMMENDATION: HOLD_P1
FULL_SELECTED_SUITE: HOLD_3_UNRELATED_FAILURES
PRODUCTION_DEFAULT_CHANGE: HOLD
```

This is a bounded implementation and real-web acceptance record. It is not a
claim that Context Provider V3, Checkpoint V2, Multi-Agent, or the whole Agent
suite is production-ready.

## Fixed in this pass

1. Durable terminal publication now notifies the browser with the finalized
   TaskState revision. A card receipt is no longer published from the stale
   pre-publication revision.
2. The deterministic parser accepts the natural hard-condition wording
   `电池健康必须90%以上` without a TaskState model call.
3. Ordinal comparison validation now accepts any unique 2- or 3-item subset of
   the current trusted Validator presentation. It no longer incorrectly
   requires a prefix such as only `第一个+第二个`.
4. FinalAnswerContextView projects the server-bound source display ordinals.
   The comparison prompt must preserve those ordinals, and a deterministic
   guard rejects unsupported claims inferred from the two selected finalists
   (for example, claiming that the bound third card does not exist).
5. Comparison prose is buffered until the comparison guard completes.
6. A newly created task ignores a ReferenceContext handle from the previous
   task. Cross-task isolation no longer blocks an explicit new task.
7. Added deterministic coverage for compact/expanded order, focused selection,
   complement selection, ambiguous three-choice selection, stale revision,
   cross-session binding, invalidated scope, expiry and IDs above `2**53`.

## Real browser observations

Runtime: `http://127.0.0.1:18000/`, `react_v1`, 439-item frozen catalog,
Elasticsearch retrieval, synthetic non-market prices.

### Accepted exact three-turn path

| Turn | Request | Result |
|---|---|---|
| 1 | `新任务：预算2000元以内，推荐三款安卓二手手机` | `req-80ed1a75156d`, status `ok`, TaskState r11, 20 candidates and three cards |
| 2 | `再加一个硬条件：电池健康必须90%以上，其他条件不变` | `req-25783eea42d0`, status `ok`, TaskState r21, constraint preserved and added |
| 3 | `比较第一个和第三个，告诉我各自适合谁` | `req-285b1c30be67`, status `ok`, TaskState r31, compared the original display IDs `1629369936065708126` and `2234766375329709880`, and the final answer preserved display ordinals 1 and 3 |

### Accepted isolation and ambiguity paths

- `req-64bfaa7b075d`: an explicit new task succeeded even though the browser
  still held a previous task's card receipt. It returned a new three-card
  result at TaskState r11.
- `req-1ed84674ce86`: after selecting card 2 while three cards were visible,
  `比较这个和另一个` did not guess. It returned a clarification at r13 with
  zero business-tool calls.

### Discovery failures retained as evidence

- `req-f075ee1362c6`: before the answer-format and guard repair, the server
  compared the correct original cards 1 and 3, but generated contradictory
  prose claiming that a third candidate did not exist. The post-fix paired
  observation is `req-285b1c30be67`.
- `req-96cd81c9cb3b`: after a clarification response, clicking card 2 and asking
  `比较这个和第三个` still produced another clarification. The pure server
  resolver test binds this shape correctly, so the remaining discrepancy is
  in the real browser-to-server focused-reference continuity. This remains P1.
- `req-f2357b05972b`: returning from a completed comparison to an unchanged
  same-scope recommendation can still fail with
  `CandidateScope Validator identity mismatch`. This remains P1; it was not
  hidden or reclassified as an accepted run.

## Verification

Green scoped runs:

```text
tests/test_run_agent.py
172 passed

tests/test_chat_endpoint.py excluding the separately listed durable replay blocker
51 passed, 1 deselected

tests/test_context_view.py + tests/test_harness_integration.py
excluding the two separately listed baseline blockers
113 passed, 2 deselected

tests/test_fast_response.py + tests/test_reference_context.py +
tests/test_evidence_research_v1.py
151 passed
```

The combined selected run was not fully green:

```text
487 passed, 3 failed
```

The three failures were reproduced individually and are not waived:

1. durable exact replay still loads history once;
2. one old FinalAnswer boundary test enters the enabled durable graph and
   cannot find its synthetic TaskState;
3. one evidence-projection assertion excludes title/brand refs now present in
   the projected evidence.

They do not validate the new ReferenceContext branches, but they keep the
overall suite and production-default decision at `HOLD`.

## External gates that remain external

- The Multi-Agent third adjudication must still be performed by a real third
  human; it was not fabricated here.
- The requested 20–50 unedited human-query corpus still requires real user
  input; synthetic operator queries in this report do not satisfy it.
- Context Provider V3 remains default-off because its frozen result lost one
  fidelity point despite reducing prompt tokens.
- Checkpoint V2 remains only a bounded acceptance; its original failed attempt
  and production/default HOLD are unchanged.

