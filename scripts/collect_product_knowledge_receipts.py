"""Recover early TaskManager request-bound receipts without rewriting run originals."""
import asyncio
import hashlib
import json
from pathlib import Path
import sys
import redis.asyncio as redis


async def collect(directory):
    data=directory/'turns.jsonl';rows=[json.loads(s) for s in data.read_text(encoding='utf8').splitlines()]
    receipts={}
    async with redis.from_url('redis://127.0.0.1:16380/0',decode_responses=True) as client:
        for row in rows:
            response=row['response'];ids=[]
            bindings=[response.get('runId'),response.get('trace',{}).get('requestId')]
            for binding in dict.fromkeys(bindings):
                if binding:ids.extend(await client.lrange('agent-model-call-receipt-v1:run:'+binding,0,-1))
            values=[]
            for iid in dict.fromkeys(ids):
                raw=await client.get('agent-model-call-receipt-v1:'+iid)
                if raw:
                    value=json.loads(raw)
                    assert value['runId'] in bindings and value['modelCallId']==iid
                    values.append(value)
            receipts[row['arm']+':'+row['turnId']]=values
    result=dict(kind='READ_ONLY_RECEIPT_JOIN_RUN_ID_AND_REQUEST_ID',sourceSha256=hashlib.sha256(data.read_bytes()).hexdigest(),receipts=receipts)
    with (directory/'model-receipts.json').open('x',encoding='utf8') as file:json.dump(result,file,ensure_ascii=False,indent=2)
    print('Joined',sum(map(len,receipts.values())),'actual receipts')


if __name__=='__main__':asyncio.run(collect(Path(sys.argv[1])))
