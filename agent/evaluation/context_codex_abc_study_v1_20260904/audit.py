"""Read-only replay audit of the completed execution ledger; no model requests."""
from collections import Counter
import math
from .common import *
from .runner import check_code, validate, NOTICE

def require(value, label):
    if not value: raise RuntimeError('audit_failed:'+label)

def audit(attempt='attempt001'):
    manifest=verify(); out=HERE / attempt; runtime=read(out/'runtime.json'); check_code(runtime)
    require(read(out/'run_complete.json')['status']=='444_EXECUTIONS_FINISHED','not_complete')
    require(not (out/'run_stop.json').exists(),'stop_record')
    schedule=read(HERE/'inputs/schedule.json'); ledger=jsonl(out/'ledger.jsonl')
    require(len(schedule)==444 and len(ledger)==888,'schedule_or_ledger_count')
    require(len(list((out/'calls').glob('*/result.json')))==444,'result_count')
    rows=[]; bindings=[]; thread_ids=[]; control_hashes=[]
    for i, scheduled in enumerate(schedule):
        start,end=ledger[2*i:2*i+2]
        require(start['event']=='START' and end['event']=='END','ledger_order')
        for event in (start,end): require(all(event[k]==v for k,v in scheduled.items()),'ledger_binding')
        d=out/'calls'/f"{scheduled['ordinal']:04d}-{scheduled['caseId']}-{scheduled['arm']}"
        result=read(d/'result.json'); request=read(d/'request.json'); events=jsonl(d/'events.jsonl')
        require(all(result[k]==v for k,v in scheduled.items()),'result_binding')
        require(file_sha(d/'result.json')==end['resultHash'],'result_hash')
        require(file_sha(d/'request.json')==end['requestHash'],'request_hash')
        fixture_path=HERE/'inputs'/(scheduled['caseId']+'.json'); f=read(fixture_path)
        prompt=f['prompts'][scheduled['arm']]
        require((d/'prompt.txt').read_text(encoding='utf-8')==prompt,'prompt_text')
        require(request['promptHash']==sha(prompt),'prompt_canonical_hash')
        require(result['fixtureHash']==file_sha(fixture_path),'fixture_hash')
        require(result['contextHash']==sha(f['contexts'][scheduled['arm']]),'context_hash')
        require(request['applicationTextTokens']==tokens(prompt)==result['applicationInputTokens'],'input_tokens')
        require(result['applicationOutputTokens']==tokens(result['answer']),'output_tokens')
        require(request['schemaHash']==(sha(f['schema']) if f['schema'] else None),'schema_hash')
        require(request['schemaTextTokens']==result['schemaTextTokens']==(tokens(canonical(f['schema'])) if f['schema'] else 0),'schema_tokens')
        expected_args=[runtime['codex'],*p.cli_options(runtime['overrides']),'exec','--ephemeral','--skip-git-repo-check','--json','--color','never']
        if f['schema']:
            require(read(d/'output.schema.json')==f['schema'],'schema_content')
            expected_args+=['--output-schema',str(d/'output.schema.json')]
        require(request['args']==expected_args+['-'],'cli_args')
        finals=[e['item']['text'] for e in events if e.get('type')=='item.completed' and e.get('item',{}).get('type')=='agent_message']
        terminals=[e for e in events if e.get('type')=='turn.completed']
        require(result['answer']==(finals[-1] if finals else ''),'answer_from_event')
        require(result['terminalCount']==len(terminals),'terminal_count')
        require(result['usage']==(terminals[0].get('usage') if len(terminals)==1 else None),'usage_from_event')
        ids=[e['thread_id'] for e in events if e.get('type')=='thread.started']
        require(result['threadIds']==ids and len(ids)==1,'thread_identity');thread_ids+=ids
        notices=[e for e in events if e.get('item',{}).get('type')=='error' and e['item'].get('message')==NOTICE]
        errors=[e for e in events if e.get('type') in {'error','turn.failed'} or (e.get('item',{}).get('type')=='error' and e not in notices)]
        tools=[e for e in events if e.get('type','').startswith('item.') and e.get('item',{}).get('type') not in {'agent_message','reasoning','error'}]
        require(errors==result['errors'] and notices==result['startupNotices'] and tools==result['toolEvents'],'event_classification')
        require(not tools and not result['malformedLines'],'execution_boundary')
        success=result['exitCode']==0 and bool(result['answer']) and result['usage'] is not None
        status='COMPLETED_WITH_TRANSPORT_WARNINGS' if success and errors else 'COMPLETED' if success else 'FAILED'
        require(result['status']==end['status']==status,'status_classification')
        for name in ('cliWallMs','contextBuildMs','validationMs','stageWallMs'):
            require(math.isfinite(result[name]) and result[name]>=0,'finite_timing')
        require(result['stageWallMs']>=sum(result[k] for k in ('cliWallMs','contextBuildMs','validationMs')),'timing_sum')
        try: validation=validate(f,result['answer']) if result['answer'] else {'error':'no_answer'}
        except Exception as exc: validation={'errorType':type(exc).__name__,'error':str(exc)[:500]}
        require(validation==result['validation'],'validation_replay')
        if scheduled['kind']=='identical_input_control': control_hashes.append(request['promptHash'])
        bindings.append({'ordinal':scheduled['ordinal'],'files':{x.name:file_sha(x) for x in d.iterdir() if x.is_file()}})
        rows.append(result)
    require(len(set(thread_ids))==444,'fresh_threads')
    require(len(control_hashes)==12 and len(set(control_hashes))==1,'identical_controls')
    require(Counter(r['arm'] for r in rows if r['kind']=='study')=={a:144 for a in ARMS},'arm_counts')
    report={'at':now(),'status':'444_LEDGER_AND_ARTIFACT_BINDINGS_PASS','attempt':attempt,
        'sampledExecutions':444,'studyExecutions':432,'controlExecutions':12,'distinctThreadIds':444,
        'resultStatusCounts':dict(Counter(r['status'] for r in rows)),
        'studyStateValidationCounts':dict(Counter('correct' if r['validation'].get('hardConditionsCorrect') else 'incorrect_or_invalid' for r in rows if r['stage']=='state' and r['kind']=='study')),
        'sourceManifestHash':file_sha(HERE/'inputs/manifest.json'),'runtimeHash':file_sha(out/'runtime.json'),
        'ledgerHash':file_sha(out/'ledger.jsonl'),'auditorHash':file_sha(__file__),'bindings':bindings,
        'replayedModelCalls':0,'providerWireComplete':False,'timingScope':'process-and-local-stage wall clock, not inference or TTFT',
        'promptHashSemantics':'SHA256(canonical JSON string); prompt.txt also preserved as raw bytes hash',
        'qualityStatus':'PENDING_INDEPENDENT_BLIND_REVIEW'}
    return report,rows

if __name__=='__main__':
    report,_=audit();write_new(HERE/'attempt001/audit001/report.json',report)
    print(canonical({k:v for k,v in report.items() if k!='bindings'}),flush=True)
