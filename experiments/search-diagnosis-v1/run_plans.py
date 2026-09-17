import asyncio, hashlib, json, sys
from pathlib import Path

REPO=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(REPO))
ROOT=Path('D:/agent-datasets/search-agent-diagnosis-20260913-v1')
def verify():
    c=json.loads((ROOT/'CONTRACT.json').read_text('utf-8'))
    for p,h in c['sourceCode'].items():assert hashlib.sha256((REPO/p).read_bytes()).hexdigest()==h,p
    for p,h in c['references'].items():assert hashlib.sha256(Path(p).read_bytes()).hexdigest()==h,p
def write(path,value):
    with path.open('x',encoding='utf-8') as f:json.dump(value,f,ensure_ascii=False,indent=2)
async def main():
    verify()
    from agent.app.catalog_conversation import plan_turn
    from agent.app.catalog_model_client import close_client
    from agent.app.settings import settings
    settings.catalog_workspace_reuse_model_client=True
    out=ROOT/'single-plans';out.mkdir(exist_ok=True)
    try:
        for q in json.loads((ROOT/'single-inputs.json').read_text('utf-8')):
            path=out/(q['query_id']+'.json')
            if path.exists():continue
            row={'input':q,'workspace':{'messages':[],'cards':[]}}
            try:row['plan'],row['receipt']=await plan_turn(q['query'],row['workspace'])
            except Exception as e:row.update(error=type(e).__name__+': '+str(e),receipt=getattr(e,'receipt',None))
            write(path,row)
            print(json.dumps({'query':q['query'],'plan':row.get('plan'),'error':row.get('error')},ensure_ascii=False),flush=True)
    finally:
        await close_client()
        verify()
if __name__=='__main__':asyncio.run(main())
