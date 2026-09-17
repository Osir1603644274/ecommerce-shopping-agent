"""Real full-catalog search through the existing storefront and independent API."""
import json
import secrets
import time
import uuid
import httpx
from topology_lab import ROOT,OUT,write

def main():
    attempt=uuid.uuid4().hex[:10];base='http://127.0.0.1:5173';workspace='/api/commerce-demo/workspace'
    client=httpx.Client(base_url=base,headers={'Origin':base,'X-Conversation-Source':'automated_test'},timeout=180,trust_env=False)
    account={'username':'micro-live-'+attempt,'password':secrets.token_urlsafe(24)}
    response=client.post('/api/commerce-demo/register',json=account);response.raise_for_status()
    me=response.json();client.headers['X-CSRF-Token']=me['csrfToken']
    write('micro-live-session-'+attempt+'.private.json',{'account':account,'cookies':dict(client.cookies),'csrf':me['csrfToken']})
    health=httpx.get('http://127.0.0.1:18110/health',trust_env=False).json();assert health['protocol']=='catalog.search.v1'
    assert httpx.post('http://127.0.0.1:18110/v1/search',json={'requestId':uuid.uuid4().hex,'query':'手机'},trust_env=False).status_code==401
    report={'attempt':attempt,'searchHealth':health,'anonymousSearchDenied':True,'queries':[]}
    response=client.get(workspace);response.raise_for_status()
    response=client.post(workspace+'/conversations',json={'expectedConversationId':response.json()['conversationId']});response.raise_for_status()
    for query,expected in [('在全量目录中查找闪电购女装217','4000000005633437')]:
        response=client.post(workspace+'/run',json={'message':query,'requestId':'micro-search-'+uuid.uuid4().hex,'mode':'continuous'})
        if response.status_code>=400:
            write('INDEPENDENT-SEARCH-FAILED-'+attempt+'.json',{'stage':'start','status':response.status_code,'body':response.text})
        response.raise_for_status();current=response.json()
        for _ in range(150):
            time.sleep(2);response=client.get(workspace+'/control');response.raise_for_status();current=response.json()
            if current['run']['status'] not in {'running','pausing'}:break
        write('MICRO-LIVE-SEARCH-'+attempt+'.json',current)
        assert current['run']['status']=='completed' and current['cards'],current.get('run')
        assert any(str(card['id'])==expected and card.get('purchasable') for card in current['cards'])
        report['queries'].append({'query':query,'expectedProductId':expected,'cards':[{'id':c['id'],'purchasable':c.get('purchasable')} for c in current['cards']]})
    assert httpx.get('http://127.0.0.1:8000/health',trust_env=False).status_code==200
    report['status']='PASS';write('INDEPENDENT-SEARCH-ACCEPTANCE-'+attempt+'.json',report)
    marker=ROOT/'.runtime/merged-commerce/search-service-release.json';state=json.loads(marker.read_text());state.update(status='LIVE_VERIFIED',verifiedAtUnix=time.time(),acceptanceArtifact='INDEPENDENT-SEARCH-ACCEPTANCE-'+attempt+'.json');marker.write_text(json.dumps(state,indent=2)+'\n')
    print(json.dumps({'status':'PASS','artifact':'INDEPENDENT-SEARCH-ACCEPTANCE-'+attempt+'.json'}))

if __name__=='__main__':main()
