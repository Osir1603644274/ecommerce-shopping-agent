"""Browser-bound shopping workspace; ordinary chat has NO transaction credential."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from contextvars import ContextVar
import json
import logging
import re
import secrets
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from . import commerce_demo as auth
from .browser_ids import browser_ids
from .transaction_agent import _raise_for_trace
from ..schemas import EcommerceChatRequest
from ..transaction_agent.runtime import (
    bind_transaction_request, dispatch_order_preview, dispatch_payment_preview,
    dispatch_confirmed_handoff, dispatch_confirmation_status,
    dispatch_cancel_preview, dispatch_refund_preview,
)

router = APIRouter(prefix="/api/commerce-demo/workspace", tags=["统一购物空间"])
logger = logging.getLogger(__name__)
VISITOR_COOKIE = "commerce_browser"
TTL = 14 * 86400
_UNLOCK = "if redis.call('GET', KEYS[1]) == ARGV[1] then return redis.call('DEL', KEYS[1]) end return 0"
_SAVE_OWNED = """
if redis.call('GET', KEYS[1]) ~= ARGV[1] then return 0 end
redis.call('SET', KEYS[2], ARGV[2], 'EX', ARGV[3])
return 1
"""
# Copy on entry; child asyncio tasks must acquire their own lock, not inherit permission.
_HELD_LOCKS: ContextVar[dict] = ContextVar('commerce_workspace_held_locks', default={})


def _write_owner(key):
    held = _HELD_LOCKS.get()
    owner_key = key if key in held else key.rsplit(':', 1)[0] if key.endswith((':run', ':answer')) else key
    owner = held.get(owner_key)
    if owner is None or owner[1] is not asyncio.current_task():
        raise RuntimeError('Workspace writes require the current task to hold its workspace lock')
    return owner_key + ':lock', owner[0]


def _lost_lock():
    return HTTPException(409, '本次操作已失去购物状态更新权限，请刷新页面核对当前状态后重试')


class ChatInput(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    message: str = Field(min_length=1, max_length=2000)
    request_id: str = Field(alias="requestId", pattern=r"^[a-zA-Z0-9_-]{16,64}$")


class SelectionInput(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    product_id: int = Field(alias="productId", gt=0)
    quantity: int = Field(default=1, ge=1, le=20)


class ConfirmationInput(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    confirmation_id: str = Field(alias="confirmationId", pattern=r"^cfm-[a-zA-Z0-9_-]{10,64}$")


class PaymentInput(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    order_id: str = Field(alias="orderId", pattern=r"^[a-zA-Z0-9_-]{1,80}$")


class RefundLine(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    item_id: int = Field(alias="itemId", gt=0)
    quantity: int = Field(ge=1, le=100000)


class RefundInput(PaymentInput):
    items: list[RefundLine] = Field(min_length=1, max_length=50)
    reason: str = Field(min_length=1, max_length=255)


def _fresh():
    return {"conversationId": secrets.token_hex(16), "engine": "web-" + secrets.token_urlsafe(24), "csrf": secrets.token_urlsafe(32),
            "messages": [], "cards": [], "selection": None, "checkout": None, "reference": None}


async def _load(key):
    raw = await auth._redis().get(key)
    value = json.loads(raw) if raw else None
    if isinstance(value, dict) and 'engine' in value and 'messages' in value:
        from ..workspace_archive import conversation_id
        # Assign the legacy identity before any operation rotates its engine.
        value.setdefault('conversationId', conversation_id(value))
    return value


async def _save(key, value):
    lock_key, token = _write_owner(key)
    # Avoid archiving a request already known to be stale. The final Lua check
    # is still required: the lease can expire during the SQLite archive write.
    if await auth._redis().get(lock_key) != token:
        raise _lost_lock()
    if 'engine' in value and 'messages' in value:
        from ..workspace_archive import get_archive, conversation_id
        value['conversationId'] = conversation_id(value)
        # Commit before accepting/executing a question; capture failure is not silent.
        await asyncio.to_thread(get_archive().record, key, value)
    saved = await auth._redis().eval(_SAVE_OWNED, 2, lock_key, key, token,
                                    json.dumps(value, ensure_ascii=False), TTL)
    if saved != 1:
        raise _lost_lock()


@asynccontextmanager
async def _lock(key):
    lock_key, token = key + ":lock", secrets.token_urlsafe(20)
    if not await auth._redis().set(lock_key, token, nx=True, ex=240):
        raise HTTPException(409, "上一项操作还在处理，请稍后回查")
    scope = _HELD_LOCKS.set({**_HELD_LOCKS.get(), key: (token, asyncio.current_task())})
    try:
        yield
    finally:
        _HELD_LOCKS.reset(scope)
        await auth._redis().eval(_UNLOCK, 1, lock_key, token)


async def _identity(request: Request, response: Response, *, bootstrap=False, authenticated=False):
    auth._require_enabled()
    auth._require_same_origin(request)
    response.headers["Cache-Control"] = "no-store"
    session = None
    if request.cookies.get(auth.COOKIE_NAME):
        try:
            session = await auth._load_session(request.cookies[auth.COOKIE_NAME])
        except HTTPException as exc:
            if not bootstrap or exc.status_code != 401:
                raise
            response.delete_cookie(auth.COOKIE_NAME, path="/")
        if session:
            auth._require_csrf(session, request.headers.get(auth.CSRF_HEADER))
    elif authenticated:
        raise HTTPException(401, "authentication required")
    visitor = request.cookies.get(VISITOR_COOKIE)
    if not visitor or not re.fullmatch(r"[a-zA-Z0-9_-]{32,64}", visitor):
        if not bootstrap:
            raise HTTPException(409, "请先恢复购物空间")
        visitor = secrets.token_urlsafe(32)
    prefix = "commerce:workspace:" + (auth.settings.commerce_workspace_epoch + ":" if auth.settings.commerce_workspace_epoch else "")
    base = prefix + auth._digest(visitor)
    if not session and await auth._redis().get(base + ":claimed"):
        if not bootstrap:
            # This browser's workspace belonged to a login that has expired.
            # Signal authentication recovery, not an endlessly retried run conflict.
            raise HTTPException(401, "authentication expired")
        visitor = secrets.token_urlsafe(32)
        base = prefix + auth._digest(visitor)
    key = (prefix + "user:" + auth._digest(session["username"])) if session else base + ":guest"
    state = await _load(key)
    adoption = bootstrap and session and not await auth._redis().get(base + ":claimed")
    if state is None or adoption:
        if not bootstrap:
            raise HTTPException(409, "请先恢复购物空间")
        async with _lock(base):
            async with _lock(key):
                state = await _load(key)
                if state is None:
                    state = _fresh()
                if session and not await auth._redis().get(base + ":claimed"):
                    guest = await _load(base + ":guest")
                    guest_run = await _load(base + ':guest:run')
                    if guest_run and guest_run.get('status') not in {'completed', 'ended'}:
                        raise HTTPException(409, '请先完成或结束游客任务，再切换账户')
                    if guest and guest["messages"] and not (state.get("checkout") and state["checkout"].get("pending")):
                        guest["messages"] = (state["messages"] + guest["messages"])[-60:]
                        state = guest
                        state["checkout"] = None
                    await auth._redis().set(base + ":claimed", "1", ex=TTL)
                    await auth._redis().delete(base + ":guest")
                await _save(key, state)
    if bootstrap:
        response.set_cookie(VISITOR_COOKIE, visitor, max_age=TTL, httponly=True,
                            secure=auth.settings.commerce_demo_cookie_secure, samesite="lax", path="/")
    elif not session:
        auth._require_csrf({"csrfDigest": auth._digest(state["csrf"])}, request.headers.get(auth.CSRF_HEADER))
    if request.method not in {'GET', 'HEAD'} and not (request.url.path.endswith('/run') or '/control/' in request.url.path):
        running = await _load(key + ':run')
        if running and running.get('status') not in {'completed', 'ended'}:
            raise HTTPException(409, '请先继续或结束导购任务，再操作交易或修改商品选择')
    return key, state, session


def _public(state):
    checkout = state.get("checkout")
    from ..workspace_archive import conversation_id
    demo = state.get('recommendationDemo') or {}
    return browser_ids({"conversationId": conversation_id(state), "recommendationDemo": {k:demo[k] for k in ('caseId','revision') if k in demo} if demo else None,
            "messages": state["messages"], "cards": state["cards"], "selection": state["selection"],
            "checkout": {k: v for k, v in checkout.items() if k in {"proposal", "outcome", "pending"}} if checkout else None})


@router.get("")
async def workspace(request: Request, response: Response):
    key, state, session = await _identity(request, response, bootstrap=True)
    from .commerce_controls import public, refresh
    from .workspace_answer_stream import read
    run = await refresh(key)
    return {**_public(state), "run": public(run), "csrfToken": state["csrf"] if not session else None,
            "answerStream": await read(key, run['id']) if run and run['status'] not in {'completed','ended'} else None}


@router.get('/conversations')
async def conversations(request: Request, response: Response, offset: int = 0):
    from ..workspace_archive import get_archive
    key, state, _ = await _identity(request, response)
    if offset < 0 or offset > 100000:
        raise HTTPException(422, '无效的历史分页位置')
    await asyncio.to_thread(get_archive().record, key, state)
    return await asyncio.to_thread(get_archive().history, key, offset)


@router.get('/conversations/{cid}')
async def conversation(request: Request, response: Response, cid: str):
    from ..workspace_archive import get_archive
    key, _, _ = await _identity(request, response)
    saved = await asyncio.to_thread(get_archive().read, key, cid)
    if saved is None:
        raise HTTPException(404, '对话不存在或不属于当前用户')
    return browser_ids(saved)


class NewConversation(BaseModel):
    model_config = ConfigDict(extra='forbid')
    expectedConversationId: str


@router.post('/conversations')
async def new_conversation(body: NewConversation, request: Request, response: Response):
    from ..workspace_archive import conversation_id
    from .commerce_controls import snapshot, refresh, TERMINAL
    key, _, _ = await _identity(request, response)
    async with _lock(key):
        state = await _load(key)
        run = await refresh(key)
        if conversation_id(state) != body.expectedConversationId:
            raise HTTPException(409, '对话已切换，请刷新后重试')
        if run and run['status'] not in TERMINAL:
            raise HTTPException(409, '请先完成或结束当前任务')
        if state.get('checkout') and state['checkout'].get('pending'):
            raise HTTPException(409, '请先回查未决交易')
        await _save(key, state)
        fresh = _fresh()
        fresh['csrf'] = state['csrf']
        await _save(key, fresh)
        # Only retire the active UI pointer; durable execution receipts remain.
        await auth._redis().delete(key + ':run')
        return await snapshot(key)


@router.post('/conversations/{cid}/activate')
async def activate_conversation(cid: str, body: NewConversation, request: Request, response: Response):
    """Continue an owner's transcript, never replay archived execution authority."""
    from copy import deepcopy
    from ..workspace_archive import get_archive, conversation_id
    from .commerce_controls import snapshot, refresh, TERMINAL
    key, _, _ = await _identity(request, response)
    async with _lock(key):
        state = await _load(key)
        if conversation_id(state) != body.expectedConversationId:
            raise HTTPException(409, '对话已切换，请刷新后重试')
        if state.get('checkout') and state['checkout'].get('pending'):
            raise HTTPException(409, '请先回查未决交易')
        run = await refresh(key)
        if run and run['status'] not in TERMINAL:
            raise HTTPException(409, '请先停止并结束当前任务，再切换对话')
        saved = await asyncio.to_thread(get_archive().read, key, cid)
        if saved is None:
            raise HTTPException(404, '对话不存在或不属于当前用户')
        if cid == conversation_id(state):
            return await snapshot(key)
        await _save(key, state)
        fresh = _fresh()
        fresh.update(conversationId=cid, csrf=state['csrf'], messages=deepcopy(saved['messages']),
                     restoredConversation=True)
        # Display saved cards as history only. Selection always calls Java again
        # and creates a fresh selectionId before any checkout preview is allowed.
        fresh['cards'] = next((m['cards'] for m in reversed(fresh['messages']) if m.get('cards')), [])
        await _save(key, fresh)
        await auth._redis().delete(key + ':run')
        return await snapshot(key)


