"""Audit or run the one-shot natural-guide Validation20-v2 identity."""
from __future__ import annotations
import argparse,asyncio,json
from pathlib import Path

def main()->int:
    p=argparse.ArgumentParser(description=__doc__);p.add_argument("--public-cases",type=Path,required=True);p.add_argument("--public-catalog",type=Path,required=True);p.add_argument("--java-base-url",default="http://127.0.0.1:18081");p.add_argument("--audit-only",action="store_true");p.add_argument("--run-dir",type=Path);p.add_argument("--timeout-seconds",type=float,default=90.0);a=p.parse_args()
    from agent.evaluation.used_phone_natural_guide_validation20_v2_runner_v1 import PublicRunnerError,audit_public_inputs,run_public_agent
    if a.java_base_url.rstrip("/")!="http://127.0.0.1:18081": raise PublicRunnerError("Java API endpoint pin mismatch")
    if a.audit_only:
        if a.run_dir is not None: raise PublicRunnerError("audit-only does not accept a run directory")
        print(json.dumps(audit_public_inputs(public_cases_path=a.public_cases,public_catalog_path=a.public_catalog,verify_java=True),ensure_ascii=False,sort_keys=True));return 0
    if a.run_dir is None: raise PublicRunnerError("--run-dir is required outside audit-only")
    m=asyncio.run(run_public_agent(public_cases_path=a.public_cases,public_catalog_path=a.public_catalog,run_dir=a.run_dir,timeout_seconds=a.timeout_seconds));print(json.dumps({"failedCaseIds":m["failedCaseIds"],"modelCallCount":m["modelCallCount"],"predictionSha256":m["prediction"]["sha256"],"succeededCaseIds":m["succeededCaseIds"]},ensure_ascii=False,sort_keys=True));return 0 if not m["failedCaseIds"] else 2

if __name__=="__main__": raise SystemExit(main())
