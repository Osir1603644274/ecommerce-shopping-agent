import copy
import hashlib
import json
import pytest
import review_workspace as rw
import reviews

CONTRACT={'query_id':'native-q','query':'甲牌洗面奶','core_product':'洗面奶',
          'required_attribute_keys':['本体','品牌'],'intent_policy':'score_product_relevance','source':'kuaisearch'}
CANDIDATE={'query_id':'native-q','document_id':'kuaisearch:123','query':'甲牌洗面奶','source':'kuaisearch',
           'catalog_text':'男士洗面奶 洁面','document':{'title':'男士洗面奶','brand':'','categories':['洁面'],'seller_name':''}}
JUDGMENT={'grade':2,'core_relation':'same','attributes':{'本体':'supported','品牌':'missing'},
          'evidence':[{'field':'title','quote':'男士洗面奶'}],'reason':'本体一致，品牌缺证','unknown_reason':None}

def prepare(tmp_path,monkeypatch):
    monkeypatch.setattr(rw,'ROOT',tmp_path.resolve())
    monkeypatch.setattr(reviews,'first_model_metadata',lambda tid:{'source_thread_id':tid,'model':'fixture-only'})
    candidates=tmp_path/'candidates.jsonl';contracts=tmp_path/'contracts.jsonl';provenance=tmp_path/'provenance.json'
    rw.write_once(candidates,[CANDIDATE],jsonl=True);rw.write_once(contracts,[CONTRACT],jsonl=True)
    rw.write_once(provenance,{'candidate_rows_sha256':rw.sha(candidates),'query_contracts_sha256':rw.sha(contracts)})
    root=tmp_path/'review'
    rw.build(root,candidates,contracts,provenance,prefix='train')
    return root

def author(root,role,grade=2,thread=None):
    packet=root/'packets/train-01';pair=rw.rows(packet/'pairs.jsonl')[0]
    judgment={**copy.deepcopy(JUDGMENT),'pair_id':pair['pair_id'],'grade':grade}
    if grade==1:
        judgment['attributes']['品牌']='contradicted';judgment['reason']='明确不同品牌可替代'
    target=root/'reviews'/(role+'01')
    source=root/'authors'/role;rid=role+'01';tid=thread or role
    for name in ('RUBRIC.md','pairs.jsonl','query-contracts.jsonl','INPUT_MANIFEST.json'):
        rw.exact_copy(packet/name,source/'input'/name)
    rw.write_once(source/'judgments.jsonl',[judgment],jsonl=True)
    rw.write_once(source/'receipt.json',{'human_gold':False,'self_review_completed':True,
        'label_source':'independent_context_model_silver_v6',
        'hashes':[rw.sha(p) for p in (source/'input').iterdir()]+[rw.sha(source/'judgments.jsonl')]})
    rw.write_once(root/'dispatch'/f'{rid}.json',{'review_id':rid,'role':role,'packet_id':'train-01',
        'fresh_context':True,'threadId':tid,'status':'DISPATCHED','expected_pairs':1,
        'projectlessOutputDirectory':str(source)})
    rw.write_once(root/'completion-events'/f'{rid}.json',{'thread_id':tid,'status':'completed',
        'turn_id':'original-'+rid,'tool':'mcp__codex_app__wait_threads'})
    actual=reviews.recompute_collected(rid,root)
    for name in ('judgments.jsonl','receipt.json'):rw.exact_copy(source/name,target/name)
    rw.write_once(target/'COLLECTED.json',actual['binding'])

def test_blinding_mapping_and_complete_merge(tmp_path,monkeypatch):
    root=prepare(tmp_path,monkeypatch)
    pair=rw.rows(root/'packets/train-01/pairs.jsonl')[0]
    assert pair['query_id']!='native-q'
    assert set(pair)=={'pair_id','query_id','query','document'}
    assert 'source' not in rw.rows(root/'packets/train-01/query-contracts.jsonl')[0]
    mapping=rw.rows(root/'private/pair-mapping.jsonl')[0]
    assert mapping['catalog_text_sha256']==hashlib.sha256(CANDIDATE['catalog_text'].encode()).hexdigest()
    author(root,'A');author(root,'B')
    result=rw.freeze(root,label_version='fixture',status='FIXTURE')
    assert result['grade_counts']=={'2':1}
    assert rw.freeze(root,label_version='fixture',status='FIXTURE')==result

def test_missing_third_does_not_freeze(tmp_path,monkeypatch):
    root=prepare(tmp_path,monkeypatch);author(root,'A');author(root,'B',grade=1)
    request=rw.prepare_third(root,'train-01')
    assert request['disagreement_pairs']==1
    assert set(rw.rows(root/'packets/third-train-01/pairs.jsonl')[0])=={'pair_id','query_id','query','document'}
    with pytest.raises(FileNotFoundError):rw.freeze(root,label_version='fixture',status='FIXTURE')

def test_same_context_or_input_tamper_rejected(tmp_path,monkeypatch):
    root=prepare(tmp_path,monkeypatch);author(root,'A',thread='same');author(root,'B',thread='same')
    with pytest.raises(ValueError,match='independent context'):rw.prepare_third(root,'train-01')
    with (root/'packets/train-01/pairs.jsonl').open('a') as stream:stream.write(' ')
    with pytest.raises(ValueError,match='input changed'):rw.read_review(root,'A01','train-01')

@pytest.mark.parametrize('edit',[
    {'query':'changed query'},
    {'document':{**CANDIDATE['document'],'source':'kuaisearch'}},
    {'catalog_text':''},
])
def test_input_mismatch_or_private_fields_rejected(edit):
    with pytest.raises(ValueError):rw.validate_candidate_inputs([{**CANDIDATE,**edit}],[CONTRACT])
