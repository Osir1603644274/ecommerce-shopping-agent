"""Real original-search regression, using only a fresh visitor's workspace."""
import json
import time
import uuid
from pathlib import Path
import requests

ROOT=Path('D:/agent-datasets/recommendation-completion-v1')
URL='http://127.0.0.1:5173/api/commerce-demo/workspace'

def main():
    session=requests.Session();session.headers['Origin']='http://127.0.0.1:5173'
    # Retire only our previous failed audit run, preserving its archived receipts.
    private=ROOT/'search-regression-session.private.json'
    if private.exists():
        old=json.loads(private.read_text(encoding='utf-8'))
        previous=requests.Session();previous.headers['Origin']='http://127.0.0.1:5173'
        previous.cookies.update(old['cookies']);previous.headers['X-CSRF-Token']=old['csrf']
        r=previous.get(URL,timeout=20);r.raise_for_status();run=r.json().get('run')
        if run and run['id']=='a8607106552fa97d4da0ba169bd8e481' and run['status']=='interrupted':
            r=previous.post(URL+'/control/end',json={'runId':run['id'],'revision':run['revision']},timeout=20)
            r.raise_for_status()
        private.unlink()
    r=session.get(URL,timeout=20);r.raise_for_status();state=r.json()
    session.headers['X-CSRF-Token']=state['csrfToken']
    rid=str(uuid.uuid4());start=time.perf_counter()
    r=session.post(URL+'/run',json={'message':'巧克力面包','requestId':rid,'mode':'continuous'},timeout=30)
    r.raise_for_status();state=r.json()
    deadline=time.monotonic()+150
    while state.get('run',{}).get('status') in {'running','pausing'} and time.monotonic()<deadline:
        time.sleep(1)
        r=session.get(URL,timeout=20);r.raise_for_status();state=r.json()
    state.pop('csrfToken',None)
    (ROOT/'SEARCH_REGRESSION_FIXED.json').write_text(json.dumps(state,ensure_ascii=False,indent=2),encoding='utf-8')
    assert state['run']['status']=='completed',state['run']['status']
    answer=next(m for m in state['messages'] if m['role']=='assistant' and m['requestId']==rid)
    assert '巧克力' in answer['content'] and 'kuaisearch:' in answer['content'] and 'multicpr:' in answer['content']
    assert '本轮不提供报价、库存及购买权限' in answer['content']
    assert 'Amazon' not in answer['content'] and not answer['cards'] and not state['cards']
    result={'status':'ORIGINAL_SEARCH_READ_ONLY_PUBLICATION_PASSED','query':'巧克力面包',
            'seconds':time.perf_counter()-start,'source_only':True,'trading_cards':0,
            'java_authority':'unavailable, not bypassed','fresh_visitor':True,'final_benchmark_changed':False}
    (ROOT/'SEARCH_REGRESSION_RESULT.json').write_text(json.dumps(result,indent=2),encoding='utf-8');print(json.dumps(result))

if __name__=='__main__':main()