async def _card(product_id: int):
    merged = auth.settings.commerce_workspace_local_offers_enabled
    data = await auth._java("GET", f"/api/products/{product_id}" + ("/purchase-view" if merged else ""))
    product = data
    product = product.get("product", product)
    price = product.get("snapshotPriceMinor") if product.get("priceStatus") == "verified" else None
    phone = product.get("categoryL3") == "二手手机"
    eligible = phone
    available = product.get("availableQuantity")
    can_purchase = phone and price is not None and available and available > 0
    kind = product.get("priceStatus")
    if merged:
        offer = data["offer"]
        price, available, kind = offer.get("priceMinor"), offer.get("available"), offer.get("kind")
        eligible = phone or (auth.settings.commerce_workspace_external_catalog_enabled
                             and offer.get("externalCatalogEligible") is True)
        can_purchase = eligible and offer.get("canPurchase") is True
    return {"id": product_id, "title": product["title"], "brand": product.get("brand") or "",
            "category": product.get("categoryL3") if product.get("categoryL3") not in {None, "", "unknown"} else product.get("categoryL1", "unknown"),
            "priceMinor": price, "currency": (offer.get("currency") if merged else product.get("currency")) or "CNY",
            "available": available, "purchasable": bool(can_purchase), "priceKind": kind,
            "catalogEligible": eligible}


