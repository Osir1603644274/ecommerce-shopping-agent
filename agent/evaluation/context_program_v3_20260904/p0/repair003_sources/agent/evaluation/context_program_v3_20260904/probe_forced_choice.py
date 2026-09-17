"""Four bounded API compatibility probes; no state writes or business tools."""
import asyncio
from copy import deepcopy
from openai import AsyncOpenAI
from agent.app.settings import settings
from .common import HERE, RecordedClient, append, check_freeze, freeze, json_new, now, rows

async def main():
    out=HERE/'p2/forcedchoice001';out.mkdir(parents=True,exist_ok=False)
    sources=[HERE/'p4/regression001/private_requests.jsonl',HERE/'p4/dev001/private_requests.jsonl']
    wanted={'ctxv3-call-00714','ctxv3-call-00789'}
    cases=[r for source in sources for r in rows(source) if r['requestId'] in wanted]
    assert len(cases)==2
    json_new(out/'protocol.json',{'at':now(),'calls':4,'cases':sorted(wanted),
        'purpose':'verify named tool_choice with explicit non-thinking on existing failed inputs',
        'source':'https://api-docs.deepseek.com/api/create-chat-completion/',
        'stateWrites':0,'businessToolCalls':0,'noRetry':True,'maxTokens':4096})
    freeze(out/'source_freeze.json')
    results=[]
    async with AsyncOpenAI(api_key=settings.deepseek_api_key,base_url='https://api.deepseek.com/beta',timeout=45,max_retries=0) as provider:
        client=RecordedClient(provider,phase='P2',output=out)
        for repeat in (1,2):
            for case in cases:
                request=deepcopy(case['request']);name=request['tools'][0]['function']['name']
                request['tool_choice']={'type':'function','function':{'name':name}}
                client.binding={'attempt':'forcedchoice001','sourceRequestId':case['requestId'],'repeat':repeat}
                record={**client.binding,'accepted':False}
                try:
                    response=await client.create(**request)
                    choice=response.choices[0];calls=choice.message.tool_calls or []
                    record.update(finish=choice.finish_reason,toolCount=len(calls),
                        accepted=choice.finish_reason!='length' and len(calls)==1 and calls[0].function.name==name)
                except Exception as exc:
                    record.update(errorType=type(exc).__name__,error=str(exc)[:250])
                    if client.halted: raise
                results.append(record);append(out/'results.jsonl',record)
                print(record,flush=True)
    check_freeze(out/'source_freeze.json')
    json_new(out/'result.json',{'passed':len(results)==4 and all(r['accepted'] for r in results),
        'results':results,'boundary':'tool-choice transport compatibility, not semantic correctness'})

if __name__=='__main__':asyncio.run(main())
