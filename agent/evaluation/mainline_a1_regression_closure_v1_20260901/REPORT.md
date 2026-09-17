# Mainline A1 regression closure V1

Date: `2026-09-01`

## Decision

```text
A1_REPORTED_THREE_REGRESSIONS: BOUNDED_ACCEPT
A1_TARGETED_AGENT_SUITE: ACCEPT_490_OF_490
BROAD_AGENT_SUITE: HOLD_3504_PASS_64_FAIL_12_SKIP
PRODUCTION_DEFAULT_CHANGE: HOLD
```

No sealed attempt was modified or rerun. The three failures named in the prior
ReferenceContext report were reproduced before repair and then closed by
updating stale test contracts; production fail-closed behavior was not relaxed.

## The three reported failures

1. `durable exact replay`
   - Reproduced: endpoint loaded history once.
   - Classification: stale test receipt. The fixture omitted the current
     task/run/thread/control-policy binding that the real durable runner requires.
   - Repair: seed the complete resolved clarification receipt. The endpoint now
     proves that the fully identity-bound exact replay is recognized before
     history loading and turn persistence.
2. `FinalAnswer boundary`
   - Reproduced: the historical unit test entered the currently default-enabled
     durable graph and failed because its synthetic TaskState was never persisted.
   - Classification: test route ambiguity, not a production-route defect.
   - Repair: the test now explicitly selects its intended non-durable boundary.
3. `evidence projection`
   - Reproduced: the assertion rejected the current title/brand identity refs.
   - Classification: old assertion against the current projection contract.
   - Repair: require every authoritative requirement-check ref and additionally
     require title/brand identity refs; enrichment may not drop base evidence.

All three exact tests pass after the repairs. The seven related test files then
pass as one set: `490 passed`.

## Broad-suite observation and classification

Correct repository-root invocation:

```powershell
$env:PYTHONPATH='F:\agent;F:\agent\agent'
python -m pytest agent/tests -q
```

Observed result:

```text
3504 passed, 64 failed, 12 skipped
```

The 64 failures split into two independently checked groups:

| Group | Count | Evidence | Classification |
| --- | ---: | --- | --- |
| Public runner failures in the broad process | 35 | Same file alone: `94 passed, 1 skipped` | Cross-file/preloaded-module order pollution (`torch.ops`) |
| Stable failures after reducing to affected files | 29 | `stable-failures.xml` | Historical/frozen or unmigrated test contracts; not waived |
| External dependency failure | 0 | No stable failure required a missing live service | None observed; the 12 declared skips remain skips |

The 29 stable failures are preserved as follows:

| Family | Count | Boundary |
| --- | ---: | --- |
| Broad natural-query behavior | 1 | Current behavior returns clarification where the old test expects immediate search; remains broad-suite HOLD |
| Commerce Pilot 96 authoring/data closure | 3 | Historical public-data/authoring contract drift |
| Context policy fixtures | 6 | Fixtures omit the current V2 authoritative state/binding or intentionally inject now-invalid legacy structures |
| ReAct bounded-choice expectation | 1 | Old test expects no action; current runtime produces a bounded evidence answer action |
| Runtime reliability contracts | 2 | Old preview tool surface and a non-durable synthetic deadline test do not match current runtime |
| Scenario Lab fixtures | 2 | Historical fixture uses the now-forbidden legacy ToolCaller instead of explicit ToolCallerV2 |
| Shopping tool exposure | 1 | Old set omits current `rerank_products_in_scope` |
| Memory frozen-source checks | 3 | Sealed/source hashes and frozen defaults differ from current source; historical evidence is not rewritten |
| ShoppingTaskState Context A/B V1–V5 | 9 | Frozen source hashes and pre-v2.1 schema fixtures no longer match current source |
| Step Debug product identity | 1 | Old assertion expects numeric ID; current wire contract emits canonical string ID |

The initial attempt from `F:\agent\agent` stopped during collection with 54
`ModuleNotFoundError` errors because that working directory shadowed the top-level
`agent` package. It is recorded as an invocation/setup error, not a test result.

## Evidence files

- `targeted-490.xml`: current A1-targeted files, all green.
- `stable-failures.xml`: reduced stable-failure group, 291 passed / 29 failed / 1 skipped.
- `public-runner-isolated.xml`: isolated order-polluted file, 94 passed / 1 skipped.
- `manifest.json`: commands, counts and decision boundaries.
- `SOURCE_HASHES.sha256`: source and JUnit bindings.
- `SHA256SUMS.txt`: package integrity.

## Boundary

This closes only the three named A1 regressions and establishes their current
test coverage. It does not claim that the broad historical Agent test corpus is
green, does not update any production default, and does not repair frozen source
hashes by changing old evidence.
