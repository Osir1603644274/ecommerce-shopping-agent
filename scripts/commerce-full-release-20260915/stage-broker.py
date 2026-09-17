"""Provision the isolated broker with the application's existing local identity."""
import json
from prepare import cmd
env=dict(v.split('=',1) for v in json.loads(cmd('inspect','commerce-full-stage-java-20260915'))[0]['Config']['Env'] if '=' in v)
name='commerce-full-stage-rabbit-20260915'
user=env.get('RABBITMQ_USER','guest');password=env.get('RABBITMQ_PASSWORD','guest')
users=cmd('exec',name,'rabbitmqctl','list_users','--silent').decode()
if not any(line.split('\t')[0]==user for line in users.splitlines()):
    cmd('exec',name,'rabbitmqctl','add_user',user,password)
else:cmd('exec',name,'rabbitmqctl','change_password',user,password)
cmd('exec',name,'rabbitmqctl','set_permissions','-p','/',user,'.*','.*','.*')
print('Isolated broker identity provisioned.')
