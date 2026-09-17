"""Replace only named, attempt-owned lab containers; preserve stopped prior instances."""
import json
import shutil
import time
from pathlib import Path
from topology_lab import OUT,ROOT,docker,start_container,write

def replace(name,image,port,memory):
    old=json.loads(docker('inspect',name))[0]
    assert old['Config']['Labels'].get('microservices.attempt')=='20260915-a1'
    env=dict(value.split('=',1) for value in old['Config']['Env'] if '=' in value)
    backup=name+'-replaced-'+str(time.time_ns())
    if old['State']['Running']:docker('stop','-t','15',name)
    docker('rename',name,backup)
    return start_container(name,image,env,port,memory)

def main():
    import argparse
    parser=argparse.ArgumentParser();parser.add_argument('--revision',default='004');parser.add_argument('--source-build',default='002');args=parser.parse_args()
    assert args.revision.isdigit() and args.source_build.isdigit()
    source=OUT/('backend-build-'+args.source_build)/'target/local-life-backend-0.1.0-SNAPSHOT.jar'
    assert source.exists()
    context=OUT/('runtime-image-'+args.revision);context.mkdir(exist_ok=False)
    shutil.copy2(source,context/'app.jar')
    image='agent-backend:micro-20260915-'+args.revision
    docker('build','--pull=false','-t',image,'-f',str(ROOT/'scripts/commerce-full-release-20260915/Dockerfile'),str(context))
    data=json.loads((OUT/'TOPOLOGY-LAB-ENDPOINTS.json').read_text());urls=data['urls']
    for role in ('catalog','trade'):
        urls[role]=replace('micro-e2e-'+role+'-a-20260915',image,8080,'512m')
        write('TOPOLOGY-LAB-ENDPOINTS.json',data)
    import hashlib
    data['tradeJarSha256']=hashlib.sha256(source.read_bytes()).hexdigest()
    data['images']={role:json.loads(docker('inspect','micro-e2e-'+role+'-a-20260915'))[0]['Image'] for role in ('catalog','trade')}
    write('TOPOLOGY-LAB-ENDPOINTS.json',data)
    print('Owned isolated lab replaced; prior containers retained stopped. Live untouched.')

if __name__=='__main__':main()
