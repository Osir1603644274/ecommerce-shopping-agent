from types import SimpleNamespace
import pytest
from remote_actions import drive_remote


def test_exhaustion_cannot_be_claimed_without_real_worker():
    driver=SimpleNamespace(inventory_fault=object(),inventory_config={'database':'isolated'},
        case={'id':'case'},config={},remote_pending={'action':'lose_inventory_ack','receiptId':None})
    with pytest.raises(RuntimeError,match='real receipt worker'):drive_remote(driver,'exhaust_receipt_retries')


def test_manual_retry_requires_recorded_exhausted_original_receipt():
    driver=SimpleNamespace(inventory_fault=object(),inventory_config={'database':'isolated'},
        case={'id':'case'},config={},remote_pending={'action':'lose_inventory_ack','receiptId':'receipt'})
    with pytest.raises(RuntimeError,match='not reached manual review'):drive_remote(driver,'manual_original_retry')
