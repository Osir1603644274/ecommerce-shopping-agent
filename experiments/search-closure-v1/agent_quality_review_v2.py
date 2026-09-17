"""Restore original model-visible scores for all40 blind answers; reuse frozen rubric.

Original Agent receipts and v1 packets stay immutable. No model is called and no
answer or grade is generated. Only the accidentally omitted score field is restored.
"""
import argparse
from copy import deepcopy
import json
import math
from pathlib import Path
import agent_quality_review as engine
from retrieval_runtime import read_json,write_once,sha,fingerprint
from review_workspace import exact_copy
from reviews import rows

ROOT=Path('D:/agent-datasets/search-closure-v1/agent-quality-review-v2')
ORIGINAL=ROOT.parent/'agent-quality-review'
COMPLETE=ROOT.parent/'agent-execution-preparation/agent-pairs-v1/COMPLETE.json'
COMPLETE_SHA='7c24fd27a0d42fe9495aea2e00ba1749f3b971a663590fbc2c8c8442b630f9ad'

def restore_scores(answer,actual_hits):
    result=deepcopy(answer);before=answer['evidence']
    if len(before)!=len(actual_hits) or len({h['docid'] for h in actual_hits})!=len(actual_hits):raise ValueError('Actual evidence coverage differs')
    for displayed,actual in zip(result['evidence'],actual_hits):
        if displayed!={k:actual[k] for k in ('source','docid','text','rank','unknown')}:raise ValueError('Original displayed evidence differs from actual model input')
        score=actual.get('score')
        if type(score) not in (int,float) or not math.isfinite(score):raise ValueError('Actual score is missing or invalid')
        displayed['score']=score
    return result

def project():
    original,mapping,_=engine.verify_run(COMPLETE,COMPLETE_SHA);packet=deepcopy(original)
    identities={(r['review_query_id'],r['label']):r for r in mapping}
    receipts={}
    for path in sorted((COMPLETE.parent/'attempts').glob('*/RECEIPT.json')):
        r=read_json(path);slot=r['identity']['slot'];key=(slot['query_id'],slot['arm'])
        if key in receipts:raise ValueError('Duplicate actual Agent identity')
        receipts[key]=(path,r)
    proof=[]
    for row in packet:
        for index,answer in enumerate(row['answers']):
            identity=identities[(row['review_query_id'],answer['label'])];path,receipt=receipts[(identity['query_id'],identity['arm'])]
            if fingerprint(receipt)!=identity['receipt_sha256']:raise ValueError('Original answer mapping changed')
            requests=[]
            for event in receipt['events']:
                if event['name'].endswith('-model-request.json'):
                    ep=path.parent/event['name'];value=read_json(ep)
                    if value['data']['ordinal']==2:requests.append((ep,value['data']['request']))
            if len(requests)!=1:raise ValueError('This actual run must have one recorded final-answer request per slot')
            request_path,request=requests[0];tool_messages=[m for m in request['messages'] if m['role']=='tool']
            if len(tool_messages)!=1:raise ValueError('Original final-answer tool input ambiguous')
            actual=json.loads(tool_messages[0]['content'])
            trace=next(t for t in receipt['traces'] if t['tool']=='search_catalog_evidence' and t['ok'])
            if actual!=trace['detail']:raise ValueError('Actual model request and tool receipt disagree')
            row['answers'][index]=restore_scores(answer,actual['hits'])
            proof.append({'review_query_id':row['review_query_id'],'label':answer['label'],
                'receipt':{'path':str(path),'sha256':sha(path)},'model_request':{'path':str(request_path),'sha256':sha(request_path)},
                'scores_restored':len(actual['hits'])})
    if len(proof)!=40:raise ValueError('Not all40 original answers restored')
    return packet,proof

