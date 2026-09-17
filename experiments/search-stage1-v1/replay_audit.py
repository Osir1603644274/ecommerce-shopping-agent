"""Replay a fixed first dev query per source against existing score bytes."""
import json,gc
from pathlib import Path
from retrieve import rows,CrossEncoder
from stage1 import dump,digest,log
from integrity import inference_binding

ROOT=Path("D:/agent-datasets/search-stage1-v1")
def replay():
    info=json.loads((ROOT/"models/manifest.json").read_text(encoding="utf-8-sig"))["reranker"]
    records=[]
    for epoch in (0,1,2,3):
        model=info["path"] if epoch==0 else ROOT/"training/run-lora-v1"/f"epoch-{epoch}"
        encoder=CrossEncoder(str(model))
        for source in ("kuaisearch","multicpr"):
            cohort=source+"-dev"
            receipt=json.loads((ROOT/"pools"/f"{cohort}.json").read_text(encoding="utf-8-sig"))
            query=json.loads((Path(receipt["run_dir"])/"analysis.json").read_text(encoding="utf-8-sig"))["queries"][0]
            path=ROOT/"pools/inputs"/cohort
            docs={d["document_id"]:d for d in rows(path/"documents.jsonl")}
            queries={q["query_id"]:q for q in rows(path/"queries.jsonl")}
            candidates=query["rrf_ranking"][:300]
            pairs=[[queries[query["query_id"]]["text"],docs[d["document_id"]]["text"]] for d in candidates]
            score_path=path/"ce.jsonl" if epoch==0 else ROOT/"evaluation/scores"/cohort/f"epoch-{epoch}/scores.jsonl"
            expected={r["document_id"]:r["score"] for r in rows(score_path) if r["query_id"]==query["query_id"]}
            actual=encoder.score(pairs)
            error=max(abs(s-expected[d["document_id"]]) for s,d in zip(actual,candidates))
            if error>1e-5:raise ValueError(f"Replay drift {source} epoch {epoch}: {error}")
            records.append({"source":source,"epoch":epoch,"query_id":query["query_id"],"pairs":len(pairs),"max_abs_error":error,"scores_sha256":digest(score_path),"inference":inference_binding(model)})
            log("score_replay_pass",source=source,epoch=epoch,error=error)
        torch=encoder.torch
        del encoder
        gc.collect();torch.cuda.empty_cache()
    dump(ROOT/"provenance/inference-replay-audit.json",{"status":"PASS","pairs":2400,"records":records,"scope":"First dev query of each source, every checkpoint and baseline; not a complete replay"})
if __name__=="__main__":replay()
