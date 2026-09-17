"""Explicit, hash-bound 25-product simulation extension for an isolated app copy."""
from copy import deepcopy
from functools import lru_cache
from pathlib import Path
import hashlib
import json
from . import _baseline_synthetic_prices as baseline
from ._baseline_synthetic_prices import *

BUNDLE=Path('D:/agent-datasets/search-closure-v1/commerce-simulated-prices-v1')
MANIFEST_SHA='1fceb02d92e24556fd40c7a015d21850136cf9fc2f1e04da3ee32bc2414edeb0'
SIM_SCHEMA='search-commerce-random-price-runtime-v1'

def _sha(p):return hashlib.sha256(Path(p).read_bytes()).hexdigest()

@lru_cache(maxsize=1)
def bound_prices():
    manifest_path=BUNDLE/'MANIFEST.json'
    if _sha(manifest_path)!=MANIFEST_SHA:raise SyntheticPriceRuntimeError('Simulation manifest differs')
    manifest=json.loads(manifest_path.read_text(encoding='utf-8'))
    if _sha(BUNDLE/'prices.jsonl')!=manifest['outputs']['prices.jsonl']:raise SyntheticPriceRuntimeError('Simulation prices differ')
    rows=[json.loads(s) for s in (BUNDLE/'prices.jsonl').read_text(encoding='utf-8').splitlines()]
    by={int(p['productId']):p for p in rows}
    if len(rows)!=25 or len(by)!=25:raise SyntheticPriceRuntimeError('Simulation identity count differs')
    for p in rows:
        if p['priceStatus']!='synthetic' or p['dataNature']!='synthetic' or p['currency']!='CNY' or type(p['referencePriceMinor']) is not int or not 50000<=p['referencePriceMinor']<=600000:
            raise SyntheticPriceRuntimeError('Invalid simulated price')
    return manifest,by

def projection(product_id,policy):
    manifest,prices=bound_prices();row=prices.get(product_id)
    if row is None:return None
    return {'schemaVersion':SIM_SCHEMA,'productId':str(product_id),'referencePriceMinor':row['referencePriceMinor'],
        'currency':'CNY','dataNature':'synthetic','priceStatus':'synthetic',
        'sourceCatalogSha256':manifest['source_report']['sha256'],
        'rulesetVersion':'uniform-random-cny-500-to-6000-v1','rulesetSha256':manifest['generator_sha256'],
        'seed':str(manifest['seed']),'policy':policy,'labelZh':'随机模拟价',
        'disclosureZh':'随机合成，非真实报价','priceManifestSha256':MANIFEST_SHA}

def apply_synthetic_prices(products,*,directory,policy):
    if Path(directory).resolve()!=BUNDLE.resolve():
        return baseline.apply_synthetic_prices(products,directory=directory,policy=policy)
    if policy not in SYNTHETIC_PRICE_POLICIES:raise SyntheticPriceRuntimeError('Unknown simulation policy')
    result=deepcopy(products)
    if policy=='disabled':return result
    for product in result:
        if product.get('priceStatus')=='verified' and product.get('snapshotPriceMinor') is not None:continue
        value=projection(product.get('id'),policy)
        if value is not None:product['syntheticReferencePrice']=value
    return result

def synthetic_price_value(product,*,allow_budget):
    value=product.get('syntheticReferencePrice')
    if not isinstance(value,dict) or value.get('schemaVersion')!=SIM_SCHEMA:
        return baseline.synthetic_price_value(product,allow_budget=allow_budget)
    policy=value.get('policy');ident=product.get('id')
    if type(ident) is not int or policy not in {'display_only','budget_and_ranking'} or value!=projection(ident,policy):
        raise SyntheticPriceRuntimeError('Simulation quote identity/provenance differs')
    if allow_budget and policy!='budget_and_ranking':return None,None
    return value['referencePriceMinor'],dict(value)

def simulated_price_eligible(product):
    value=product.get('syntheticReferencePrice')
    if not isinstance(value,dict) or value.get('schemaVersion')!=SIM_SCHEMA:return False
    price,_=synthetic_price_value(product,allow_budget=True)
    return price is not None

def canonical_synthetic_price_value(product_id,*,directory,policy,allow_budget):
    if Path(directory).resolve()!=BUNDLE.resolve():
        return baseline.canonical_synthetic_price_value(product_id,directory=directory,policy=policy,allow_budget=allow_budget)
    projected=apply_synthetic_prices([{'id':product_id,'priceStatus':'unverified','snapshotPriceMinor':None}],directory=directory,policy=policy)
    return synthetic_price_value(projected[0],allow_budget=allow_budget)

def clear_synthetic_price_cache():
    bound_prices.cache_clear();baseline.clear_synthetic_price_cache()
