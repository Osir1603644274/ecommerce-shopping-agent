"""Read-only raw-source preflight. Writes only a new, explicitly selected audit directory.

No importer/apply mode. MySQL access is one READ ONLY transaction; SQLite source is mode=ro.
"""
import argparse
import collections
import hashlib
import json
import shutil
import sqlite3
import subprocess
import threading
import time
import urllib.request
from pathlib import Path
import pymysql
from rules import *

ROOT=Path(__file__).resolve().parents[2]

def dump(path,obj):
    path.write_text(json.dumps(obj,ensure_ascii=False,indent=2,default=str)+'\n',encoding='utf-8')

def sha(path):
    h=hashlib.sha256()
    with path.open('rb') as f:
        for b in iter(lambda:f.read(8*1024*1024),b''): h.update(b)
    return h.hexdigest()

def snapshot():
    info=json.loads(subprocess.check_output(['docker','inspect','local-life-mysql'],text=True))[0]
    env=dict(v.split('=',1) for v in info['Config']['Env'] if '=' in v)
    db=pymysql.connect(host='127.0.0.1',port=13306,user=env['MYSQL_USER'],password=env['MYSQL_PASSWORD'],
                       database=env['MYSQL_DATABASE'],autocommit=False,connect_timeout=5,read_timeout=15,
                       charset='utf8mb4',cursorclass=pymysql.cursors.DictCursor)
    result={'mysqlMounts':info['Mounts']}
    try:
        with db.cursor() as c:
            c.execute('SET TRANSACTION READ ONLY')
            for table in ['product','product_local_offer','inventory_stock']:
                c.execute('SELECT * FROM '+table)
                result[table]=c.fetchall()
            c.execute('SELECT version,success FROM flyway_schema_history ORDER BY installed_rank')
            result['migrations']=c.fetchall()
        db.rollback()
    finally: db.close()
    return result

def monitor(stop,path):
    with path.open('x',encoding='utf-8') as f:
        while not stop.is_set():
            start=time.monotonic()
            try:
                with urllib.request.urlopen('http://127.0.0.1:5173/',timeout=5) as r:
                    result={'status':r.status,'bytes':len(r.read())}
            except Exception as e: result={'error':type(e).__name__}
            result.update(at=time.time(),ms=round((time.monotonic()-start)*1000,2))
            f.write(json.dumps(result)+'\n');f.flush()
            stop.wait(10)

