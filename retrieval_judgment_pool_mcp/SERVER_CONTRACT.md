# Retrieval Judgment Pool MCP v1

This local STDIO server exposes the same deterministic `PoolService` used by the CLI and repository Skill. It creates public, pre-label candidate pools for human relevance judgment; it never creates qrels or relevance labels and never authorizes production release.

## Launch

```powershell
$env:RJP_DATA_ROOT = 'F:\agent\evaluation\retrieval-judgment-pool-v1-20260901\fixtures'
$env:RJP_RUN_ROOT = 'F:\agent\evaluation\retrieval-judgment-pool-v1-20260901\mcp-runs'
F:\agent\.venv\Scripts\python.exe -m retrieval_judgment_pool_mcp.server
```

Codex STDIO configuration can point `command` at the virtual-environment Python executable, set `args = ["-m", "retrieval_judgment_pool_mcp.server"]`, and provide the two roots above as environment variables. No repository or user configuration is changed by this package.

## Tools and order

1. `create_pool_run(dataset_ref)`
2. `submit_retrieval_run(run_id, retriever_id, submission_ref)` only for declared external retrievers
3. `build_pool(run_id)`
4. `get_run_status(run_id)` to resume or inspect
5. `verify_run(run_id)`
6. `export_blind_packet(run_id)`

Every result uses `retrieval-judgment-pool-mcp-response-v1` with `ok`, typed `data`, and either no error or a stable `{code, message}` error. Tools are local-only, non-destructive, and idempotent; read-only metadata distinguishes status/export/verify from state-creating operations.

## Safety boundary

- Dataset and submission references are relative to configured roots and cannot traverse them.
- Keys or references associated with qrels, relevance labels, sealed, hidden, private, gold, or production evaluation data fail closed.
- All candidates are `UNJUDGED`; absence of a judgment is never converted to a negative label.
- Blind exports omit retriever provenance, scores, selection reasons, and filesystem paths.
- The server returns generic unexpected-error text and does not include environment values or stack traces in tool results.
