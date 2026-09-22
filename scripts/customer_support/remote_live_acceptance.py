"""Owned native trade/BFF + real remote inventory probe. Creates a fresh isolated DB.

No existing listeners are stopped. Runtime secrets stay under runtime/private.json.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess
import time
import uuid

import httpx
import pymysql
from inventory_fault import InventoryFault
from live_evaluation import LiveDriver, save
from managed_bff import ManagedBff
from progress_oracle import grade_progress


def run(root, runtime, output,probe='refund',batch_args=None):
    output.mkdir(parents=True, exist_ok=False)
    runtime.mkdir(parents=True, exist_ok=False)
    save(output/'RUN_CONFIG.json',{'requestedProbe':probe})
    for name in ('remote_live_acceptance.py','receipt_retry_probe.py','unknown_reserve_probe.py','action_oracle.py','remote_exchange_probe.py','logistics_failure_probe.py','warehouse_fault.py','remote_actions.py','live_evaluation.py','inventory_fault.py','managed_bff.py','progress_oracle.py','business_oracle.py','simulator.py','trace_metering.py'):
        source=Path(__file__).with_name(name)
        (output/name).write_bytes(source.read_bytes())
    base=root/'docs/implementation/customer-support-20260919/runtime'
    env=os.environ.copy()
    for line in (base/'live-001/service.env').read_text().splitlines():
        if '=' in line:
            key,value=line.split('=',1);env[key]=value
    secrets={}
    for line in (base/'mysql-remote-002/test.env').read_text().splitlines():
        if '=' in line:
            key,value=line.split('=',1);secrets[key]=value
    inventory_path=base/'inventory-network-001'
    inventory=json.loads((inventory_path/'private.json').read_text())
    info=json.loads(subprocess.check_output(['docker','inspect',inventory['container']]))[0]
    if info['Config']['Labels'].get('support.attempt')!='inventory-network-001':raise ValueError('wrong inventory container')
    binding=info['NetworkSettings']['Ports']['8083/tcp'][0]
    if binding['HostIp']!='127.0.0.1':raise ValueError('inventory must be loopback')
    with socket.socket() as listener_probe:
        if os.name=='nt':listener_probe.setsockopt(socket.SOL_SOCKET,socket.SO_EXCLUSIVEADDRUSE,1)
        listener_probe.bind(('127.0.0.1',18081))
    database='support_live_remote_'+uuid.uuid4().hex[:12]
    config={k:inventory[k] for k in ('host','port','user','password')}
    config.update(database=database,authority='http://127.0.0.1:18081',inventoryRuntime=str(inventory_path))
    config['ownedNativeRuntime']=True
    config['paymentCallbackSecret']=env.get('PAYMENT_CALLBACK_SECRET')
    if probe=='local':config.pop('inventoryRuntime')
    if probe=='logistics':config['warehouseFaultActive']=True
    if probe=='receipt-retry':config['supportRecoveryActive']=True
    with pymysql.connect(host=config['host'],port=config['port'],user='root',password=secrets['MYSQL_ROOT_PASSWORD'],autocommit=True) as db,db.cursor() as c:
        c.execute('CREATE DATABASE '+database)
        c.execute('GRANT ALL PRIVILEGES ON '+database+'.* TO %s@%s',(config['user'],'%'))
    save(runtime/'private.json',config)
    jar=root/'backend/target/local-life-backend-0.1.0-SNAPSHOT.jar'
    save(output/'JAVA_BUILD.json',{'jarSha256':hashlib.sha256(jar.read_bytes()).hexdigest(),
         'sourceFiles':{str(p.relative_to(root)):hashlib.sha256(p.read_bytes()).hexdigest() for p in (root/'backend/src/main').rglob('*') if p.is_file()},
         'scope':'source and jar captured at launch; package command is recorded separately'})
    fault=InventoryFault('http://127.0.0.1:'+binding['HostPort'],inventory['writeToken'])
    warehouse=None
    process=None;driver=None
    log=(output/'java.log').open('w',encoding='utf-8')
    try:
        env.update(SERVER_PORT='18081',SERVER_ADDRESS='127.0.0.1',
                   SPRING_DATASOURCE_URL=f"jdbc:mysql://127.0.0.1:{config['port']}/{database}?allowPublicKeyRetrieval=true&useSSL=false&serverTimezone=UTC",
                   DB_USER=config['user'],DB_PASSWORD=config['password'],REDIS_HOST='127.0.0.1',REDIS_PORT='16379',
                   LOCAL_LIFE_INVENTORY_REMOTE_ENABLED='false' if probe=='local' else 'true',LOCAL_LIFE_INVENTORY_RECOVERY_ENABLED='false',
                   LOCAL_LIFE_SUPPORT_RECOVERY_ENABLED='false',LOCAL_LIFE_FULFILLMENT_WORKER_ENABLED='false',
                   LOCAL_LIFE_INVENTORY_SERVICE_URL=fault.url,LOCAL_LIFE_INVENTORY_SERVICE_TOKEN=inventory['writeToken'])
        if probe=='receipt-retry':
            env.update(LOCAL_LIFE_SUPPORT_RECOVERY_ENABLED='true',LOCAL_LIFE_SUPPORT_RECOVERY_MAX_ATTEMPTS='8')
        if probe=='logistics':
            from warehouse_fault import WarehouseUnavailable
            warehouse=WarehouseUnavailable()
            env.update(LOCAL_LIFE_FULFILLMENT_WORKER_ENABLED='true',LOCAL_LIFE_FULFILLMENT_WAREHOUSE_URL=warehouse.url,
                       LOCAL_LIFE_FULFILLMENT_WAREHOUSE_TOKEN='synthetic-warehouse-fault-token',LOCAL_LIFE_FULFILLMENT_MAX_ATTEMPTS='8')
        java=Path('C:/Users/ming/.jdks/jdk-21/jdk-21.0.10/bin/java.exe')
        process=subprocess.Popen([str(java),'-Duser.timezone=UTC','-jar',str(jar)],cwd=root,env=env,
                                 stdin=subprocess.DEVNULL,stdout=log,stderr=subprocess.STDOUT,
                                 creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
        deadline=time.monotonic()+100
        while time.monotonic()<deadline:
            if process.poll() is not None:raise RuntimeError('owned trade exited')
            try:
                if httpx.get(config['authority']+'/actuator/health',timeout=2,trust_env=False).status_code==200:break
            except httpx.HTTPError:pass
            time.sleep(.5)
        else:raise TimeoutError('owned trade readiness')
        # Same declared deployed remote topology as the independent Java acceptance.
        with pymysql.connect(**{k:config[k] for k in ('host','port','user','password','database')},autocommit=True) as db,db.cursor() as c:
            if probe!='local':c.execute('ALTER TABLE order_line_allocation DROP FOREIGN KEY fk_line_allocation_stock')
        with ManagedBff(18001,authority=config['authority']) as bff:
            save(output/'BFF_SOURCE_HASHES.json',bff.source_hashes)
            if batch_args is not None:
                from live_evaluation import main
                batch_args.runtime=runtime;batch_args.bff=bff.origin
                batch_args.bff_supervisor=bff;batch_args.inventory_fault=fault if probe!='local' else None
                batch_args.output=output/'batch'
                main(batch_args)
                return
            if probe=='receipt-retry':
                from receipt_retry_probe import run_receipt_retry
                run_receipt_retry(runtime,bff,fault,output)
                return
            if probe=='unknown-reserve':
                from unknown_reserve_probe import run_unknown_reserve
                run_unknown_reserve(runtime,bff,fault,output)
                return
            if probe=='logistics':
                from logistics_failure_probe import run_logistics
                run_logistics(runtime,bff,warehouse,output)
                return
            if probe=='exchange':
                from remote_exchange_probe import run_exchange
                run_exchange(runtime,bff,fault,output)
                return
            driver=LiveDriver(runtime,bff.origin)
            driver.inventory_fault=fault
            scenario={'fixture':{'kind':'return_inspection','unitPriceMinor':101,'purchasedQuantity':3,'requestedQuantity':1},'expected':{'routeOrOutcome':'after_sale'}}
            driver.prepare(scenario)
            driver.receipt('INSPECTION_ACCEPTED',itemId=driver.item,quantity=1,sellable=True)
            driver.drive('lose_inventory_ack',None)
            command=driver.remote_pending['commandId']
            pending=driver.sql('SELECT command_id,status,attempts FROM inventory_command_journal WHERE command_id=%s',(command,))[0]
            save(output/'pending-command.json',pending)
            assert pending['status']=='PENDING'
            before=driver.snapshot();save(output/'before.json',before)
            driver.drive('attempt_refund',None)
            receipt={'id':driver.remote_pending['receiptId']}
            blocked=driver.snapshot();save(output/'blocked.json',blocked)
            assert blocked['order_line_allocation']==before['order_line_allocation']
            observed=driver.chat('退货退款现在到账了吗？',uuid.uuid4().hex)
            save(output/'http-and-trace.json',observed)
            after=driver.snapshot();save(output/'after-chat.json',after)
            grade=grade_progress(scenario,observed,blocked,after)
            save(output/'VERDICT.json',grade)
            driver.drive('recover_inventory',None)
            final=driver.snapshot();save(output/'final.json',final)
            assert final['inventory_stock']==before['inventory_stock']
            assert final['order_line_allocation'][0]['refunded_minor']==101
            driver.admin('inventory-retry',driver.case['id'],{'commandId':command});driver.admin('receipt-apply',receipt['id'])
            assert final==driver.snapshot()
            save(output/'RESULT.json',{'status':'PASS' if grade['verdict']=='PASS' else 'FAIL','scope':'additional real remote-return Agent probe; not formal240',
                 'grade':grade,'events':fault.events,'refundBlockedWithoutAck':True,'recoveryReplayedWithoutChanges':True})
            print(json.dumps({'status':grade['verdict'],'scope':'remote return Agent probe'}),flush=True)
    finally:
        save(output/'proxy-events.json',fault.events)
        try:
            if driver:save(output/'driver-evidence.json',driver.driver_evidence)
            if driver and driver.order:save(output/'last-snapshot.json',driver.snapshot())
        finally:
            if driver:driver.close()
            if process and process.poll() is None:process.terminate();process.wait(timeout=20)
            log.close();fault.close()
            if warehouse:
                save(output/'warehouse-events.json',warehouse.events);warehouse.close()


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--runtime',type=Path,required=True);p.add_argument('--output',type=Path,required=True)
    p.add_argument('--probe',choices=('refund','exchange','logistics','unknown-reserve','receipt-retry'),default='refund')
    args=p.parse_args();run(Path(__file__).resolve().parents[2],args.runtime.resolve(),args.output.resolve(),args.probe)
