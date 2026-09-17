"""Start exactly one frozen batch, with durable logs outside the tool session."""
import argparse
import os
import subprocess
import sys
from .common import *
from .runner import check_code

def launch(attempt):
    out=HERE / attempt
    runtime=read(out / 'runtime.json')
    verify(); check_code(runtime)
    assert read(out / 'preflight.json')['status']=='READY_FOR_FIXED_STAGE_STUDY'
    if any((out / name).exists() for name in ('launch.json','ledger.jsonl','batch.stdout.txt','batch.stderr.txt')):
        raise RuntimeError('existing_batch_no_duplicate_launch')
    args=[sys.executable,'-B','-m','agent.evaluation.context_codex_abc_study_v1_20260904.runner','run','--attempt',attempt]
    with (out / 'batch.stdout.txt').open('x',encoding='utf-8') as stdout, (out / 'batch.stderr.txt').open('x',encoding='utf-8') as stderr:
        proc=subprocess.Popen(args,cwd=ROOT,stdin=subprocess.DEVNULL,stdout=stdout,stderr=stderr,
            creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0)|getattr(subprocess,'CREATE_NEW_PROCESS_GROUP',0))
    receipt={'at':now(),'pid':proc.pid,'args':args,'cwd':str(ROOT),'launcherHash':file_sha(__file__),
        'head':subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip(),
        'gitStatus':subprocess.check_output(['git','status','--porcelain=v1'],cwd=ROOT,text=True,encoding='utf-8'),
        'manifestHash':file_sha(HERE / 'inputs/manifest.json'),'runtimeHash':file_sha(out / 'runtime.json')}
    write_new(out / 'launch.json',receipt)
    print(canonical({k:receipt[k] for k in ('at','pid','args','manifestHash')}),flush=True)

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--attempt',default='attempt001');args=parser.parse_args()
    if not args.attempt.isalnum(): raise ValueError('invalid_attempt')
    launch(args.attempt)
