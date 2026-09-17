"""Real anonymous single-step API check; no commerce writes."""
import asyncio
import argparse
import json
from pathlib import Path
import time
import uuid

import httpx

BASE='http://127.0.0.1:5173'
PREFIX='/api/commerce-demo/workspace'


async def main(out):
    out.mkdir(exist_ok=False)
    async with httpx.AsyncClient(base_url=BASE,timeout=60,headers={'Origin':BASE,'Sec-Fetch-Site':'same-origin',
                'X-Conversation-Source':'automated_test'}) as c:
        response=await c.get(PREFIX);response.raise_for_status()
        c.headers['X-CSRF-Token']=response.json()['csrfToken']
        response=await c.post(PREFIX+'/run',json=dict(message='无糖可乐 888ml',requestId=str(uuid.uuid4()),mode='step'))
        response.raise_for_status();value=response.json()
        for phase in range(1,5):
            run=value['run']
            response=await c.post(PREFIX+'/control/step',json=dict(runId=run['id'],revision=run['revision']))
            response.raise_for_status();began=time.perf_counter()
            while time.perf_counter()-began<120:
                response=await c.get(PREFIX);response.raise_for_status();value=response.json()
                run=value['run']
                if run['status']=='interrupted' or len(run['nodes'])>=phase:break
                await asyncio.sleep(.5)
            (out/f'step{phase}.json').write_text(json.dumps(value,ensure_ascii=False,indent=2),encoding='utf-8')
            assert len(run['nodes'])==phase,(run['status'],run.get('notice'))
            node=run['nodes'][-1]
            assert node.get('input') is not None and node.get('output') is not None
            if phase==1:
                assert node['input']['用户原话']=='无糖可乐 888ml'
                assert any('888' in r['要求'] for r in node['output']['当前条件'])
            if phase==2:
                assert '888' in node['input']['检索词']
                assert len(node['output']['来源返回'])==2
            if phase==3:
                kept=node['output']['保留候选']
                assert kept and '888' in kept[0]['标题']
                assert all(not any(x in g['标题'] for x in ['330ml','500毫升','1L*','200ml']) for g in kept)
            print(json.dumps(dict(phase=phase,status=run['status'],input=node['input'],output=node['output']),ensure_ascii=False),flush=True)
        assert value['run']['status']=='completed'
        answer=next(m for m in value['messages'] if m['role']=='assistant')
        assert len(answer['flow'])==4
        (out/'RESULTS.json').write_text(json.dumps(dict(status='passed',steps=4,unitsPreserved=True,
            publicInputOutput=True,publishedFlowComplete=True),indent=2),encoding='utf-8')


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--output',type=Path,required=True)
    asyncio.run(main(parser.parse_args().output))
