"""Four bounded replays of a saved simultaneous-error repair; zero writes."""
import asyncio
import json
from openai import AsyncOpenAI
from openai.types.chat import ChatCompletion
from agent.app import llm
from agent.app.task_state import TaskState
from .common import HERE, RecordedClient, append, check_freeze, freeze, json_new, now, rows, sha

async def main():
    out=HERE/'p2/jointrepair001';out.mkdir(parents=True,exist_ok=False)
    source=HERE/'p4/regression003'
    request=next(r for r in rows(source/'private_requests.jsonl') if r['requestId']=='ctxv3-call-00965')
    response=ChatCompletion.model_validate(next(r['response'] for r in rows(source/'private_responses.jsonl') if r['requestId']==request['requestId']))
    with (source/'private_states.jsonl').open(encoding='utf-8') as stream:
        state_row=next(json.loads(line) for line in stream if json.loads(line)['executionOrdinal']==27)
    state=TaskState.model_validate(state_row['before'])
    before=state.model_dump_json()
    call=response.choices[0].message.tool_calls[0]
    llm.settings.task_state_extraction_strict_enabled=True
    llm.settings.deepseek_base_url='https://api.deepseek.com/beta'
    arguments=llm._parse_task_state_arguments(call,response=response,strict=True)
    error=llm.TaskStatePayloadValidationError('goal must be a real task goal; use JSON null for unchanged goal',
        code='invalid_goal_placeholder',field_path='goal')
    planning=request['request']['messages'][:-1] # remove old strict hint; production submit adds the current hint
    message=next(r['content'] for r in planning if r['role']=='user')
    json_new(out/'protocol.json',{'at':now(),'sourceRequestId':request['requestId'],
        'sourceRequestSha256':request['requestSha256'],'stateSha256':sha(state_row['before']),
        'calls':4,'purpose':'bounded repair of jointly invalid goal and blocker transition',
        'expected':'legal patch without invented resolution of the unchanged reference blocker',
        'persistCalls':0,'businessToolCalls':0,'productionDefaultChanged':False})
    freeze(out/'source_freeze.json')
    records=[]
    async with AsyncOpenAI(api_key=llm.settings.deepseek_api_key,base_url=llm.settings.deepseek_base_url,timeout=45,max_retries=0) as provider:
        client=RecordedClient(provider,phase='P2',output=out)
        for repeat in range(4):
            client.binding={'attempt':'jointrepair001','repeat':repeat}
            try:
                patched,_=await llm._repair_task_state_payload(client,planning_messages=planning,
                    original_call=call,validation_error=error,state=state,proposed_arguments=arguments)
                payload,_=llm._build_validated_task_state_payload(state,patched,message=message,
                    require_status=True,allow_auto_ready=False)
                remaining=llm._effective_unknowns(state,payload)
                passed=(remaining==state.unknowns and payload['status']=='collecting_information'
                    and bool(payload.get('pendingQuestions',state.pending_questions)))
                record={'repeat':repeat,'passed':passed,'payload':payload}
            except Exception as exc:
                record={'repeat':repeat,'passed':False,'error':type(exc).__name__,'message':str(exc)[:1000]}
            assert state.model_dump_json()==before
            append(out/'results.jsonl',record);records.append(record);print(repeat,record['passed'],flush=True)
    check_freeze(out/'source_freeze.json')
    json_new(out/'result.json',{'passed':all(r['passed'] for r in records),'cases':len(records),
        'passedCases':sum(r['passed'] for r in records),'stateUnchanged':True,'persistCalls':0,'businessToolCalls':0})

if __name__=='__main__':asyncio.run(main())
