"""Frozen candidate-set scoring and explicit pooled-unknown evaluation."""
from __future__ import annotations

import argparse
import gc
import json
import time
from collections import defaultdict
from pathlib import Path

from metrics import paired_bootstrap,query_metrics
from retrieve import CrossEncoder,DEPTH,frozen_jsonl,rows
from stage1 import digest,dump,log
from integrity import checked_file,verify_checkpoint,verify_model,verify_qrels,inference_binding,verify_score_state
from retrieval_judgment_pool_core import PoolService


def checkpoint(root,epoch,split_name):
    directory=root/"training/run-lora-v1"/f"epoch-{epoch}"
    receipt=verify_checkpoint(directory,digest(root/"training/run-lora-v1/config.json"))
    verify_model(json.loads((root/"models/manifest.json").read_text(encoding="utf-8-sig"))["reranker"])
    if split_name=="test":
        selected=json.loads((root/"evaluation/selected-checkpoint.json").read_text(encoding="utf-8-sig"))
        checked_file(root/"evaluation/selection-policy.json",selected["policy_sha256"])
        for item in selected["dev_evidence"]:checked_file(item["path"],item["sha256"])
        if selected["epoch"]!=epoch or selected["model_sha256"]!=receipt["model_sha256"]:
            raise ValueError("Only the dev-selected checkpoint may score test")
    return directory,receipt


def score(root,source,split_name,epoch):
    model_path,model_receipt=checkpoint(root,epoch,split_name)
    cohort=f"{source}-{split_name}"
    pool_receipt=json.loads((root/"pools"/f"{cohort}.json").read_text(encoding="utf-8-sig"))
    run_dir=Path(pool_receipt["run_dir"])
    PoolService(root/"pools/inputs",root/"pools/runs").verify_run(pool_receipt["run_id"])
    analysis=json.loads((run_dir/"analysis.json").read_text(encoding="utf-8"))
    dataset=root/"pools/inputs"/cohort
    docs={d["document_id"]:d for d in rows(dataset/"documents.jsonl")}
    queries={q["query_id"]:q for q in rows(dataset/"queries.jsonl")}
    destination=root/"evaluation/scores"/cohort/f"epoch-{epoch}"
    destination.mkdir(parents=True,exist_ok=True)
    binding={"model_sha256":model_receipt["model_sha256"],"analysis_sha256":digest(run_dir/"analysis.json"),
        "documents_sha256":digest(dataset/"documents.jsonl"),"queries_sha256":digest(dataset/"queries.jsonl"),"max_length":256,"candidate_budget":DEPTH}
    if (destination/"complete.json").exists():
        prior=json.loads((destination/"complete.json").read_text(encoding="utf-8-sig"))
        if prior["binding"]!=binding or digest(destination/"scores.jsonl")!=prior["scores_sha256"]:raise ValueError("Scoring resume drift")
        verify_score_state(destination,model_path)
        log("checkpoint_scores_reuse",cohort=cohort,epoch=epoch);return
    encoder=CrossEncoder(str(model_path))
    result,timings=[],[]
    for query in analysis["queries"]:
        qid=query["query_id"]
        candidates=query["rrf_ranking"][:DEPTH]
        pairs=[[queries[qid]["text"],docs[d["document_id"]]["text"]] for d in candidates]
        encoder.torch.cuda.synchronize();started=time.perf_counter()
        scores=encoder.score(pairs)
        encoder.torch.cuda.synchronize()
        timings.append({"query_id":qid,"pairs":len(pairs),"seconds":time.perf_counter()-started})
        result.extend({"query_id":qid,"document_id":d["document_id"],"score":s} for d,s in zip(candidates,scores))
        log("checkpoint_query_scored",cohort=cohort,epoch=epoch,query_id=qid)
    frozen_jsonl(destination/"scores.jsonl",result)
    frozen_jsonl(destination/"timings.jsonl",timings)
    dump(destination/"inference-receipt.json",{"inference":inference_binding(model_path),"scores_sha256":digest(destination/"scores.jsonl"),"capture":"Scoring completion"})
    dump(destination/"complete.json",{"binding":binding,"scores_sha256":digest(destination/"scores.jsonl"),
         "timings_sha256":digest(destination/"timings.jsonl"),"labels_read":False})


