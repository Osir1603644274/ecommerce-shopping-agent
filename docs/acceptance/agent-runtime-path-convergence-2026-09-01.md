# Agent runtime path convergence

- Date: 2026-09-01
- Decision: `BOUNDED_ACCEPT`
- Full-suite status: `HOLD_NOT_ALL_GREEN`

## Current paths

- Production: `react_v1`
- Rollback/control: `fixed_v1`
- Historical evaluation only: `react_v0_shadow`, `react_v0`,
  `adaptive_hybrid_v1`, requiring
  `AGENT_EXPERIMENTAL_CONTROL_RUNTIMES_ENABLED=true`
- Context production: `context_pack`
- Context rollback: `legacy`
- ContextCompiler: explicit default-off shadow/evaluation path

Unknown runtime names and historical modes without the second experiment gate
fail during settings validation.

## Verification

- Runtime config, historical gate, shadow integration, CandidateScope rollback
  contract and full `test_run_agent.py`: `270 passed`.
- CandidateScope's historical contract tests now explicitly select the legacy
  authority path; they no longer accidentally depend on the production V2
  default.
- A broad `agent/tests` run produced `3484 passed, 75 failed, 12 skipped` before
  the CandidateScope fixture repair. Isolated checks established:
  - the current CandidateScope file is now `80 passed`;
  - used-phone public runner is `94 passed, 1 skipped` when isolated, so its
    broad-suite failures are order pollution;
  - frozen historical memory/Context packages correctly report source-hash
    drift and must not be edited to appear current.

The broad suite has not yet been rerun after all current fixes and is not
claimed green. Sealed historical source freezes remain immutable.

