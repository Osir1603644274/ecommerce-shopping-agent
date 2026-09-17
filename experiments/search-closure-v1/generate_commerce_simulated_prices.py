"""Generate user-authorized random prices for the exact 25 commerce candidates."""
from pathlib import Path
from random import Random
from copy import deepcopy
import json
from retrieval_runtime import read_json, sha, write_once

ROOT=Path('D:/agent-datasets/search-closure-v1')
SEED=20260910
LOW_CNY=500
HIGH_CNY=6000

def build(products):
    assert len(products)==25 and len({p['id'] for p in products})==25
    assert all(type(p['id']) is int and p['id']>0 for p in products)
    rng=Random(SEED);prices=[];projected=[]
    for product in sorted(products,key=lambda p:p['id']):
        offer={'schemaVersion':'search-commerce-random-simulated-price-v1',
            'productId':str(product['id']),'referencePriceMinor':rng.randint(LOW_CNY,HIGH_CNY)*100,
            'currency':'CNY','minorUnit':'fen','dataNature':'synthetic','priceStatus':'synthetic',
            'priceKind':'local_simulated','seed':SEED,
            'generationRule':'uniform_integer_CNY_500_through_6000_inclusive_sorted_product_id',
            'disclosureZh':'随机模拟价格，仅用于实验，非真实报价'}
        prices.append(offer)
        copied=deepcopy(product);assert 'simulatedOffer' not in copied
        copied['simulatedOffer']=offer
        assert {k:v for k,v in copied.items() if k!='simulatedOffer'}==product
        projected.append(copied)
    return prices,projected

def main():
    source=ROOT/'commerce-prerequisite-audit-v1/live-authority-recheck-002'
    report=read_json(source/'REPORT.json');products=[]
    assert report['returned']==25 and len(report['bindings'])==3
    for binding in report['bindings']:
        path=Path(binding['path']);assert sha(path)==binding['sha256']
        products.extend(read_json(path)['data'])
    assert all(p['priceStatus']=='unverified' and p['snapshotPriceMinor'] is None for p in products)
    prices,projected=build(products)
    assert build(list(reversed(products)))==(prices,projected)
    out=ROOT/'commerce-simulated-prices-v1'
    write_once(out/'prices.jsonl',prices,jsonl=True)
    write_once(out/'products-with-simulated-prices.jsonl',projected,jsonl=True)
    lines=['# 25个商城候选的随机模拟价格','',
        '用户授权随机合成。固定种子20260910；按商品ID排序后，在500～6000元之间均匀抽取整数元，文件以人民币分存储。',
        '全部标记为模拟价格。原始商品价格状态仍为unverified，snapshotPriceMinor仍为空；模拟报价放在simulatedOffer字段。',
        '本包只生成实验数据，尚未导入商城数据库或接入检索运行时，不使用旧合成价格包的身份或规则签名。',
        '', '| 商品ID | 商品原始标题 | 模拟价（元） |', '|---|---|---:|']
    for p in projected:
        title=str(p.get('title','')).replace('|','\\|').replace('\n',' ')
        lines.append(f'| {p["id"]} | {title} | {p["simulatedOffer"]["referencePriceMinor"]/100:.2f} |')
    raw='\n'.join(lines)+'\n';md=out/'prices.md'
    if md.exists():assert md.read_text(encoding='utf-8')==raw
    else:md.write_text(raw,encoding='utf-8')
    manifest={'status':'25_USER_AUTHORIZED_SIMULATED_PRICES_GENERATED','rowCount':25,
        'authorization':'那就 直接把剩下的二十五个价格随机合成吧。',
        'seed':SEED,'currency':'CNY','storageUnit':'fen','rangeCNY':[LOW_CNY,HIGH_CNY],
        'actualRangeCNY':[min(p['referencePriceMinor'] for p in prices)//100,max(p['referencePriceMinor'] for p in prices)//100],
        'dataNature':'synthetic','priceStatus':'synthetic','original_price_fields_preserved':True,
        'ids_serialized_as_strings_in_price_sidecar':True,'database_updated':False,'runtime_integrated':False,
        'frozen_search_or_agent_runs_modified':False,'model_calls':0,
        'source_report':{'path':str(source/'REPORT.json'),'sha256':sha(source/'REPORT.json')},
        'source_responses':report['bindings'],'generator_sha256':sha(Path(__file__)),
        'outputs':{name:sha(out/name) for name in ['prices.jsonl','products-with-simulated-prices.jsonl','prices.md']}}
    write_once(out/'MANIFEST.json',manifest)
    for name,digest in manifest['outputs'].items():assert sha(out/name)==digest
    for name in ['prices.jsonl','products-with-simulated-prices.jsonl']:
        assert len([json.loads(s) for s in (out/name).read_text(encoding='utf-8').splitlines()])==25
    print(json.dumps({'status':manifest['status'],'count':25,'actual_range_cny':manifest['actualRangeCNY'],
        'manifest_sha256':sha(out/'MANIFEST.json'),'directory':str(out),'database_updated':False},ensure_ascii=False))

if __name__=='__main__':main()
