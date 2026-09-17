import pytest
import run_final_test_pool as testpool
from test_run_training_pool import FakeRuntime,channels_for
from retrieval_runtime import weighted_rrf,sha,read_json

def test_all_registered_top10_retained_and_fill80():
    channels=channels_for('kuaisearch');pool=weighted_rrf(channels,(1,1,1))
    arms={'baseline':pool,'winner':list(reversed(pool))}
    result=testpool.choose_test_candidates(channels,arms)
    required={r['document_id'] for records in [*channels.values(),*arms.values()] for r in records[:10]}
    assert len(result)==80 and required<={r['document_id'] for r in result}

def test_same_two_arms_score_once_per_query_and_resume_without_calls(tmp_path,monkeypatch):
    runtime=FakeRuntime();queries=[]
    for source in testpool.SOURCES:
        for i in range(40):queries.append({'query_id':f'closure-{source[:2]}-test-{i:03}','query':f'fixture {source} {i}','source':source,'query_cohort':'main'})
    binding={'arms':{a:{'profile':'w111','model':runtime.base} for a in ['baseline','winner']},
        'contracts':{'path':'fixture-only','sha256':'fixture'},'fixture_only':True}
    result=testpool.execute(runtime,tmp_path,queries,binding)
    assert result['candidate_count']==6400 and len(runtime.scores)==80
    assert runtime.recalls==[('kuaisearch',40),('multicpr',40)]
    digest=sha(tmp_path/'POOL_COMPLETE.json')
    monkeypatch.setattr(runtime,'retrieve_batch',lambda *a:pytest.fail('Repeated test recall'))
    monkeypatch.setattr(runtime,'score_pairs',lambda *a:pytest.fail('Repeated test CE'))
    assert testpool.execute(runtime,tmp_path,queries,binding)==result
    assert sha(tmp_path/'POOL_COMPLETE.json')==digest
    assert result['quality_comparison_arms']==['baseline','winner']
    monkeypatch.setattr(testpool,'write_once',lambda *a,**k:pytest.fail('CPU verification wrote output'))
    monkeypatch.setattr(testpool,'save_cached',lambda *a,**k:pytest.fail('CPU verification wrote cache'))
    assert testpool.execute(runtime,tmp_path,queries,binding,cache_only=True)==result
    candidate=tmp_path/'candidate-rows.jsonl'
    candidate.write_text(candidate.read_text(encoding='utf-8').replace('raw title 0','forged title 0'),encoding='utf-8')
    with pytest.raises(ValueError,match='CPU replay'):testpool.execute(runtime,tmp_path,queries,binding,cache_only=True)

def test_partial_heldout_set_cannot_initialize_retrieval(tmp_path):
    runtime=FakeRuntime()
    with pytest.raises(ValueError,match='fixed80'):testpool.execute(runtime,tmp_path,[],{'arms':{}})
    assert not runtime.recalls and not runtime.scores
