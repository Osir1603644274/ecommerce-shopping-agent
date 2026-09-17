"""Versioned live development replay. Same model/budget, flag-off/on, no benchmark claims."""
import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid
import httpx

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'agent'))
from app.settings import settings

async def main(out,limit,only_arm,case_path=None,port_base=18794):
    out.mkdir(parents=True,exist_ok=False)
    original=case_path or ROOT/'agent/evaluation/real_user_multiturn_replay_20260903_v7/conversations.jsonl'
    original=original.resolve()
    cases=[json.loads(x) for x in original.read_text(encoding='utf8').splitlines()]
    manifest=dict(kind='DEVELOPMENT_LIVE_REPLAY_NOT_SEALED_BENCHMARK',source=str(original.relative_to(ROOT)),
        sourceSha256=hashlib.sha256(original.read_bytes()).hexdigest(),model=settings.deepseek_model,
        runtime='fixed_v1',shoppingStateAuthority='v2',catalogCount=439,arms=[only_arm] if only_arm else ['legacy','knowledge'],
        knowledgeVersion=json.loads((ROOT/'datasets/knowledge/phone-v2/manifest.json').read_text())['version'],
        budget='same 45s request / 10000-token ContextView / 800 final-answer output tokens; final-answer thinking disabled in both arms',limit=limit,
        codeSha256={str(path.relative_to(ROOT)):hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted((ROOT/'agent/app').rglob('*.py'))})
    (out/'manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf8')
    for arm,port in [('legacy',port_base),('knowledge',port_base+1)]:
        if only_arm and arm!=only_arm: continue
        env={**os.environ,'PYTHONPATH':str(ROOT/'agent'),'BACKEND_BASE_URL':'http://127.0.0.1:18083',
            'REDIS_URL':'redis://127.0.0.1:16380/0','ECOMMERCE_GUIDE_ENABLED':'true',
            'PRODUCT_RETRIEVAL_MODE':'hybrid','PRODUCT_VECTOR_BACKEND':'local','PRODUCT_VECTOR_TIMEOUT_SECONDS':'30',
            'PRODUCT_TITLE_RERANKER_ENABLED':'false','USED_PHONE_SYNTHETIC_PRICE_POLICY':'budget_and_ranking',
            'USED_PHONE_SYNTHETIC_PRICE_DIR':str(ROOT/'datasets/current/used-phone'),
            'WEB_QUERY_INTAKE_ENABLED':'false','MODEL_CALL_RECEIPTS_ENABLED':'true','AGENT_TRANSACTION_ENABLED':'false','AGENT_ORCHESTRATOR_MODE':'unified',
            'AGENT_CONTROL_RUNTIME':'fixed_v1','AGENT_REACT_LIVE_ENABLED':'false','AGENT_LEGACY_FALLBACK_ENABLED':'false',
            'AGENT_FINAL_ANSWER_THINKING':'disabled','AGENT_FINAL_ANSWER_MAX_TOKENS':'800',
            'SHOPPING_STATE_AUTHORITY':'v2','PRODUCT_KNOWLEDGE_ENABLED':str(arm=='knowledge').lower(),
            'PRODUCT_KNOWLEDGE_MCP_URL':'http://127.0.0.1:18791/mcp','PRODUCT_KNOWLEDGE_TIMEOUT_SECONDS':'10',
            'RAG_MODEL_CACHE_DIR':str(ROOT/'agent/.cache/fastembed')}
        with (out/f'{arm}.log').open('w',encoding='utf8') as log:
            proc=subprocess.Popen([sys.executable,'-B','-m','uvicorn','app.main:app','--host','127.0.0.1','--port',str(port)],cwd=ROOT,env=env,
                stdout=log,stderr=log,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
            try:
                async with httpx.AsyncClient(timeout=180) as client:
                    for _ in range(100):
                        if proc.poll() is not None: raise RuntimeError('agent exited')
                        try:
                            r=await client.get(f'http://127.0.0.1:{port}/health');r.raise_for_status();break
                        except (httpx.HTTPError,OSError): await asyncio.sleep(.3)
                    count=0
                    for case in cases:
                        sid=f'kb-{arm}-{uuid.uuid4().hex[:20]}'
                        for turn in case['turns']:
                            if limit and count>=limit: break
                            started=time.perf_counter()
                            request=dict(message=turn['rawUserText'],sessionId=sid,domainHint='ecommerce')
                            try:
                                response=await client.post(f'http://127.0.0.1:{port}/agent/chat-llm',json=request)
                                value=response.json();status=response.status_code
                            except Exception as exc:
                                value=dict(errorType=type(exc).__name__,message=str(exc));status=0
                            result=dict(arm=arm,turnId=turn['turnId'],request=request,seconds=round(time.perf_counter()-started,4),httpStatus=status,response=value)
                            if value.get('runId'):
                                import redis.asyncio as redis
                                async with redis.from_url(env['REDIS_URL'],decode_responses=True) as rc:
                                    receipt_ids=[]
                                    for binding in dict.fromkeys([value['runId'],value.get('trace',{}).get('requestId')]):
                                        if binding:receipt_ids.extend(await rc.lrange('agent-model-call-receipt-v1:run:'+binding,0,-1))
                                    receipt_ids=list(dict.fromkeys(receipt_ids))
                                    result['modelCallReceipts']=[json.loads(raw) for rid in receipt_ids
                                        if (raw:=await rc.get('agent-model-call-receipt-v1:'+rid))]
                            with (out/'turns.jsonl').open('a',encoding='utf8') as file:file.write(json.dumps(result,ensure_ascii=False)+'\n')
                            print(json.dumps(dict(arm=arm,turn=turn['turnId'],status=status,seconds=result['seconds'],answer=str(value.get('answer',value))[:160]),ensure_ascii=False),flush=True)
                            count+=1
                        if limit and count>=limit: break
            finally:
                if proc.poll() is None: proc.terminate();proc.wait(timeout=20)

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--output',type=Path,required=True);p.add_argument('--limit',type=int,default=0)
    p.add_argument('--arm',choices=['legacy','knowledge'])
    p.add_argument('--cases',type=Path);p.add_argument('--port-base',type=int,default=18794)
    args=p.parse_args();asyncio.run(main(args.output,args.limit,args.arm,args.cases,args.port_base))
