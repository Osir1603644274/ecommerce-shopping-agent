"""Actual customer HTTP commands + separate simulator CLI processes + SQL oracle."""
import argparse
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import time
import uuid

import httpx
import pymysql


def run(runtime, output):
    config = json.loads((runtime / 'private.json').read_text())
    assert config['database'].startswith('support_live_')
    output.mkdir(exist_ok=False)
    report = {'status': 'RUNNING', 'scope': 'Customer Java HTTP + independent simulator CLI + SQL; no model/BFF/browser yet', 'cases': []}
    def save():
        (output / 'RESULT.json').write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding='utf-8')
    def sql(text, args=()):
        with pymysql.connect(**{k: config[k] for k in ('host','port','user','password','database')}, autocommit=True, cursorclass=pymysql.cursors.DictCursor) as db, db.cursor() as c:
            c.execute(text,args)
            return c.fetchall() if c.description else []
    client = httpx.Client(base_url=config['authority'],timeout=15,trust_env=False)
    def api(method,path,body=None,key=None):
        r=client.request(method,path,json=body,headers={'Idempotency-Key':key} if key else {})
        if not r.is_success: raise RuntimeError(f'{method} {path} {r.status_code}: {r.text[:500]}')
        result=r.json();assert result['success'] is True
        return result['data']
    try:
        for _ in range(120):
            try:
                if client.get('/actuator/health').status_code==200: break
            except httpx.HTTPError: pass
            time.sleep(1)
        else: raise RuntimeError('live service not healthy')
        suffix=uuid.uuid4().hex[:12]
        admin={'username':'support-admin-'+suffix,'password':secrets.token_urlsafe(24)}
        registered=api('POST','/api/auth/register',admin)
        sql("INSERT INTO user_role(user_id,role_name) VALUES(%s,'ADMIN')",(registered['user']['id'],))
        admin_token=api('POST','/api/auth/login',admin)['accessToken']
        customer={'username':'support-user-'+suffix,'password':secrets.token_urlsafe(24)}
        login=api('POST','/api/auth/register',customer)
        # Private credentials let later browser evaluation reuse this isolated fixture.
        (runtime/('customer-'+suffix+'.private.json')).write_text(json.dumps(customer),encoding='utf-8')
        client.headers['Authorization']='Bearer '+login['accessToken']
        item=740000000+int(suffix[:6],16)
        sql("""INSERT INTO product(id,source,source_item_id,title,brand,seller,category_l1,category_l2,category_l3,
                snapshot_price_minor,currency,price_status,attribute_text,data_nature,dataset_revision,source_license,provenance_url)
                VALUES(%s,'support_fixture',%s,'客服模拟手机黑色256GB','模拟品牌','模拟商家','数码','手机','智能手机',
                101,'CNY','verified','颜色：黑色。存储容量：256GB。仅为本项目客服验收模拟商品。','synthetic_fixture',
                'support-live-v1','project-fixture','urn:support:fixture')""",(item,suffix))
        sql("INSERT INTO inventory_stock(item_type,item_id,total_quantity,available_quantity) VALUES('PRODUCT',%s,100,100) ON DUPLICATE KEY UPDATE item_id=item_id",(item,))
        sql("INSERT INTO product_local_offer(product_id,price_minor,currency,price_kind,source_revision,version) VALUES(%s,101,'CNY','local_simulated','support-live-fixture',1) ON DUPLICATE KEY UPDATE price_minor=101",(item,))
        sql("INSERT INTO support_sale_specification(product_id,code,label,version) VALUES(%s,'support-fixture-black-256','模拟商品：黑色256GB',1) ON DUPLICATE KEY UPDATE product_id=product_id",(item,))
        baseline=sql("SELECT * FROM inventory_stock WHERE item_type='PRODUCT' AND item_id=%s",(item,))[0]
        assert baseline['available_quantity']>=7
        env=dict(os.environ,SUPPORT_SIMULATOR_ADMIN_TOKEN=admin_token,PYTHONUTF8='1',PYTHONIOENCODING='utf-8')
        def recipe(name,order,case,sellable=False):
            journal=output/(name+'-'+order+'.sqlite3')
            args=[sys.executable,str(Path(__file__).with_name('simulator.py')),'run','--authority',config['authority'],'--recipe',name,'--order-id',order,'--case-id',case,'--tracking','SIM-'+order,'--journal',str(journal)]
            if sellable:args.append('--sellable')
            for attempt in range(2):
                before=sql("SELECT * FROM inventory_stock WHERE item_id=%s",(item,)) if attempt else None
                p=subprocess.run(args,env=env,capture_output=True)
                (output/(name+'-'+order+f'-{attempt}.log')).write_bytes(p.stdout+p.stderr)
                assert p.returncode==0,f'{name} CLI failed; see saved log'
                if attempt:assert sql("SELECT * FROM inventory_stock WHERE item_id=%s",(item,))==before
            return json.loads(p.stdout)
        for name,kind in [('refund-only','REFUND_ONLY'),('return-refund','RETURN_REFUND'),('exchange','EXCHANGE')]:
            order=api('POST','/api/orders/cart',{'items':[{'itemType':'PRODUCT','itemId':item,'quantity':2}]},'cart-'+uuid.uuid4().hex)['id']
            payment=api('POST','/api/payments/orders/'+order)
            api('POST','/api/payments/'+payment['id']+'/simulate-success')
            recipe('original-delivery',order,'')
            card=api('POST','/api/after-sales/preview',{'orderId':order,'itemId':item,'quantity':1,'type':kind,'reason':'模拟器实链路验收'})
            assert card['amountMinor']==101
            case=api('POST','/api/after-sales/confirm',{'previewId':card['previewId']},'confirm-'+uuid.uuid4().hex)
            if kind!='REFUND_ONLY':
                case=api('POST','/api/after-sales/'+case['id']+'/return-shipment',{'expectedVersion':case['version'],'trackingNo':'RETURN-'+order},'return-'+uuid.uuid4().hex)
            recipe(name,order,case['id'],name=='return-refund')
            row=sql('SELECT * FROM support_case WHERE id=%s',(case['id'],))[0]
            assert row['phase']=='COMPLETED'
            allocation=sql('SELECT * FROM order_line_allocation WHERE order_id=%s',(order,))[0]
            assert allocation['refunded_minor']==(0 if kind=='EXCHANGE' else 101)
            assert not sql('SELECT * FROM support_order_claim WHERE case_id=%s',(case['id'],))
            report['cases'].append({'type':kind,'orderId':order,'caseId':case['id'],'case':row,'allocation':allocation})
            save()
        final=sql('SELECT * FROM inventory_stock WHERE item_id=%s',(item,))[0]
        assert final['total_quantity']==baseline['total_quantity']-1
        assert final['available_quantity']==baseline['available_quantity']-6
        assert final['sold_quantity']==baseline['sold_quantity']+5
        assert final['reserved_quantity']==baseline['reserved_quantity']
        report.update(status='PASS',baselineStock=baseline,finalStock=final)
    except BaseException as error:
        report.update(status='FAIL',failure=type(error).__name__+': '+str(error));raise
    finally:
        save();client.close()
    print(json.dumps({'status':report['status'],'cases':len(report['cases'])}))


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--runtime',type=Path,required=True);p.add_argument('--output',type=Path,required=True)
    a=p.parse_args();run(a.runtime,a.output)
