import json
from pathlib import Path
from types import SimpleNamespace

import pytest
import run_suite


def test_version_guard_rejects_modified_added_and_removed_files():
    baseline = {'source.py': 'original', 'model-config': 'one'}
    run_suite.assert_fixed(baseline, dict(baseline))
    for changed in ({'source.py': 'changed', 'model-config': 'one'},
                    {**baseline, 'new.py': 'new'}, {'source.py': 'original'}):
        with pytest.raises(RuntimeError, match='fixed-version contract changed'):
            run_suite.assert_fixed(baseline, changed)


def test_failed_child_keeps_full_denominator_and_stops_following_batches(tmp_path, monkeypatch):
    dataset = Path(__file__).resolve().parents[2] / 'docs/implementation/customer-support-20260919/evaluation/dataset-draft-003/scenarios.jsonl'
    calls = []
    def failed_child(command, **kwargs):
        calls.append(command)
        return SimpleNamespace(returncode=1)
    monkeypatch.setattr(run_suite.subprocess, 'run', failed_child)
    monkeypatch.setattr(run_suite, 'fingerprint', lambda *_: {'source': 'fixed'})
    args = SimpleNamespace(dataset=dataset, output=tmp_path/'output', runtime=tmp_path/'runtime', plan_only=False)
    with pytest.raises(RuntimeError, match='batch process failed'):
        run_suite.run(args)
    report = json.loads((args.output/'REPORT.json').read_text())
    state = json.loads((args.output/'STATE.json').read_text())
    assert len(calls) == 1
    assert report['quality']['denominator'] == report['quality']['missing'] == 720
    assert report['acceptance'] is False
    assert state['status'] == 'STOPPED_INCOMPLETE'
