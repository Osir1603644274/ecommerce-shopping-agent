"""Score one authenticated natural-guide Validation20-v2 run."""
from __future__ import annotations
import argparse,json
from pathlib import Path

def main()->int:
    p=argparse.ArgumentParser(description=__doc__);p.add_argument("--predictions",type=Path,required=True);p.add_argument("--manifest",type=Path,required=True);p.add_argument("--public-catalog",type=Path,required=True);p.add_argument("--preregistration",type=Path,required=True);p.add_argument("--output",type=Path);a=p.parse_args()
    from agent.evaluation.used_phone_natural_guide_validation20_v2_evaluator_v1 import canonical_bytes,score_validation20_v2
    r=score_validation20_v2(predictions_path=a.predictions,manifest_path=a.manifest,public_catalog_path=a.public_catalog,preregistration_path=a.preregistration)
    if a.output is not None:
        if a.output.exists(): raise ValueError("score output already exists")
        a.output.write_bytes(canonical_bytes(r))
    print(json.dumps(r,ensure_ascii=False,sort_keys=True));return 0 if r["gatePassed"] else 2

if __name__=="__main__": raise SystemExit(main())
