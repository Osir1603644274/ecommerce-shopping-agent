import copy
import pytest
from reviews import validate_judgments
from reviews import find_output

def test_mirrored_outputs_must_be_byte_identical(tmp_path):
    output=tmp_path/'outputs';output.mkdir()
    (tmp_path/'judgments.jsonl').write_bytes(b'original')
    (output/'judgments.jsonl').write_bytes(b'original')
    assert find_output(output,'judgments.jsonl') == output/'judgments.jsonl'
    (output/'judgments.jsonl').write_bytes(b'changed')
    with pytest.raises(ValueError): find_output(output,'judgments.jsonl')

CONTRACT=[{'query_id':'q','required_attribute_keys':['本体','品牌'],'intent_policy':'score_product_relevance'}]
PAIRS=[{'pair_id':'p','query_id':'q','query':'甲牌洗面奶','document':{'title':'男士洗面奶','brand':'','categories':['洁面'],'seller_name':''}}]
VALID={'pair_id':'p','grade':2,'core_relation':'same','attributes':{'本体':'supported','品牌':'missing'},
       'evidence':[{'field':'title','quote':'男士洗面奶'}],'reason':'洗面奶本体支持，品牌缺证','unknown_reason':None}

def test_partial_with_missing_is_valid():
    assert validate_judgments([VALID],PAIRS,CONTRACT)['pairs']==1

@pytest.mark.parametrize('change',[{'grade':True},{'grade':3},{'attributes':{'本体':'supported'}},
    {'evidence':[{'field':'title','quote':'甲牌男士洗面奶'}]},{'pair_id':'other'},
    {'unknown_reason':'conflicting_evidence'},{'grade':2,'attributes':{'本体':'supported','品牌':'contradicted'}}])
def test_rejects_inconsistent_or_fabricated_output(change):
    row=copy.deepcopy(VALID);row.update(change)
    with pytest.raises(ValueError):validate_judgments([row],PAIRS,CONTRACT)

def test_missing_duplicate_and_ambiguous():
    with pytest.raises(ValueError):validate_judgments([],PAIRS,CONTRACT)
    with pytest.raises(ValueError):validate_judgments([VALID,VALID],PAIRS,CONTRACT)
    contract=copy.deepcopy(CONTRACT);contract[0]['intent_policy']='query_intent_ambiguous'
    with pytest.raises(ValueError):validate_judgments([VALID],PAIRS,contract)