def _event(kind, **values):
    return "data: " + json.dumps({"type": kind, **values}, ensure_ascii=False) + "\n\n"


@router.post("/chat")
async def chat(body: ChatInput, request: Request, response: Response):
    key, _, _ = await _identity(request, response)
    async def locked_stream():
        async with _lock(key):
            state = await _load(key)
            original = next((m for m in state["messages"] if m.get("requestId") == body.request_id and m["role"] == "user"), None)
            if original and original["content"] != body.message:
                yield _event("error", message="重试内容与原请求不一致，请重新发送。")
                return
            if any(m.get("requestId") == body.request_id and m["role"] == "assistant" for m in state["messages"]):
                yield _event("complete", workspace=_public(state))
                return
            if state.get("checkout") and state["checkout"].get("pending"):
                yield _event("error", message="交易结果尚未确认，请先回查结果。")
                return
            state["checkout"] = None
            if not any(m.get("requestId") == body.request_id for m in state["messages"]):
                state["messages"].append({"role": "user", "content": body.message, "requestId": body.request_id})
            await _save(key, state)
            from ..main import chat_llm_stream
            if any(word in body.message for word in ("退款", "取消订单", "我的订单", "查订单", "查看订单", "物流", "发货")):
                answer = "请打开「我的订单」，选择对应订单查看最新状态、履约记录或预览取消/退款。退款需要你选择明细与数量，核对金额后再确认；我不会替你猜测。"
                state["messages"].append({"role": "assistant", "content": answer, "requestId": body.request_id})
                await _save(key, state)
                yield _event("complete", workspace=_public(state))
                return
            try:
                reference = state.get("reference")
                hint = {"handle": reference["handle"], "presentationMode": "compact"} if reference and reference.get("handle") else None
                if hint and state.get("selection"):
                    hint["focusedProductId"] = str(state["selection"]["product"]["id"])
                payload = EcommerceChatRequest(message=body.message, sessionId=state["engine"],
                                               domainHint="ecommerce", referenceContext=hint)
                async with asyncio.timeout(180):
                    upstream = await chat_llm_stream(payload, authorization=None, shopping_memory_session=None)
                    buffer = ""
                    async for chunk in upstream.body_iterator:
                        buffer += chunk.decode() if isinstance(chunk, bytes) else chunk
                        while "\n\n" in buffer:
                            block, buffer = buffer.split("\n\n", 1)
                            for line in block.splitlines():
                                if not line.startswith("data: "):
                                    continue
                                event = json.loads(line[6:])
                                if event.get("type") == "delta":
                                    yield _event("delta", text=event.get("delta", event.get("text", "")))
                                elif event.get("type") == "status":
                                    yield _event("status", message="正在为你寻找合适的商品…")
                                elif event.get("type") == "complete":
                                    data = event["data"]
                                    if (data.get("trace") or {}).get("status", "ok") != "ok":
                                        # Upstream failure answers may contain provider diagnostics.
                                        # Do not publish them or persist them as a successful reply.
                                        raise RuntimeError("upstream_chat_failed")
                                    rows = (data.get("guideResult") or {}).get("products", [])
                                    ids = list(dict.fromkeys(int(row["product"]["id"]) for row in rows))[:6]
                                    cards = await asyncio.gather(*(_card(i) for i in ids))
                                    state["messages"].append({"role": "assistant", "content": data["answer"],
                                                             "requestId": body.request_id, "cards": cards})
                                    state["messages"] = state["messages"][-60:]
                                    state["cards"] = cards
                                    state["reference"] = data.get("referenceContext")
                                    state["selection"] = None
                                    await _save(key, state)
                                    yield _event("complete", workspace=_public(state))
            except Exception as exc:
                logger.warning("Browser chat incomplete: %s", type(exc).__name__)
                yield _event("error", message="本次回答暂未完成，可以重试；不会自动下单。")
    async def stream():
        try:
            async for event in locked_stream():
                yield event
        except HTTPException as exc:
            yield _event("error", message=str(exc.detail))
        except Exception as exc:
            logger.warning("Browser workspace stream unavailable: %s", type(exc).__name__)
            yield _event("error", message="购物空间暂不可用，请恢复连接后重试。")
    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})


