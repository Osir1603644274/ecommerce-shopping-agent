import pytest
from managed_bff import ManagedBff


def test_isolated_runtime_authority_is_preserved_and_remote_origins_rejected():
    assert ManagedBff(authority='http://127.0.0.1:18081').authority=='http://127.0.0.1:18081'
    for origin in ('http://example.com:18081','http://127.0.0.1:18081/api','http://secret@127.0.0.1:18081','http://127.0.0.1'):
        with pytest.raises(ValueError):ManagedBff(authority=origin)

def test_managed_restart_rejects_changed_source_before_launch(tmp_path):
    app=tmp_path/'agent/app/customer_support';app.mkdir(parents=True)
    code=app/'planner.py';code.write_text('version=1\n')
    (app/'policy_v1.json').write_text('{}')
    bff=ManagedBff();bff.root=tmp_path;bff.source_hashes=bff.fingerprint()
    code.write_text('version=2\n')
    with pytest.raises(RuntimeError,match='source changed across'):bff.start()
    assert bff.process is None

def test_source_manifest_excludes_private_environment(tmp_path):
    app=tmp_path/'agent/app/customer_support';app.mkdir(parents=True)
    (app/'policy_v1.json').write_text('{}');(tmp_path/'agent/.env').write_text('KEY=not-a-real-secret')
    bff=ManagedBff();bff.root=tmp_path
    assert set(bff.fingerprint())=={'agent/app/customer_support/policy_v1.json'}
