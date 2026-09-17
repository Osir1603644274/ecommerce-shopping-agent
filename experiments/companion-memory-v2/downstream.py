"""Reuse the frozen stage-2 pipeline with retrieved public histories."""
import argparse, hashlib, json, sys
from pathlib import Path
import history as h
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'behavior-search-v1'))
import companion as c

VARIANT='bm25'
def folder():return h.OUT/('pipeline' if VARIANT=='bm25' else 'semantic-pipeline')
def setup():
    out=folder();assert not (out/'CONTRACT.json').exists()
    rows=[]
    for meta in h.read(h.OUT/'DATASET.json')['public_tasks']:
        task=h.read(h.OUT/'public'/f"{meta['id']}.json")
        if VARIANT=='bm25':retrieved=h.read(h.OUT/'retrieval'/f"{meta['id']}.json")['ranked'][:5]
        else:
            selected=h.read(h.OUT/'SEMANTIC_SELECTION.json')['selected']
            assert selected!='bm25', 'no new pipeline needed if baseline selected'
            retrieved=h.read(h.OUT/'semantic'/f"{meta['id']}.json")[selected][:5]
        byid={s['id']:s['text'] for s in task['sessions']};snippets=[]
        for hit in retrieved:
            text=byid[hit['id']]
            if len(text)>6000:text=text[:3000]+'\n[Middle omitted for context budget]\n'+text[-3000:]
            snippets.append('[Retrieved session '+hit['id']+']\n'+text)
        rows.append({**meta,'memory':'\n\n'.join(snippets),'memory_present':bool(snippets),'retrieved_ids':[r['id'] for r in retrieved]})
    h.write(out/'public.json',rows)
    h.write(out/'CONTRACT.json',{'public_sha256':c.digest(out/'public.json'),'pipeline_sha256':c.digest(Path(c.__file__)),'variant':VARIANT,'arms':['top5_memory_then_existing_expansion_and_verification'],'max_calls':80,'max_output_tokens_per_call':900,'max_context_chars_per_session':6000,'context_policy':'first3000+last3000 if over6000; fixed before downstream execution','concurrency':2,'scope':'constructed long history, same previously exposed 40 tasks; not official test','input_boundary':'No private injection map, original reward_model, target ID or wanted_features read by predictor'})
    print('prepared downstream',len(rows),flush=True)

def predict():
    c.OUT=folder()
    assert c.digest(Path(c.__file__))==h.read(c.OUT/'CONTRACT.json')['pipeline_sha256']
    c.predict()

def retry():
    src=folder();dest=h.OUT/('retry-attempt002' if VARIANT=='bm25' else 'semantic-retry-attempt002');rows=h.read(src/'public.json')
    remaining=[r for r in rows if h.read(src/f"{r['id']}.prediction.json")['status']!='complete']
    if not remaining: print('no retries needed');return
    if not (dest/'CONTRACT.json').exists():
        h.write(dest/'public.json',remaining)
        h.write(dest/'CONTRACT.json',{**h.read(src/'CONTRACT.json'),'public_sha256':c.digest(dest/'public.json'),'retry':'single retry for failed tasks; originals retained'})
    c.OUT=dest;c.predict()

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('phase',choices=['setup','predict','retry']);p.add_argument('--variant',choices=['bm25','semantic'],default='bm25');a=p.parse_args();VARIANT=a.variant;globals()[a.phase]()