@router.post("/selection")
async def select(body: SelectionInput, request: Request, response: Response):
    key, _, session = await _identity(request, response)
    async with _lock(key):
        state = await _load(key)
        if state.get("checkout") and state["checkout"].get("pending"):
            raise HTTPException(409, "请先回查上一笔交易")
        known = body.product_id in {c["id"] for m in state["messages"] for c in m.get("cards", [])}
        if not known and session:
            saved = await auth._java("GET", "/api/product-favorites", access_token=session["accessToken"])
            known = any(p["id"] == body.product_id for p in saved)
        if not known:
            raise HTTPException(409, "请从自己的对话或收藏中选择商品")
        card = await _card(body.product_id)
        state["selection"] = {"product": card, "quantity": body.quantity}
        # Human browser selection is a distinct, server-owned purchase context.
        # Never modify or fabricate the Agent's current CandidateScope/TaskState.
        state["selectionId"] = secrets.token_urlsafe(24)
        state["checkout"] = None
        await _save(key, state)
        return _public(state)


def _binding(state, session, checkout, phrase):
    return bind_transaction_request(authorization="Bearer " + session["accessToken"],
        session_id=state["engine"], task_id=checkout["taskId"], task_revision=checkout["revision"],
        candidate_scope_id=checkout["scopeId"], user_message=phrase, browser_confirmation=True)


