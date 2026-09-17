from copy import deepcopy
from types import SimpleNamespace
import pytest
import prepare_test_contracts as m

def sample():
    queries=[{'query_id':str(i),'query':'原文'+str(i),'source':'kuaisearch' if i<40 else 'multicpr'} for i in range(80)]
    proposal=[{**q,'no_added_requirements':True,'core_purpose_requirements':[],'substitutable_attributes':['颜色'],
        'required_attribute_keys':['本体','颜色'],'core_product':'明确商品本体','interpretation_notes':'按原文解释',
        'intent_policy':'score_product_relevance'} for q in queries]
    return proposal,queries

def test_missing_query_and_rewrite_rejected():
    proposal,queries=sample();m.validate_proposal(proposal,queries)
    with pytest.raises(ValueError,match='All original80'):m.validate_proposal(proposal[:-1],queries)
    proposal[0]['query']='重写查询'
    with pytest.raises(ValueError,match='was changed'):m.validate_proposal(proposal,queries)

def test_contract_cannot_drop_or_duplicate_explicit_attribute():
    proposal,queries=sample();proposal[0]['required_attribute_keys']=['本体']
    with pytest.raises(ValueError,match='coverage'):m.validate_proposal(proposal,queries)
    proposal,queries=sample();proposal[0]['core_purpose_requirements']=['颜色']
    proposal[0]['required_attribute_keys']=['本体','颜色','颜色']
    with pytest.raises(ValueError,match='coverage'):m.validate_proposal(proposal,queries)

def test_export_gate_failure_does_not_write_or_read_query_proposal(monkeypatch):
    def reject(*a):raise ValueError('selection missing')
    monkeypatch.setattr(m,'authorize',reject)
    monkeypatch.setattr(m,'write_once',lambda *a,**k:pytest.fail('wrote query'))
    with pytest.raises(ValueError,match='selection missing'):
        m.execute(SimpleNamespace(selection='missing',selection_sha256='bad',command='export'))

def test_pool_revalidates_actual_query_interpretation_author(tmp_path,monkeypatch):
    import run_final_test_pool as pool
    from pathlib import Path
    from retrieval_runtime import write_once,sha,fingerprint
    monkeypatch.setattr(pool,'ROOT',tmp_path)
    proposal,queries=sample();target=tmp_path/'test-preparation'
    source=target/'proposal.jsonl';frozen=target/'query-contracts-frozen.jsonl'
    write_once(source,proposal,jsonl=True);write_once(frozen,proposal,jsonl=True)
    author=target/'review.json'
    write_once(author,{'status':'ROOT_READ_ALL80_ORIGINAL_TEST_QUERIES_BEFORE_CANDIDATES','proposal_sha256':sha(source),
        'model_selection_sha256':'selection','query_identity_sha256':fingerprint(queries),'human_gold':False})
    seal=target/'QUERY_CONTRACTS_FROZEN.json'
    write_once(seal,{'status':'TEST_QUERY_CONTRACTS_FROZEN_BEFORE_CANDIDATES','query_count':80,
        'model_selection_sha256':'selection','query_contracts_sha256':sha(frozen),'rubric_sha256':pool.RUBRIC_SHA,
        'proposal_path':str(source),'proposal_sha256':sha(source),'author_review':{'path':str(author),'sha256':sha(author)},
        'freezer_sha256':sha(Path(m.__file__))})
    expected=sha(seal)
    assert pool.verify_contracts(frozen,seal,expected,queries,'selection')['sha256']==sha(frozen)
    author.write_text('{}')
    with pytest.raises(ValueError,match='author chain changed'):
        pool.verify_contracts(frozen,seal,expected,queries,'selection')
