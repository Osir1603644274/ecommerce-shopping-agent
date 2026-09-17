import asyncio
from copy import deepcopy
import json
from pathlib import Path
import httpx
import pytest
from prepare_commerce_simulation import load_simulation_app

modules=load_simulation_app()
from app.domains.ecommerce import synthetic_prices as prices
from app.domains.ecommerce import tools as shop

ROOT=Path('D:/agent-datasets/search-closure-v1')
rows=[json.loads(x) for x in (ROOT/'commerce-simulated-prices-v1/products-with-simulated-prices.jsonl').read_text(encoding='utf-8').splitlines()]
PRODUCTS=[{k:v for k,v in p.items() if k!='simulatedOffer'} for p in rows]

def project(products=PRODUCTS,policy='budget_and_ranking'):
    return prices.apply_synthetic_prices(products,directory=str(prices.BUNDLE),policy=policy)

def test_all25_source_facts_unchanged_and_prices_canonical():
    output=project()
    for before,after,fixture in zip(PRODUCTS,output,rows):
        assert {k:v for k,v in after.items() if k!='syntheticReferencePrice'}==before
        assert prices.synthetic_price_value(after,allow_budget=True)[0]==fixture['simulatedOffer']['referencePriceMinor']
        assert prices.simulated_price_eligible(after)
        assert after['priceStatus']=='unverified' and after['snapshotPriceMinor'] is None

@pytest.mark.parametrize('field,value',[('referencePriceMinor',1),('productId','999'),('priceStatus','verified'),('rulesetSha256','0'*64),('priceManifestSha256','0'*64)])
def test_tampering_rejected(field,value):
    p=project([PRODUCTS[0]])[0];p['syntheticReferencePrice'][field]=value
    with pytest.raises(prices.SyntheticPriceRuntimeError):prices.synthetic_price_value(p,allow_budget=True)

def test_quote_cannot_be_reused_for_another_product():
    p,q=project(PRODUCTS[:2]);q['syntheticReferencePrice']=p['syntheticReferencePrice']
    with pytest.raises(prices.SyntheticPriceRuntimeError):prices.synthetic_price_value(q,allow_budget=True)

def test_display_only_and_missing_prices_are_not_eligible():
    p=project([PRODUCTS[0]],policy='display_only')[0]
    assert prices.synthetic_price_value(p,allow_budget=False)[0]>0
    assert not prices.simulated_price_eligible(p)
    q=deepcopy(PRODUCTS[0]);q['id']=999999999
    assert 'syntheticReferencePrice' not in project([q])[0]
    assert not prices.simulated_price_eligible(q)

def test_verified_real_price_is_never_shadowed():
    p=deepcopy(PRODUCTS[0]);p.update(priceStatus='verified',snapshotPriceMinor=123456)
    assert project([p])==[p]

@pytest.mark.parametrize('policy',['disabled','display_only','budget_and_ranking'])
def test_actual_search_gate_keeps_stock_and_price_boundaries(monkeypatch,policy):
    valid=deepcopy(PRODUCTS[0]);sold=deepcopy(PRODUCTS[1]);sold['availableQuantity']=0
    unknown=deepcopy(PRODUCTS[2]);unknown['id']=999999999
    candidates=[valid,sold,unknown]
    for key,value in {'product_retrieval_mode':'elasticsearch','product_cross_encoder_enabled':False,'product_title_reranker_enabled':False,'used_phone_synthetic_price_policy':policy,'used_phone_synthetic_price_dir':str(prices.BUNDLE),'ecommerce_guide_enabled':True}.items():monkeypatch.setattr(modules.settings,key,value)
    async def run():
        async def handler(request):
            if request.url.path.endswith('/retrieval'):
                return httpx.Response(200,json={'data':{'products':candidates,'channel':'elasticsearch','recallCount':3}})
            assert request.url.path.endswith('/resolve')
            ids=json.loads(request.content)['productIds']
            return httpx.Response(200,json={'data':[p for p in candidates if p['id'] in ids]})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            monkeypatch.setattr(shop,'_product_http_client',lambda:client)
            return await shop.search_products_tool('iPhone','手机',requirements=[])
    result=asyncio.run(run())
    if policy=='budget_and_ranking':
        assert result.ok,result.detail
        assert result.detail['candidatePoolIds']==[valid['id']]
        assert result.detail['candidates'][0]['facts']['priceStatus']=='synthetic'
    else:assert not result.ok
