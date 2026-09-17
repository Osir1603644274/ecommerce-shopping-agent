import pytest
from reconcile_development import vote_grade

@pytest.mark.parametrize('a,b,t,result',[(3,3,None,(3,'two_contexts_agree')),
    ('UNKNOWN','UNKNOWN',None,('UNKNOWN','two_contexts_agree')),
    (0,2,2,(2,'two_of_three_majority')),(0,'UNKNOWN','UNKNOWN',('UNKNOWN','two_of_three_majority')),
    (0,2,3,('UNKNOWN','review_no_majority'))])
def test_resolution(a,b,t,result):assert vote_grade(a,b,t)==result

def test_cannot_resolve_without_third():
    with pytest.raises(ValueError):vote_grade(1,2)
