"""Pick qualified tail records, outside the first-5000/source acceptance sample."""
import hashlib,json,sys
from prepare import ROOT,OUT,write
sys.path.insert(0,str(ROOT/'scripts/catalog-preflight-20260914'))
from rules import decode,candidate,validate
from pathlib import Path
manifest=json.loads(Path('D:/agent-datasets/integration-repair-20260913-v1/metadata/MANIFEST.json').read_text())
selected=[]
for source in ('kuaisearch','multicpr'):
    meta=manifest['sources'][source];path=Path(meta['path'])
    with path.open('rb') as f:
        f.seek(max(0,path.stat().st_size-65536));f.readline();tail=f.readlines()
    for raw in reversed(tail):
        try:
            ident,fields=decode(source,raw);row=candidate(source,ident,fields,meta['sha256'])
            if validate(row):continue
        except (ValueError,UnicodeError,KeyError,TypeError):continue
        selected.append(dict(source=source,sourceItemId=ident,rawSha256=hashlib.sha256(raw).hexdigest(),
            expectedProductId=row['id'],title=row['title'],localPriceMinor=row['localOffer']['priceMinor'],
            selection='last qualified source record; not first-5000 acceptance sample'))
        break
assert len(selected)==2
write('LATE-ACCEPTANCE-INPUTS.json',selected)
print(json.dumps(selected,ensure_ascii=False))
