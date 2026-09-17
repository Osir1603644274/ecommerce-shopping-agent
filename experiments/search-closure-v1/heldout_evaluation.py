"""All-main heldout orchestration using the unchanged frozen metric functions.

The development wrapper assumes both main and diagnostic cohorts exist. The
registered heldout split has only main queries; no dummy diagnostic rows are added.
"""
import baseline_eval as original

def evaluate(queries,rankings,labels,causes,*,repetitions=10000,seed=20260909,baseline='baseline'):
    original.require(queries and all(q['query_cohort']=='main' for q in queries.values()),'Heldout wrapper requires nonempty all-main queries')
    original.require(baseline in rankings and set(labels)==set(queries),'Heldout method/label coverage differs')
    original.require(all(set(r)==set(queries) for r in rankings.values()),'Heldout ranking coverage differs')
    common={qid:original.build_common_pool(labels[qid],{m:rankings[m][qid] for m in rankings}) for qid in queries}
    records=[{**queries[qid],'method':method,'ranking_sha256':original.canonical_hash(rankings[method][qid]),
        **original.query_metrics(rankings[method][qid],labels[qid],common_pool=common[qid],unknown_causes=causes[qid])}
        for method in sorted(rankings) for qid in sorted(queries)]
    eligibility=original.shared_eligibility(records,sorted(rankings));common_ids=set(eligibility['eligible_query_ids'])
    summary={m:original.aggregate([r for r in records if r['method']==m]) for m in sorted(rankings)}
    def comparisons(selected):
        base={r['query_id']:r for r in selected if r['method']==baseline}
        return {m:original.paired_interval_bootstrap(base,{r['query_id']:r for r in selected if r['method']==m},
            repetitions=repetitions,seed=seed) for m in sorted(rankings) if m!=baseline}
    compared=comparisons(records)
    conditional={'status':eligibility['status'],'eligibility_manifest_sha256':eligibility['manifest_sha256'],
        'query_ids':sorted(common_ids),'methods':{},'comparisons':{}}
    if eligibility['status']!='NO_VALID_SELECTION':
        subset=[r for r in records if r['query_id'] in common_ids]
        conditional['methods']={m:original.aggregate([r for r in subset if r['method']==m]) for m in sorted(rankings)}
        conditional['comparisons']=comparisons(subset)
    # All and main have exactly the same original rows. Reuse identical results,
    # preserving every query, undefined metric and paired-bootstrap definition.
    return {'per_query':records,'summary':{'main':summary,'all':summary},
        'comparisons':{'main':compared,'all':compared},'shared_eligibility':{'main':eligibility,'all':eligibility},
        'common_conditional':{'main':conditional,'all':conditional},
        'common_pools':[{'query_id':qid,'document_ids':common[qid],'sha256':original.canonical_hash(common[qid])} for qid in sorted(common)]}
