"""Real same-origin HTTP acceptance; test identity and cookies stay private."""
import json
import secrets
import sys
import time
import httpx
from prepare import OUT,write

BASE='http://127.0.0.1:5174'
W='/api/commerce-demo/workspace'


def trade(call,current):
        if (OUT/'ACCEPTANCE-TRADE.json').exists():raise ValueError('trade already attempted; inspect receipts before retry')
        card=current['cards'][0];ident=card['id'];assert card['purchasable']
        report={'productId':ident,'sourceDocid':card.get('sourceDocid'),'steps':[]}
        def step(name,value):
            report['steps'].append(dict(name=name,result=value));write('ACCEPTANCE-TRADE.json',report)
        call('PUT',W+'/favorites/'+str(ident))
        call('POST',W+'/selection',dict(productId=ident,quantity=2))
        quote=call('POST',W+'/preview',dict(productId=ident,quantity=2))
        confirmation=quote['checkout']['proposal']['confirmationId']
        order=call('POST',W+'/confirm',dict(confirmationId=confirmation))['checkout']['outcome']['result']
        step('create',order)
        assert order['status']=='PENDING_PAYMENT'
        replay=call('POST',W+'/confirm',dict(confirmationId=confirmation))['checkout']['outcome']['result']
        assert replay['id']==order['id'];step('same-confirmation-replay',{'sameOrder':True})
        payment_quote=call('POST',W+'/payment-preview',dict(orderId=order['id']))
        payment=call('POST',W+'/confirm',dict(confirmationId=payment_quote['checkout']['proposal']['confirmationId']))['checkout']['outcome']['result']
        step('payment-created',payment)
        paid=call('POST','/api/commerce-demo/payments/'+payment['id']+'/simulate-success')
        step('local-payment-success',paid)
        before=call('GET',W+'/orders/'+order['id']+'/after-sales');step('paid-order',before)
        refund_quote=call('POST',W+'/refund-preview',dict(orderId=order['id'],items=[dict(itemId=ident,quantity=1)],reason='full catalog isolated acceptance'))
        refund_id=refund_quote['checkout']['proposal']['confirmationId']
        refund=call('POST',W+'/confirm',dict(confirmationId=refund_id))['checkout']['outcome']['result'];step('refund-created',refund)
        refunded=call('POST',W+'/refunds/'+refund['id']+'/simulate-success');step('local-refund-success',refunded)
        after=call('GET',W+'/orders/'+order['id']+'/after-sales');step('refund-order-recheck',after)
        report['status']='HTTP_FLOW_COMPLETE';write('ACCEPTANCE-TRADE.json',report)
        print(json.dumps({'status':report['status'],'productId':ident,'orderId':order['id'],'refundId':refund['id']}))


def main():
    client=httpx.Client(base_url=BASE,headers={'Origin':BASE,'X-Conversation-Source':'automated_test'},timeout=120)
    statefile=OUT/'acceptance-session.private.json'
    if statefile.exists():
        saved=json.loads(statefile.read_text())
        for k,v in saved['cookies'].items():client.cookies.set(k,v,domain='127.0.0.1',path='/')
        restored=client.get('/api/commerce-demo/me');restored.raise_for_status()
        saved['csrf']=restored.json()['csrfToken']
        client.headers['X-CSRF-Token']=saved['csrf']
    else:
        saved=dict(username='full-stage-'+secrets.token_hex(4),password=secrets.token_urlsafe(24))
        r=client.post('/api/commerce-demo/register',json=saved);r.raise_for_status()
        saved['csrf']=r.json()['csrfToken'];client.headers['X-CSRF-Token']=saved['csrf']
    def call(method,path,body=None):
        r=client.request(method,path,json=body);r.raise_for_status();return r.json()
    current=call('GET',W)
    saved['cookies']=dict(client.cookies);write('acceptance-session.private.json',saved)
    if '--search' in sys.argv:
        query=next((s.split('=',1)[1] for s in sys.argv if s.startswith('--query=')),'帮我找香榭丽舍貂皮大衣')
        label=next((s.split('=',1)[1] for s in sys.argv if s.startswith('--label=')),'search')
        if label!='search' and '--keep-conversation' not in sys.argv:call('POST',W+'/conversations',{'expectedConversationId':current['conversationId']})
        current=call('POST',W+'/run',{'message':query,'requestId':'full-search-'+secrets.token_hex(12),'mode':'continuous'})
        for _ in range(150):
            time.sleep(2);current=call('GET',W+'/control')
            if current['run']['status'] not in {'running','pausing'}:break
        write('acceptance-'+label+'.json',current)
        print(json.dumps({'status':current['run']['status'],'cards':len(current.get('cards',[])),
            'titles':[c['title'] for c in current.get('cards',[])]},ensure_ascii=False))
    elif '--trade' in sys.argv:
        trade(call,current)
    else:
        print(json.dumps({'run':current.get('run'),'cards':current.get('cards',[])},ensure_ascii=False))
    client.close()


if __name__=='__main__':main()
