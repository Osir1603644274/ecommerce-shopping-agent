"""Artifact invariants and request reconstruction, without another model call."""
import collections,hashlib,json,sys
from pathlib import Path
import numpy as np
import history as h

def digest(p):
    hh=hashlib.sha256()
    with p.open('rb') as f:
        for block in iter(lambda:f.read(1024*1024),b''):hh.update(block)
    return hh.hexdigest()

def main():
    checks=collections.Counter();dataset=h.read(h.OUT/'DATASET.json')
    assert digest(h.UP)==dataset['longmemeval']['sha256'];checks['upstream_hash']+=1
    assert digest(h.PUBLIC)==dataset['source_public_sha256'];checks['source_public_hash']+=1
    gold={g['id']:g for g in h.read(h.OUT/'private/injection_map.json')}
    originals={r['id']:r for r in h.read(h.PUBLIC)}
    for meta in dataset['public_tasks']:
        task=h.read(h.OUT/'public'/f"{meta['id']}.json")
        assert set(task)=={'id','query','partition','sessions'}
        assert all(set(s)=={'id','text'} for s in task['sessions'])
        assert task['query']==originals[task['id']]['query']
        ids=[s['id'] for s in task['sessions']];assert len(ids)==len(set(ids));assert gold[task['id']]['injected_session_id'] in ids
        target=next(s for s in task['sessions'] if s['id']==gold[task['id']]['injected_session_id'])
        assert h.sha(target['text'])==gold[task['id']]['public_memory_sha256'];checks['public_history_valid']+=1
        path=h.OUT/'semantic/index'/f"{task['id']}.npy"
        if path.exists():
            vectors=np.load(path);index=h.read(path.with_suffix('.json'))
            assert vectors.shape==(len(index['chunk_session_ids']),384)
            assert set(index['chunk_session_ids'])==set(ids)
            assert np.isfinite(vectors).all() and np.max(abs(np.linalg.norm(vectors,axis=1)-1))<1e-4
            checks['semantic_index_valid']+=1
    for folder in ['pipeline','semantic-pipeline']:
        out=h.OUT/folder
        if not (out/'CONTRACT.json').exists():continue
        contract=h.read(out/'CONTRACT.json');assert digest(out/'public.json')==contract['public_sha256']
        assert digest(Path(__file__).resolve().parents[1]/'behavior-search-v1/companion.py')==contract['pipeline_sha256']
        rows=h.read(out/'public.json');assert len(rows)==40
        for row in rows:
            task=h.read(h.OUT/'public'/f"{row['id']}.json");ids={s['id'] for s in task['sessions']}
            assert set(row['retrieved_ids'])<=ids and len(row['retrieved_ids'])<=5
            p=out/f"{row['id']}.prediction.json";pred=h.read(p)
            if pred['status']!='complete':raise AssertionError('incomplete '+str(p))
            assert len(pred['calls'])==2
            payloads=[{'query':row['query'],'memory':row['memory']},{'query':row['query'],'preferences':pred['extraction'].get('preferences',[]),'candidates':pred['catalog_top20']}]
            for receipt,payload in zip(pred['calls'],payloads):
                assert hashlib.sha256(json.dumps(payload,sort_keys=True).encode()).hexdigest()==receipt['request_sha256']
                assert receipt['finish_reason']=='stop'
                checks['request_hash_reconstructed']+=1
            assert pred['C'] is None or pred['C'] in {d['id'] for d in pred['catalog_top20']}
            checks[folder+'_complete']+=1
    h.write(h.OUT/'FINAL_VALIDATION.json',{'status':'PASS','checks':dict(checks),'scope':'input fields, immutable source hashes, embedding geometry and recorded request payloads; not semantic ground-truth certification'})
    print(json.dumps(dict(checks)),flush=True)

if __name__=='__main__':main()
