"""Recompute report values from frozen ranked IDs and qrels before publication."""
from metrics import query_metrics,paired_bootstrap

METRICS=("ndcg_at_10","ndcg_lower_bound_at_10","ndcg_upper_bound_at_10","judged_at_10","known_relevant_recall_at_10","known_relevant_recall_at_300")

def summaries(records,methods):
    means={}
    for method in methods:
        selected=[r for r in records if r["method"]==method]
        means[method]={}
        for metric in METRICS:
            values=[r[metric] for r in selected if r[metric] is not None]
            means[method][metric]={"mean":sum(values)/len(values) if values else None,"eligible_queries":len(values),"total_queries":len(selected)}
    comparisons={}
    for metric in ("ndcg_at_10","ndcg_lower_bound_at_10","judged_at_10"):
        before={r["query_id"]:r[metric] for r in records if r["method"]=="base_ce"}
        after={r["query_id"]:r[metric] for r in records if r["method"]=="trained_ce"}
        comparisons[metric]=paired_bootstrap(before,after)
    return means,comparisons

def validate_report(report,records,rankings,labels):
    expected={(method,qid):{"method":method,"query_id":qid,**query_metrics(ranking,labels[qid])} for method,queries in rankings.items() for qid,ranking in queries.items()}
    actual={(r["method"],r["query_id"]):r for r in records}
    if len(actual)!=len(records) or actual!=expected:raise ValueError("Per-query report does not reproduce from scores/qrels")
    means,comparisons=summaries(records,rankings)
    if report["means"]!=means or report["paired_before_after"]!=comparisons:raise ValueError("Report means or bootstrap CI does not reproduce")