@router.post("/preview")
async def preview(body: SelectionInput, request: Request, response: Response):
    key, _, session = await _identity(request, response, authenticated=True)
    async with _lock(key):
        state = await _load(key)
        if state.get("checkout") and state["checkout"].get("pending"):
            raise HTTPException(409, "请先回查上一笔交易")
        selection = state.get("selection")
        if (not selection or selection["product"]["id"] != body.product_id
                or not state.get("selectionId")):
            raise HTTPException(409, "请先选择商品，重新核对价格与库存")
        card = dict(selection["product"])
        # Eligibility is stable catalog scope; stock/price may have changed since
        # selection. Legacy selections without this field conservatively reselect.
        if not card.get("catalogEligible", card.get("purchasable", False)):
            raise HTTPException(409, "这件商品暂不可下单，可先收藏或继续挑选")
        checkout = {"taskId": "browser-order-" + state["selectionId"], "revision": 0,
                    "scopeId": "browser-selection-" + state["selectionId"]}
        with _binding(state, session, checkout, "订单预览"):
            trace = await dispatch_order_preview(body.product_id, body.quantity, None)
        if not trace.ok:
            _raise_for_trace(trace)
        # Java preview is the current authority. The selected card only gates
        # the server-owned catalog context; it must not supply checkout prices.
        current = trace.detail.get("preview", {})
        if type(current.get("availableQuantity")) is int:
            card["purchasable"] = current["availableQuantity"] >= body.quantity
        for source, target in (("unitPriceMinor", "priceMinor"), ("availableQuantity", "available")):
            if type(current.get(source)) is int:
                card[target] = current[source]
        for field in ("title", "currency"):
            if isinstance(current.get(field), str):
                card[field] = current[field]
        checkout.update(proposal=trace.detail, pending=False, outcome=None)
        state["selection"] = {"product": card, "quantity": body.quantity}
        state["checkout"] = checkout
        await _save(key, state)
        return _public(state)