def prepare():
    packet,proof=project();target=ROOT/'packets/agent-01';source=COMPLETE.parent/'blind-review'
    write_once(target/'packet.jsonl',packet,jsonl=True)
    for name in ('INSTRUCTIONS.json','AGENT_QUALITY_RUBRIC.md'):exact_copy(source/name,target/name)
    write_once(target/'INPUT_MANIFEST.json',{'status':'UNJUDGED','files':{n:sha(target/n) for n in ('packet.jsonl','INSTRUCTIONS.json','AGENT_QUALITY_RUBRIC.md')},
        'query_count':20,'answer_slots':40,'projection_version':'v2_original_model_visible_scores_restored','semantic_judgments_generated':0})
    write_once(ROOT/'PROJECTION_RECEIPT.json',{'status':'ALL40_ORIGINAL_SCORE_FIELDS_RESTORED_WITHOUT_MODEL_REPLAY',
        'agent_complete_sha256':COMPLETE_SHA,'projection_code_sha256':sha(Path(__file__)),
        'quality_engine_sha256':sha(Path(engine.__file__)),'original_packet_sha256':sha(source/'packet.jsonl'),
        'restored_packet_sha256':sha(target/'packet.jsonl'),'actual_model_request_bindings':proof,
        'restored_score_count':sum(p['scores_restored'] for p in proof),'answers_or_text_changed':False,'new_model_calls':0})
    write_once(ROOT/'INPUTS.json',{'status':'ACTUAL_AGENT_ANSWERS_READY_FOR_INDEPENDENT_REVIEW',
        'agent_complete':{'path':str(COMPLETE),'sha256':COMPLETE_SHA},'packet_input_sha256':sha(target/'INPUT_MANIFEST.json'),
        'answer_count':40,'projection_receipt':{'path':str(ROOT/'PROJECTION_RECEIPT.json'),'sha256':sha(ROOT/'PROJECTION_RECEIPT.json')}})
    write_once(ORIGINAL/'SUPERSEDED_INPUT_VIEW.json',{'status':'SUPERSEDED_FOR_FINAL_QUALITY_DECISION','reason':'Original model-visible score was omitted from all40 blind answer evidence views.',
        'original_inputs_retained':True,'new_workspace':str(ROOT),'projection_receipt_sha256':sha(ROOT/'PROJECTION_RECEIPT.json'),
        'rubric_unchanged':True,'all40_rereviewed_in_fresh_contexts_required':True,'semantic_grades_used_to_select_rows':False})
    return {'status':'AGENT_V2_BLIND_PACKETS_READY','answers':40,'score_fields':sum(p['scores_restored'] for p in proof),'path':str(target)}

def verify_projection():
    inputs=read_json(ROOT/'INPUTS.json');proof=read_json(ROOT/'PROJECTION_RECEIPT.json')
    if sha(ROOT/'PROJECTION_RECEIPT.json')!=inputs['projection_receipt']['sha256'] or proof['projection_code_sha256']!=sha(Path(__file__)) or proof['quality_engine_sha256']!=sha(Path(engine.__file__)):
        raise ValueError('V2 evidence projection binding changed')
    packet,bindings=project();target=ROOT/'packets/agent-01'
    if packet!=rows(target/'packet.jsonl') or bindings!=proof['actual_model_request_bindings'] or sha(target/'packet.jsonl')!=proof['restored_packet_sha256']:
        raise ValueError('V2 evidence differs from original actual model input')
    manifest=read_json(target/'INPUT_MANIFEST.json')
    if sha(target/'INPUT_MANIFEST.json')!=inputs['packet_input_sha256']:raise ValueError('V2 input manifest changed')
    for name,wanted in manifest['files'].items():
        if sha(target/name)!=wanted:raise ValueError('V2 blind input changed')
    if sha(target/'AGENT_QUALITY_RUBRIC.md')!=sha(COMPLETE.parent/'blind-review/AGENT_QUALITY_RUBRIC.md'):raise ValueError('Frozen rubric changed')
    engine.ROOT=ROOT

def collect(rid):
    verify_projection();return engine.collect(rid)

def main():
    p=argparse.ArgumentParser();p.add_argument('command',choices=['prepare','collect','third','finalize']);p.add_argument('--review-id');args=p.parse_args()
    if args.command=='prepare':result=prepare()
    else:
        verify_projection()
        result=engine.collect(args.review_id)[1] if args.command=='collect' else engine.third() if args.command=='third' else engine.finalize()
    print(json.dumps(result,ensure_ascii=False))

if __name__=='__main__':main()
