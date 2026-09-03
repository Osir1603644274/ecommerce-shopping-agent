FROM python:3.12-slim AS test

WORKDIR /app
ENV PYTHONPATH=/app
# Two deterministic harness tests construct an SDK client before all model
# methods are mocked.  This test-stage-only placeholder never reaches runtime.
ENV OPENAI_API_KEY=container-build-test-placeholder
COPY pyproject.toml .
COPY app ./app
COPY evaluation ./evaluation
COPY knowledge_data ./knowledge_data
COPY rag ./rag
COPY recommendation ./recommendation
COPY scripts ./scripts
COPY tests ./tests
RUN pip install --no-cache-dir ".[dev]"
COPY place_data ./place_data
# The repository also contains sealed experiment/acceptance tests that resolve
# artifacts from the repository root.  The deploy image deliberately gates the
# executable runtime surface only; repository-wide acceptance remains a
# separate host-side workflow.
RUN pytest -q \
    tests/test_run_agent.py \
    tests/test_reference_context.py \
    tests/test_historical_events.py \
    tests/test_long_term_memory_contract.py \
    tests/test_memory_bff.py \
    tests/test_memory_candidate_worker.py \
    tests/test_memory_context_projection.py \
    tests/test_memory_contextpack_integration.py \
    tests/test_memory_governance.py \
    tests/test_memory_projection_client.py \
    tests/test_memory_runtime_bridge.py \
    tests/test_memory_v3_runtime.py \
    tests/test_session_memory.py \
    tests/test_task_state.py \
    tests/test_shopping_task_state_v2_contract.py \
    tests/test_shopping_state_update.py \
    tests/test_shopping_state_authority.py \
    tests/test_ecommerce_transactions.py \
    tests/test_transaction_capabilities.py \
    tests/test_transaction_agent_runtime.py \
    tests/test_transaction_agent_api.py \
    tests/test_commerce_demo_api.py \
    tests/test_order_status_agent.py \
    && touch /test-passed

FROM python:3.12-slim

WORKDIR /app
ENV PYTHONPATH=/app
COPY --from=test /test-passed /tmp/test-passed
COPY pyproject.toml .
COPY app ./app
COPY evaluation ./evaluation
COPY knowledge_data ./knowledge_data
COPY place_data ./place_data
COPY rag ./rag
COPY recommendation ./recommendation
COPY scripts ./scripts
RUN pip install --no-cache-dir . && rm /tmp/test-passed

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
