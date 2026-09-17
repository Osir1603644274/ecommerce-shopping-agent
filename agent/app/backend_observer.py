"""Opt-in, bounded, owner-bound diagnostics. Never a business/audit authority.

Only explicitly instrumented Java calls are covered. Business inputs use a
field allowlist; no raw payloads, JWTs or background completion claims.
"""
from collections import OrderedDict
import asyncio
from contextvars import ContextVar
from dataclasses import dataclass, field
import hashlib
import re
import secrets
import time
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Request, Response
from starlette.datastructures import MutableHeaders
from .settings import settings


@dataclass
class Collection:
    calls: list = field(default_factory=list)
    closed: bool = False


_current: ContextVar[Collection | None] = ContextVar("backend_observer", default=None)
_tickets: OrderedDict = OrderedDict()
router = APIRouter(prefix="/api/commerce-demo/backend-traces")


def enabled():
    return settings.backend_observer_enabled and 32 <= len(settings.backend_observer_key) <= 256


def observer_headers():
    value = _current.get()
    return {"X-Backend-Observer-Key": settings.backend_observer_key} if enabled() and value and not value.closed else {}


def begin_call(body=None, params=None):
    from .execution_view import safe_value
    collection = _current.get()
    if not collection or collection.closed:
        return None
    return {'startedAt': datetime.now(timezone.utc).isoformat(), '_clock': time.perf_counter(),
            'input': {'body': safe_value(body or {}), 'query': safe_value(params or {})}}


def observe_response(response, method, path, timing=None):
    value = _current.get()
    if value is None or value.closed or len(value.calls) >= 30:
        return
    trace = response.headers.get("X-Java-Trace-Id", "") if response is not None else ''
    safe_path = re.sub(r"/[0-9a-fA-F-]{8,}(?=/|$)|/\d+(?=/|$)", "/:id", path.split("?")[0])
    item = {"method": method, "path": safe_path[:160], "status": response.status_code if response is not None else 0,
            "traceId": trace if re.fullmatch(r"[0-9a-f-]{36}", trace) else None}
    instance=response.headers.get('X-Service-Instance','') if response is not None else ''
    if instance in settings.backend_observer_instances:item['serviceInstance']=instance
    if timing:
        item.update(startedAt=timing['startedAt'], finishedAt=datetime.now(timezone.utc).isoformat(),
                    durationMs=round((time.perf_counter()-timing['_clock'])*1000, 2), input=timing['input'])
    value.calls.append(item)


def _owner(request):
    cookie = request.cookies.get("commerce_demo_session", "")
    return hashlib.sha256(cookie.encode()).hexdigest() if 1 <= len(cookie) <= 128 else None


def browser_trace(value, key=''):
    """Do not round Java Long identifiers when JSON is parsed by browsers."""
    if isinstance(value, dict):
        return {k: browser_trace(v, k) for k, v in value.items()}
    if isinstance(value, list):
        return [browser_trace(v, key) for v in value]
    if isinstance(value, int) and not isinstance(value, bool) and (key == 'id' or key.endswith(('Id', 'Ids')) or abs(value) > 9007199254740991):
        return str(value)
    return value


def _save(owner, collection):
    now = time.monotonic()
    for key in list(_tickets):
        if _tickets[key][0] <= now:
            del _tickets[key]
    ticket = secrets.token_hex(24)
    _tickets[ticket] = (now + 900, owner, list(collection.calls))
    while len(_tickets) > 500:
        _tickets.popitem(last=False)
    return ticket


class BackendObserverMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        request = Request(scope)
        owner = _owner(request)
        if not (enabled() and owner and request.headers.get("x-backend-observe") == "1"
                and request.url.path.startswith("/api/commerce-demo/")
                and not request.url.path.startswith("/api/commerce-demo/backend-traces")):
            return await self.app(scope, receive, send)
        collection = Collection()
        token = _current.set(collection)

        async def traced_send(message):
            if message["type"] == "http.response.start":
                collection.closed = True
                # Observability storage failure must never fail the business response.
                try:
                    ticket = _save(owner, collection)
                    MutableHeaders(scope=message)["X-Backend-Trace-Ticket"] = ticket
                except Exception:
                    pass
            await send(message)
        try:
            await self.app(scope, receive, traced_send)
        finally:
            collection.closed = True
            _current.reset(token)


@router.get("/{ticket}")
async def read_trace(ticket: str, request: Request, response: Response):
    from .api import commerce_demo as auth
    if not enabled():
        raise HTTPException(404, "backend observer disabled")
    auth._require_same_origin(request)
    session = await auth._load_session(request.cookies.get(auth.COOKIE_NAME))
    auth._require_csrf(session, request.headers.get(auth.CSRF_HEADER))
    response.headers["Cache-Control"] = "no-store"
    entry = _tickets.get(ticket)
    if not entry or entry[0] <= time.monotonic() or entry[1] != _owner(request):
        raise HTTPException(404, "trace unavailable or expired")
    import httpx
    semaphore = asyncio.Semaphore(6)
    async with httpx.AsyncClient(base_url=settings.backend_base_url, timeout=1.0,trust_env=False,follow_redirects=False) as client:
        async def read_call(call):
            item = dict(call, detail=None)
            if call["traceId"]:
                try:
                    async with semaphore:
                        # A trace lives on its issuing JVM. Never follow a URL from a response header.
                        origin=settings.backend_observer_instances.get(call.get('serviceInstance'),'').rstrip('/')
                        result = await client.get(origin+"/api/diagnostics/backend-traces/" + call["traceId"],
                                                  headers={"X-Backend-Observer-Key": settings.backend_observer_key})
                    if result.status_code == 200:
                        item["detail"] = browser_trace(result.json().get("data"))
                except (httpx.HTTPError, ValueError, AttributeError):
                    pass
            return item
        calls = browser_trace(await asyncio.gather(*(read_call(call) for call in entry[2])))
    return {"calls": calls, "scope": "最多记录 30 次同步 Java 调用；不含后台消费者、未接入的客户端调用及进程内部完整调用栈。记录保留最多 15 分钟，重启丢失。"}