@router.post("/payment-preview")
async def payment_preview(body: PaymentInput, request: Request, response: Response):
    key, _, session = await _identity(request, response, authenticated=True)
    async with _lock(key):
        state = await _load(key)
        if state.get("checkout") and state["checkout"].get("pending"):
            raise HTTPException(409, "请先回查上一笔交易")
        await auth._java("GET", f"/api/orders/{body.order_id}", access_token=session["accessToken"])
        checkout = {"taskId": "browser-payment-" + body.order_id, "revision": 0,
                    "scopeId": "owned-order-" + body.order_id}
        with _binding(state, session, checkout, "支付预览"):
            trace = await dispatch_payment_preview(body.order_id)
        if not trace.ok:
            _raise_for_trace(trace)
        checkout.update(proposal=trace.detail, pending=False, outcome=None)
        state["checkout"] = checkout
        await _save(key, state)
        return _public(state)


@router.post("/confirm")
@router.post("/reconcile")
async def confirm(body: ConfirmationInput, request: Request, response: Response):
    key, _, session = await _identity(request, response, authenticated=True)
    async with _lock(key):
        state = await _load(key)
        checkout = state.get("checkout")
        if not checkout or checkout["proposal"]["confirmationId"] != body.confirmation_id:
            raise HTTPException(409, "确认卡已更新，请重新核对")
        proposal = checkout["proposal"]
        was_pending = checkout.get("pending", False)
        read_only = request.url.path.endswith("/reconcile")
        if not read_only:
            checkout["pending"] = True
            await _save(key, state)
        with _binding(state, session, checkout, proposal["confirmationPhrase"]):
            trace = (await dispatch_confirmation_status(proposal["action"], body.confirmation_id)
                     if read_only else await dispatch_confirmed_handoff(proposal["action"], body.confirmation_id))
        if trace.ok:
            checkout["outcome"] = trace.detail
            checkout["pending"] = trace.detail.get("status") == "unknown"
        else:
            rejected = trace.detail.get("code") in {"transaction_rejected", "confirmation_missing_or_expired", "confirmation_mismatch"}
            if was_pending and trace.detail.get("code") != "transaction_rejected":
                # An expired/missing recovery record is not proof of no DB effect.
                rejected = False
            checkout["pending"] = not rejected
            checkout["outcome"] = {"status": "rejected" if rejected else "unknown", "message": trace.detail.get("message", "请回查交易结果")}
        await _save(key, state)
    return _public(state)


@router.post("/cancel-preview")
async def cancel_preview(body: PaymentInput, request: Request, response: Response):
    return await _after_sale_preview(body, request, response, False)


