import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from agent.evaluation.context_program_v2_20260904 import common, datasets


def test_immutable_output(tmp_path):
    target = tmp_path / "test.json"
    common.json_new(target, {"v": 1})
    with pytest.raises(FileExistsError):
        common.json_new(target, {"v": 2})


def test_data_count_and_family_disjoint():
    assert len(datasets.extraction_cases()) == 24
    dev, confirm = datasets.conversations("dev", 16), datasets.conversations("confirm", 64)
    assert len(dev) == 16 and len(confirm) == 64
    assert not ({r["familyId"].split(":")[1] for r in dev} & {r["familyId"].split(":")[1] for r in confirm})
    assert len({r["familyId"] for r in confirm}) == 4  # do not inflate independent N


def test_oracle_rejects_silent_loss_and_conflicting_duplicates():
    case = {"expectedHard": {"os": "ios"}, "absentKeys": ["price_minor"]}
    good = datasets.requirement("os", "eq", "ios", "enum")
    assert datasets.check_requirements({"requirements": [good]}, case) == []
    assert datasets.check_requirements({"requirements": []}, case)
    assert datasets.check_requirements({"requirements": [good, good]}, case)
    assert datasets.check_requirements({"requirements": [good, datasets.requirement("price_minor", "lte", 100, "CNY_MINOR")]}, case)


def test_client_preserves_budget_and_records_wire(tmp_path, monkeypatch):
    monkeypatch.setattr(common, "HERE", tmp_path)
    common.json_new(tmp_path / "p0/contract.json", {"deadlineAt": (datetime.now(timezone.utc)+timedelta(hours=1)).isoformat()})
    seen = []
    class Response:
        choices = [SimpleNamespace(finish_reason="tool_calls")]
        usage = SimpleNamespace(model_dump=lambda: {"total_tokens": 20})
        def model_dump(self): return {"content": "fixture"}
    async def create(**kwargs):
        seen.append(kwargs)
        return Response()
    provider = SimpleNamespace(base_url="https://api.deepseek.com/beta", max_retries=0,
        chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    client = common.RecordedClient(provider, phase="P2", output=tmp_path)
    asyncio.run(client.create(model="test", messages=[], max_tokens=4096))
    assert seen[0]["max_tokens"] == 4096
    assert seen[0]["extra_body"]["thinking"]["type"] == "disabled"
    events = common.rows(tmp_path / "provider_ledger.jsonl")
    assert [e["event"] for e in events] == ["START", "END"]
    assert client.usage_state()[1] == 20


def test_unknown_request_stays_charged_and_no_retry(tmp_path, monkeypatch):
    monkeypatch.setattr(common, "HERE", tmp_path)
    common.json_new(tmp_path / "p0/contract.json", {"deadlineAt": (datetime.now(timezone.utc)+timedelta(hours=1)).isoformat()})
    called = []
    async def create(**kwargs):
        called.append(kwargs)
        raise TimeoutError("fixture")
    provider = SimpleNamespace(base_url="https://api.deepseek.com/beta", max_retries=0,
        chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    client = common.RecordedClient(provider, phase="P2", output=tmp_path)
    with pytest.raises(TimeoutError): asyncio.run(client.create(model="test", messages=[], max_tokens=4096))
    assert len(called) == 1
    assert client.usage_state()[1] > 4096


def test_exhausted_phase_no_dispatch(tmp_path, monkeypatch):
    monkeypatch.setattr(common, "HERE", tmp_path)
    monkeypatch.setattr(common, "CAPS", {"P2": 0})
    common.json_new(tmp_path / "p0/contract.json", {"deadlineAt": (datetime.now(timezone.utc)+timedelta(hours=1)).isoformat()})
    client = common.RecordedClient(SimpleNamespace(), phase="P2", output=tmp_path)
    with pytest.raises(common.BudgetStop): asyncio.run(client.create(model="test", messages=[]))
    assert not (tmp_path / "provider_ledger.jsonl").exists()


def test_manifest_rejects_path_escape(tmp_path):
    (tmp_path / "sum.txt").write_text("a  ../outside\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="manifest_path_escape"):
        common.manifest_check(tmp_path / "sum.txt")
