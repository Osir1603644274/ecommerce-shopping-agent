"""Actual HTTP acceptance using fresh visitor cookies; no other user's workspace touched."""
import time, uuid, requests, re
from common import *

ORIGIN='http://127.0.0.1:5173'

def main():
    session=requests.Session();session.headers['Origin']=ORIGIN
    def get(path):
        r=session.get(ORIGIN+'/api/commerce-demo/workspace'+path,timeout=30);r.raise_for_status();return r.json()
    state=get('');session.headers['X-CSRF-Token']=state['csrfToken'];checks=[];transcript=[]
    cases=get('/recommendation/cases');assert len(cases['cases'])==13
    checks.append('server-owned public case allowlist')
    def send(message,case='demo-01'):
        nonlocal state
        body={'message':message,'caseId':case,'expectedConversationId':state['conversationId'],
              'expectedRevision':(state.get('recommendationDemo') or {}).get('revision',0),'requestId':str(uuid.uuid4())}
        start=time.perf_counter();r=session.post(ORIGIN+'/api/commerce-demo/workspace/recommendation/run',json=body,timeout=30)
        if r.status_code!=200:raise RuntimeError(f'HTTP {r.status_code}: {r.text[:300]}')
        state=r.json();transcript.append({'message':message,'body':body,'response':state,'seconds':time.perf_counter()-start})
        assert not state['cards'] and state['selection'] is None and state['checkout'] is None
        return body,state['messages'][-1]['content']
    body,first=send('推荐');assert '| 10 |' in first and 'Amazon' in first;checks.append('real recommendation response, ten source-bound products')
    r=session.post(ORIGIN+'/api/commerce-demo/workspace/recommendation/run',json=body,timeout=30)
    assert r.status_code==200 and len(r.json()['messages'])==len(state['messages']);checks.append('same command idempotent replay')
    r=session.post(ORIGIN+'/api/commerce-demo/workspace/recommendation/run',json={**body,'message':'取消'},timeout=30)
    assert r.status_code==409;checks.append('same ID different command rejected')
    r=session.post(ORIGIN+'/api/commerce-demo/workspace/recommendation/run',json={**body,'requestId':str(uuid.uuid4())},timeout=30)
    assert r.status_code==409;checks.append('stale revision rejected')
    original_ids=re.findall(r'Amazon · ([A-Z0-9]+)',first)
    for message,expected_action in [('不要第一个商品','exclude'),('撤销','undo'),('喜欢第一个商品','like'),('比较第一个和第二个商品','compare'),('搜索护手霜','search')]:
        _,answer=send(message)
        assert state['run']['nodes'][1]['detail']['action']==expected_action
        found=re.findall(r'Amazon · ([A-Z0-9]+)',answer)
        if expected_action in {'exclude','like'}:assert original_ids[0] not in found
        if expected_action=='undo':assert found==original_ids
        if expected_action=='compare':assert len(found)==2
        checks.append('live model and verified tool effect: '+message)
    assert 'Amazon' in state['messages'][-1]['content'];checks.append('search and recommendation use same catalog')
    _,answer=send('推荐');assert state['run']['nodes'][1]['detail']['action']=='clarify' and '搜索范围' in answer;checks.append('search scope not silently widened by generic recommendation')
    _,answer=send('喜欢第一个商品');assert '保留当前搜索词' in answer;checks.append('like within search preserves query scope')
    _,answer=send('重新推荐');assert state['run']['nodes'][1]['detail']['action']=='recommend';checks.append('explicit release returns to history recommendation')
    _,answer=send('推荐',case='cold');assert '热门' in answer;checks.append('cold user explicit popularity fallback')
    _,answer=send('取消',case='cold');assert '清除' in answer;checks.append('cancel restores demo state')
    current={'message':'推荐','caseId':'demo-01','expectedConversationId':state['conversationId'],
             'expectedRevision':state['recommendationDemo']['revision'],'requestId':str(uuid.uuid4())}
    bad=session.post(ORIGIN+'/api/commerce-demo/workspace/recommendation/run',json=current,headers={'Origin':'https://unrelated.example'},timeout=10)
    assert bad.status_code==403;checks.append('cross-origin rejected')
    bad=session.post(ORIGIN+'/api/commerce-demo/workspace/recommendation/run',json=current,headers={'X-CSRF-Token':'wrong'},timeout=10)
    assert bad.status_code==403;checks.append('wrong CSRF rejected')
    other=requests.Session();other.headers['Origin']=ORIGIN
    otherstate=other.get(ORIGIN+'/api/commerce-demo/workspace',timeout=10).json();other.headers['X-CSRF-Token']=otherstate['csrfToken']
    bad=other.post(ORIGIN+'/api/commerce-demo/workspace/recommendation/run',json=current,timeout=10)
    assert bad.status_code==409;checks.append('another visitor cannot address first conversation')
    bad=session.post(ORIGIN+'/api/commerce-demo/workspace/recommendation/run',json={**current,'caseId':'demo-99'},timeout=10)
    assert bad.status_code==422;checks.append('unknown public persona rejected')
    bad=session.post(ORIGIN+'/api/commerce-demo/workspace/selection',json={'productId':'B00004U9V2','quantity':1},timeout=10)
    assert bad.status_code==422;checks.append('source ASIN not accepted as commerce product ID')
    folder=ROOT/'http-acceptance-003';folder.mkdir(exist_ok=True)
    # Persist public demo content and request identities, never cookie or CSRF credentials.
    for row in transcript:
        row['response'].pop('csrfToken',None)
    write(folder/'TRANSCRIPT.json',transcript)
    write(folder/'RESULT.json',{'status':'REAL_HTTP_MODEL_TOOL_ACCEPTANCE_PASSED','checks':checks,'check_count':len(checks),
        'turn_count':len(transcript),'seconds':[r['seconds'] for r in transcript],'identity':'fresh visitor cookies, public sample personas',
        'source':SOURCE,'transaction_calls':0,'final_benchmark_changed':False})
    print(read(folder/'RESULT.json'))

if __name__=='__main__':main()