@router.post("/refund-preview")
async def refund_preview(body: RefundInput, request: Request, response: Response):
    return await _after_sale_preview(body, request, response, True)


async def _after_sale_preview(body, request, response, refund):
    key, _, session = await _identity(request, response, authenticated=True)
    async with _lock(key):
        state = await _load(key)
        if state.get("checkout") and state["checkout"].get("pending"):
            raise HTTPException(409, "请先回查上一笔交易")
        checkout = {"taskId": "browser-after-sale-" + secrets.token_urlsafe(16), "revision": 0,
                    "scopeId": "owned-order-" + body.order_id}
        with _binding(state, session, checkout, "售后预览"):
            trace = (await dispatch_refund_preview(body.order_id, [line.model_dump(by_alias=True) for line in body.items], body.reason)
                     if refund else await dispatch_cancel_preview(body.order_id))
        if not trace.ok:
            _raise_for_trace(trace)
        checkout.update(proposal=trace.detail, pending=False, outcome=None)
        state["checkout"] = checkout
        await _save(key, state)
        return _public(state)


@router.get("/orders/{order_id}/after-sales")
async def after_sales(order_id: str, request: Request, response: Response):
    _, _, session = await _identity(request, response, authenticated=True)
    if not re.fullmatch(r"[a-zA-Z0-9_-]{1,80}", order_id):
        raise HTTPException(422, "订单编号无效")
    token = session["accessToken"]
    order = await auth._java("GET", f"/api/orders/{order_id}", access_token=token)
    balance = await auth._java("GET", f"/api/payments/orders/{order_id}/refund-balance", access_token=token)
    try:
        fulfillment = await auth._java("GET", f"/api/orders/{order_id}/fulfillment", access_token=token)
    except HTTPException as exc:
        if exc.status_code != 404:
            raise
        fulfillment = None  # Historical orders without enrollment; do not invent progress.
    remaining = None
    if order.get('status') == 'PENDING_PAYMENT' and order.get('expiresAt'):
        # Java order timestamps are written with Clock.systemUTC; browser timezone is irrelevant.
        deadline = datetime.fromisoformat(order['expiresAt']).replace(tzinfo=timezone.utc)
        remaining = max(0, int((deadline - datetime.now(timezone.utc)).total_seconds()))
    return browser_ids({"order": order, "balance": balance, "fulfillment": fulfillment, "remainingSeconds": remaining})


@router.post("/refunds/{refund_id}/simulate-success")
async def simulate_refund(refund_id: str, request: Request, response: Response):
    _, _, session = await _identity(request, response, authenticated=True)
    if not auth.settings.commerce_demo_payment_simulation_enabled:
        raise HTTPException(404, "本地模拟未开启")
    if not re.fullmatch(r"[a-zA-Z0-9_-]{1,80}", refund_id):
        raise HTTPException(422, "退款编号无效")
    return await auth._java("POST", f"/api/payments/partial-refunds/{refund_id}/simulate-success", access_token=session["accessToken"])


@router.get("/favorites")
async def favorites(request: Request, response: Response):
    _, _, session = await _identity(request, response, authenticated=True)
    rows = await auth._java("GET", "/api/product-favorites", access_token=session["accessToken"])
    if auth.settings.commerce_workspace_local_offers_enabled:
        return browser_ids({"products": await asyncio.gather(*(_card(p["id"]) for p in rows))})
    return browser_ids({"products": [{"id": p["id"], "title": p["title"], "brand": p.get("brand") or "",
                          "priceMinor": p.get("snapshotPriceMinor"), "currency": p.get("currency") or "CNY"} for p in rows]})


@router.put("/favorites/{product_id}")
@router.delete("/favorites/{product_id}")
async def favorite(product_id: int, request: Request, response: Response):
    if product_id <= 0:
        raise HTTPException(422, "商品编号无效")
    _, _, session = await _identity(request, response, authenticated=True)
    return await auth._java(request.method, f"/api/product-favorites/{product_id}", access_token=session["accessToken"])
