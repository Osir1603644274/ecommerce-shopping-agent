"""Meaningful guards before any training: paired exposure, isolation, source weights, loss direction."""
import collections, json
import torch
import run as r

prepared=r.read(r.ROOT/'PREPARED.json')
for f,h in prepared['hashes'].items(): assert r.sha(r.ROOT/f)==h
a=r.rows(r.ROOT/'random.jsonl');b=r.rows(r.ROOT/'hard.jsonl')
assert len(a)==len(b)==528
for x,y in zip(a,b):
    assert {k:v for k,v in x.items() if k!='neg'}=={k:v for k,v in y.items() if k!='neg'}
    for row in [x,y]:
        assert row['pos']['grade']==3 and row['neg']['grade']==0
        assert r.key(row['pos']['text'])!=r.key(row['neg']['text'])
        assert row['source']==row['pos']['source']==row['neg']['source']
assert set(collections.Counter(x['query_id'] for x in a).values())=={8}
weights={s:sum(x['weight'] for x in a if x['source']==s) for s in ['kuaisearch','multicpr']}
assert abs(weights['kuaisearch']-weights['multicpr'])<1e-9
good=torch.tensor([[.8,.2]],requires_grad=True)
bad=torch.tensor([[.2,.8]],requires_grad=True)
target=torch.tensor([0])
lg=torch.nn.functional.cross_entropy(good/.02,target)
lb=torch.nn.functional.cross_entropy(bad/.02,target)
assert lg<lb
lb.backward();assert bad.grad[0,0]<0 and bad.grad[0,1]>0
report={'checks':'input hashes, matched exposures, positive/negative separation, source balance, loss direction and gradients',
        'samples':len(a),'queries':len({x['query_id'] for x in a}),'changed_negatives':sum(x['neg']['document_id']!=y['neg']['document_id'] for x,y in zip(a,b)),
        'source_weight_sums':weights,'passed':True}
r.save(r.ROOT/'VALIDATION.json',report)
print(json.dumps(report),flush=True)
