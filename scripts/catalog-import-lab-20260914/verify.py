"""Independent, read-only check of sealed trial evidence and all 10000 mapped rows."""
import hashlib
import json
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'scripts/catalog-preflight-20260914'))
from rules import candidate, validate, VERSION
from scan import sha, dump

OUT=Path('D:/agent-datasets/commerce-import-lab-20260914-attempt001')
DEST=OUT/'verification'


def load(name):
    return json.loads((OUT/name).read_text(encoding='utf8'))


def main():
    if DEST.exists():
        raise ValueError('verification already exists; do not overwrite')
    manifest=load('EVIDENCE-MANIFEST.json')
    for name,item in manifest.items():
        assert sha(OUT/name)==item['sha256'], name
        assert (OUT/name).stat().st_size==item['bytes'], name
    report=load('RESULT.json')
    assert report['status']=='PASS_BOUNDED_10000_DATABASE_REHEARSAL'
    for name,digest in report['sourceCodeSha256'].items():
        assert sha(ROOT/name)==digest, 'executed source differs from current source'
    rows=[json.loads(x) for x in (OUT/'sample.jsonl').read_text(encoding='utf8').splitlines()]
    assert len(rows)==10000
    assert sha(OUT/'sample.jsonl')==report['inputSha256']
    final=load('lab-final-state.json')
    before=load('production-before.json')
    after=load('production-after.json')
    fields=('title','brand','seller','categoryL1','categoryL2','categoryL3')
    ps={str(p['id']):p for p in final['product']}
    offers={str(p['product_id']):p for p in final['product_local_offer']}
    stock={str(p['item_id']):p for p in final['inventory_stock'] if p['item_type']=='PRODUCT'}
    ids=set()
    for index,r in enumerate(rows):
        expected=candidate(r['source'],r['sourceItemId'],{k:r[k] for k in fields},r['datasetRevision'])
        assert {k:r[k] for k in expected}==expected
        assert not validate(r)
        assert r['id'] not in ids
        ids.add(r['id'])
        p=ps[r['id']];o=offers[r['id']];s=stock[r['id']]
        assert (p['source'],p['source_item_id'])==(r['source'],r['sourceItemId'])
        for name in ('title','brand','seller'):
            assert p[name]==r[name]
        for n in (1,2,3):
            assert p['category_l'+str(n)]==r['categoryL'+str(n)]
        assert p['snapshot_price_minor'] is None and p['price_status']=='missing'
        assert p['dataset_revision']==r['datasetRevision'] and p['source_license']=='unknown'
        assert p['provenance_url']==r['provenanceUrl']
        assert p['lifecycle_status']==('ARCHIVED' if index==0 else 'ACTIVE')
        assert o['price_minor']==r['localOffer']['priceMinor']+(100 if index==0 else 0)
        assert o['price_kind']=='local_simulated' and o['source_revision']==VERSION and o['currency']=='CNY'
        assert (s['total_quantity'],s['available_quantity'],s['reserved_quantity'],s['sold_quantity'])==(
            (10,9,1,0) if index==0 else (10,10,0,0))
    for t in ('product','product_local_offer','inventory_stock'):
        pk='product_id' if t=='product_local_offer' else 'id'
        key=lambda r:r[pk]
        assert sorted(before[t],key=key)==sorted(after[t],key=key), 'production changed'
        old_ids={r[pk] for r in before[t]}
        assert [r for r in final[t] if r[pk] in old_ids]==sorted(before[t],key=key), 'old catalog changed'
        assert len(final[t])-len(before[t])==10000
    restored_digest=hashlib.sha256(json.dumps(final,sort_keys=True,ensure_ascii=False,default=str).encode()).hexdigest()
    assert restored_digest==report['backup']['restoredBusinessStateSha256']
    workers=[json.loads(x) for x in (OUT/'workers.jsonl').read_text(encoding='utf8').splitlines()]
    assert [w['exitCode'] for w in workers]==[77,78,0,0,0,1,1]
    assert all(json.loads(workers[n]['stdout'])['skipped']==10000 for n in (3,4))
    assert 'checkpoint input/version mismatch' in workers[5]['stderr']
    assert 'primary key collision' in workers[6]['stderr']
    DEST.mkdir()
    result=dict(status='PASS',verifiedCandidates=len(rows),businessRowsAdded=30000,
                originalRowsPreserved={t:len(before[t]) for t in ('product','product_local_offer','inventory_stock')},
                filesVerified=len(manifest),testFixtureExceptions=1,
                executedImporterSourceMatched=True,productionUnchanged=True,
                verifierSha256=sha(Path(__file__).resolve()))
    dump(DEST/'VALIDATION.json',result)
    print(json.dumps(result))


if __name__=='__main__':
    main()
