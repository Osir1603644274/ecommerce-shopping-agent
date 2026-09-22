"""Browser-owned customer support. No simulator/admin routes or model credentials."""
from typing import Annotated, Literal

from fastapi import APIRouter, Header, HTTPException, Path, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from . import commerce_demo as auth
from .commerce_workspace import _identity, _load, _save, _lock
from .browser_ids import browser_ids

router = APIRouter(prefix="/api/commerce-demo/workspace/support", tags=["客服售后"])
Identifier = Annotated[str, Path(pattern=r"^[a-zA-Z0-9_-]{1,80}$")]
Key = Annotated[str, Header(alias="Idempotency-Key", pattern=r"^[a-zA-Z0-9_-]{8,128}$")]


class Body(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class Preview(Body):
    order_id: str = Field(alias="orderId", pattern=r"^[a-zA-Z0-9_-]{1,80}$")
    item_id: int = Field(alias="itemId", gt=0)
    quantity: int = Field(ge=1, le=100000)
    type: Literal["REFUND_ONLY", "RETURN_REFUND", "EXCHANGE"]
    reason: str = Field(min_length=1, max_length=1000)


class Confirmation(Body):
    preview_id: str = Field(alias="previewId", pattern=r"^[a-zA-Z0-9_-]{1,80}$")


class Version(Body):
    expected_version: int = Field(alias="expectedVersion", ge=0)


class Shipment(Version):
    tracking_no: str = Field(alias="trackingNo", pattern=r"^[a-zA-Z0-9_-]{3,128}$")


class Ticket(Body):
    order_id: str = Field(alias="orderId", pattern=r"^[a-zA-Z0-9_-]{1,80}$")
    case_id: str | None = Field(default=None, alias="caseId", pattern=r"^[a-zA-Z0-9_-]{1,80}$")
    category: Literal["DELIVERY_DELAY", "PAYMENT_QUERY", "AFTERSALE_DISPUTE", "INFO_VERIFY", "COMPLAINT"]
    summary: str = Field(min_length=1, max_length=1000)


class Reply(Version):
    message: str = Field(min_length=1, max_length=2000)


async def forward(request, response, method, path, body=None, key=None, params=None):
    _, _, session = await _identity(request, response, authenticated=True)
    kwargs = {"access_token": session["accessToken"]}
    if body is not None:
        kwargs["body"] = body.model_dump(by_alias=True)
    if key is not None:
        kwargs["idempotency_key"] = key
    if params is not None:
        kwargs["params"] = params
    return browser_ids(await auth._java(method, path, **kwargs))


@router.get("/orders/{id}/cases")
async def order_cases(id: Identifier, request: Request, response: Response):
    return await forward(request, response, "GET", f"/api/after-sales/orders/{id}")


@router.get("/cases/{id}")
async def case(id: Identifier, request: Request, response: Response):
    return await forward(request, response, "GET", f"/api/after-sales/{id}")


@router.get("/cases/{id}/events")
async def case_events(id: Identifier, request: Request, response: Response):
    return await forward(request, response, "GET", f"/api/after-sales/{id}/events")


@router.post("/preview")
async def preview(body: Preview, request: Request, response: Response):
    return await forward(request, response, "POST", "/api/after-sales/preview", body)


@router.post("/confirm")
async def confirm(body: Confirmation, key: Key, request: Request, response: Response):
    return await forward(request, response, "POST", "/api/after-sales/confirm", body, key)


@router.post("/cases/{id}/cancel")
async def cancel(id: Identifier, body: Version, key: Key, request: Request, response: Response):
    return await forward(request, response, "POST", f"/api/after-sales/{id}/cancel", body, key)


@router.post("/cases/{id}/return-shipment")
async def shipment(id: Identifier, body: Shipment, key: Key, request: Request, response: Response):
    return await forward(request, response, "POST", f"/api/after-sales/{id}/return-shipment", body, key)


@router.post("/cases/{id}/wait-stock")
async def wait_stock(id: Identifier, body: Version, key: Key, request: Request, response: Response):
    return await forward(request, response, "POST", f"/api/after-sales/{id}/wait-stock", body, key)


@router.post("/cases/{id}/conversion-preview")
async def conversion_preview(id: Identifier, request: Request, response: Response):
    return await forward(request, response, "POST", f"/api/after-sales/{id}/conversion-preview")


@router.post("/conversion-confirm")
async def conversion_confirm(body: Confirmation, key: Key, request: Request, response: Response):
    return await forward(request, response, "POST", "/api/after-sales/conversion-confirm", body, key)


@router.get("/tickets")
async def tickets(request: Request, response: Response, offset: int = Query(0, ge=0), limit: int = Query(20, ge=1, le=100)):
    return await forward(request, response, "GET", "/api/support/tickets", params={"offset": str(offset), "limit": str(limit)})


@router.post("/tickets")
async def ticket(body: Ticket, key: Key, request: Request, response: Response):
    return await forward(request, response, "POST", "/api/support/tickets", body, key)


@router.get("/tickets/{id}/events")
async def ticket_events(id: Identifier, request: Request, response: Response):
    return await forward(request, response, "GET", f"/api/support/tickets/{id}/events")


@router.post("/tickets/{id}/reply")
async def reply(id: Identifier, body: Reply, key: Key, request: Request, response: Response):
    return await forward(request, response, "POST", f"/api/support/tickets/{id}/reply", body, key)


class SupportChat(Body):
    message: str = Field(min_length=1, max_length=2000)
    request_id: str = Field(alias="requestId", pattern=r"^[a-zA-Z0-9_-]{16,64}$")


def conversation_public(state):
    # Model/tool diagnostics stay in server-owned state for evaluation; no credential or raw upstream response is published.
    return browser_ids({"enabled": True, "turns": [{key: turn.get(key) for key in ("requestId", "message", "status", "result")} for turn in state.get("turns", [])]})


@router.get("/orders/{id}/conversation")
async def support_conversation(id: Identifier, request: Request, response: Response):
    key, _, session = await _identity(request, response, authenticated=True)
    if not auth.settings.customer_support_agent_enabled:
        return {"enabled": False, "turns": []}
    order = await auth._java("GET", f"/api/orders/{id}", access_token=session["accessToken"])
    if order.get("id") != id:
        raise HTTPException(502, "订单记录不匹配，请刷新核对")
    return conversation_public(await _load(key + ":support:" + id) or {"turns": []})


@router.post("/orders/{id}/conversation")
async def support_chat(id: Identifier, body: SupportChat, request: Request, response: Response):
    import asyncio
    import time
    import uuid
    from ..customer_support import planner
    from ..customer_support.knowledge import retrieve_policy
    from ..customer_support.runtime import digest, execute_plan
    request_started = time.perf_counter()
    key, _, session = await _identity(request, response, authenticated=True)
    if not auth.settings.customer_support_agent_enabled:
        raise HTTPException(503, "客服对话尚未启用，可继续使用售后表单和工单")
    async def java(method, path, **kwargs):
        return await auth._java(method, path, access_token=session["accessToken"], **kwargs)
    # Verify ownership before reading any conversation, including retries and previous answers.
    order_read_started = time.perf_counter()
    order = await java("GET", f"/api/orders/{id}")
    order_read_duration = (time.perf_counter() - order_read_started) * 1000
    if order.get("id") != id:
        raise HTTPException(502, "订单记录不匹配，请刷新核对")
    chat_key = key + ":support:" + id
    order = dict(order)
    async with _lock(chat_key):
        state = await _load(chat_key) or {"turns": []}
        turn = next((row for row in state["turns"] if row["requestId"] == body.request_id), None)
        if turn and turn["message"] != body.message:
            raise HTTPException(409, "重试内容与原客服请求不一致")
        if turn and turn["status"] == "COMPLETED":
            return conversation_public(state)
        if turn is None:
            if len(state["turns"]) >= 100:
                raise HTTPException(409, "本订单对话已达记录上限，请使用工单继续沟通")
            turn = {"requestId": body.request_id, "message": body.message, "status": "RUNNING", "attempts": [], "plan": None}
            state["turns"].append(turn)
        history = []
        for row in state["turns"]:
            if row is turn: break
            history.append({"role": "user", "content": row["message"]})
            if row.get("result"): history.append({"role": "assistant", "content": row["result"]["answer"]})
        started = request_started
        run_id = str(uuid.uuid4())
        attempt = {"runId": run_id, "requestId": body.request_id, "orderId": id, "status": "STARTED", "modelReceipt": {"status": "POTENTIALLY_STARTED", "usage": None, "cost": None},
                   "toolReceipts": [{"toolCallId": str(uuid.uuid4()), "runId": run_id, "method": "GET", "path": f"/api/orders/{id}", "status": "SUCCEEDED", "responseSha256": digest(order), "durationMs": round(order_read_duration, 3)}], "firstContentMs": None}
        turn["attempts"].append(attempt); turn["status"] = "RUNNING"; turn["result"] = None
        # Persist intent before calling the model so an interrupted attempt is not silently counted as zero cost.
        await _save(chat_key, state)
        try:
            cases_started = time.perf_counter()
            case_read = {"toolCallId": str(uuid.uuid4()), "runId": run_id, "method": "GET", "path": f"/api/after-sales/orders/{id}", "status": "STARTED"}
            attempt['toolReceipts'].append(case_read)
            try:
                rows = await java('GET', f'/api/after-sales/orders/{id}')
                if not isinstance(rows, list) or any(row.get('orderId') != id for row in rows): raise ValueError('invalid case context')
                order['_supportCases'] = rows; order['_supportCasesAvailable'] = True
                case_read.update(status='SUCCEEDED', responseSha256=digest(rows))
            except Exception as exc:
                if isinstance(exc, HTTPException) and exc.status_code in {401, 403}: raise
                order['_supportCases'] = []; order['_supportCasesAvailable'] = False
                case_read.update(status='FAILED', errorType=type(exc).__name__)
            finally:
                case_read['durationMs'] = round((time.perf_counter() - cases_started) * 1000, 3)
            retrieval = retrieve_policy(body.message)
            attempt["retrieval"] = {"status": retrieval["status"], "policyVersion": retrieval["policyVersion"], "citationIds": [row["id"] for row in retrieval["citations"]], "resultSha256": digest(retrieval)}
            async with asyncio.timeout(75):
                if turn.get("plan") and not turn['plan'].get('case_number'):
                    plan = planner.SupportPlan.model_validate(turn["plan"])
                    attempt["modelReceipt"] = {"status": "REUSED_SAVED_PLAN", "usage": None, "cost": None}
                else:
                    plan, receipt = await planner.plan_turn(body.message, history, order, retrieval, run_id=run_id)
                    attempt["modelReceipt"] = receipt; turn["plan"] = plan.model_dump()
                    await _save(chat_key, state)
                turn["result"] = await execute_plan(plan, order=order, retrieval=retrieval, java=java, run_id=run_id, tool_receipts=attempt["toolReceipts"], question=body.message)
            turn["status"] = "COMPLETED"; attempt["status"] = "SUCCEEDED"
            # JSON response is non-streaming: first useful content is the completed supported answer, never a status heartbeat.
            attempt["firstContentMs"] = round((time.perf_counter() - started) * 1000, 3)
        except planner.PlanningFailure as exc:
            attempt["modelReceipt"] = exc.receipt; attempt["status"] = "FAILED"; turn["status"] = "FAILED"
            turn["result"] = {"answer": "本次客服理解未完成，请重试原消息或使用售后表单。", "kind": "error", "citations": [], "preview": None, "ticketDraft": None}
        except Exception as exc:
            attempt.update(status="FAILED", errorType=type(exc).__name__); turn["status"] = "FAILED"
            message = "当前缺少可确认的业务结果，请重试原消息或提交信息核实工单。"
            if isinstance(exc, HTTPException) and exc.status_code in {400, 404, 409, 422}:
                from ..customer_support.rejections import rejection_answer
                message = rejection_answer(exc.detail)
            turn["result"] = {"answer": message, "kind": "error", "citations": [], "preview": None, "ticketDraft": None}
            if isinstance(exc, HTTPException) and exc.status_code in {401, 403}:
                await _save(chat_key, state)
                raise
        finally:
            attempt["durationMs"] = round((time.perf_counter() - started) * 1000, 3)
        await _save(chat_key, state)
        return conversation_public(state)
