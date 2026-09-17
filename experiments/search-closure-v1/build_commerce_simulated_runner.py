"""Derive a reviewable simulated-price runner, preserving the original failed attempt."""
from pathlib import Path
import difflib
HERE=Path(__file__).resolve().parent

def main():
    original=(HERE/'run_commerce_component.py').read_text(encoding='utf-8');text=original
    replacements=[
        ('"""Actual local Java + CE tool experiment; no web-controller or quality claim."""','"""Actual Java/CE tool experiment with explicitly synthetic prices in an isolated app build."""'),
        ('import run_agent_pairs as agent_runner','import run_agent_pairs as agent_runner\nfrom prepare_commerce_simulation import load_simulation_app, verify as verify_simulation_build, BUILD'),
        ("out=ROOT/'commerce-component-v1'","out=ROOT/'commerce-component-simulated-v1'"),
        ('modules=agent_runner.app_modules();settings=modules.settings','modules=load_simulation_app();settings=modules.settings'),
        ("'used_phone_synthetic_price_policy':'disabled','product_title_reranker_enabled':False","'used_phone_synthetic_price_policy':'budget_and_ranking','used_phone_synthetic_price_dir':str(ROOT/'commerce-simulated-prices-v1'),'product_title_reranker_enabled':False"),
        ('    from app import tools as dispatcher','    from app import tools as dispatcher\n    from app.domains.ecommerce.synthetic_prices import apply_synthetic_prices, synthetic_price_value'),
        ("'component_scope':'Real existing search/detail/compare tool chain; no LLM planner, no web controller, no TaskState mutation, no relevance improvement claim'","'component_scope':'Real Java retrieval/resolution and real CE; isolated app with explicitly synthetic prices. No real quote, production, full web controller, TaskState or relevance improvement claim',\n        'simulation_build':verify_simulation_build(),'simulation_price_manifest_sha256':sha(ROOT/'commerce-simulated-prices-v1/MANIFEST.json')"),
        ("'no_synthetic_price':d['retrievalTrace']['syntheticPricePolicy']=='disabled'","'simulated_prices_disclosed':d['retrievalTrace']['syntheticPricePolicy']=='budget_and_ranking' and all(p.get('priceStatus')=='unverified' and p.get('snapshotPriceMinor') is None and p.get('facts',{}).get('priceStatus')=='synthetic' and p.get('syntheticReferencePrice',{}).get('priceStatus')=='synthetic' for p in d['candidates'])"),
        ("if p.get('priceStatus')!='verified' or p.get('lifecycleStatus')!='ACTIVE'", "if p.get('priceStatus')!='unverified' or p.get('snapshotPriceMinor') is not None or p.get('lifecycleStatus')!='ACTIVE'"),
        ("                raise ValueError('Ineligible authority facts entered CE')", "                raise ValueError('Ineligible authority facts entered CE')\n            projected=apply_synthetic_prices([p],directory=settings.used_phone_synthetic_price_dir,policy='budget_and_ranking')[0]\n            if synthetic_price_value(projected,allow_budget=True)[0] is None:raise ValueError('Missing bound simulated price')"),
        ("        if not seed.get('brand') or type(seed.get('snapshotPriceMinor')) is not int:raise ValueError('Real deterministic fixture seed lacks brand/price')", "        seed_price=synthetic_price_value(seed,allow_budget=True)[0]\n        if not seed.get('brand') or type(seed_price) is not int:raise ValueError('Deterministic fixture lacks brand or simulated price')"),
        ("'value':seed['snapshotPriceMinor']", "'value':seed_price"),
        ("'case_derivation':'Lowest positive ID from the first real unchanged original search; fixture constraints only, not relevance labels'", "'case_derivation':'Lowest positive ID from first actual search using the frozen25 synthetic-price bundle; constraints are simulation fixtures, not relevance labels'"),
        ("        result={'status':'COMMERCE_COMPONENT_EXPERIMENT_COMPLETE'", "        comparison_prices_disclosed=comparison.ok and bool(comparison.detail.get('products')) and all(x['facts']['priceStatus']=='synthetic' and x['product']['priceStatus']=='unverified' and x['product']['snapshotPriceMinor'] is None for x in comparison.detail['products'])\n        result={'status':'COMMERCE_SIMULATED_PRICE_COMPONENT_EXPERIMENT_COMPLETE'"),
        ("'component_checks_pass':not failures and fallback_ok and comparison.ok", "'component_checks_pass':not failures and fallback_ok and comparison.ok and comparison_prices_disclosed,\n            'price_data_nature':'synthetic','real_verified_price_experiment_completed':False,'comparison_prices_disclosed':comparison_prices_disclosed"),
        ("        if model_binding(component['model_path'])!=bound:","        verify_simulation_build()\n        if model_binding(component['model_path'])!=bound:"),
    ]
    for old,new in replacements:
        if text.count(old)!=1:raise ValueError('Runner source replacement not unique: '+old[:80])
        text=text.replace(old,new)
    target=HERE/'run_commerce_simulated.py'
    if target.exists():assert target.read_text(encoding='utf-8')==text
    else:target.write_text(text,encoding='utf-8')
    patch=Path('D:/agent-datasets/search-closure-v1/commerce-simulation-build-v1/runner-change.diff')
    raw=''.join(difflib.unified_diff(original.splitlines(True),text.splitlines(True),fromfile='run_commerce_component.py',tofile='run_commerce_simulated.py'))
    if patch.exists():assert patch.read_text(encoding='utf-8')==raw
    else:patch.write_text(raw,encoding='utf-8')
    print({'runner':str(target),'bounded_replacements':len(replacements)})

if __name__=='__main__':main()
