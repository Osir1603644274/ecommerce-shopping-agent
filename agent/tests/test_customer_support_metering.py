from app.customer_support.metering import estimate

URL = 'https://api.deepseek.com'


def receipt(**changes):
    return {'resolvedModel':'deepseek-flash','startedAt':'2026-09-18T23:00:00+00:00','finishedAt':'2026-09-18T23:00:02+00:00',
            'usage':{'prompt_tokens':1000,'completion_tokens':100,'total_tokens':1100,'prompt_cache_hit_tokens':800,'prompt_cache_miss_tokens':200},**changes}


def test_known_cache_usage_is_exact_and_failed_attempt_is_still_charged():
    assert estimate(receipt(status='FAILED'),URL)['cost']=='0.0000924'


def test_peak_weekday_and_weekend_use_distinct_rates():
    peak=receipt(startedAt='2026-09-18T01:00:00+00:00',finishedAt='2026-09-18T01:00:01+00:00')
    weekend=receipt(startedAt='2026-09-19T01:00:00+00:00',finishedAt='2026-09-19T01:00:01+00:00')
    assert estimate(peak,URL)['cost']=='0.0001848'
    assert estimate(weekend,URL)['cost']=='0.0000924'


def test_unknown_and_invalid_usage_are_not_free_calls():
    for usage in (None,{}, {'prompt_tokens':True,'completion_tokens':0}):
        result=estimate(receipt(usage=usage),URL)
        assert result['cost'] is None and result['costStatus']=='UNKNOWN_USAGE'
    invalid=receipt();invalid['usage']['prompt_cache_miss_tokens']=300
    assert estimate(invalid,URL)['costStatus']=='INCONSISTENT_USAGE'


def test_missing_cache_split_preserves_range_and_unknown_total():
    result=estimate(receipt(usage={'prompt_tokens':1000,'completion_tokens':100}),URL)
    assert result['cost'] is None and result['costRange']==['0.000063','0.00021']


def test_price_boundary_and_other_provider_cannot_claim_exact_cost():
    row=receipt(startedAt='2026-09-18T00:59:59+00:00',finishedAt='2026-09-18T01:00:01+00:00')
    assert estimate(row,URL)['costStatus']=='PRICE_WINDOW_AMBIGUOUS'
    assert estimate(receipt(),'https://other.example')['costStatus']=='UNVERIFIED_PROVIDER_PRICING'
    assert estimate(receipt(resolvedModel='unknown'),URL)['costStatus']=='UNKNOWN_BILLED_MODEL'
