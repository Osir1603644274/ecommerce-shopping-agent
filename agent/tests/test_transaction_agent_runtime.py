import asyncio
import json
from dataclasses import replace

import httpx

from app.domains.ecommerce import transactions
from app.domains.ecommerce.transactions import (
    TransactionContext,
    _bind_authenticated_transaction_context,
    execute_confirmed_transaction,
    preview_order_tool,
)
from app.schemas import ToolTrace
from app.settings import settings
from app.transaction_agent import runtime
from app.transaction_agent.capabilities import _issue_transaction_capability


class FakeRedis:
    def __init__(self):
        self.values = {}
        self.sets = {}

    async def set(self, key, value, **kwargs):
        if kwargs.get("nx") and key in self.values:
            return False
        self.values[key] = value
        return True

    async def get(self, key):
        return self.values.get(key)

    async def delete(self, *keys):
        for key in keys:
            self.values.pop(key, None)
            self.sets.pop(key, None)
        return len(keys)

    async def expire(self, key, ttl):
        return True

    async def sadd(self, key, value):
        self.sets.setdefault(key, set()).add(value)

    async def srem(self, key, value):
        self.sets.setdefault(key, set()).discard(value)

    async def getdel(self, key):
        return self.values.pop(key, None)

    async def eval(self, script, numkeys, *args):
        if script == transactions._CONFIRM_PROPOSAL_LUA:
            key, expected, replacement, _ttl = args
            current = self.values.get(key)
            if current is None:
                return ""
            if current != expected:
                return current
            self.values[key] = replacement
            return replacement
        if script == transactions._DELETE_PROPOSAL_LUA:
            key, index_key, expected = args
            if self.values.get(key) != expected:
                return 0
            self.values.pop(key, None)
            self.sets.setdefault(index_key, set()).discard(key)
            return 1
        raise AssertionError("unexpected Redis script")


def _context(message: str) -> TransactionContext:
    return TransactionContext(
        access_token="signed-access-token",
        owner_user_id="user-1",
        session_id="session-1",
        task_id="task-1",
        task_revision=7,
        candidate_scope_id="scope-1",
        user_message=message,
    )


def test_browser_confirmation_id_replay_has_one_effect_and_readonly_receipt(monkeypatch):
    monkeypatch.setattr(settings, "agent_transaction_enabled", True)
    store = FakeRedis()
    monkeypatch.setattr(transactions, "_redis_client", store)
    effects = []
    async def backend(context, method, path, **kwargs):
        if path.endswith("preview"):
            return {"itemType":"PRODUCT", "itemId":1001, "quantity":1, "payableMinor":100}
        effects.append((method,path))
        return {"id":"order-browser","status":"PENDING_PAYMENT"}
    monkeypatch.setattr(transactions,"_request_backend",backend)
    async def run():
        with _bind_authenticated_transaction_context(_context("确认下单"), _issue_transaction_capability()):
            preview = await preview_order_tool(1001,1)
            cid=preview.detail["confirmationId"]
            denied = await execute_confirmed_transaction("create_order","cfm-another-card")
            assert not denied.ok and not effects
            first = await execute_confirmed_transaction("create_order",cid)
            again = await execute_confirmed_transaction("create_order",cid)
            read = await transactions.confirmation_status_tool("create_order",cid)
            assert first.ok and again.detail == first.detail and read.detail == first.detail
            assert effects == [("POST","/api/orders")]
    asyncio.run(run())


def test_browser_confirmation_survives_token_rotation_but_not_owner_change(monkeypatch):
    monkeypatch.setattr(settings, "agent_transaction_enabled", True)
    monkeypatch.setattr(transactions, "_redis_client", FakeRedis())
    effects = []
    async def backend(context, method, path, **kwargs):
        if path.endswith("preview"):
            return {"itemType":"PRODUCT","itemId":1001,"quantity":1,"unitPriceMinor":100,"payableMinor":100}
        assert kwargs["json_body"]["expectedUnitPriceMinor"] == 100
        assert kwargs["json_body"]["expectedPayableMinor"] == 100
        effects.append(context.access_token)
        return {"id":"order-rotated","status":"PENDING_PAYMENT"}
    monkeypatch.setattr(transactions, "_request_backend", backend)
    async def run():
        original = replace(_context("确认下单"), browser_confirmation=True)
        with _bind_authenticated_transaction_context(original, _issue_transaction_capability()):
            preview = await preview_order_tool(1001,1)
        cid = preview.detail["confirmationId"]
        other = replace(original, owner_user_id="user-2", access_token="other-owner")
        with _bind_authenticated_transaction_context(other, _issue_transaction_capability()):
            assert not (await execute_confirmed_transaction("create_order",cid)).ok
        assert effects == []
        rotated = replace(original, access_token="refreshed-access-token")
        with _bind_authenticated_transaction_context(rotated, _issue_transaction_capability()):
            first = await execute_confirmed_transaction("create_order",cid)
            assert first.ok
        relogin = replace(original, access_token="new-login-token")
        with _bind_authenticated_transaction_context(relogin, _issue_transaction_capability()):
            read = await transactions.confirmation_status_tool("create_order",cid)
            replay = await execute_confirmed_transaction("create_order",cid)
            assert read.detail == first.detail == replay.detail
        assert effects == ["refreshed-access-token"]
        # Non-browser Agent flows retain their original credential binding.
        assert _context("确认下单").credential_fingerprint != replace(_context("确认下单"), access_token="new").credential_fingerprint
    asyncio.run(run())


