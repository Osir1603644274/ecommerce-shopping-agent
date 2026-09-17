"""Independent source-set and old-feature checks for every snapshot."""
import collections,hashlib,json,time
import numpy as np
import snapshots as s

def main():
    started=time.time();cfg=s.read(s.OUT/'INDEX.json');built=s.read(s.OUT/'BUILT.json');db=s.connect();checked=collections.Counter();metrics={};examples=[]
    assert s.digest(s.SRC)==cfg['source_sha256'];assert s.digest(s.OLD/'config.json')==cfg['old_config_sha256']
    prior=s.read(s.OLD/'config.json')['calibration_click_fraction']
    raw=s.SRC.open('rb')
    # Re-read every source line against indexed fields, independently of snapshot construction.
    cursor=db.execute('select sid,uid,t,split,exposures,clicks,items,byte_offset,byte_length,line_sha256 from requests order by byte_offset')
    offset=0
    for line,indexed in zip(raw,cursor,strict=True):
        r=json.loads(line)
        assert indexed[:6]==(r['session_id'],r['user_id'],r['time_index'],r['split'],len(r['impressed_item_ids']),len(r['clicked_item_ids']))
        assert json.loads(indexed[6])==r['clicked_item_ids']
        assert indexed[7:]==(offset,len(line),hashlib.sha256(line).hexdigest())
        offset+=len(line);checked['raw_index_rows']+=1
    assert offset==s.SRC.stat().st_size
    def original(sid):
        off,size,sha=db.execute('select byte_offset,byte_length,line_sha256 from requests where sid=?',(sid,)).fetchone()
        raw.seek(off);line=raw.read(size);assert hashlib.sha256(line).hexdigest()==sha
        return json.loads(line)
    for sp in ['train','dev','test']:
        for name,d in cfg['old_sources'][sp].items():assert s.digest(s.OLD/name)==d
        assert s.digest(s.OUT/f'{sp}_snapshots.jsonl')==built['snapshot_hashes'][sp]
        X=np.load(s.OLD/f'{sp}_x.npy',mmap_mode='r');groups=s.read(s.OLD/f'{sp}_groups.json');counts=collections.Counter();lengths=collections.defaultdict(list);seen=set();examples_for_sp=set()
        with (s.OUT/f'{sp}_snapshots.jsonl').open(encoding='utf8') as f:
            for line in f:
                snap=json.loads(line);sid=snap['sid'];uid=snap['uid'];t=snap['request_time_index'];seen.add(sid)
                row=db.execute('select uid,t,split from requests where sid=?',(sid,)).fetchone();assert row[:2]==(uid,t)
                target=db.execute('select partition,ordinal,first_pair,last_pair,old_has_history from targets where sid=?',(sid,)).fetchone()
                assert target[0]==sp and target[1]==snap['ordinal']
                assert groups[snap['ordinal']]['sid']==sid
                if sp=='train':assert row[2]=='train' and cfg['history_end']<t<=cfg['train_end'];end=t-1
                elif sp=='dev':assert row[2]=='train' and t>cfg['train_end'];end=cfg['train_end']
                else:assert row[2]=='test' and t>cfg['train_end'];end=cfg['train_end']
                assert snap['history_cutoff_inclusive']==end
                expected=db.execute("select sid,t,exposures,clicks,items from requests where uid=? and t<=? and split='train' order by t,sid",(uid,end)).fetchall()
                actual=[]
                times=[g['time_index'] for g in snap['history_time_groups']];assert times==sorted(set(times))
                for group in snap['history_time_groups']:
                    for req in group['requests']:actual.append((req['sid'],group['time_index'],req['exposures'],len(req['clicked_item_ids']),req['clicked_item_ids']))
                assert actual==[(r[0],r[1],r[2],r[3],json.loads(r[4])) for r in expected]
                assert all(r[1]<t and r[0]!=sid for r in actual)
                checked['exact_history_set']+=1;checked['history_memberships']+=len(actual)
                for label,eligible in [('old',[r for r in expected if r[1]<=cfg['history_end']]),('new',expected)]:
                    facts=(len(eligible),sum(r[2] for r in eligible),sum(r[3] for r in eligible),sum(r[3]>0 for r in eligible),max((r[1] for r in eligible),default=None))
                    assert tuple(snap[label][k] for k in ['requests','exposures','clicks','clicked_requests','last_time'])==facts
                    counts[label+'_has_request_history']+=facts[0]>0;counts[label+'_has_click_history']+=facts[2]>0
                    for k in ['requests','clicks','clicked_requests']:lengths[label+'_'+k].append(snap[label][k])
                assert snap['new']['unique_clicked_items']==len({i for r in actual for i in r[4]})
                recent=snap['recent20_including_boundary_ties'];threshold=expected[-20][1] if len(expected)>=20 else (expected[0][1] if expected else None)
                assert recent['start_time_inclusive']==threshold
                recent_rows=[r for r in expected if threshold is not None and r[1]>=threshold]
                assert recent['requests']==len(recent_rows) and recent['clicks']==sum(r[3] for r in recent_rows)
                counts['recent_boundary_expanded']+=len(recent_rows)>20;lengths['recent_requests'].append(len(recent_rows))
                assert bool(snap['old']['clicks'])==bool(target[4])
                a,b=target[2:4]
                assert np.allclose(X[a:b,11],np.log1p(snap['old']['clicks']),rtol=1e-6,atol=1e-6)
                assert np.allclose(X[a:b,12],(snap['old']['clicks']+20*prior)/(snap['old']['exposures']+20),rtol=1e-6,atol=1e-6)
                assert np.all(X[a:b,15]==int(snap['old']['clicks']>0))
                checked['old_history_feature_rows']+=b-a
                counts['tasks']+=1;counts['gained_click_history']+=snap['old']['clicks']==0 and snap['new']['clicks']>0
                counts['lost_click_history']+=snap['old']['clicks']>0 and snap['new']['clicks']==0
                same=db.execute("select sid from requests where uid=? and t=? and split='train' and sid!=?",(uid,t,sid)).fetchall()
                if sp=='train' and same:counts['train_targets_with_same_time_peers']+=1
                typ='same_time_excluded' if sp=='train' and same else ('gained_history' if snap['old']['clicks']==0 and snap['new']['clicks']>0 else None)
                if typ and typ not in examples_for_sp:
                    examples_for_sp.add(typ)
                    examples.append({'type':typ,'partition':sp,'target':original(sid),'old':snap['old'],'new':snap['new'],'cutoff':end,'last_history_requests':[original(r[0]) for r in expected[-3:]],'excluded_same_time_peers':[original(r[0]) for r in same[:3]]})
        assert len(seen)==len(groups)
        metrics[sp]={'counts':dict(counts),'lengths':{k:{'min':int(min(v)),'p50':float(np.quantile(v,.5)),'p90':float(np.quantile(v,.9)),'max':int(max(v))} for k,v in lengths.items()}}
        print(sp,json.dumps(metrics[sp]['counts']),flush=True)
    # Global accounting independent of sampled targets.
    global_info={}
    for label,query in [
        ('eligible_source_requests',"select count(*),sum(exposures),sum(clicks),count(distinct uid) from requests where split='train' and t<=?"),
        ('heldout_native_train',"select count(*),sum(exposures),sum(clicks),count(distinct uid) from requests where split='train' and t>?")]:
        global_info[label]=dict(zip(['requests','exposures','clicks','users'],db.execute(query,(cfg['train_end'],)).fetchone()))
    raw.close();db.close()
    s.write(s.OUT/'RAW_EXAMPLES.json',examples)
    s.write(s.OUT/'REPORT.json',{'partitions':metrics,'source_periods':global_info,'scope':'request-time static/rolling snapshots under assumed availability of previous-request feedback; no click-arrival-time guarantee'})
    s.write(s.OUT/'VALIDATION.json',{'status':'PASS','checks':dict(checked),'source_hash_match':True,'sample_and_label_hashes_unchanged':True,'same_time_excluded':True,'dev_test_feedback_excluded':True,'full_history_set_checked':True,'seconds':time.time()-started,'limitations':['final feedback of earlier request assumed available','no impression position or exact click timestamps','previously exposed regression not new blind test']})
    print('VALIDATION PASS',dict(checked),flush=True)

if __name__=='__main__':main()
