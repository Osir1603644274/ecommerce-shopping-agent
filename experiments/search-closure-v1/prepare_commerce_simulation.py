"""Create a separately bound app build for the user-authorized simulated-price run."""
from pathlib import Path
import difflib
import sys
from types import SimpleNamespace
from retrieval_runtime import read_json,sha,write_once

ROOT=Path('D:/agent-datasets/search-closure-v1')
BUILD=ROOT/'commerce-simulation-build-v1'
REPO=Path('F:/agent')
HERE=REPO/'experiments/search-closure-v1'

def prepare():
    if (BUILD/'MANIFEST.json').exists():return verify()
    copied=[]
    for source in sorted((REPO/'agent/app').rglob('*.py')):
        relative=source.relative_to(REPO/'agent');target=BUILD/'agent'/relative
        target.parent.mkdir(parents=True,exist_ok=True)
        raw=source.read_bytes()
        if target.exists():assert target.read_bytes()==raw
        else:target.write_bytes(raw)
        copied.append({'source':str(source),'relative':str(relative),'sha256':sha(source)})
    original=(REPO/'agent/app/domains/ecommerce/tools.py').read_text(encoding='utf-8')
    text=original.replace('from .synthetic_prices import apply_synthetic_prices','from .synthetic_prices import apply_synthetic_prices, simulated_price_eligible')
    price_block='''        if category_code == "phone" and synthetic_policy != "disabled":
            authoritative_products = apply_synthetic_prices(
                authoritative_products,
                directory=settings.used_phone_synthetic_price_dir,
                policy=synthetic_policy,
            )
'''
    assert text.count(price_block)==1
    text=text.replace(price_block,'')
    anchor='''        if retrieval_mode == "elasticsearch":
            authoritative_products = [
'''
    assert text.count(anchor)==1
    text=text.replace(anchor,price_block+anchor)
    old='''                and product.get("priceStatus") == "verified"
                and type(product.get("snapshotPriceMinor")) is int
'''
    new='''                and (
                    (product.get("priceStatus") == "verified"
                     and type(product.get("snapshotPriceMinor")) is int)
                    or simulated_price_eligible(product)
                )
'''
    assert text.count(old)==1;text=text.replace(old,new)
    (BUILD/'agent/app/domains/ecommerce/tools.py').write_text(text,encoding='utf-8')
    synthetic=REPO/'agent/app/domains/ecommerce/synthetic_prices.py'
    (BUILD/'agent/app/domains/ecommerce/_baseline_synthetic_prices.py').write_bytes(synthetic.read_bytes())
    adapter=HERE/'commerce_simulated_price_adapter.py'
    (BUILD/'agent/app/domains/ecommerce/synthetic_prices.py').write_bytes(adapter.read_bytes())
    (BUILD/'tools-change.diff').write_text(''.join(difflib.unified_diff(original.splitlines(True),text.splitlines(True),fromfile='original/tools.py',tofile='simulation/tools.py')),encoding='utf-8')
    outputs=[{'path':str(p),'sha256':sha(p)} for p in sorted((BUILD/'agent/app').rglob('*.py'))]
    manifest={'status':'ISOLATED_SIMULATED_PRICE_APP_PREPARED','scope':'Real Java/CE/tool code, explicit synthetic price adapter and eligibility in a separate app build. Original app unchanged.',
        'authorization':'User requested random synthesis of remaining25 prices and continuation to close remaining experiment.',
        'source_files':copied,'build_files':outputs,'generator_sha256':sha(Path(__file__)),
        'adapter_sha256':sha(adapter),'patch_sha256':sha(BUILD/'tools-change.diff'),
        'price_manifest':{'path':str(ROOT/'commerce-simulated-prices-v1/MANIFEST.json'),'sha256':sha(ROOT/'commerce-simulated-prices-v1/MANIFEST.json')},
        'modified_original_app':False,'modified_verified_price_fields':False,'production_default_changed':False}
    write_once(BUILD/'MANIFEST.json',manifest);return verify()

def verify():
    manifest=read_json(BUILD/'MANIFEST.json')
    for r in manifest['source_files']:assert sha(Path(r['source']))==r['sha256'],r['source']
    for r in manifest['build_files']:assert sha(Path(r['path']))==r['sha256'],r['path']
    assert sha(Path(__file__))==manifest['generator_sha256']
    assert sha(HERE/'commerce_simulated_price_adapter.py')==manifest['adapter_sha256']
    assert sha(BUILD/'tools-change.diff')==manifest['patch_sha256']
    assert sha(Path(manifest['price_manifest']['path']))==manifest['price_manifest']['sha256']
    return {'status':manifest['status'],'copied_source_files':len(manifest['source_files']),'manifest_sha256':sha(BUILD/'MANIFEST.json')}

def load_simulation_app():
    verify()
    if any(name=='app' or name.startswith('app.') for name in sys.modules):raise ValueError('Load simulation in a fresh Python process')
    sys.path.insert(0,str(BUILD/'agent'))
    from app import settings as module
    module.settings=module.Settings(_env_file=(REPO/'agent/.env',REPO/'.env'))
    assert Path(module.__file__).resolve().is_relative_to(BUILD.resolve())
    return SimpleNamespace(settings=module.settings)

if __name__=='__main__':print(prepare())