def test_server_authenticated_dispatch_mints_private_capability_only_for_operation(monkeypatch):
    monkeypatch.setattr(settings, "agent_transaction_enabled", True)

    async def authenticate(_authorization):
        return runtime._AuthenticatedOwner(
            user_id="user-1", roles=("USER",), access_token="signed-access-token"
        )

    async def preview(product_id, quantity, user_coupon_id):
        required = transactions._require_context("preview_order")
        assert isinstance(required, TransactionContext)
        assert required.owner_user_id == "user-1"
        assert (product_id, quantity, user_coupon_id) == (1001, 1, None)
        return ToolTrace(tool="preview_order", ok=True, detail={"status": "confirmation_required"})

    monkeypatch.setattr(runtime, "_authenticate", authenticate)
    monkeypatch.setattr(runtime, "preview_order_tool", preview)

    with runtime.bind_transaction_request(
        authorization="Bearer signed-access-token",
        session_id="session-1",
        task_id="task-1",
        task_revision=7,
        candidate_scope_id="scope-1",
        user_message="订单预览",
    ):
        trace = asyncio.run(runtime.dispatch_order_preview(1001, 1, None))

    assert trace.ok is True
    assert isinstance(transactions._require_context("preview_order"), ToolTrace)


def test_preview_then_exact_confirmation_executes_once_with_same_bound_identity(monkeypatch):
    monkeypatch.setattr(settings, "agent_transaction_enabled", True)
    fake_redis = FakeRedis()
    monkeypatch.setattr(transactions, "_redis_client", fake_redis)
    calls = []

    async def backend(_context, method, path, *, json_body=None, idempotency_key=None):
        calls.append((method, path, json_body, idempotency_key))
        if path == "/api/orders/preview":
            return {
                "itemType": "PRODUCT", "itemId": 1001, "quantity": 1,
                "payableMinor": 249900, "currency": "CNY",
            }
        return {
            "id": "order-1", "orderNo": "ORDER-1",
            "payableMinor": 249900, "status": "PENDING_PAYMENT",
        }

    monkeypatch.setattr(transactions, "_request_backend", backend)

    with _bind_authenticated_transaction_context(
        _context("订单预览"), _issue_transaction_capability()
    ):
        preview = asyncio.run(preview_order_tool(1001, 1))
    assert preview.ok is True

    with _bind_authenticated_transaction_context(
        _context("确认下单"), _issue_transaction_capability()
    ):
        first = asyncio.run(execute_confirmed_transaction("create_order"))
        second = asyncio.run(execute_confirmed_transaction("create_order"))

    assert first.ok is True
    assert first.detail["result"]["orderNo"] == "ORDER-1"
    assert second.ok is False
    assert second.detail["code"] == "confirmation_missing_or_expired"
    assert [path for _method, path, _body, _key in calls] == [
        "/api/orders/preview", "/api/orders",
    ]
    assert calls[1][3].startswith("agent-")


def test_create_order_timeout_reconciles_by_idempotency_key(monkeypatch):
    monkeypatch.setattr(settings, "agent_transaction_enabled", True)
    fake_redis = FakeRedis()
    monkeypatch.setattr(transactions, "_redis_client", fake_redis)
    calls = []

    async def backend(_context, method, path, *, json_body=None, idempotency_key=None):
        calls.append((method, path, idempotency_key))
        if path == "/api/orders/preview":
            return {"itemType": "PRODUCT", "itemId": 1001, "quantity": 1}
        if method == "POST":
            raise httpx.ReadTimeout("response lost after commit")
        assert path.startswith("/api/orders/by-idempotency-key/agent-")
        return {
            "id": "order-1", "orderNo": "ORDER-1",
            "payableMinor": 249900, "status": "PENDING_PAYMENT",
        }

    monkeypatch.setattr(transactions, "_request_backend", backend)
    with _bind_authenticated_transaction_context(
        _context("订单预览"), _issue_transaction_capability()
    ):
        assert asyncio.run(preview_order_tool(1001, 1)).ok is True
    with _bind_authenticated_transaction_context(
        _context("确认下单"), _issue_transaction_capability()
    ):
        trace = asyncio.run(execute_confirmed_transaction("create_order"))

    assert trace.ok is True
    assert trace.detail["result"]["id"] == "order-1"
    assert [call[0] for call in calls] == ["POST", "POST", "GET"]
    assert not fake_redis.values


