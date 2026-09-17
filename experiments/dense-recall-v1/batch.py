"""Sequential single-GPU execution; preserve receipts and fail immediately."""
import gc, time
import run as r

for arm in ['random','hard']:
    for seed in r.SEEDS:
        out=r.ROOT/'runs'/f'{arm}-{seed}'
        if (out/'COMPLETE.json').exists():
            record=r.read(out/'COMPLETE.json')
            assert record['code_sha256']==r.sha(r.__file__)
            print('SKIP completed',out,flush=True)
        else:
            print('START train',arm,seed,flush=True)
            r.train(arm,seed)
        gc.collect();r.torch.cuda.empty_cache()
for name in ['base']+[f'{arm}-{seed}' for arm in ['random','hard'] for seed in r.SEEDS]:
    out=r.ROOT/'evaluation'/name
    if (out/'REPORT.json').exists():
        assert r.read(out/'REPORT.json')['code_sha256']==r.sha(r.__file__)
        print('SKIP completed',out,flush=True)
    else:
        print('START diagnostic',name,flush=True)
        r.evaluate(name)
    gc.collect();r.torch.cuda.empty_cache()
print('BATCH COMPLETE',flush=True)
