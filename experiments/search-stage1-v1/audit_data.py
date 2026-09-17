"""Validate the actual frozen split, training labels and complete-corpus indices."""
from collections import Counter
import json
from pathlib import Path
import sqlite3

from data_prep import NearIndex
from retrieve import rows
from stage1 import digest,dump,key,log

ROOT=Path("D:/agent-datasets/search-stage1-v1")

def audit():
    queries=rows(ROOT/"queries.selected.jsonl")
    split=json.loads((ROOT/"split-report.json").read_text(encoding="utf-8"))
    assert digest(ROOT/"queries.selected.jsonl")==split["queries_sha256"]
    counts=Counter((q["source"],q["split"]) for q in queries)
    assert counts==Counter({("kuaisearch","dev"):20,("kuaisearch","test"):80,("multicpr","dev"):20,("multicpr","test"):80})
    old=json.loads((ROOT/"provenance/previous-query-exclusions.json").read_text(encoding="utf-8"))
    excluded=NearIndex()
    for value in old["query_keys"]:excluded.add(value)
    for query in queries:
        assert query["query_key"]==key(query["text"])
        assert not excluded.match(query["query_key"])
        excluded.add(query["query_key"])
    pairs=rows(ROOT/"training/kuaisearch.human.train.jsonl")
    for row in pairs:
        assert row["native_split"]=="train" and not excluded.match(row["query_key"])
        assert type(row["original_grade"]) is int and row["original_grade"] in range(4)
        assert row["target"]==int(row["original_grade"]==3)
    assert len({(r["query_key"],key(r["text"])) for r in pairs})==len(pairs)
    normalization=json.loads((ROOT/"normalization-report.json").read_text(encoding="utf-8"))
    assert normalization["sources_sha256"]==digest(ROOT/"sources.json")
    corpus=sqlite3.connect(f"file:{(ROOT/'catalog.sqlite').as_posix()}?mode=ro",uri=True)
    actual=dict(corpus.execute("SELECT source,count(*) FROM documents GROUP BY source"))
    expected={r["source"]:r["count"] for r in normalization["documents"]}
    assert actual==expected
    index_audit={}
    for source,count in actual.items():
        directory=ROOT/"indexes"/source
        index_audit[source]={}
        for name in ("lexical","dense"):
            path=directory/f"{name}.manifest.json"
            if not path.exists():
                index_audit[source][name]="INCOMPLETE";continue
            manifest=json.loads(path.read_text(encoding="utf-8"))
            binding=manifest.get("binding",manifest)
            assert binding["documents"]==count
            assert binding["normalization_sha256"]==digest(ROOT/"normalization-report.json")
            if name=="lexical":
                db=sqlite3.connect(f"file:{(directory/'lexical.sqlite').as_posix()}?mode=ro",uri=True)
                assert db.execute("SELECT count(*) FROM words").fetchone()[0]==count
                assert db.execute("SELECT count(*) FROM chars").fetchone()[0]==count
                db.close()
            else:
                import numpy as np
                vectors=np.load(directory/"embeddings.npy",mmap_mode="r")
                assert vectors.shape==(count,512) and vectors.dtype==np.float16
                # Deterministic distributed samples verify representation sanity;
                # the complete file SHA was computed by the embedding writer.
                sample=vectors[np.linspace(0,count-1,1000,dtype=int)].astype(np.float32)
                assert np.isfinite(sample).all()
                assert np.max(np.abs(np.linalg.norm(sample,axis=1)-1))<.002
                assert digest(directory/"embeddings.npy")==manifest["vectors_sha256"]
            artifact=directory/("lexical.sqlite" if name=="lexical" else "embeddings.npy")
            index_audit[source][name]={"status":"PASS","artifact_sha256":digest(artifact),"manifest_sha256":digest(path)}
    corpus.close()
    result={"split_counts":{str(k):v for k,v in counts.items()},"old_query_exclusion_scope":old["scope"],
        "old_query_keys":len(old["query_keys"]),"train_pairs":len(pairs),"training_grades":dict(Counter(r["original_grade"] for r in pairs)),
        "query_family_isolation":"PASS under the frozen normalization and trigram rule",
        "corpus_counts":actual,"catalog_sha256":digest(ROOT/"catalog.sqlite"),"indices":index_audit,"training_sha256":digest(ROOT/"training/kuaisearch.human.train.jsonl"),
        "split_sha256":digest(ROOT/"queries.selected.jsonl")}
    dump(ROOT/"data-audit.json",result)
    log("data_audit_complete",**result)

if __name__=="__main__":audit()
