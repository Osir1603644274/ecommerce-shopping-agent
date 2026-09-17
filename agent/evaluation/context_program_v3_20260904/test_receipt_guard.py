import asyncio
import pytest
from .common import BudgetStop
from .test_runner import client_at

def test_unregistered_stream_is_rejected_before_provider_or_charge(tmp_path,monkeypatch):
    client,provider=client_at(tmp_path,monkeypatch)
    with pytest.raises(BudgetStop,match='streaming_receipts_not_registered'):
        asyncio.run(client.create(model='fixture',messages=[],stream=True))
    provider.assert_not_awaited()
    assert not (tmp_path/'provider_ledger.jsonl').exists()
    assert not (tmp_path/'private_requests.jsonl').exists()
