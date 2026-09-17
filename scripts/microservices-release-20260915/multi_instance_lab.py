"""Add second isolated instances and Gateway; never change the storefront route."""
import json
import shutil
import time
from topology_lab import OUT,ROOT,docker,start_container,write,prepare

def main():
    config=prepare();data=json.loads((OUT/'TOPOLOGY-LAB-ENDPOINTS.json').read_text());urls=data['urls']
    proxy='micro-e2e-fault-proxy-20260915';control=OUT/'fault-control';control.mkdir(exist_ok=True)
    if proxy not in docker('ps','-a','--format','{{.Names}}').splitlines():
        docker('run','-d','--pull=never','--name',proxy,'--label','microservices.attempt=20260915-a1',
            '--network','agent_default','--memory','64m','--cpus','0.5',
            '-v',str(ROOT/'scripts/microservices-release-20260915/fault_proxy.py')+':/app/proxy.py:ro',
            '-v',str(control)+':/control','python:3.12-slim','python','/app/proxy.py')
    for role in ('catalog','trade'):
        original=json.loads(docker('inspect','micro-e2e-'+role+'-a-20260915'))[0]
        assert original['Config']['Labels'].get('microservices.attempt')=='20260915-a1'
        env=dict(value.split('=',1) for value in original['Config']['Env'] if '=' in value)
        env['SERVICE_INSTANCE_ID']=role+'-b'
        if role=='trade':env['LOCAL_LIFE_INVENTORY_SERVICE_URL']='http://'+proxy+':18083'
        urls[role+'B']=start_container('micro-e2e-'+role+'-b-20260915',original['Image'],env,8080,'512m')
        write('TOPOLOGY-LAB-ENDPOINTS.json',data)
    context=OUT/'gateway-image';context.mkdir(exist_ok=True)
    shutil.copy2(ROOT/'backend-gateway/target/local-life-gateway-0.1.0-SNAPSHOT.jar',context/'app.jar')
    docker('build','--pull=false','-t','agent-gateway:micro-lab-20260915','-f',str(ROOT/'scripts/commerce-full-release-20260915/Dockerfile'),str(context))
    env={'SPRING_PROFILES_ACTIVE':'microservices','JWT_SECRET':config['jwtSecret'],
        'REDIS_HOST':'micro-e2e-redis-20260915','RATE_LIMIT_ENABLED':'true',
        'CATALOG_INSTANCE_A':'http://micro-e2e-catalog-a-20260915:8080','CATALOG_INSTANCE_B':'http://micro-e2e-catalog-b-20260915:8080',
        'TRADE_INSTANCE_A':'http://micro-e2e-trade-a-20260915:8080','TRADE_INSTANCE_B':'http://micro-e2e-trade-b-20260915:8080',
        'JAVA_TOOL_OPTIONS':'-Xms48m -Xmx192m -XX:MaxDirectMemorySize=48m -XX:ActiveProcessorCount=2'}
    urls['gateway']=start_container('micro-e2e-gateway-20260915','agent-gateway:micro-lab-20260915',env,8080,'384m')
    write('TOPOLOGY-LAB-ENDPOINTS.json',data)
    print('Two catalog JVMs, two trade JVMs, independent inventory and Gateway ready in isolated lab.')

if __name__=='__main__':main()
