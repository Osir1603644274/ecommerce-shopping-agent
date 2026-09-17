from types import SimpleNamespace
import pytest
from run_commerce_component import check_request,main_checks

@pytest.mark.parametrize('method,url,body',[
    ('POST','http://localhost:8080/api/orders',{}),
    ('GET','http://example.com:8080/api/products',None),
    ('GET','http://u:p@localhost:8080/api/products',None),
    ('POST','http://localhost:8080/api/products/resolve',{'productIds':['kuaisearch:1']}),
    ('POST','http://localhost:8080/api/products/resolve',{'productIds':[True]}),
])
def test_read_only_transport_boundary(method,url,body):
    with pytest.raises(ValueError):check_request(method,url,body,'http://localhost:8080')

def test_resolution_read_is_allowed_without_mutation_endpoint():
    check_request('POST','http://localhost:8080/api/products/resolve',{'productIds':[1,2]},'http://localhost:8080')

def test_active_claim_requires_real_inference_receipt_and_hard_checks():
    trace=SimpleNamespace(ok=True,detail={'candidatePoolIds':[1],'rankedItemIds':[1],
        'candidates':[{'checks':[{'priority':'hard','status':'fail'}]}],
        'retrievalTrace':{'syntheticPricePolicy':'disabled','crossEncoder':{'status':'active'}}})
    result=main_checks(trace,[],'a'*64,None)
    assert result['one_actual_model_call'] is False
    assert result['no_ranked_hard_constraint_failure'] is False
