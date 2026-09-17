"""Independent catalog-category/option audit of completed predictions."""
import json,sqlite3,collections
from pathlib import Path
import companion as c
O=c.OUT/'resolved';rows=json.loads((O/'SCORE_DETAILS.json').read_text(encoding='utf8'));truths={r['id']:r['truth'] for r in json.loads((O/'oracle.private.json').read_text(encoding='utf8'))};stats=collections.defaultdict(collections.Counter);details=[];db=sqlite3.connect((c.CAT/'retrieval-v1/catalog.sqlite3').as_uri()+'?mode=ro',uri=True)
def pairs(d):
 out=set()
 for k,v in d.items():
  for val in v if isinstance(v,list) else [v]:out.add((c.normalize(k),c.normalize(val)))
 return out
for r in rows:
 t=truths[r['id']];wanted={tuple(x) for x in r['wanted']};s=stats[r['partition']];outcomes={}
 for arm,v in r['outcomes'].items():
  if v.get('status')=='abstain':s[arm+'_abstain']+=1;continue
  d=v['catalog'];same=c.normalize(t['category']) in {c.normalize(x) for x in str(d['category']).split(' - ')};base=pairs(d['attributes']);single=bool(wanted) and (wanted<=base or any(wanted<=base|pairs(op) for op in d['options']));strict=same and single
  s[arm+'_reference_category_in_path']+=same;s[arm+'_category_and_one_option_attribute_coverage']+=strict;s[arm+'_union_only_attribute_match']+=v['all_values_present'] and not single
  outcomes[arm]={'title':d['title'],'category':d['category'],'reference_category':t['category'],'reference_category_in_path':same,'one_option_attribute_coverage':single,'combined_check':strict}
 reference=c.products(db,[t['product_id']]);ref_ok=False
 if reference:
  d=reference[0];base=pairs(d['attributes']);ref_ok=bool(wanted) and (wanted<=base or any(wanted<=base|pairs(op) for op in d['options']))
 s['tasks']+=1;s['reference_catalog_attribute_consistent']+=ref_ok
 details.append({'id':r['id'],'partition':r['partition'],'query':r['query'],'wanted':r['wanted'],'outcomes':outcomes,'reference_catalog_attribute_consistent':ref_ok})
result={'counts':{k:dict(v) for k,v in stats.items()},'scope':'reference category occurring in catalog hierarchy plus wanted values within base attributes and a single option dictionary; still not human-judged full task success; absence means unconfirmed not irrelevant','details':details}
(O/'CATALOG_REVIEW.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf8');print(json.dumps(result['counts'],ensure_ascii=False))
