"""Select the independently owned search process; launcher performs controlled restart."""
import json
import secrets
import shutil
import time
from pathlib import Path
from topology_lab import ROOT,OUT,write

runtime=ROOT/'.runtime/merged-commerce'
marker=runtime/'search-service-release.json'
token=runtime/'secrets/catalog-search.token'
if marker.exists():
    saved=json.loads(marker.read_text(encoding='utf8-sig'))
    assert saved['protocol']=='catalog.search.v1' and saved['url']=='http://127.0.0.1:18110'
else:
    snapshot=OUT/('search-before-'+str(int(time.time())))
    snapshot.mkdir()
    shutil.copy2(runtime/'processes.json',snapshot/'processes.json')
    if not token.exists():token.write_text(secrets.token_hex(32)+'\n',encoding='ascii')
    assert len(token.read_text().strip())>=32
    marker.write_text(json.dumps({'protocol':'catalog.search.v1','url':'http://127.0.0.1:18110','status':'SELECTED_NOT_YET_VERIFIED',
        'selectedAtUnix':time.time(),'evidenceDirectory':str(OUT)},indent=2)+'\n',encoding='utf8')
print('Independent search selected. Runtime restart and full-catalog acceptance still required.')
