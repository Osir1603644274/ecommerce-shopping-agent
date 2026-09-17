"""Provision isolated Kafka and warehouse before enabling acceptance consumers."""
import json,secrets
from prepare import OUT,ROOT,cmd,write
from stage import NETWORK

def run_owned(name,image,args):
    if name in cmd('ps','-a','--format','{{.Names}}').decode().splitlines():
        info=json.loads(cmd('inspect',name))[0]
        assert info['Config']['Labels']['commerce.release']=='20260915-a1'
        if not info['State']['Running']:cmd('start',name)
        return
    cmd('run','-d','--pull=never','--label','commerce.release=20260915-a1','--network',NETWORK,'--name',name,*args,image)

name='commerce-full-stage-kafka-20260915'
original=json.loads(cmd('inspect','local-life-kafka'))[0]
env=dict(v.split('=',1) for v in original['Config']['Env'] if v.startswith('KAFKA_'))
env.update(KAFKA_ADVERTISED_LISTENERS=f'INTERNAL://{name}:9092',KAFKA_LISTENERS='INTERNAL://:9092,CONTROLLER://:9093',
    KAFKA_CONTROLLER_QUORUM_VOTERS=f'1@{name}:9093',KAFKA_HEAP_OPTS='-Xms256m -Xmx256m')
args=['--memory=768m']
for k,v in env.items():args+=['-e',k+'='+v]
run_owned(name,'apache/kafka:3.9.1',args)
state=OUT/'stage-warehouse';state.mkdir(exist_ok=True)
tokenfile=state/'warehouse.token'
if not tokenfile.exists():tokenfile.write_text(secrets.token_hex(32),encoding='ascii')
warehouse='commerce-full-stage-warehouse-20260915'
if warehouse not in cmd('ps','-a','--format','{{.Names}}').decode().splitlines():
    cmd('run','-d','--pull=never','--label','commerce.release=20260915-a1','--network',NETWORK,'--name',warehouse,
        '--memory=128m','--mount',f'type=bind,src={ROOT}/scripts/backend-strengthening/warehouse_simulator.py,dst=/fixture.py,readonly',
        '--mount',f'type=bind,src={state},dst=/state','python:3.12-slim','python','/fixture.py',
        '--database','/state/warehouse.sqlite3','--token-file','/state/warehouse.token','--host','0.0.0.0')
else:
    info=json.loads(cmd('inspect',warehouse))[0];assert info['Config']['Labels']['commerce.release']=='20260915-a1'
write('stage-fulfillment.json',dict(enabled=True,kafka=name,warehouse=warehouse,isolated=True))
print('Isolated fulfillment dependencies provisioned; original brokers/warehouse untouched.')
