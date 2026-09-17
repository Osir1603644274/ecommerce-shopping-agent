"""Paired warm CE latency on dev requests, with identical candidate/text inputs."""
import json
import time
from collections import defaultdict
from pathlib import Path

from retrieve import CrossEncoder,DEPTH,frozen_jsonl,rows
from stage1 import digest,dump,log
from integrity import checked_file,inference_binding
from retrieval_judgment_pool_core import PoolService

ROOT=Path("D:/agent-datasets/search-stage1-v1")

def benchmark():
    import numpy as np
    selected=json.loads((ROOT/"evaluation/selected-checkpoint.json").read_text(encoding="utf-8"))
    model_info=json.loads((ROOT/"models/manifest.json").read_text(encoding="utf-8-sig"))["reranker"]
    inference={"base_ce":inference_binding(model_info["path"]),"trained_ce":inference_binding(selected["model_path"])}
    destination=ROOT/"evaluation/latency"
    if (destination/"report.json").exists():
        prior=json.loads((destination/"report.json").read_text(encoding="utf-8-sig"))
        if prior["selected_checkpoint_sha256"]!=digest(ROOT/"evaluation/selected-checkpoint.json"):raise ValueError("Latency checkpoint drift")
        if prior["inference"]!=inference:raise ValueError("Latency inference drift")
        checked_file(destination/"observations.jsonl",prior["observations_sha256"])
        log("latency_reuse");return
    requests=[]
    for source in ("kuaisearch","multicpr"):
        cohort=source+"-dev"
        receipt=json.loads((ROOT/"pools"/f"{cohort}.json").read_text(encoding="utf-8-sig"))
        PoolService(ROOT/"pools/inputs",ROOT/"pools/runs").verify_run(receipt["run_id"])
        analysis=json.loads((Path(receipt["run_dir"])/"analysis.json").read_text(encoding="utf-8"))
        queries={q["query_id"]:q for q in rows(ROOT/"pools/inputs"/cohort/"queries.jsonl")}
        docs={d["document_id"]:d for d in rows(ROOT/"pools/inputs"/cohort/"documents.jsonl")}
        for q in sorted(analysis["queries"],key=lambda q:q["query_id"])[:10]:
            requests.append((q["query_id"],source,[[queries[q["query_id"]]["text"],docs[d["document_id"]]["text"]] for d in q["rrf_ranking"][:DEPTH]]))
    models={"base_ce":CrossEncoder(model_info["path"]),"trained_ce":CrossEncoder(selected["model_path"])}
    for encoder in models.values():encoder.score(requests[0][2][:32])
    records=[]
    for repeat in range(3):
        for qid,source,pairs in requests:
            methods=("base_ce","trained_ce") if repeat%2==0 else ("trained_ce","base_ce")
            for method in methods:
                encoder=models[method]
                encoder.torch.cuda.synchronize();started=time.perf_counter()
                encoder.score(pairs)
                encoder.torch.cuda.synchronize()
                records.append({"query_id":qid,"source":source,"repeat":repeat,"method":method,"pairs":len(pairs),"seconds":time.perf_counter()-started})
        log("latency_repeat_complete",repeat=repeat+1)
    frozen_jsonl(destination/"observations.jsonl",records)
    summary={}
    for method in models:
        values=[r["seconds"] for r in records if r["method"]==method]
        summary[method]={"mean_seconds":float(np.mean(values)),"median_seconds":float(np.median(values)),"p95_seconds":float(np.quantile(values,.95)),"observations":len(values)}
    dump(destination/"report.json",{"selected_checkpoint_sha256":digest(ROOT/"evaluation/selected-checkpoint.json"),
         "inference":inference,
         "observations_sha256":digest(destination/"observations.jsonl"),"summary":summary,"queries":20,"repeats":3,
         "pair_budget":DEPTH,"max_length":256,"microbatch":16,"model_loading_included":False,
         "scope":"Paired warm cross-encoder component including tokenization/transfer/forward/result copy, on fixed dev candidates. Not online end-to-end p95 or a production SLO."})
    log("latency_complete",**summary)

if __name__=="__main__":benchmark()
