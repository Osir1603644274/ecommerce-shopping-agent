"""Recover the original dedicated public acceptance request, never replace its key."""
import json
import uuid
import httpx
from topology_lab import OUT,write

saved=json.loads((OUT/'public-trade-session-dd4540adda.private.json').read_text(encoding='utf8'))
assert saved['account']['username'].startswith('micro-live-')
base='http://127.0.0.1:5173'; workspace='/api/commerce-demo/workspace'
report={'originalAttempt':'dd4540adda','steps':[]}
with httpx.Client(base_url=base,timeout=30,trust_env=False,headers={'Origin':base,'X-Conversation-Source':'automated_test'}) as client:
    login=client.post('/api/commerce-demo/login',json=saved['account']);login.raise_for_status()
    client.headers['X-CSRF-Token']=login.json()['csrfToken']
    state=client.get(workspace);state.raise_for_status()
    report['before']=state.json().get('checkout')
    assert report['before']['proposal']['confirmationId']=='cfm-9zbi91nnSsPBHluo'
    for action in ('reconcile','confirm','reconcile'):
        response=client.post(workspace+'/'+action,json={'confirmationId':'cfm-9zbi91nnSsPBHluo'})
        report['steps'].append({'action':action,'http':response.status_code,'checkout':response.json().get('checkout',response.json())})
        if response.status_code!=200:break
    report['status']='OBSERVED'
write('PUBLIC-UNKNOWN-RECOVERY-'+uuid.uuid4().hex[:8]+'.json',report)
print(json.dumps(report,ensure_ascii=False))
