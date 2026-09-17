"""Live public SSE evidence; dedicated synthetic account, no commerce writes."""
import json
import secrets
import time
import uuid
from pathlib import Path

import httpx

OUT = Path('D:/agent-experiments/chat-streaming-20260915-attempt001')
BASE = 'http://127.0.0.1:5173'
WS = '/api/commerce-demo/workspace'


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    attempt=uuid.uuid4().hex[:10]
    account=dict(username='stream-test-'+attempt,password=secrets.token_urlsafe(24))
    report=dict(attempt=attempt,queries=[],status='RUNNING')
    def write(): (OUT/('live-'+attempt+'.json')).write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf8')
    with httpx.Client(base_url=BASE,timeout=180,trust_env=False,headers={'Origin':BASE,'X-Conversation-Source':'automated_test'}) as client:
        response=client.post('/api/commerce-demo/register',json=account);response.raise_for_status()
        client.headers['X-CSRF-Token']=response.json()['csrfToken']
        (OUT/'session.private.json').write_text(json.dumps(dict(account=account,cookies=dict(client.cookies)),indent=2),encoding='utf8')
        client.get(WS).raise_for_status()
        for query in ['在全量目录中查找闪电购女装217','推荐一部 1000 元以内的二手手机，说明已有资料和缺失的实测依据。']:
            current=client.get(WS).json()
            client.post(WS+'/conversations',json={'expectedConversationId':current['conversationId']}).raise_for_status()
            began=time.perf_counter()
            response=client.post(WS+'/run',json=dict(message=query,requestId='stream-'+uuid.uuid4().hex,mode='continuous'))
            response.raise_for_status();current=response.json();runid=current['run']['id']
            row=dict(query=query,runId=runid,startReturnedSeconds=time.perf_counter()-began,events=[])
            report['queries'].append(row);write()
            terminal=None
            for _ in range(8):
                with client.stream('GET',WS+'/control/events',params={'runId':runid}) as stream:
                    stream.raise_for_status()
                    for line in stream.iter_lines():
                        if not line.startswith('data: '):continue
                        event=json.loads(line[6:]);elapsed=time.perf_counter()-began
                        if event['type']=='answer_delta':
                            row['events'].append(dict(seconds=elapsed,type='answer_delta',sequence=event['sequence'],chars=len(event['text']),replace=event['replace']))
                            print(json.dumps(dict(run=runid,event='delta',chars=len(event['text']),seconds=round(elapsed,2))),flush=True)
                            write()
                        elif event['type']=='snapshot':
                            current=event['workspace']
                            if current['run']['status'] not in {'running','pausing'}:
                                terminal=current['run']['status'];row['terminalSeconds']=elapsed
                        elif event['type']=='error':raise AssertionError(event)
                if terminal:break
            row.update(status=terminal,cards=len(current['cards']),answer=current['messages'][-1]['content'])
            deltas=[e for e in row['events'] if e['chars']>0]
            row['nonemptyDeltaFrames']=len(deltas)
            row['contentBeforeComplete']=bool(deltas and deltas[0]['seconds']<row['terminalSeconds'])
            write()
            assert terminal=='completed',current['run']
            assert len(deltas)>=2 and row['contentBeforeComplete'],row
            assert current['cards'],row
        report['status']='PASS';write()
        (OUT/'LATEST.json').write_text(json.dumps({'report':'live-'+attempt+'.json','session':'session.private.json'}),encoding='utf8')
        print(json.dumps(dict(status='PASS',artifact=str(OUT/('live-'+attempt+'.json')))),flush=True)


if __name__=='__main__':main()
