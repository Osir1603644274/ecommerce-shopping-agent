# Formal public retrieval judgment-pool v2 result

Authority: `result.json`, `receipt.json`, and `SHA256SUMS.txt` in this directory.

## Decisions

- Core: `CORE_ACCEPT_FORMAL_PUBLIC_MULTIFAMILY`
- Skill: `SKILL_ACCEPT_EXPLICIT_STRUCTURE_AND_SCRIPT`
- MCP: `MCP_ACCEPT_LOCAL_STDIO_BOUNDED`
- Joint: `JOINT_ACCEPT_FORMAL_PUBLIC_FIXTURE`
- Retrieval quality: `HOLD_NO_HUMAN_LABELS`
- Implicit Skill routing: `HOLD_NOT_MODEL_EXECUTED`
- Production/default: `HOLD_NOT_INSTALLED_OR_PRODUCTION_VALIDATED` / `HOLD`

## Actually executed retrievers

- `bm25f-v1` — `lexical_bm25f` / `builtin-bm25f@v1`
- `char-fuzzy-v1` — `char_ngram` / `builtin-char-ngram@v1`
- `semantic-hash-v1` — `semantic_hash` / `builtin-hashed-subword-dense-substitute@v1`
- `es-standard-compatible-v1` — `external` / `local-es-standard-compatible-bm25@standard-tokenizer-v1`
- `zh-dictionary-lexical-v1` — `external` / `local-zh-dictionary-bm25@lexicon-v1`
- `sparse-semantic-expansion-v1` — `external` / `local-sparse-semantic-expansion@concept-map-v1`
- `dense-hash-v1.0` — `external` / `precomputed-local-dense-hash@1.0.0-d128`
- `dense-hash-v2.0` — `external` / `precomputed-local-dense-hash@2.0.0-d192-concepts`
- `colbert-maxsim-hash-v1` — `external` / `local-colbert-like-maxsim-hash@late-interaction-v1-d32`
- `structured-v1` — `structured` / `builtin-structured@v1`

The ES path is a local Standard-compatible analyzer, not an Elasticsearch service. Sparse semantic is deterministic concept expansion, not a learned sparse checkpoint. Dense v1/v2 are verified precomputed hash-vector runs, not neural embeddings. ColBERT-like uses local token-vector MaxSim, not a neural ColBERT checkpoint. The deterministic pair reranker runs only on the frozen recall union and is not a recall source.

Overlap, unique-candidate, disagreement, and external unit-cost evidence are descriptive. With no human labels, marginal relevant documents, leave-one-out relevant omission rate, relevant coverage, and quality remain HOLD. All candidates remain `UNJUDGED`; qrels and sealed data were not read. MCP is accepted only as a local STDIO integration and was not installed as a production default.
