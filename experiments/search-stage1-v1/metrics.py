"""Pooled relevance metrics that keep UNKNOWN distinct from judged zero."""
from __future__ import annotations

import math


def dcg(grades, k=10):
    return sum((2**g-1)/math.log2(i+2) for i,g in enumerate(grades[:k]))


def query_metrics(ranking, judgments, k=10):
    if len(ranking)!=len(set(ranking)): raise ValueError("Duplicate ranked document")
    if any(not (type(g) is int and 0<=g<=3 or g=="UNKNOWN") for g in judgments.values()):
        raise ValueError("Invalid qrel")
    top=ranking[:k]
    known={d:g for d,g in judgments.items() if type(g) is int}
    unknown={d for d,g in judgments.items() if g=="UNKNOWN"}
    missing={d for d in top if d not in judgments}
    # These are explicit extremal assignments for numerical BOUNDS only.
    # They never write unknown/outside-pool documents back as grade-zero qrels.
    minimum=[known.get(d,0) for d in top]
    maximum=[known[d] if d in known else 3 for d in top]
    ideal_known=dcg(sorted(known.values(),reverse=True),k)
    ideal_possible=dcg(sorted([*known.values(),*([3]*(len(unknown)+len(missing)))],reverse=True),k)
    lower=dcg(minimum,k)/ideal_possible if ideal_possible else None
    upper=min(1.0,dcg(maximum,k)/ideal_known) if ideal_known else (1.0 if ideal_possible else None)
    complete=not unknown and not missing
    relevant={d for d,g in known.items() if g>=2}
    full={d for d,g in known.items() if g==3}
    return {"ndcg_at_10":dcg(minimum,k)/ideal_known if complete and ideal_known else None,
        "ndcg_lower_bound_at_10":lower,"ndcg_upper_bound_at_10":upper,
        "judged_at_10":sum(d in known for d in top)/k,"returned_at_10":len(top),
        "unknown_top10":sum(d in unknown for d in top),"outside_pool_top10":len(missing),
        "known_relevant_recall_at_10":len(set(top)&relevant)/len(relevant) if relevant else None,
        "known_relevant_recall_at_300":len(set(ranking[:300])&relevant)/len(relevant) if relevant else None,
        "known_fully_relevant_recall_at_10":len(set(top)&full)/len(full) if full else None,
        "known_relevant_count":len(relevant),"known_fully_relevant_count":len(full),
        "pool_unknown_count":len(unknown),"pool_judged_count":len(known),
        "scope":"Pooled silver qrels only; neither denominator nor bounds cover unseen corpus relevance"}


def paired_bootstrap(a,b,seed=20260909,repetitions=10000):
    import numpy as np
    keys=sorted(q for q in set(a)&set(b) if a[q] is not None and b[q] is not None)
    if not keys: return {"queries":0,"delta":None,"ci95":None}
    differences=np.array([b[q]-a[q] for q in keys],dtype=float)
    rng=np.random.default_rng(seed)
    samples=np.mean(differences[rng.integers(0,len(keys),size=(repetitions,len(keys)))],axis=1)
    return {"queries":len(keys),"delta":float(differences.mean()),"ci95":np.quantile(samples,[.025,.975]).tolist(),
            "seed":seed,"repetitions":repetitions,"orientation":"second minus first"}