def rankings(root,source,split_name,epoch):
    checkpoint(root,epoch,split_name)
    cohort=f"{source}-{split_name}"
    receipt=json.loads((root/"pools"/f"{cohort}.json").read_text(encoding="utf-8-sig"))
    PoolService(root/"pools/inputs",root/"pools/runs").verify_run(receipt["run_id"])
    analysis=json.loads((Path(receipt["run_dir"])/"analysis.json").read_text(encoding="utf-8"))
    result={method:defaultdict(list) for method in ("bm25","character","dense","rrf","base_ce","trained_ce")}
    for method,family in (("bm25","lexical"),("character","character"),("dense","dense")):
        for row in rows(Path(receipt["run_dir"])/"runs"/f"{family}.jsonl"):
            result[method][row["query_id"]].append((row["rank"],row["document_id"]))
        result[method]={q:[d for _,d in sorted(values)] for q,values in result[method].items()}
    result["rrf"]={q["query_id"]:[d["document_id"] for d in q["rrf_ranking"][:DEPTH]] for q in analysis["queries"]}
    trained_path=root/"evaluation/scores"/cohort/f"epoch-{epoch}"
    scored_receipt=json.loads((trained_path/"complete.json").read_text(encoding="utf-8-sig"))
    verify_score_state(trained_path,root/"training/run-lora-v1"/f"epoch-{epoch}")
    model_info=json.loads((root/"models/manifest.json").read_text(encoding="utf-8-sig"))["reranker"]
    verify_score_state(root/"pools/inputs"/cohort,model_info["path"],"ce.jsonl")
    checked_file(trained_path/"scores.jsonl",scored_receipt["scores_sha256"])
    expected_binding={"model_sha256":verify_checkpoint(root/"training/run-lora-v1"/f"epoch-{epoch}")["model_sha256"],
        "analysis_sha256":digest(Path(receipt["run_dir"])/"analysis.json"),
        "documents_sha256":digest(root/"pools/inputs"/cohort/"documents.jsonl"),
        "queries_sha256":digest(root/"pools/inputs"/cohort/"queries.jsonl"),"max_length":256,"candidate_budget":DEPTH}
    if scored_receipt["binding"]!=expected_binding:raise ValueError("Score inputs/model binding drift")
    for method,path in (("base_ce",Path(receipt["run_dir"])/"reranker_scores.jsonl"),
                        ("trained_ce",root/"evaluation/scores"/cohort/f"epoch-{epoch}/scores.jsonl")):
        for row in rows(path):
            if method=="base_ce" and row["status"]!="SCORED":continue
            result[method][row["query_id"]].append((-row["score"],row["document_id"]))
        result[method]={q:[d for _,d in sorted(values)] for q,values in result[method].items()}
    for qid,rrf in result["rrf"].items():
        if set(rrf)!=set(result["base_ce"][qid]) or set(rrf)!=set(result["trained_ce"][qid]):
            raise ValueError("Before/after CE candidate sets differ")
    return result


def needed_topups(root,source,split_name,epochs):
    cohort=f"{source}-{split_name}"
    existing={(r["original_query_id"],r["original_document_id"]) for r in rows(root/"labeling/provenance"/f"{cohort}.mapping.jsonl")}
    missing=defaultdict(set)
    for epoch in epochs:
        for method,queries in rankings(root,source,split_name,epoch).items():
            for qid,ranking in queries.items():
                for did in ranking[:10]:
                    if (qid,did) not in existing:missing[(qid,did)].add(f"{method}:epoch-{epoch}")
    result=[{"query_id":q,"document_id":d,"required_by":sorted(methods)} for (q,d),methods in sorted(missing.items())]
    destination=root/"evaluation/topup"/cohort
    frozen_jsonl(destination/"required.jsonl",result)
    dump(destination/"report.json",{"cohort":cohort,"epochs":epochs,"pairs":len(result),"queries":len({r["query_id"] for r in result}),
         "policy":"Dev covers every candidate checkpoint top 10 before selection; test covers only the already-selected checkpoint."})
    log("topups_required",cohort=cohort,pairs=len(result))


