import pytest

from evaluation.post_training_task_state_v1_20260904.completion_audit_v2 import (
    normalize_raw, strict_object,
)


@pytest.mark.parametrize("raw", [
    '{"status":"ready","status":"executing"}',
    '{"nested":{"a":1,"a":2}}',
    '{"value":NaN}', '{"value":Infinity}', '[]',
    '```json\n{"status":"ready"}\n```',
])
def test_strict_json_rejects_ambiguous_or_non_object_output(raw):
    assert strict_object(raw) is None


def test_raw_normalization_never_corrects_contract_semantics():
    assert strict_object(' {"status":"ready"} ') == {"status": "ready"}
    assert normalize_raw({"status": "ready", "addUnknowns": []}) == {"status": "ready"}
    assert normalize_raw({"status": "READY"}) != normalize_raw({"status": "ready"})
    assert normalize_raw({"value": "5000"}) != normalize_raw({"value": 5000})
    assert normalize_raw({"values": [1, 2]}) == normalize_raw({"values": [2, 1]})
