import asyncio
import uuid

import pytest
import redis.asyncio as redis

from app.domains.ecommerce import transactions
from app.domains.ecommerce.transactions import (
    TransactionContext,
    _bind_authenticated_transaction_context,
    execute_confirmed_transaction,
    preview_order_tool,
)
from app.settings import settings
from app.transaction_agent.capabilities import _issue_transaction_capability


def _context(session_id: str, message: str) -> TransactionContext:
    return TransactionContext(
        access_token="signed-access-token",
        owner_user_id="user-redis-recovery",
        session_id=session_id,
        task_id="task-redis-recovery",
        task_revision=7,
        candidate_scope_id="scope-redis-recovery",
        user_message=message,
    )


def test_confirmed_command_survives_process_restart_on_real_redis(monkeypatch):
    async def scenario() -> None:
        session_id = "transaction-recovery-" + uuid.uuid4().hex
        first_client = redis.from_url(settings.redis_url, decode_responses=True)
        try:
            try:
                await first_client.ping()
            except Exception as exc:
                pytest.skip(f"real Redis unavailable: {type(exc).__name__}")
            monkeypatch.setattr(settings, "agent_transaction_enabled", True)
            monkeypatch.setattr(transactions, "_redis_client", first_client)

            async def preview_backend(
                _context, method, path, *, json_body=None, idempotency_key=None
            ):
                assert path == "/api/orders/preview"
                return {"itemType": "PRODUCT", "itemId": 1001, "quantity": 1}

            monkeypatch.setattr(transactions, "_request_backend", preview_backend)
            with _bind_authenticated_transaction_context(
                _context(session_id, "订单预览"),
                _issue_transaction_capability(),
            ):
                assert (await preview_order_tool(1001, 1)).ok is True

            confirmed_context = _context(session_id, "确认下单")
            with _bind_authenticated_transaction_context(
                confirmed_context,
                _issue_transaction_capability(),
            ):
                proposal = await transactions._consume_proposal(
                    confirmed_context, "create_order"
                )
            assert proposal is not None and proposal.confirmed_at is not None
            key = transactions._proposal_key(confirmed_context, "create_order")
            assert await first_client.get(key) is not None

            # Simulate a new process: a new Redis client observes the confirmed
            # command and safely replays the same backend idempotency key.
            await first_client.aclose()
            restarted_client = redis.from_url(
                settings.redis_url, decode_responses=True
            )
            monkeypatch.setattr(transactions, "_redis_client", restarted_client)
            seen_keys = []

            async def restarted_backend(
                _context, method, path, *, json_body=None, idempotency_key=None
            ):
                seen_keys.append(idempotency_key)
                return {
                    "id": "order-redis-1",
                    "orderNo": "ORDER-REDIS-1",
                    "payableMinor": 249900,
                    "status": "PENDING_PAYMENT",
                }

            monkeypatch.setattr(transactions, "_request_backend", restarted_backend)
            with _bind_authenticated_transaction_context(
                confirmed_context,
                _issue_transaction_capability(),
            ):
                recovered = await execute_confirmed_transaction("create_order")

            assert recovered.ok is True
            assert seen_keys == [proposal.idempotency_key]
            assert await restarted_client.get(key) is None
            await restarted_client.delete(
                transactions._proposal_index_key(session_id)
            )
            await restarted_client.aclose()
        finally:
            transactions._redis_client = None

    asyncio.run(scenario())