def evaluate(root,source,split_name,epoch,qrels_dir):
    checkpoint(root,epoch,split_name)
    cohort=f"{source}-{split_name}"
    labels=defaultdict(dict)
    verify_qrels(qrels_dir)
    for row in rows(qrels_dir/"qrels.jsonl"):
        if row["document_id"] in labels[row["query_id"]]:raise ValueError("Duplicate qrel")
        labels[row["query_id"]][row["document_id"]]=row["grade"]
    all_rankings=rankings(root,source,split_name,epoch)
    records=[]
    for method,queries in all_rankings.items():
        for qid,ranking in queries.items():
            if qid not in labels:raise ValueError("Query has no qrels")
            metric=query_metrics(ranking,labels[qid])
            if metric["outside_pool_top10"]:raise ValueError("Final top 10 has unpooled documents; supplement and judge first")
            records.append({"query_id":qid,"method":method,**metric})
    destination=root/"evaluation/results"/cohort/f"epoch-{epoch}"
    frozen_jsonl(destination/"per-query.jsonl",records)
    means={}
    for method in all_rankings:
        selected=[r for r in records if r["method"]==method]
        means[method]={}
        for metric in ("ndcg_at_10","ndcg_lower_bound_at_10","ndcg_upper_bound_at_10","judged_at_10","known_relevant_recall_at_10","known_relevant_recall_at_300"):
            values=[r[metric] for r in selected if r[metric] is not None]
            means[method][metric]={"mean":sum(values)/len(values) if values else None,"eligible_queries":len(values),"total_queries":len(selected)}
    comparisons={}
    for metric in ("ndcg_at_10","ndcg_lower_bound_at_10","judged_at_10"):
        before={r["query_id"]:r[metric] for r in records if r["method"]=="base_ce"}
        after={r["query_id"]:r[metric] for r in records if r["method"]=="trained_ce"}
        comparisons[metric]=paired_bootstrap(before,after)
    dump(destination/"report.json",{"cohort":cohort,"epoch":epoch,"qrels_sha256":digest(qrels_dir/"qrels.jsonl"),
         "per_query_sha256":digest(destination/"per-query.jsonl"),"means":means,"paired_before_after":comparisons,
         "labels":"Model silver; UNKNOWN kept separate. Point nDCG only for completely judged pools; otherwise explicit conservative pooled bounds.",
         "recall":"Denominator contains only known pooled relevant documents, never claimed as corpus-wide recall."})
    log("evaluation_complete",cohort=cohort,epoch=epoch)


if __name__=="__main__":
    p=argparse.ArgumentParser()
    p.add_argument("--root",type=Path,default=Path("D:/agent-datasets/search-stage1-v1"))
    p.add_argument("command",choices=("score","topups","evaluate"))
    p.add_argument("--source",required=True,choices=("kuaisearch","multicpr"))
    p.add_argument("--split",required=True,choices=("dev","test"))
    p.add_argument("--epoch",type=int,choices=(1,2,3))
    p.add_argument("--qrels-dir",type=Path)
    a=p.parse_args()
    if a.command=="topups":
        epochs=[1,2,3] if a.split=="dev" else [a.epoch]
        if None in epochs:p.error("test topups requires selected --epoch")
        needed_topups(a.root,a.source,a.split,epochs)
    elif a.command=="evaluate":
        if not a.epoch or not a.qrels_dir:p.error("evaluate requires epoch and qrels-dir")
        evaluate(a.root,a.source,a.split,a.epoch,a.qrels_dir)
    else:
        if not a.epoch:p.error("score requires epoch")
        score(a.root,a.source,a.split,a.epoch)
