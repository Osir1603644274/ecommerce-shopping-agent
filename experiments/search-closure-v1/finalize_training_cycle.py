"""Bind the actual single training-cycle outcome before any dev Top10 top-up."""
import argparse
import json
from pathlib import Path
from retrieval_runtime import read_json,sha,write_once,model_binding
from prepare_dev_topup import verify_training_cycle

ROOT=Path('D:/agent-datasets/search-closure-v1')

def verify_stage_order():
    paths=[ROOT/'development-topup',ROOT/'test-pool',ROOT/'final-selection/SELECTION.json']
    for path in paths:
        if path.is_file() or (path.is_dir() and any(p.is_file() for p in path.rglob('*'))):
            raise ValueError('Later-stage artifacts already exist before training decision: '+str(path))
    return {'checked_paths':[str(p) for p in paths],
            'scope':'Canonical experiment output paths only; this is not a claim about unobservable external access.'}

def finalize(args):
    target=ROOT/'training-preparation/TRAINING_CYCLE_DECIDED.json'
    # An existing decision is still revalidated below; its absence check is an
    # observation made on first publication, not a claim about later stages.
    if target.exists():
        stage_check=read_json(target)['stage_order_check']
    else:
        stage_check=verify_stage_order()
    if sha(args.gate)!=args.gate_sha256:raise ValueError('Gate changed')
    gate=read_json(args.gate)
    source=Path(args.grid)
    complete=read_json(source/'COMPLETE.json')
    if complete.get('status')!='COMPLETE' or sha(source/'binding.json')!=complete['binding_sha256']:
        raise ValueError('Original grid is not complete')
    models=read_json(source/'binding.json')['models'].copy()
    if set(models)!={'base','epoch1','epoch2','epoch3'}:raise ValueError('Expected original grid only')
    decision={'status':'TRAINING_CYCLE_DECIDED','gate':{'path':str(Path(args.gate).resolve()),'sha256':args.gate_sha256},
              'original_grid_complete':{'path':str((source/'COMPLETE.json').resolve()),'sha256':sha(source/'COMPLETE.json')},
              'stage_order_check':stage_check}
    if not gate.get('triggered'):
        if args.prepared or args.training_complete:raise ValueError('Negative gate cannot have a training branch')
        decision['branch']='not_triggered'
    elif args.training_complete:
        path=Path(args.training_complete).resolve();record=read_json(path)
        decision['branch']='three_epochs_completed'
        decision['training_complete']={'path':str(path),'sha256':sha(path)}
        for epoch in (1,2,3):models[f'new_epoch{epoch}']=model_binding(path.parent/'checkpoints'/f'epoch-{epoch}')
    elif args.prepared:
        path=Path(args.prepared).resolve()
        decision.update(branch='insufficient_new_training_queries',prepared={'path':str(path),'sha256':sha(path)})
    else:raise ValueError('Positive gate requires actual preparation or complete three-epoch training')
    # Validate the outcome before publishing its canonical destination.
    from tempfile import TemporaryDirectory
    with TemporaryDirectory(prefix='search-cycle-') as temporary:
        probe=Path(temporary)/'decision.json';write_once(probe,decision)
        verify_training_cycle(probe,sha(probe),models)
    write_once(target,decision)
    if decision['branch']=='three_epochs_completed':
        write_once(ROOT/'training-preparation/new-models.json',{k:v['path'] for k,v in models.items() if k.startswith('new_')})
    return {'path':str(target),'sha256':sha(target),'branch':decision['branch']}

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--gate',type=Path,required=True);p.add_argument('--gate-sha256',required=True)
    p.add_argument('--grid',type=Path,default=ROOT/'development-grid-r2')
    p.add_argument('--prepared',type=Path);p.add_argument('--training-complete',type=Path)
    print(json.dumps(finalize(p.parse_args())))

if __name__=='__main__':main()
