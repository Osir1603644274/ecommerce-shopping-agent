"""Freeze confirmation analysis before observing confirmation outcomes."""
from .common import HERE, file_sha, json_new, now

out = HERE / 'p4/analysis_plan_v1.json'
confirmation = HERE / 'p4/confirm001'
if confirmation.exists(): raise RuntimeError('confirmation_already_started')
json_new(out, {'at':now(),'datasetSha256':file_sha(HERE/'p1/breadth001/confirm.jsonl'),
    'primary':'per-arm runtime success plus per-turn frozen hard-requirement oracle',
    'safety':'zero identity mismatch, safe-stop, oracle error; no turn deletion',
    'clusters':'primitiveFamily; 64 template-composed conversations are not 64 independent populations',
    'cost':'sum of all provider START/END calls bound to each attempt/arm, including extraction/repair/decision/answer; unknown reservation retained',
    'latency':'per-arm turn duration P50/P95; descriptive; no removal of failures or retries',
    'resampling':'paired primitiveFamily cluster bootstrap, 5000 iterations, seed 20260904',
    'NI':'margin -0.03; bootstrap lower bound alone cannot establish NI with zero observed failures and only eight synthetic families; report conservative exact upper bound for any family-level worsening',
    'efficiency':'total token ratio <=0.90, 95% upper <1, P95 latency ratio <=1.20; separate from execution gate',
    'humanLabels':False,'confirmationExpansionAfterOutcomes':False,
    'limitations':['same-agent synthetic compositions','not real-user population NI','no preference/gold/qrel labels',
        'runtime A/B changes subsequent action/model-call count; fixed-input P3 isolates model-input cost instead'],
    'finalAuthority':'bounded ACCEPT/HOLD only; never change production defaults'})
print('Confirmation analysis frozen before outcomes')
