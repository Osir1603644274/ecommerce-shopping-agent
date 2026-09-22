"""Execute one explicit profile batch with an owned trade/BFF lifecycle."""
import argparse
import hashlib
import json
from pathlib import Path

from batch_plan import PROFILES, select_cases
from remote_live_acceptance import run


def main(args):
    # Validate selection before creating databases, processes, or output directories.
    data=args.dataset.read_bytes()
    rows=[json.loads(line) for line in data.decode('utf-8').splitlines()]
    selected=select_cases(rows,split=args.split,profile=args.profile,category=args.category,
                          case_id=args.case_id,outcome=args.outcome,limit=args.limit)
    expected=hashlib.sha256(data).hexdigest()
    if args.dataset_sha256 and expected!=args.dataset_sha256:
        raise ValueError('dataset fingerprint changed')
    root=Path(__file__).resolve().parents[2]
    run(root,args.runtime.resolve(),args.output.resolve(),args.profile,batch_args=args)
    print(json.dumps({'status':'BATCH_EXECUTED_NOT_ACCEPTANCE','caseCount':len(selected),
                      'datasetSha256':expected}))


if __name__=='__main__':
    p=argparse.ArgumentParser()
    p.add_argument('--runtime',type=Path,required=True)
    p.add_argument('--output',type=Path,required=True)
    p.add_argument('--dataset',type=Path,required=True)
    p.add_argument('--dataset-sha256')
    p.add_argument('--split',choices=('dev','heldout'),required=True)
    p.add_argument('--profile',choices=PROFILES,required=True)
    p.add_argument('--category');p.add_argument('--case-id');p.add_argument('--outcome')
    p.add_argument('--limit',type=int)
    p.add_argument('--repetition',type=int,choices=(1,2,3),default=1)
    main(p.parse_args())