def test_confirmed_command_survives_effect_before_receipt_crash(monkeypatch):
    monkeypatch.setattr(settings, "agent_transaction_enabled", True)
    fake_redis = FakeRedis()
    monkeypatch.setattr(transactions, "_redis_client", fake_redis)
    keys = []

    async def preview_backend(_context, method, path, *, json_body=None, idempotency_key=None):
        assert path == "/api/orders/preview"
        return {"itemType": "PRODUCT", "itemId": 1001, "quantity": 1}

    monkeypatch.setattr(transactions, "_request_backend", preview_backend)
    with _bind_authenticated_transaction_context(
        _context("订单预览"), _issue_transaction_capability()
    ):
        assert asyncio.run(preview_order_tool(1001, 1)).ok is True

    # Process A durably records confirmation and then disappears after the
    # external effect, before any local success receipt/deletion.
    with _bind_authenticated_transaction_context(
        _context("确认下单"), _issue_transaction_capability()
    ):
        proposal = asyncio.run(
            transactions._consume_proposal(_context("确认下单"), "create_order")
        )
    assert proposal is not None and proposal.confirmed_at is not None
    assert fake_redis.values

    async def restarted_backend(_context, method, path, *, json_body=None, idempotency_key=None):
        keys.append(idempotency_key)
        return {
            "id": "order-1", "orderNo": "ORDER-1",
            "payableMinor": 249900, "status": "PENDING_PAYMENT",
        }

    monkeypatch.setattr(transactions, "_request_backend", restarted_backend)
    with _bind_authenticated_transaction_context(
        _context("确认下单"), _issue_transaction_capability()
    ):
        recovered = asyncio.run(execute_confirmed_transaction("create_order"))

    assert recovered.ok is True
    assert keys == [proposal.idempotency_key]
    assert not fake_redis.values


def test_cancel_timeout_reconciles_from_order_status(monkeypatch):
    monkeypatch.setattr(settings, "agent_transaction_enabled", True)
    fake_redis = FakeRedis()
    monkeypatch.setattr(transactions, "_redis_client", fake_redis)

    async def prepare():
        return await transactions._save_proposal(
            _context("取消预览"),
            action="cancel_order",
            payload={"orderId": "order-1"},
            preview={"order": {"id": "order-1", "status": "PENDING_PAYMENT"}},
        )

    with _bind_authenticated_transaction_context(
        _context("取消预览"), _issue_transaction_capability()
    ):
        asyncio.run(prepare())

    async def backend(_context, method, path, *, json_body=None, idempotency_key=None):
        if method == "POST":
            raise httpx.ReadTimeout("cancel response lost")
        assert path == "/api/orders/order-1"
        return {"id": "order-1", "orderNo": "ORDER-1", "status": "CANCELLED"}

    monkeypatch.setattr(transactions, "_request_backend", backend)
    with _bind_authenticated_transaction_context(
        _context("确认取消订单"), _issue_transaction_capability()
    ):
        trace = asyncio.run(execute_confirmed_transaction("cancel_order"))
    assert trace.ok is True
    assert trace.detail["result"]["status"] == "CANCELLED"


def test_payment_timeout_reconciles_from_natural_order_key(monkeypatch):
    monkeypatch.setattr(settings, "agent_transaction_enabled", True)
    fake_redis = FakeRedis()
    monkeypatch.setattr(transactions, "_redis_client", fake_redis)

    async def prepare():
        return await transactions._save_proposal(
            _context("支付预览"),
            action="create_payment",
            payload={"orderId": "order-1"},
            preview={"order": {"id": "order-1", "status": "PENDING_PAYMENT"}},
        )

    with _bind_authenticated_transaction_context(
        _context("支付预览"), _issue_transaction_capability()
    ):
        asyncio.run(prepare())

    async def backend(_context, method, path, *, json_body=None, idempotency_key=None):
        if method == "POST":
            raise httpx.ReadTimeout("payment response lost")
        assert path == "/api/payments/orders/order-1"
        return {
            "id": "payment-1", "paymentNo": "PAYMENT-1",
            "orderId": "order-1", "status": "CREATED",
        }

    monkeypatch.setattr(transactions, "_request_backend", backend)
    with _bind_authenticated_transaction_context(
        _context("确认发起支付"), _issue_transaction_capability()
    ):
        trace = asyncio.run(execute_confirmed_transaction("create_payment"))
    assert trace.ok is True
    assert trace.detail["result"]["id"] == "payment-1"


