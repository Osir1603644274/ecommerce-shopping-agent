from types import SimpleNamespace
import pytest
import run_dev_latency as m
from retrieval_runtime import write_once,fingerprint

def test_latency_schedule_pairs_exact_queries_and_rotates_methods():
    queries=[{'query_id':str(i),'query':str(i),'source':'kuaisearch'} for i in range(4)]
    slots=m.schedule(queries,['one','two'])
    assert slots==m.schedule(list(reversed(queries)),['one','two'])
    assert len(slots)==8 and slots[0]['query']==slots[1]['query']
    assert slots[0]['method']!=slots[2]['method']

def test_reuse_real_receipts_without_extra_inference_and_keep_ambiguous(tmp_path):
    calls=[]
    runtime=SimpleNamespace(search_one=lambda *a,**kw:calls.append((a,kw)) or {'hits':[],'timings':{'fixture':True}})
    binding={'arms':{'m':{'profile':'w111','model_path':None}}}
    slots=[{'slot':1,'method':'m','query':{'query':'original','source':'kuaisearch'}}]
    first=m.run_slots(runtime,tmp_path,binding,slots)
    assert m.run_slots(runtime,tmp_path,binding,slots)==first and len(calls)==1
    slot={'slot':2,'method':'m','query':{'query':'next','source':'kuaisearch'}}
    write_once(tmp_path/'slots/0002/STARTED.json',{'binding_sha256':fingerprint(binding),'slot':slot})
    with pytest.raises(ValueError,match='Interrupted'):m.run_slots(runtime,tmp_path,binding,[slot])
    assert len(calls)==1

def test_latency_cannot_make_invalid_quality_selectable(monkeypatch):
    monkeypatch.setattr(m,'GRID_CE_METHODS',['a','b'])
    report={'methods':['a','b','c'],'common_conditional':{'main':{'methods':{
        'a':{'equal_source_mean_lower':.8},'b':{'equal_source_mean_lower':.8},'c':{'equal_source_mean_lower':.9}}}}}
    assert m.tied_methods(report)==['a','b']
    report['common_conditional']['main']['methods']['a']['equal_source_mean_lower']=None
    with pytest.raises(ValueError,match='denominators'):m.tied_methods(report)

def test_readonly_verification_never_recreates_missing_measurement(tmp_path,monkeypatch):
    monkeypatch.setattr(m,'write_once',lambda *a,**k:pytest.fail('verification wrote'))
    slot={'slot':1,'method':'m','query':{'query':'q','source':'kuaisearch'}}
    with pytest.raises(ValueError,match='read-only'):
        m.run_slots(None,tmp_path,{'arms':{}},[slot],verify_only=True)
