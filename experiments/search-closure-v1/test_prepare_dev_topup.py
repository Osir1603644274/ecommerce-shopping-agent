import pytest
from prepare_dev_topup import topup_members,verify_training_cycle
from retrieval_runtime import write_once,sha

def test_complete_union_without_overwriting_existing_or_dropping_ties():
    queries={'q':{'source':'kuaisearch'}}
    ranks={'one':{'q':['old','new1','new2']},'two':{'q':['new2','new3','old']}}
    result=topup_members(queries,ranks,[{'query_id':'q','document_id':'old'}])
    assert set(result['q'])=={'new1','new2','new3'}
    assert result['q']['new2']==[{'method':'one','rank':3},{'method':'two','rank':1}]

def test_changed_query_denominator_and_duplicate_base_rejected():
    with pytest.raises(ValueError):topup_members({'q':{}},{'method':{}},[])
    with pytest.raises(ValueError):topup_members({'q':{}},{'m':{'q':[]}},[{'query_id':'q','document_id':'x'}]*2)

def test_training_branch_requires_bound_gate_and_exact_model_set(tmp_path):
    gate=tmp_path/'gate.json';decision=tmp_path/'decision.json'
    write_once(gate,{'status':'TRAINING_NOT_TRIGGERED','triggered':False})
    write_once(decision,{'status':'TRAINING_CYCLE_DECIDED','branch':'not_triggered','gate':{'path':str(gate),'sha256':sha(gate)}})
    models={n:{} for n in ('base','epoch1','epoch2','epoch3')}
    assert verify_training_cycle(decision,sha(decision),models)['branch']=='not_triggered'
    with pytest.raises(ValueError):verify_training_cycle(decision,sha(decision),{**models,'new_epoch1':{}})
    gate.write_text('{}')
    with pytest.raises(ValueError):verify_training_cycle(decision,sha(decision),models)
