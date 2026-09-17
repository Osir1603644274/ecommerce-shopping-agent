"""Synthetic temporary end-to-end review artifacts; no real Agent/API or labels."""
import json
from pathlib import Path
import agent_quality_review as q
import reviews
from retrieval_runtime import write_once,sha
from test_run_agent_pairs import query_fixture

def test_actual_file_chain_collection_and_quality_aggregation(tmp_path,monkeypatch):
    run_root=tmp_path/'agent-runs';out=run_root/'fixture';quality=tmp_path/'quality'
    monkeypatch.setattr(q,'ROOT',quality);monkeypatch.setattr(q.runner,'ROOT',run_root)
    queries=list(query_fixture().values())
    for r in queries[11:18]:r['query_cohort']='diagnostic'
    metadata=tmp_path/'queries.jsonl';write_once(metadata,queries,jsonl=True)
    monkeypatch.setattr(q.runner.evaluation,'QUERIES',metadata)
    selected=q.runner.selected_twenty({r['query_id']:r for r in queries})
    slots=q.runner.schedule_queries(selected)
    write_once(out/'schedule.jsonl',slots,jsonl=True)
    write_once(out/'PREPARED.json',{'inputs':[{'path':str(metadata),'sha256':sha(metadata)}],'code':[],
        'files':{'schedule.jsonl':sha(out/'schedule.jsonl')}})
    prep_sha=sha(out/'PREPARED.json');receipts=[];receipt_hashes=[]
    for slot in slots:
        did=slot['source']+':1'
        receipt={'identity':{'slot':slot,'prepared_sha256':prep_sha},'outcome':'SUCCEEDED','answer':'fixture answer',
            'events':[],'traces':[{'tool':'search_catalog_evidence','ok':True,'detail':{'hits':[
                {'source':slot['source'],'docid':did,'text':'fixture original evidence','rank':1,'unknown':[]}]}}]}
        path=out/'attempts'/f"slot-{slot['slot']:03d}"/'RECEIPT.json'
        write_once(path,receipt);receipts.append(receipt);receipt_hashes.append({'slot':slot['slot'],'sha256':sha(path)})
    packet,mapping=q.runner.blind_packet(receipts)
    write_once(out/'blind-review/packet.jsonl',packet,jsonl=True)
    write_once(out/'private/blind-mapping.jsonl',mapping,jsonl=True)
    write_once(out/'blind-review/INSTRUCTIONS.json',{'status':'SYNTHETIC_FIXTURE_ONLY'})
    (out/'blind-review/AGENT_QUALITY_RUBRIC.md').write_text('synthetic fixture',encoding='utf-8')
    filenames=['packet.jsonl','INSTRUCTIONS.json','AGENT_QUALITY_RUBRIC.md']
    write_once(out/'blind-review/INPUT_MANIFEST.json',{'files':{n:sha(out/'blind-review'/n) for n in filenames}})
    write_once(out/'report.json',{'receipt_hashes':receipt_hashes,'arms':{arm:{'completed_wall':{'n':20,'p95_seconds':1.0}} for arm in ['baseline','winner']}})
    complete=out/'COMPLETE.json';write_once(complete,{'status':'ALL40_SLOTS_TERMINAL_NOT_QUALITY_APPROVED',
        'report_sha256':sha(out/'report.json'),'prepared_sha256':prep_sha,'blind_packet_sha256':sha(out/'blind-review/packet.jsonl')})
    from types import SimpleNamespace
    assert q.prepare(SimpleNamespace(agent_complete=complete,agent_complete_sha256=sha(complete)))['answers']==40
    keymap={(r['review_query_id'],r['label']):r for r in mapping}
    monkeypatch.setattr(reviews,'first_model_metadata',lambda tid:{'source_thread_id':tid,'model':'SYNTHETIC_FIXTURE'})
    for rid in ['A01','B01']:
        author=tmp_path/'authors'/rid;inp=author/'input';inp.mkdir(parents=True)
        source=quality/'packets/agent-01'
        for name in [*filenames,'INPUT_MANIFEST.json']:q.exact_copy(source/name,inp/name)
        values=[{'review_query_id':r['review_query_id'],'label':a['label'],'checks':{c:'PASS' for c in q.CHECKS},
            'useful_grade':2 if keymap[(r['review_query_id'],a['label'])]['arm']=='winner' else 1,
            'reason':'Synthetic grade solely to exercise aggregation; not a judgment of real answers.','findings':[]}
            for r in packet for a in r['answers']]
        write_once(author/'judgments.jsonl',values,jsonl=True)
        write_once(author/'receipt.json',{'human_gold':False,'self_review_completed':True,
            'label_source':'independent_context_agent_quality_silver_v1',
            'files':{**{n:sha(inp/n) for n in [*filenames,'INPUT_MANIFEST.json']},'judgments.jsonl':sha(author/'judgments.jsonl')}})
        write_once(quality/'dispatch'/f'{rid}.json',{'review_id':rid,'role':rid[0],'fresh_context':True,
            'packet_id':'agent-01','threadId':rid,'projectlessOutputDirectory':str(author/'outputs')})
        write_once(quality/'completion-events'/f'{rid}.json',{'thread_id':rid,'status':'completed','turn_id':'fixture-'+rid,'tool':'mcp__codex_app__wait_threads'})
        assert q.collect(rid)[1]['answers']==40
    assert q.third()['disagreement_answers']==0
    assert q.finalize()=={'status':'INDEPENDENT_AGENT_QUALITY_REVIEW_COMPLETE','agent_gate_passed':True}
    # Rehashed copied output cannot replace the actual independent author's file.
    collected=quality/'reviews/A01/judgments.jsonl';collected.write_text('changed')
    import pytest
    with pytest.raises(ValueError,match='differs'):q.collect('A01')