def run(args):
    out=args.output.resolve()
    if out.exists(): raise ValueError('output already exists; preserve prior attempt')
    if not str(out).startswith('D:\\agent-datasets\\commerce-preflight-'):
        raise ValueError('audit output must be a new D:/agent-datasets/commerce-preflight-* directory')
    if shutil.disk_usage('D:/').free<4*1024**3: raise ValueError('insufficient audit headroom')
    out.mkdir(parents=True)
    stop=threading.Event(); worker=threading.Thread(target=monitor,args=(stop,out/'health.jsonl'),daemon=True); worker.start()
    started=time.time()
    try:
        manifest_path=Path(args.manifest); manifest=json.loads(manifest_path.read_text(encoding='utf-8'))
        frozen_manifest_sha=sha(manifest_path)
        before=snapshot();dump(out/'target-before.json',before)
        existing={(p['source'],p['source_item_id']):int(p['id']) for p in before['product']}
        ids={int(p['id']) for p in before['product']}
        metadata=manifest_path.parent/'metadata.sqlite'
        print('Verify metadata SHA256',flush=True)
        if sha(metadata)!=manifest['artifacts']['metadata.sqlite']['sha256']: raise ValueError('metadata hash mismatch')
        meta=sqlite3.connect(metadata.as_uri()+'?mode=ro',uri=True);meta.execute('PRAGMA query_only=ON')
        # This unique index plus byte-for-byte traversal proves source identities are unique.
        indexes=meta.execute("PRAGMA index_list('records')").fetchall()
        if not any(x[1]=='records_docid' and x[2]==1 for x in indexes): raise ValueError('missing identity uniqueness index')
        if meta.execute("PRAGMA index_info('records_docid')").fetchall()[0][2]!='docid': raise ValueError('wrong identity index')
        summary={'scope':'READ_ONLY_PREFLIGHT_NOT_IMPORT_ACCEPTANCE','ruleVersion':VERSION,
                 'head':subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip(),
                 'manifestSha256':frozen_manifest_sha,'sources':{},'sampleCandidates':[],
                 'freeBytes':{d:shutil.disk_usage(d+':/').free for d in 'CDEF'},'technicalCandidates':0}
        preserved=[]
        with (out/'anomalies.jsonl').open('x',encoding='utf-8') as anomalies:
            for source,info in manifest['sources'].items():
                path=Path(info['path']); stat0=path.stat(); h=hashlib.sha256();offset=0
                stats={'rows':0,'technicalCandidateRows':0,'quarantineRows':0,'preservedExisting':0,
                       'reasons':collections.Counter(),'unknownFields':collections.Counter(),'maxUtf16':collections.Counter(),
                       'rawBytes':0,'mappedUtf8Bytes':0,'priceMinMinor':None,'priceMaxMinor':None,
                       'candidateDigest':None,'sourceSha256':None}
                ch=hashlib.sha256()
                cursor=iter(meta.execute('SELECT docid,source_line,byte_offset,byte_length,record_sha256 FROM records WHERE source=? ORDER BY rowid',(source,)))
                with path.open('rb') as stream:
                    for line,raw in enumerate(stream,1):
                        h.update(raw); stats['rows']+=1; reasons=[]
                        bound=next(cursor,None)
                        raw_sha=hashlib.sha256(raw).hexdigest()
                        if not bound or (bound[1],bound[2],bound[3],bound[4])!=(line,offset,len(raw),raw_sha):
                            raise ValueError(f'metadata/source mismatch {source}:{line}')
                        offset+=len(raw)
                        try:
                            ident,fields=decode(source,raw)
                            if bound[0]!=source+':'+ident: raise ValueError('metadata identity mismatch')
                            old=existing.get((source,ident))
                            row=candidate(source,ident,fields,info['sha256'],old)
                            reasons=validate(row)
                            if old is None and int(row['id']) in ids: reasons.append('target_primary_key_collision')
                            for k,v in fields.items():
                                if v=='unknown': stats['unknownFields'][k]+=1
                                stats['maxUtf16'][k]=max(stats['maxUtf16'][k],len(v.encode('utf-16-le'))//2)
                            if old is not None:
                                stats['preservedExisting']+=1;preserved.append({'docid':bound[0],'productId':str(old)})
                            b=json.dumps(row,sort_keys=True,ensure_ascii=False,separators=(',',':')).encode()
                            ch.update(b+b'\n');stats['mappedUtf8Bytes']+=len(b)+1
                            p=row['localOffer']['priceMinor']
                            stats['priceMinMinor']=p if stats['priceMinMinor'] is None else min(p,stats['priceMinMinor'])
                            stats['priceMaxMinor']=p if stats['priceMaxMinor'] is None else max(p,stats['priceMaxMinor'])
                            if line<=3: summary['sampleCandidates'].append(row)
                        except (ValueError,TypeError,KeyError,UnicodeError,OverflowError) as error:
                            reasons=['decode_or_contract:'+str(error)[:180]]
                        if reasons:
                            stats['quarantineRows']+=1;stats['reasons'].update(reasons)
                            anomalies.write(json.dumps({'source':source,'line':line,'docid':bound[0],
                                'byteOffset':bound[2],'sha256':raw_sha,'reasons':reasons},ensure_ascii=False)+'\n')
                        else: stats['technicalCandidateRows']+=1
                        if line%250000==0:
                            print(json.dumps({'source':source,'rows':line,'quarantine':stats['quarantineRows'],'seconds':round(time.time()-started)}),flush=True)
                            time.sleep(.15)
                if next(cursor,None) is not None: raise ValueError('extra metadata rows')
                if stats['rows']!=EXPECTED[source]: raise ValueError('unexpected row count')
                if h.hexdigest()!=info['sha256']: raise ValueError('source hash differs')
                if (stat0.st_size,stat0.st_mtime_ns)!=(path.stat().st_size,path.stat().st_mtime_ns): raise ValueError('source changed during read')
                stats.update(sourceSha256=h.hexdigest(),candidateDigest=ch.hexdigest(),rawBytes=offset)
                summary['sources'][source]=stats;summary['technicalCandidates']+=stats['technicalCandidateRows']
                dump(out/'progress.json',summary)
        meta.close()
        if sha(manifest_path)!=frozen_manifest_sha: raise ValueError('manifest changed during run')
        after=snapshot();dump(out/'target-after.json',after)
        summary['targetTablesUnchanged']={t:before[t]==after[t] for t in ['product','product_local_offer','inventory_stock']}
        summary['existingIdentityMatches']=preserved
        summary['seconds']=round(time.time()-started,2)
        summary['status']='FULL_SOURCE_SCAN_COMPLETE_NO_IMPORT'
        dump(out/'RESULT.json',summary)
        dump(out/'PROVENANCE.json',{'files':{str(p):sha(p) for p in [Path(__file__),Path(__file__).with_name('rules.py'),ROOT/'backend/src/main/java/com/example/locallife/product/CreateProductRequest.java',ROOT/'backend/src/main/resources/db/migration/V1__baseline_schema.sql']}})
        print(json.dumps({'status':summary['status'],'rows':sum(s['rows'] for s in summary['sources'].values()),'output':str(out)}),flush=True)
    except Exception as e:
        dump(out/'FAILED.json',{'error':str(e),'seconds':time.time()-started});raise
    finally:
        stop.set();worker.join(6)

if __name__=='__main__':
    import sys
    sys.stdout.reconfigure(encoding='utf-8')
    p=argparse.ArgumentParser();p.add_argument('--output',type=Path,required=True)
    p.add_argument('--manifest',default='D:/agent-datasets/integration-repair-20260913-v1/metadata/MANIFEST.json')
    run(p.parse_args())
