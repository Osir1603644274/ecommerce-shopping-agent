from copy import deepcopy
import pytest
from agent_quality_review_v2 import restore_scores

def fixture():
    hit={'source':'kuaisearch','docid':'kuaisearch:1','text':'original product text','rank':1,'unknown':['price'],'score':2.51171875,'provenance':{'private_strategy':'hidden'}}
    answer={'label':'A','answer':'score 2.51','run_outcome':'SUCCEEDED','evidence':[{k:hit[k] for k in ('source','docid','text','rank','unknown')}]}
    return answer,hit

def test_original_numeric_evidence_restored_without_answer_change_or_unblinding():
    answer,hit=fixture();before=deepcopy(answer);restored=restore_scores(answer,[hit])
    assert answer==before and restored['answer']==answer['answer']
    assert restored['evidence'][0]['score']==2.51171875
    assert 'provenance' not in restored['evidence'][0]
    del restored['evidence'][0]['score'];assert restored==answer

@pytest.mark.parametrize('change',[{'docid':'kuaisearch:2'},{'text':'different actual evidence'},{'rank':2},{'score':float('nan')}])
def test_mismatched_actual_input_or_invalid_score_rejected(change):
    answer,hit=fixture();hit.update(change)
    with pytest.raises(ValueError):restore_scores(answer,[hit])

def test_duplicate_or_missing_original_evidence_cannot_be_silently_filled():
    answer,hit=fixture()
    with pytest.raises(ValueError):restore_scores(answer,[])
    answer['evidence']*=2
    with pytest.raises(ValueError):restore_scores(answer,[hit,hit])
