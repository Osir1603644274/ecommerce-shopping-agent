import pytest
import authorize_final_test as a

def test_unfinished_selection_rejected_before_any_model_or_query_read(monkeypatch):
    monkeypatch.setattr(a,'model_binding',lambda _:pytest.fail('Invalid selection loaded model'))
    with pytest.raises(ValueError,match='actual final'):a.validate_selection({'status':'TRAINING_TRIGGERED'})

def test_wrong_selection_path_never_reads_any_file(tmp_path,monkeypatch):
    monkeypatch.setattr(a,'read_json',lambda _:pytest.fail('Wrong selection path opened file'))
    with pytest.raises(ValueError,match='actual final selection'):a.authorize(tmp_path/'SELECTION.json','a'*64)

def test_final_arm_model_bytes_must_match(monkeypatch):
    monkeypatch.setattr(a,'model_binding',lambda _:{'actual':'binding'})
    spec={'method':'w111/epoch1','profile':'w111','model_path':'fixture','model_binding_sha256':'0'*64}
    with pytest.raises(ValueError,match='selected model changed'):
        a.validate_selection({'status':'FINAL_SEARCH_SELECTION_FROZEN','seed':20260909,'new_test_accessed':False,
            'inputs':[{}],'selected':spec,'strongest_old':spec})
