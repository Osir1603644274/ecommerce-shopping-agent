"""Run the independent console with a private isolated acceptance credential."""
import argparse
import json
from pathlib import Path
import uvicorn
from simulator import SimulatorClient,create_app

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--fixture',type=Path,required=True);args=p.parse_args()
    data=json.loads(args.fixture.read_text(encoding='utf-8'))
    if data['authority']!='http://127.0.0.1:18080':raise ValueError('isolated authority required')
    client=SimulatorClient(data['authority'],data['adminToken'])
    try:uvicorn.run(create_app(client,19093),host='127.0.0.1',port=19093,access_log=False)
    finally:client.close()
