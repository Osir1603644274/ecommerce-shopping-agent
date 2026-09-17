"""Choose one of the three fixed checkpoints using development evidence only."""
import json
from pathlib import Path
from stage1 import digest,dump,log
from integrity import checked_file,verify_qrels
from evaluate import rankings,checkpoint
from retrieve import rows
from metrics import query_metrics
from collections import defaultdict

ROOT=Path("D:/agent-datasets/search-stage1-v1")

def select():
    evidence=[]
    options=[]
    for epoch in (1,2,3):
        values=[]
        for source in ("kuaisearch","multicpr"):
            path=ROOT/"evaluation/results"/f"{source}-dev"/f"epoch-{epoch}/report.json"
            report=json.loads(path.read_text(encoding="utf-8"))
            assert report["cohort"]==f"{source}-dev" and report["epoch"]==epoch
            checkpoint(ROOT,epoch,"dev")
            current_rankings=rankings(ROOT,source,"dev",epoch)
            qrels=ROOT/"qrels/final-v1"/f"{source}-dev"
            verify_qrels(qrels)
            checked_file(qrels/"qrels.jsonl",report["qrels_sha256"])
            checked_file(path.parent/"per-query.jsonl",report["per_query_sha256"])
            expected={q["query_id"] for q in rows(ROOT/"queries.selected.jsonl") if q["source"]==source and q["split"]=="dev"}
            actual=[r for r in rows(path.parent/"per-query.jsonl") if r["method"]=="trained_ce"]
            labels=defaultdict(dict)
            for row in rows(qrels/"qrels.jsonl"):labels[row["query_id"]][row["document_id"]]=row["grade"]
            for row in actual:
                expected_metric=query_metrics(current_rankings["trained_ce"][row["query_id"]],labels[row["query_id"]])
                if any(row[k]!=v for k,v in expected_metric.items()):raise ValueError("Per-query metrics do not reproduce from bound scores/qrels")
            if len(actual)!=20 or {r["query_id"] for r in actual}!=expected or set(current_rankings["trained_ce"])!=expected:raise ValueError("Development query set incomplete")
            item=report["means"]["trained_ce"]["ndcg_lower_bound_at_10"]
            values_now=[r["ndcg_lower_bound_at_10"] for r in actual if r["ndcg_lower_bound_at_10"] is not None]
            if len(values_now)!=item["eligible_queries"] or abs(sum(values_now)/len(values_now)-item["mean"])>1e-12:raise ValueError("Reported selection mean does not match per-query evidence")
            if item["mean"] is None:raise ValueError("Development source has no evaluable queries")
            values.append(item["mean"])
            evidence.append({"epoch":epoch,"source":source,"path":str(path),"sha256":digest(path),
                             "qrels_sha256":report["qrels_sha256"],"eligible_queries":item["eligible_queries"]})
        options.append({"epoch":epoch,"equal_source_mean_ndcg_lower_bound":sum(values)/len(values)})
    for source in ("kuaisearch","multicpr"):
        if len({e["qrels_sha256"] for e in evidence if e["source"]==source})!=1:
            raise ValueError("Candidate checkpoints were evaluated against different dev qrels")
    chosen=max(options,key=lambda r:(r["equal_source_mean_ndcg_lower_bound"],-r["epoch"]))
    model=ROOT/"training/run-lora-v1"/f"epoch-{chosen['epoch']}/adapter_model.safetensors"
    result={**chosen,"model_sha256":digest(model),"model_path":str(model.parent),
            "policy_sha256":digest(ROOT/"evaluation/selection-policy.json"),"candidates":options,"dev_evidence":evidence,
            "test_read":False,"production_switch_authorized":False}
    destination=ROOT/"evaluation/selected-checkpoint.json"
    if destination.exists() and json.loads(destination.read_text(encoding="utf-8"))!=result:
        raise ValueError("Refusing to change the selected checkpoint after freezing")
    dump(destination,result)
    log("checkpoint_selected",epoch=chosen["epoch"],test_read=False)

if __name__=="__main__":select()
