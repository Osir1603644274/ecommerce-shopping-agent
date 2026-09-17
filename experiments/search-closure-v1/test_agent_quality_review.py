from copy import deepcopy
import pytest
import agent_quality_review as q

def sample():
    packet=[{'review_query_id':'q1','query':'杯子','answers':[
        {'label':'A','run_outcome':'SUCCEEDED','answer':'文档说陶瓷杯','evidence':[{'docid':'kuaisearch:1','text':'陶瓷杯'}]},
        {'label':'B','run_outcome':'SUCCEEDED','answer':'文档说玻璃杯','evidence':[{'docid':'kuaisearch:2','text':'玻璃杯'}]}]}]
    values=[{'review_query_id':'q1','label':label,'checks':{c:'PASS' for c in q.CHECKS},'useful_grade':2,
        'reason':'回答了杯子信息','findings':[]} for label in ['A','B']]
    return packet,values

def test_cannot_borrow_other_answer_evidence():
    packet,values=sample()
    values[0]['checks']['grounding']='FAIL'
    values[0]['findings']=[{'criterion':'grounding','answer_quote':'陶瓷杯','docid':'kuaisearch:2',
        'evidence_quote':'玻璃杯','explanation':'错误使用另一个回答的证据'}]
    with pytest.raises(ValueError,match='this answer original'):q.validate(values,packet)

def test_failures_need_real_quotes_not_freeform_flags():
    packet,values=sample();values[0]['checks']['grounding']='FAIL'
    with pytest.raises(ValueError,match='lacks actual finding'):q.validate(values,packet)
    values[0]['findings']=[{'criterion':'grounding','answer_quote':'编造的原文','docid':'','evidence_quote':'','explanation':'无证据'}]
    with pytest.raises(ValueError,match='actual answer'):q.validate(values,packet)

def test_failed_run_stays_unassessable_and_covered():
    packet,values=sample();packet[0]['answers'][0]['run_outcome']='FAILED'
    with pytest.raises(ValueError,match='promoted'):q.validate(values,packet)
    values[0].update(checks={c:'NOT_ASSESSABLE' for c in q.CHECKS},useful_grade='NOT_ASSESSABLE')
    assert len(q.validate(values,packet))==2
    with pytest.raises(ValueError,match='every supplied'):q.validate(values[1:],packet)

def paired(gain):
    return [{'query_id':s+str(i),'source':s,'arm':arm,'useful_grade':1+(gain if arm=='winner' else 0)}
        for s in ['kuaisearch','multicpr'] for i in range(10) for arm in ['baseline','winner']]

def test_bootstrap_full_pairing_no_deleting_unknown():
    assert q.paired_usefulness(paired(1))['bootstrap95']==[1,1]
    assert not q.paired_usefulness(paired(0))['eligible_for_activation']
    rows=paired(1);rows[0]['useful_grade']='UNKNOWN'
    assert q.paired_usefulness(rows)['status']=='INCOMPLETE_QUALITY_EVIDENCE'
    with pytest.raises(ValueError,match='Missing quality arm'):q.paired_usefulness(paired(1)[:-1])

def test_three_distinct_judgments_remain_unknown():
    assert q.majority([1,2,3])=='UNKNOWN'
    assert q.majority(['PASS','FAIL','UNKNOWN'])=='UNKNOWN'
    assert q.majority([2,2])==2
