import pytest
import finalize_training_cycle as module

def test_stage_order_ignores_empty_output_directories(tmp_path,monkeypatch):
    monkeypatch.setattr(module,'ROOT',tmp_path)
    (tmp_path/'test-pool').mkdir()
    assert len(module.verify_stage_order()['checked_paths'])==3

@pytest.mark.parametrize('relative',['test-pool/cache/query.json','development-topup/candidate-rows.jsonl','final-selection/SELECTION.json'])
def test_stage_order_rejects_actual_later_artifacts(tmp_path,monkeypatch,relative):
    monkeypatch.setattr(module,'ROOT',tmp_path)
    p=tmp_path/relative;p.parent.mkdir(parents=True);p.write_text('{}')
    with pytest.raises(ValueError,match='Later-stage artifacts'):module.verify_stage_order()
