from types import SimpleNamespace
import pytest
import evaluate_final_test as module
from metrics_v2 import canonical_hash

def sample():
    queries=[];rankings=[];qrels=[]
    for source in ['kuaisearch','multicpr']:
        for i in range(40):
            q={'query_id':source+str(i),'query':'query'+str(i),'source':source,'query_cohort':'main'}
            queries.append(q)
            docs=[source+':'+str(n) for n in range(100)]
            for m in ['baseline','winner','bm25','character','dense']:
                ranking=docs if m!='winner' else [docs[1],docs[0],*docs[2:]]
                rankings.append({**q,'method':m,'ranking':ranking,'ranking_sha256':canonical_hash(ranking)})
            qrels.extend({**q,'document_id':d,'grade':3 if n==0 else 'UNKNOWN' if n==1 else 0,
                'unknown_causes':['missing_evidence'] if n==1 else []} for n,d in enumerate(docs[:80]))
    return queries,rankings,qrels

def test_only_frozen_two_arms_compared_unknown_kept():
    queries,rankings,labels,causes=module.load_test_data(*sample())
    assert set(rankings)=={'baseline','winner'} and len(queries)==80
    assert labels['kuaisearch0']['kuaisearch:1']=='UNKNOWN'
    assert causes['kuaisearch0']['kuaisearch:1']==['missing_evidence']

def test_missing_top10_cannot_be_hidden_by_same_size_pool():
    queries,rankings,qrels=sample()
    qrels[0]['document_id']='kuaisearch:99'
    with pytest.raises(ValueError,match='Top10'):module.load_test_data(queries,rankings,qrels)

def test_dropping_query_or_changing_method_rejected():
    queries,rankings,qrels=sample()
    with pytest.raises(ValueError,match='80 heldout'):module.load_test_data(queries[:-1],rankings,qrels)
    rankings[0]['method']='posthoc_chosen'
    with pytest.raises(ValueError,match='Unexpected retrieval'):module.load_test_data(queries,rankings,qrels)

def test_authorization_failure_happens_before_review_or_pool_access(monkeypatch):
    def reject(*args):raise ValueError('not frozen')
    monkeypatch.setattr(module,'authorize',reject)
    monkeypatch.setattr(module.review_workspace,'freeze',lambda *a,**k:pytest.fail('read reviewers'))
    monkeypatch.setattr(module,'sha',lambda *a:pytest.fail('read pool'))
    with pytest.raises(ValueError,match='not frozen'):
        module.execute(SimpleNamespace(selection='not-final',selection_sha256='bad',pool_complete_sha256='bad'))
