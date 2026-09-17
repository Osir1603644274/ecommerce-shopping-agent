# ReferenceContext real-web closure report (V2)

Generated: `2026-09-01`

## Decision

```text
REFERENCE_CONTEXT_FOCUSED_PLUS_EXPLICIT_ORDINAL_UI: BOUNDED_ACCEPT
POST_COMPARE_SAME_SCOPE_REUSE: BOUNDED_ACCEPT
AFFECTED_REGRESSION_SUITE: BOUNDED_ACCEPT
CONTEXT_V4_A3_EXECUTION: NOT_STARTED
PRODUCTION_WIDE_ACCEPTANCE: NOT_CLAIMED
```

This package closes only the two A2 real-browser P1 blockers retained by V1.
It does not claim that the broad historical Agent suite, Context V4, Multi-Agent,
or enterprise deployment is production-ready.

## Root causes and fixes

1. The durable clarification endpoint resolved the browser receipt, but the
   resumed graph did not pass that `ResolvedReferenceContext` into the
   TaskState update that binds focus plus ordinal references. The applier now
   consumes the same resolved context after a one-revision graph-owned rebase
   and rejects any other revision jump.
2. Comparison completion advanced TaskState while the browser retained an old
   receipt. The server now reissues the same CandidateScope presentation at the
   latest revision, preserving presentation order, comparison subset, and UI
   focus. Browser-only `scopeId` state is not serialized into the strict request
   DTO.
3. ReAct reuses a generic step ID and replaces the one-slot `stepOutputs` map on
   each Plan. A later comparison therefore hid the original search output even
   though CandidateScope was still valid. The fallback now reconstructs the
   source search projection from the append-only Executor receipt using exact
   task/plan/step/tool identity and revalidates the ranking contract. A
   presentation-only follow-up preferentially projects the exact persisted
   comparison subset.

## Final real-browser run

Runtime: `http://127.0.0.1:18000/`, `react_v1`, 439-item frozen catalog,
Elasticsearch retrieval, synthetic non-market prices.

| Turn | Request | Request ID | Result | TaskState |
|---|---|---|---|---|
| 1 | `预算2000元，只要安卓，推荐三款二手手机` | `req-a3e67d9c9fab` | 20 candidates, three cards | r11 |
| 2 | click card 2, `比较这个和另一个` | `req-557a3f8430e0` | safe clarification; no guessed ID | r13 |
| 3 | click card 2 again, `比较这个和第三个` | `req-00fbe92d495a` | compared IDs `4491800864589334497` and `1597801`; focus retained | r23 |
| 4 | `根据已有属性告诉我怎么选` | `req-790b1a82be53` | reused the same two-item validated comparison; no stale/scope error | r26 |

The final turn resolved a receipt from revision 23 with the same scope source
revision 9, focused product `4491800864589334497`, and compared IDs
`[4491800864589334497, 1597801]`.

## Regression verification

```text
577 passed in 39.80s
```

Covered files:

- `test_run_agent.py`
- `test_chat_endpoint.py`
- `test_context_view.py`
- `test_harness_integration.py`
- `test_fast_response.py`
- `test_reference_context.py`
- `test_evidence_research_v1.py`
- `test_candidate_scope.py`

The JUnit artifact is `pytest-affected-final.xml`.

## Retained negative evidence

- V1 request `req-96cd81c9cb3b` repeated clarification after durable resume.
- V1 request `req-f2357b05972b` failed on same-scope reuse.
- During this repair, `req-32586c422745` reached the deeper
  `CandidateScope normalized output identity mismatch`, proving the stale
  receipt fix alone was insufficient.
- A subsequent browser attempt exposed a strict-Schema HTTP 422 when an
  internal `scopeId` was serialized. The final request projector excludes this
  browser-only field, and the four-turn run above is post-fix evidence.

The prior V1 package is unchanged and remains evidence of the original failure.
