from types import SimpleNamespace
import pytest
import prepare_verified_training as module
from retrieval_runtime import write_once,sha,read_json

def fixture(tmp_path,monkeypatch):
    monkeypatch.setattr(module,'ROOT',tmp_path)
    selection=tmp_path/'selection/FROZEN.json'
    write_once(selection,{'files':{'train.queries.jsonl':'train-sha'}})
    args=SimpleNamespace(workspace=tmp_path/'training-review',gate=tmp_path/'gate.json',
        gate_sha256='gate-sha',selection_sha256=sha(selection),output_name='pairwise-v1')
    return args

def test_rejected_author_chain_never_prepares_training(tmp_path,monkeypatch):
    args=fixture(tmp_path,monkeypatch)
    calls=[]
    def reject(*a,**kw):raise ValueError('author output changed')
    monkeypatch.setattr(module.review_workspace,'freeze',reject)
    monkeypatch.setattr(module.train_pairwise,'prepare',lambda a:calls.append(a))
    with pytest.raises(ValueError,match='author output changed'):module.prepare(args)
    assert calls==[]
    assert not (tmp_path/'training-preparation').exists()

def test_verified_chain_binds_exact_prepared_manifest(tmp_path,monkeypatch):
    args=fixture(tmp_path,monkeypatch)
    order=[]
    def freeze(root,**kw):
        order.append('verify')
        assert kw['selected_train_sha256']=='train-sha'
        write_once(root/'frozen/qrels.jsonl',[],jsonl=True)
        record={'qrels_sha256':sha(root/'frozen/qrels.jsonl')}
        write_once(root/'frozen/MANIFEST.json',record)
        return record
    def prepare(inner):
        order.append('prepare')
        assert order==['verify','prepare']
        assert inner.annotation_sha256==sha(inner.annotation_manifest)
        record={'status':'READY','valid_query_count':51,'pair_count':1600}
        write_once(tmp_path/'training-preparation/prepared/pairwise-v1/MANIFEST.json',record)
        return record
    monkeypatch.setattr(module.review_workspace,'freeze',freeze)
    monkeypatch.setattr(module.train_pairwise,'prepare',prepare)
    assert module.prepare(args)['valid_query_count']==51
    out=tmp_path/'training-preparation/prepared/pairwise-v1'
    proof=read_json(out/'AUTHOR_CHAIN_VERIFIED.json')
    assert proof['prepared_manifest_sha256']==sha(out/'MANIFEST.json')
    assert proof['semantic_judgments_generated_by_wrapper']==0

def test_changed_query_selection_precedes_review_or_training(tmp_path,monkeypatch):
    args=fixture(tmp_path,monkeypatch);args.selection_sha256='changed'
    monkeypatch.setattr(module.review_workspace,'freeze',lambda *a,**k:pytest.fail('read labels'))
    with pytest.raises(ValueError,match='seal changed'):module.prepare(args)
