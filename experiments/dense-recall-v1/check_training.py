"""Check deterministic training objective on all exposures, not a cherry-picked minibatch."""
import gc, json
import run as r

result={}
names=['base']+[f'{a}-{s}' for a in ['random','hard'] for s in r.SEEDS]
for name in names:
    tok,m=r.load(r.base() if name=='base' else r.ROOT/'runs'/name/'model');m.eval()
    result[name]={}
    for arm in (['random','hard'] if name=='base' else [name.split('-')[0]]):
        rr=r.rows(r.ROOT/f'{arm}.jsonl');total=0.;plain=0.
        with r.torch.no_grad():
            for i in range(0,len(rr),8):
                b=rr[i:i+8];lv=r.loss_values(tok,m,b)
                total+=float((lv*r.torch.tensor([x['weight'] for x in b],device='cuda')).sum())
                plain+=float(lv.sum())
        result[name][arm]={'source_equal_objective':total/len(rr),'unweighted_objective':plain/len(rr)}
    print(name,result[name],flush=True)
    del m,tok;gc.collect();r.torch.cuda.empty_cache()
r.save(r.ROOT/'TRAINING_OBJECTIVE_CHECK.json',result)
