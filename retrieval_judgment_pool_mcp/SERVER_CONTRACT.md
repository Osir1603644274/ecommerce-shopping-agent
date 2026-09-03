# Retrieval Judgment Pool MCP v1

This local STDIO server exposes the same deterministic `PoolService` used by the CLI and repository Skill. It builds unlabeled candidate pools that can later be reviewed by human relevance judges.

## Launch

```powershell
$env:RJP_DATA_ROOT = 'D:\datasets\retrieval'
$env:RJP_RUN_ROOT = "$PWD\.runtime\retrieval-pool-runs"
python -m retrieval_judgment_pool_mcp.server
```

An MCP client can point `command` at the project Python executable, set `args = ["-m", "retrieval_judgment_pool_mcp.server"]`, and provide the two roots above as environment variables.

## Tools and order

1. `create_pool_run(dataset_ref)`
2. `submit_retrieval_run(run_id, retriever_id, submission_ref)` only for declared external retrievers
3. `build_pool(run_id)`
4. `get_run_status(run_id)` to resume or inspect
5. `verify_run(run_id)`
6. `export_blind_packet(run_id)`

Every result uses `retrieval-judgment-pool-mcp-response-v1` with `ok`, typed `data`, and either no error or a stable `{code, message}` error. Tools are local-only, non-destructive, and idempotent; read-only metadata distinguishes status/export/verify from state-creating operations.

## Input and output rules

- Dataset and submission references are relative to configured roots and cannot traverse them.
- References associated with qrels, relevance labels, hidden/private labels or production evaluation data are rejected.
- All candidates are `UNJUDGED`; absence of a judgment is never converted to a negative label.
- Blind exports omit retriever provenance, scores, selection reasons, and filesystem paths.
- The server returns generic unexpected-error text and does not include environment values or stack traces in tool results.