def test_unresolved_ambiguous_write_stays_durable_and_recovers(monkeypatch):
    monkeypatch.setattr(settings, "agent_transaction_enabled", True)
    fake_redis = FakeRedis()
    monkeypatch.setattr(transactions, "_redis_client", fake_redis)

    async def preview_backend(_context, method, path, *, json_body=None, idempotency_key=None):
        return {"itemType": "PRODUCT", "itemId": 1001, "quantity": 1}

    monkeypatch.setattr(transactions, "_request_backend", preview_backend)
    with _bind_authenticated_transaction_context(
        _context("订单预览"), _issue_transaction_capability()
    ):
        asyncio.run(preview_order_tool(1001, 1))

    async def unavailable(_context, method, path, *, json_body=None, idempotency_key=None):
        raise httpx.ReadTimeout("all responses unavailable")

    monkeypatch.setattr(transactions, "_request_backend", unavailable)
    with _bind_authenticated_transaction_context(
        _context("确认下单"), _issue_transaction_capability()
    ):
        unknown = asyncio.run(execute_confirmed_transaction("create_order"))
    assert unknown.ok is False
    assert unknown.detail["code"] == "transaction_outcome_unknown"
    assert fake_redis.values

    seen = []

    async def recovered_backend(_context, method, path, *, json_body=None, idempotency_key=None):
        seen.append(idempotency_key)
        return {
            "id": "order-1", "orderNo": "ORDER-1",
            "payableMinor": 249900, "status": "PENDING_PAYMENT",
        }

    monkeypatch.setattr(transactions, "_request_backend", recovered_backend)
    with _bind_authenticated_transaction_context(
        _context("确认下单"), _issue_transaction_capability()
    ):
        recovered = asyncio.run(execute_confirmed_transaction("create_order"))
    assert recovered.ok is True
    assert len(seen) == 1 and seen[0].startswith("agent-")
    assert not fake_redis.values


def test_tampered_durable_command_is_rejected_before_backend(monkeypatch):
    monkeypatch.setattr(settings, "agent_transaction_enabled", True)
    fake_redis = FakeRedis()
    monkeypatch.setattr(transactions, "_redis_client", fake_redis)

    async def preview_backend(_context, method, path, *, json_body=None, idempotency_key=None):
        return {"itemType": "PRODUCT", "itemId": 1001, "quantity": 1}

    monkeypatch.setattr(transactions, "_request_backend", preview_backend)
    with _bind_authenticated_transaction_context(
        _context("订单预览"), _issue_transaction_capability()
    ):
        asyncio.run(preview_order_tool(1001, 1))
    proposal_key = next(iter(fake_redis.values))
    tampered = json.loads(fake_redis.values[proposal_key])
    tampered["payload"]["quantity"] = 2
    fake_redis.values[proposal_key] = json.dumps(tampered)

    async def forbidden(*args, **kwargs):
        raise AssertionError("tampered command must not reach backend")

    monkeypatch.setattr(transactions, "_request_backend", forbidden)
    with _bind_authenticated_transaction_context(
        _context("确认下单"), _issue_transaction_capability()
    ):
        trace = asyncio.run(execute_confirmed_transaction("create_order"))
    assert trace.ok is False
    assert trace.detail["code"] == "confirmation_missing_or_expired"


def test_concurrent_confirmations_share_one_backend_command_key(monkeypatch):
    monkeypatch.setattr(settings, "agent_transaction_enabled", True)
    fake_redis = FakeRedis()
    monkeypatch.setattr(transactions, "_redis_client", fake_redis)
    seen = []

    async def backend(_context, method, path, *, json_body=None, idempotency_key=None):
        if path == "/api/orders/preview":
            return {"itemType": "PRODUCT", "itemId": 1001, "quantity": 1}
        seen.append(idempotency_key)
        await asyncio.sleep(0)
        return {
            "id": "order-1", "orderNo": "ORDER-1",
            "payableMinor": 249900, "status": "PENDING_PAYMENT",
        }

    monkeypatch.setattr(transactions, "_request_backend", backend)
    with _bind_authenticated_transaction_context(
        _context("订单预览"), _issue_transaction_capability()
    ):
        asyncio.run(preview_order_tool(1001, 1))

    async def confirm_twice():
        with _bind_authenticated_transaction_context(
            _context("确认下单"), _issue_transaction_capability()
        ):
            return await asyncio.gather(
                execute_confirmed_transaction("create_order"),
                execute_confirmed_transaction("create_order"),
            )

    first, second = asyncio.run(confirm_twice())
    assert first.ok is True and second.ok is True
    assert len(seen) == 2 and len(set(seen)) == 1
    assert not fake_redis.values
