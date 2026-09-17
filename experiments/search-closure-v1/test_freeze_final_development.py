import pytest
from freeze_final_development import merge_rows

def row(did):return {'query_id':'q','document_id':did,'query':'original','document':{'title':did},'source':'fixture','query_cohort':'main','grade':'UNKNOWN'}

def test_append_preserves_base_and_unknown_grade():
    base=[row('old')];extra=[row('new')]
    result=merge_rows(base,extra,extra)
    assert result==[row('new'),row('old')]
    assert base==[row('old')]

@pytest.mark.parametrize('mutation',['overlap','missing','extra','document','duplicate'])
def test_rejects_changed_judgments_or_coverage(mutation):
    base=[row('old')];extra=[row('new')];candidates=[row('new')]
    if mutation=='overlap':extra=base;candidates=base
    elif mutation=='missing':extra=[]
    elif mutation=='extra':extra.append(row('unexpected'))
    elif mutation=='document':extra[0]['document']={'title':'fabricated'}
    else:extra+=extra
    with pytest.raises(ValueError):merge_rows(base,extra,candidates)
