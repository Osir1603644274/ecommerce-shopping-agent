import pytest
from evaluation import summarize


CASES=[{'id':'a','category':'refund','split':'dev'},{'id':'b','category':'exchange','split':'heldout'}]


def test_missing_failed_unjudged_stay_in_denominator_and_block_gates():
    rows=[{'caseId':'a','repetition':1,'verdict':'PASS','oracleEvidence':['sql-a.json'],'firstContentMs':8000},
          {'caseId':'a','repetition':2,'verdict':'UNJUDGED'},
          {'caseId':'b','repetition':1,'verdict':'FAIL','hardFailures':['unauthorized']}]
    result=summarize(CASES,rows)
    assert result['quality']['denominator']==6 and result['quality']['passed']==1
    assert result['quality']['missing']==3 and result['quality']['unjudged']==1
    assert not result['gates']['noHardFailure'] and not result['gates']['humanSupport95']
    assert not result['gates']['firstContentP95Within5s'] and len(result['failures'])==5


def test_failed_calls_count_and_unknown_usage_prevents_zero_cost_claim():
    known={'modelCallId':'k','status':'FAILED','usage':{'prompt_tokens':10,'completion_tokens':2},'costStatus':'LIST_PRICE_ESTIMATE','cost':'0.001'}
    unknown={'modelCallId':'u','status':'FAILED','usage':None,'cost':None}
    rows=[{'caseId':'a','repetition':1,'verdict':'FAIL','modelCalls':[known,unknown]},
          {'caseId':'a','repetition':2,'verdict':'FAIL','modelCalls':[known]}]
    result=summarize(CASES,rows)['usage']
    assert result['modelCalls']==2 and result['knownTokens']['prompt_tokens']==10
    assert result['estimatedTotalCostUSD'] is None and result['knownEstimatedCostUSD']=='0.001'


def test_model_success_cannot_substitute_for_external_verdict():
    with pytest.raises(ValueError):summarize(CASES,[{'caseId':'a','repetition':1,'success':True}])
    with pytest.raises(ValueError):summarize(CASES,[{'caseId':'a','repetition':1,'verdict':'PASS'}])
    row={'caseId':'a','repetition':1,'verdict':'FAIL'}
    with pytest.raises(ValueError):summarize(CASES,[row,row])


def test_all_automatic_checks_are_not_proof_of_critical_fact_accuracy():
    rows=[{'caseId':c['id'],'repetition':1,'verdict':'PASS','oracleEvidence':['sql.json'],
           'criticalAssertions':{'total':10,'passed':10,'complete':True}} for c in CASES]
    result=summarize(CASES,rows,repetitions=1)
    assert result['oracleChecks']['passed']==20
    assert result['criticalFacts']['rate'] is None
    assert not result['gates']['criticalFacts100']
    for row in rows:
        row['independentFactReview']={'status':'REVIEWED','reviewerType':'AGENT','sourceReviewSha256':'a'*64,
            'complete':True,'criticalTotal':2,'criticalSupported':2}
    assert summarize(CASES,rows,repetitions=1)['gates']['criticalFacts100']
    rows[0]['independentFactReview']['criticalSupported']=1
    assert not summarize(CASES,rows,repetitions=1)['gates']['criticalFacts100']


def test_control_cost_is_separate_but_included_in_executed_total():
    known={'modelCallId':'customer','usage':{'prompt_tokens':10,'completion_tokens':2},
           'costStatus':'LIST_PRICE_ESTIMATE','cost':'0.001'}
    extra={**known,'modelCallId':'control','cost':'0.002'}
    row={'caseId':'a','repetition':1,'verdict':'FAIL','modelCalls':[known],'meteringComplete':True,
         'evaluationControlMetering':{'modelCalls':[extra],'meteringComplete':True}}
    result=summarize(CASES[:1],[row],repetitions=1)
    assert result['usage']['estimatedTotalCostUSD']=='0.001'
    assert result['evaluationControlUsage']['estimatedTotalCostUSD']=='0.002'
    assert result['allExecutedModelUsage']['estimatedTotalCostUSD']=='0.003'
    extra['usage']=None;extra['cost']=None
    assert summarize(CASES[:1],[row],repetitions=1)['allExecutedModelUsage']['estimatedTotalCostUSD'] is None


def test_agent_review_does_not_count_as_human_review():
    row={'caseId':'a','repetition':1,'verdict':'FAIL','agentReview':{'status':'REVIEWED',
         'reviewerType':'AGENT','assertions':20,'supported':19,'unknown':1}}
    result=summarize(CASES[:1],[row],repetitions=1)
    assert result['agentReviewGates']['agentSupport95']
    assert result['agentGrounding']['unknown']==1
    assert result['humanGrounding']['reviewedRuns']==0 and not result['gates']['humanSupport95']
    assert not result['acceptanceGates']['independentSupport95']
    selected=summarize(CASES[:1],[row],repetitions=1,required_review_method='AGENT')
    assert selected['acceptanceGates']['independentSupport95']
    assert not selected['acceptanceGates']['threeRepetitions']
    assert not selected['gates']['humanSupport95']
