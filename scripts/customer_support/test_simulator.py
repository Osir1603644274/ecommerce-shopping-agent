import importlib.util
import json
from pathlib import Path
import re
import sys

import httpx
import pytest
from fastapi.testclient import TestClient

spec = importlib.util.spec_from_file_location("support_simulator", Path(__file__).with_name("simulator.py"))
sim = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = sim
spec.loader.exec_module(sim)


def test_authority_is_loopback_only_and_unknown_actions_cannot_proxy():
    for url in ["https://example.com", "http://example.com", "http://localhost@evil.test", "http://127.0.0.1/api", "http://127.0.0.1?next=evil"]:
        with pytest.raises(ValueError):
            sim.SimulatorClient(url, "secret")
    observed = []
    def handle(request):
        observed.append(request)
        return httpx.Response(200, json={"success": True, "data": {"id": "receipt-1"}})
    client = sim.SimulatorClient("http://127.0.0.1:8080", "secret", httpx.MockTransport(handle))
    assert client.execute(sim.Command(action="refund-success", id="case-1", key="operation-1")) == {"id": "receipt-1"}
    assert observed[0].headers["authorization"] == "Bearer secret"
    assert observed[0].headers["idempotency-key"] == "operation-1"
    assert observed[0].url.path == "/api/admin/support-simulator/cases/case-1/refund-success"
    with pytest.raises(ValueError):
        client.execute(sim.Command(action="/api/orders", id="case-1", key="operation-1"))
    assert len(observed) == 1
    client.close()


def test_console_rejects_foreign_origin_and_never_returns_admin_token():
    class Client:
        calls = 0
        def execute(self, command):
            self.calls += 1
            return {"itemId": 9007199254740993, "phase": "AWAITING_REVIEW"}
    upstream = Client()
    with TestClient(sim.create_app(upstream), base_url="http://127.0.0.1:19093") as client:
        html = client.get("/")
        csrf = re.search(r"csrf='([^']+)'", html.text).group(1)
        assert html.headers["x-frame-options"] == "DENY"
        command = {"action": "case-state", "id": "case-1", "key": "operation-1"}
        assert client.post("/action", json=command).status_code == 403
        assert client.post("/action", json=command, headers={"Origin": "http://evil.test", "X-Simulator-CSRF": csrf}).status_code == 403
        assert client.get("/", headers={"Host": "evil.test"}).status_code == 403
        good = client.post("/action", json=command, headers={"Origin": "http://127.0.0.1:19093", "X-Simulator-CSRF": csrf})
        assert good.json()["data"]["itemId"] == "9007199254740993"
        assert upstream.calls == 1


def test_recipe_resumes_lost_response_with_saved_version_and_key(tmp_path):
    class Client:
        def __init__(self):
            self.commands = []
            self.lost = True
        def execute(self, command):
            self.commands.append(command)
            if command.action == "case-state":
                return {"case": {"version": 7, "type": "REFUND_ONLY", "phase": "AWAITING_REVIEW"}}
            if command.action == "receipt" and self.lost:
                self.lost = False
                raise httpx.ReadTimeout("committed, response lost")
            return {"id": "receipt-1"}
    client = Client(); journal = tmp_path / "run.sqlite3"
    params = {"case_id": "case-1", "order_id": "", "tracking": "SIM-1", "sellable": False}
    with pytest.raises(httpx.ReadTimeout):
        sim.run_recipe(client, "refund-only", params, journal)
    result = sim.run_recipe(client, "refund-only", params, journal)
    reviews = [command for command in client.commands if command.action == "receipt"]
    assert len(reviews) == 2 and reviews[0] == reviews[1]
    assert reviews[0].body["expectedVersion"] == 7
    assert "settled" in result
    assert len([command for command in client.commands if command.action == "case-state"]) == 1
    count = len(client.commands)
    assert sim.run_recipe(client, "refund-only", params, journal) == result
    assert len(client.commands) == count
    with pytest.raises(ValueError, match="different recipe"):
        sim.run_recipe(client, "refund-only", {**params, "case_id": "other"}, journal)


def test_mismatched_phase_stops_before_receipt_creation(tmp_path):
    class Client:
        def execute(self, command):
            assert command.action == "case-state"
            return {"case": {"version": 0, "type": "REFUND_ONLY", "phase": "COMPLETED"}}
    with pytest.raises(RuntimeError, match="expected AWAITING_REVIEW"):
        sim.run_recipe(Client(), "refund-only", {"case_id": "case-1"}, tmp_path / "run.sqlite3")


def test_fixed_recipes_do_not_include_customer_confirmation_or_arbitrary_commands():
    scripts = sim.recipes()
    assert set(scripts) == {"original-delivery", "refund-only", "return-refund", "exchange"}
    for steps in scripts.values():
        assert len({step["name"] for step in steps}) == len(steps)
        assert all(step["action"] in sim.ACTIONS for step in steps)
        assert not any(step["action"] in {"confirm", "conversion-confirm", "return-shipment"} for step in steps)
    encoded = json.dumps(scripts["exchange"])
    assert 'REPLACEMENT_READY' in encoded
    for name in ('return-refund', 'exchange'):
        step = next(step for step in scripts[name] if step['name'] == 'inspection')
        body = sim.resolve(step['body'], {'sellable': False}, {'inspection_state': {'case': {'version': 3, 'itemId': 9007199254740993, 'quantity': 2}}})
        assert body['itemId'] == 9007199254740993 and body['quantity'] == 2 and body['sellable'] is False
