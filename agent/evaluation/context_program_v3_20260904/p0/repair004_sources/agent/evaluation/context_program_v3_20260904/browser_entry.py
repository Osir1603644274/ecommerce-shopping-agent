"""Owned real HTTP/SSE UI, frozen read-only catalog, real model ledger.

Test instrumentation injects an evaluation capability only in this process.
This is not production deployment, backend/Elasticsearch, or payment testing.
"""
import argparse
import asyncio
import json
import os
import socket
import uuid
from .common import HERE, RecordedClient, append, check_freeze, file_sha, freeze, json_new, now, sha
from .multiturn import owned_redis


async def serve(out,port):
    import uvicorn
    from openai import AsyncOpenAI
    from agent.app import llm
    from agent.app.evaluation_context_arm import issue_evaluation_context_arm
    from agent.evaluation.real_user_multiturn_ab_executor_20260902_v2 import lane_runtime as old
    cfg=llm.settings
    assert cfg.redis_url==os.environ['REDIS_URL']
    cfg.multi_agent_v2_enabled=False
    cfg.memory_projection_client_enabled=False
    cfg.memory_bff_enabled=False
    cfg.used_phone_fast_preview_enabled=False
    cfg.web_query_intake_enabled=True
    cfg.web_query_intake_path=str(out/'intake.sqlite3')
    cfg.task_state_extraction_strict_enabled=True
    cfg.deepseek_base_url='https://api.deepseek.com/beta'
    cfg.used_phone_synthetic_price_policy='budget_and_ranking'
    cfg.used_phone_synthetic_price_dir=str(old.CATALOG_DIR)
    if file_sha(old.CATALOG_PATH)!=old.EXPECTED_CATALOG_SHA256:raise RuntimeError('catalog_drift')
    transport=old.FrozenCatalogTransport(tuple(old._product_from_catalog(r) for r in old._read_jsonl(old.CATALOG_PATH)))
    from agent.app import main,agent_trace
    original=main.run_agent
    async with AsyncOpenAI(api_key=cfg.deepseek_api_key,base_url=cfg.deepseek_base_url,timeout=45,max_retries=0) as provider:
        client=RecordedClient(provider,phase='P6',output=out)
        llm.get_client=lambda:client
        async def recorded_run(message,**kwargs):
            state=kwargs['task_state'];run_id='ctxv3-ui-'+uuid.uuid4().hex[:12]
            cap=issue_evaluation_context_arm(arm='CONTEXT_TREATMENT',run_id=run_id,
                task_id=state.task_id,session_id=state.session_id,model=cfg.deepseek_model,
                model_client=client,tool_transport=transport,provider_max_retries=0)
            client.binding={**client.binding,'runId':run_id,'taskId':state.task_id,'sessionId':state.session_id}
            kwargs['history']=None
            kwargs['evaluation_context_arm']=cap
            result=await original(message,**kwargs)
            trace=await agent_trace.get_trace_store().get(result[3])
            if trace is not None:
                body=trace.model_dump(by_alias=True,mode='json')
                append(out/'traces.jsonl',{'runId':result[3],'sha256':sha(body),'trace':body})
            append(out/'turns.jsonl',{'at':now(),'input':message,'taskId':state.task_id,
                'sessionId':state.session_id,'runId':result[3],'answer':result[0],
                'summary':result[4].model_dump(by_alias=True,mode='json') if result[4] else None})
            return result
        main.run_agent=recorded_run
        async def recorded_app(scope,receive,send):
            if scope['type']!='http' or scope.get('method')!='POST' or not scope.get('path','').startswith('/agent/chat-llm'):
                return await main.app(scope,receive,send)
            request_id='ui-http-'+uuid.uuid4().hex[:12]
            client.binding={'attempt':'browser001','requestId':request_id,'route':scope['path']}
            incoming=bytearray();outgoing=bytearray();status=None
            async def observed_receive():
                event=await receive()
                if event['type']=='http.request':incoming.extend(event.get('body',b''))
                return event
            async def observed_send(event):
                nonlocal status
                if event['type']=='http.response.start':status=event['status']
                if event['type']=='http.response.body':outgoing.extend(event.get('body',b''))
                await send(event)
            try: await main.app(scope,observed_receive,observed_send)
            finally:
                append(out/'http.jsonl',{'at':now(),'requestId':request_id,'route':scope['path'],
                    'request':incoming.decode('utf-8'),'response':outgoing.decode('utf-8'),
                    'status':status,'requestSha256':sha(incoming.decode('utf-8')),
                    'responseSha256':sha(outgoing.decode('utf-8')),'headersRecorded':False})
        server=uvicorn.Server(uvicorn.Config(recorded_app,host='127.0.0.1',port=port,lifespan='off',log_level='warning'))
        async def stop_when_requested():
            for _ in range(900):
                if (out/'STOP').exists():break
                await asyncio.sleep(1)
            server.should_exit=True
        stopper=asyncio.create_task(stop_when_requested())
        try: await server.serve()
        finally:stopper.cancel()
    check_freeze(out/'source_freeze.json')
    json_new(out/'stopped.json',{'at':now(),'ownedServicesStopped':True,'notAnAcceptanceReport':True})


def main():
    out=HERE/'p6/browser001';out.mkdir(parents=True,exist_ok=False)
    with socket.socket() as sock:sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
    json_new(out/'protocol.json',{'at':now(),'pid':os.getpid(),'url':f'http://127.0.0.1:{port}/',
        'hardTimeoutSeconds':900,'phaseRequestCap':40,'memory':False,'multiAgent':False,
        'realBrowserAndHTTP':True,'catalogTransport':'test-owned frozen read-only 439 items',
        'notTested':['real backend search','Elasticsearch','payment','login','real-user memory'],
        'scriptedMessages':['预算2000元，只要安卓，推荐三款二手手机','比较这个和另一个','比较这个和第三个','根据已有属性告诉我怎么选'],
        'noDefaultSwitch':True,'noSharedRedis':True})
    freeze(out/'source_freeze.json')
    with owned_redis(out):asyncio.run(serve(out,port))


if __name__=='__main__':main()
