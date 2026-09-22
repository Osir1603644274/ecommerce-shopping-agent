"""Independent loopback simulator console and resumable fixed scripts. No model tools."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
from typing import Any
from urllib.parse import urlsplit

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, Field

IDENTIFIER = re.compile(r"^[a-zA-Z0-9_-]{1,80}$")
ACTIONS = {
    "case-state": ("GET", "/cases/{id}"),
    "receipt": ("POST", "/cases/{id}/receipts"),
    "receipt-apply": ("POST", "/receipts/{id}/apply"),
    "process": ("POST", "/cases/{id}/process"),
    "inventory-retry": ("POST", "/cases/{id}/inventory-retry"),
    "order-inventory-retry": ("POST", "/orders/{id}/inventory-retry"),
    "resume-review": ("POST", "/cases/{id}/resume-review"),
    "refund-success": ("POST", "/cases/{id}/refund-success"),
    "replacement-dispatch": ("POST", "/cases/{id}/replacement-dispatch"),
    "replacement-received": ("POST", "/cases/{id}/replacement-received"),
    "clock-bind": ("POST", "/orders/{id}/clock"),
    "clock-advance": ("POST", "/orders/{id}/clock/advance"),
    "order-dispatch": ("POST", "/orders/{id}/dispatch"),
    "order-received": ("POST", "/orders/{id}/received"),
    "order-apply": ("POST", "/order-receipts/{id}/apply"),
    "ticket-action": ("POST", "/tickets/{id}/actions"),
}


class Command(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: str
    id: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,80}$")
    key: str = Field(min_length=8, max_length=128, pattern=r"^[a-zA-Z0-9_-]+$")
    body: dict[str, Any] = Field(default_factory=dict)


class SimulatorClient:
    def __init__(self, base_url: str, token: str, transport=None):
        parsed = urlsplit(base_url)
        if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"} or parsed.username or parsed.password or parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
            raise ValueError("Simulator authority must be an explicit loopback HTTP origin")
        if not token or "\n" in token or "\r" in token:
            raise ValueError("SUPPORT_SIMULATOR_ADMIN_TOKEN is required")
        self.http = httpx.Client(base_url=base_url.rstrip("/"), headers={"Authorization": "Bearer " + token},
                                 timeout=30, follow_redirects=False, trust_env=False, transport=transport)

    def execute(self, command: Command):
        if command.action not in ACTIONS:
            raise ValueError("Unknown simulator action")
        method, path = ACTIONS[command.action]
        response = self.http.request(method, "/api/admin/support-simulator" + path.format(id=command.id),
                                     headers={"Idempotency-Key": command.key}, json=command.body if method == "POST" else None)
        try:
            result = response.json()
        except ValueError:
            raise RuntimeError(f"Authority returned invalid JSON ({response.status_code})") from None
        if not response.is_success or not isinstance(result, dict) or result.get("success") is not True:
            message = result.get("message", "request rejected") if isinstance(result, dict) else "request rejected"
            raise RuntimeError(f"Authority rejected operation ({response.status_code}): {message}")
        return result["data"]

    def close(self):
        self.http.close()


def create_app(client: SimulatorClient, port: int = 19093):
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    nonce = secrets.token_urlsafe(32)
    hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
    origins = {"http://" + host for host in hosts}

    @app.middleware("http")
    async def local_boundary(request: Request, call_next):
        if request.headers.get("host") not in hosts:
            return JSONResponse({"detail": "loopback host required"}, status_code=403)
        if request.method != "GET" and (request.headers.get("origin") not in origins or not secrets.compare_digest(request.headers.get("x-simulator-csrf", ""), nonce)):
            return JSONResponse({"detail": "console origin and CSRF required"}, status_code=403)
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'nonce-" + nonce + "'; style-src 'unsafe-inline'; frame-ancestors 'none'; connect-src 'self'"
        return response

    @app.get("/", response_class=HTMLResponse)
    def index():
        return Path(__file__).with_name("console.html").read_text(encoding="utf-8").replace("__NONCE__", nonce)

    @app.post("/action")
    def action(command: Command):
        try:
            return {"data": browser_safe(client.execute(command))}
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None
        except (RuntimeError, httpx.HTTPError) as exc:
            # Do not expose request headers, tokens, or transport representations.
            raise HTTPException(502, str(exc) if isinstance(exc, RuntimeError) else "Authority connection failed; retry with the original key") from None

    return app


def browser_safe(value):
    if isinstance(value, dict):
        return {key: browser_safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [browser_safe(item) for item in value]
    if type(value) is int and abs(value) > 9007199254740991:
        return str(value)
    return value


def ref(name: str):
    return {"ref": name}


def param(name: str):
    return {"param": name}


def recipes():
    def step(name, action, target, body=None, expected=None):
        return {"name": name, "action": action, "id": target, "body": body or {}, "expected": expected or {}}
    case, order = param("case_id"), param("order_id")
    state = lambda name, phase, kind: step(name, "case-state", case, expected={"case.phase": phase, "case.type": kind})
    receipt = lambda name, event, state_name, extra={}: step(name, "receipt", case, {
        "event": event, "expectedVersion": ref(state_name + ".case.version"), "reason": "固定模拟脚本独立事件", **extra})
    apply = lambda name, receipt_name: step(name, "receipt-apply", ref(receipt_name + ".id"))
    finish_refund = [step("channel", "refund-success", case), apply("settled", "channel")]
    result = {
        "original-delivery": [step("clock", "clock-bind", order), step("dispatch", "order-dispatch", order, {"trackingNo": param("tracking")}),
                              step("dispatched", "order-apply", ref("dispatch.id")), step("signature", "order-received", order, {"trackingNo": param("tracking")}),
                              step("signed", "order-apply", ref("signature.id"))],
        "refund-only": [state("initial", "AWAITING_REVIEW", "REFUND_ONLY"), receipt("review", "APPROVE", "initial"), apply("approved", "review"), *finish_refund],
    }
    for name, kind in [("return-refund", "RETURN_REFUND"), ("exchange", "EXCHANGE")]:
        prefix = [state("initial", "RETURN_IN_TRANSIT", kind), receipt("warehouse", "RETURN_RECEIVED", "initial", {
            "itemId": ref("initial.case.itemId"), "quantity": ref("initial.case.quantity")}), apply("arrived", "warehouse"),
            state("inspection_state", "AWAITING_INSPECTION", kind), receipt("inspection", "INSPECTION_ACCEPTED", "inspection_state", {"sellable": param("sellable"),
                "itemId": ref("inspection_state.case.itemId"), "quantity": ref("inspection_state.case.quantity")}),
            apply("inspected", "inspection"), step("inventory", "process", case)]
        if kind == "RETURN_REFUND":
            result[name] = prefix + finish_refund
        else:
            result[name] = prefix + [state("ready", "REPLACEMENT_READY", kind), step("dispatch", "replacement-dispatch", case, {"trackingNo": param("tracking")}),
                                     apply("shipped", "dispatch"), step("signature", "replacement-received", case, {"trackingNo": param("tracking")}), apply("completed", "signature")]
    return result


def resolve(value, params, outputs):
    if isinstance(value, dict):
        if set(value) == {"param"}:
            return params[value["param"]]
        if set(value) == {"ref"}:
            parts = value["ref"].split(".")
            result = outputs[parts[0]]
            for part in parts[1:]:
                result = result[part]
            return result
        return {key: resolve(item, params, outputs) for key, item in value.items()}
    return value


@contextmanager
def exclusive_run(path: Path):
    """OS lock is released on process death; never leaves a logical stale lease."""
    with path.open("a+b") as handle:
        handle.seek(0); handle.write(b"0"); handle.flush(); handle.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def run_recipe(client: SimulatorClient, recipe_name: str, params: dict, journal: Path):
    steps = recipes()[recipe_name]
    fingerprint = hashlib.sha256(json.dumps([steps, params], sort_keys=True).encode()).hexdigest()
    journal.parent.mkdir(parents=True, exist_ok=True)
    with exclusive_run(journal.with_suffix(journal.suffix + ".lock")), sqlite3.connect(journal) as db:
        db.execute("PRAGMA synchronous=FULL")
        db.execute("CREATE TABLE IF NOT EXISTS run (fingerprint TEXT NOT NULL)")
        db.execute("CREATE TABLE IF NOT EXISTS step (name TEXT PRIMARY KEY, request TEXT NOT NULL, result TEXT)")
        prior = db.execute("SELECT fingerprint FROM run").fetchone()
        if prior and prior[0] != fingerprint:
            raise ValueError("Journal belongs to a different recipe or parameters; use a new journal")
        if not prior:
            db.execute("INSERT INTO run VALUES (?)", (fingerprint,)); db.commit()
        outputs = {}
        for step in steps:
            stored = db.execute("SELECT request,result FROM step WHERE name=?", (step["name"],)).fetchone()
            if stored:
                command = Command.model_validate_json(stored[0])
            else:
                command = Command(action=step["action"], id=resolve(step["id"], params, outputs), body=resolve(step["body"], params, outputs),
                                  key="script-" + hashlib.sha256((fingerprint + str(journal.resolve()) + step["name"]).encode()).hexdigest())
                db.execute("INSERT INTO step(name,request) VALUES (?,?)", (step["name"], command.model_dump_json())); db.commit()
            result = json.loads(stored[1]) if stored and stored[1] is not None else client.execute(command)
            for field, expected in step["expected"].items():
                actual = result
                for part in field.split("."):
                    actual = actual[part]
                if actual != expected:
                    raise RuntimeError(f"Script stopped at {step['name']}: {field} is {actual}, expected {expected}")
            db.execute("UPDATE step SET result=? WHERE name=?", (json.dumps(result, ensure_ascii=False), step["name"])); db.commit()
            outputs[step["name"]] = result
        return outputs


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=["serve", "run"])
    parser.add_argument("--authority", default="http://127.0.0.1:8080")
    parser.add_argument("--port", type=int, default=19093)
    parser.add_argument("--recipe", choices=list(recipes()))
    parser.add_argument("--case-id", default="")
    parser.add_argument("--order-id", default="")
    parser.add_argument("--tracking", default="SIM-SUPPORT-001")
    parser.add_argument("--sellable", action="store_true", help="Explicitly mark accepted returned goods as sellable; default is quarantine")
    parser.add_argument("--journal", type=Path)
    args = parser.parse_args()
    client = SimulatorClient(args.authority, os.environ.get("SUPPORT_SIMULATOR_ADMIN_TOKEN", ""))
    try:
        if args.mode == "serve":
            import uvicorn
            uvicorn.run(create_app(client, args.port), host="127.0.0.1", port=args.port, access_log=False)
        else:
            if not args.recipe or not args.journal:
                parser.error("run requires --recipe and --journal")
            outputs = run_recipe(client, args.recipe, {"case_id": args.case_id, "order_id": args.order_id, "tracking": args.tracking, "sellable": args.sellable}, args.journal)
            print(json.dumps(outputs, ensure_ascii=False, indent=2))
    finally:
        client.close()


if __name__ == "__main__":
    main()
