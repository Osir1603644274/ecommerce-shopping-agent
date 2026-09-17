from copy import deepcopy
import baseline_eval as original
import heldout_evaluation
from test_evaluate_final_test import sample
from evaluate_final_test import load_test_data

def test_all80_main_end_to_end_retains_undefined_and_unknown():
    q,r,l,c=load_test_data(*sample())
    zero='kuaisearch0'
    l[zero]={d:0 for d in l[zero]};c[zero]={}
    result=heldout_evaluation.evaluate(q,r,l,c,repetitions=100,baseline='baseline')
    assert len(result['per_query'])==160 and {x['query_id'] for x in result['per_query']}==set(q)
    assert all(x['ndcg_at_10'] is None for x in result['per_query'] if x['query_id']==zero)
    assert zero not in result['common_conditional']['main']['query_ids']
    assert any(x['unknown_top10'] for x in result['per_query'])
    assert result['summary']['all']==result['summary']['main'] and 'diagnostic' not in result['summary']

def test_main_results_exactly_match_unchanged_development_metric_pipeline():
    q,r,l,c=load_test_data(*sample())
    ids=['kuaisearch1','multicpr1'];q={k:q[k] for k in ids};r={m:{k:v[k] for k in ids} for m,v in r.items()};l={k:l[k] for k in ids};c={k:c[k] for k in ids}
    actual=heldout_evaluation.evaluate(q,r,l,c,repetitions=100,baseline='baseline')
    mixed_q,mixed_r,mixed_l,mixed_c=deepcopy((q,r,l,c))
    for k in ids:
        extra=k+'-diagnostic';mixed_q[extra]={**q[k],'query_id':extra,'query_cohort':'diagnostic'}
        mixed_l[extra]=l[k];mixed_c[extra]=c[k]
        for m in r:mixed_r[m][extra]=r[m][k]
    expected=original.evaluate(mixed_q,mixed_r,mixed_l,mixed_c,repetitions=100,baseline='baseline')
    for field in ['summary','comparisons','shared_eligibility','common_conditional']:
        assert actual[field]['main']==expected[field]['main']
    assert actual['per_query']==[x for x in expected['per_query'] if x['query_cohort']=='main']
