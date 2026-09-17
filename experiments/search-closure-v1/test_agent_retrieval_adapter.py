import asyncio
from pathlib import Path
from types import SimpleNamespace
import pytest
from agent_retrieval_adapter import AgentRetrievalRuntime
from retrieval_runtime import sha,write_once

@pytest.mark.parametrize('profile',['bm25','character','dense'])
def test_raw_provider_preserves_channel_ranks_text_and_negative_scores(tmp_path,profile):
    # Exercise inherited real provider dispatch with a synthetic recall boundary.
    rt=object.__new__(AgentRetrievalRuntime);rt.root=tmp_path.resolve()
    rt.asset={'audit_sha256':'a'*64,'files':{str(rt.root/'catalog.sqlite'):{'sha256':'b'*64}}}
    calls=[]
    def recall(source,queries,*,include_dense):
        calls.append((source,queries,include_dense))
        return {'channels':{queries[0]['query_id']:{profile:[{'document_id':'kuaisearch:2','rank':1,'score':-2.0},
            {'document_id':'kuaisearch:1','rank':2,'score':-3.0}]}},'timings':{'synthetic':True}}
    rt.retrieve_batch=recall
    rt.document_texts=lambda s,ids:{d:'full original '+d for d in ids}
    path=tmp_path/'strategy.json';write_once(path,rt.strategy_manifest(profile=profile))
    binding=SimpleNamespace(data_root=str(tmp_path),manifest_sha256=sha(path),model_dump=lambda **kw:{'dataRoot':str(tmp_path)})
    request=SimpleNamespace(binding=binding,query='原始查询',source='kuaisearch',limit=10)
    observed=[]
    provider=rt.agent_provider(binding,manifest_path=path,profile=profile,on_result=lambda req,result:observed.append(result))
    result=asyncio.run(provider(request))
    assert [r['docid'] for r in result['hits']]==['kuaisearch:2','kuaisearch:1']
    assert [r['score'] for r in result['hits']]==[-2,-3]
    assert result['hits'][0]['text']=='full original kuaisearch:2'
    assert calls[0][1][0]['query']=='原始查询' and calls[0][2]==(profile=='dense')
    assert observed[0]['timings']['ce'] is None
    path.write_text('{}')
    with pytest.raises(ValueError,match='manifest binding'):
        rt.agent_provider(binding,manifest_path=path,profile=profile)

def test_raw_ce_combination_rejected_before_search():
    rt=object.__new__(AgentRetrievalRuntime)
    with pytest.raises(ValueError,match='cannot have'):
        rt.search_one('q','kuaisearch',profile='bm25',model_path='F:/model')
