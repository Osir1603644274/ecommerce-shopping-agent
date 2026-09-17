"""Versioned catalog identity audit and evidence build. No source data mutation."""
from collections import Counter, defaultdict
import hashlib
import json
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'datasets/knowledge/phone-v1'
CAT = ROOT / 'datasets/current/used-phone/catalog.jsonl'

def read(p):
    return [json.loads(s) for s in p.read_text(encoding='utf-8-sig').splitlines() if s.strip()]

def dump(p, rows):
    p.write_text(''.join(json.dumps(r,ensure_ascii=False,sort_keys=True)+'\n' for r in rows),encoding='utf8')

# Explicit title corrections reviewed against exact catalog rows; not physical verification.
OVERRIDES = {
 '2104906':'oneplus:ace2','4255788115043726016':'oneplus:ace2',
 '2352578':'realme:neo7x','245670':'apple:13','2755097':'apple:12',
 '3396939':'oppo:findn5','3457269':'vivo:xfold','3576464':'apple:15promax',
 '3633971':'oppo:findx','3934984':'apple:13','4749690':'vivo:xfold2',
 '614202':'huawei:matext','796291':'redmi:turbo3','867158':'apple:5s','867273':'apple:5s',
 '3266320283083347318':'oneplus:ace3pro','1710390321395944236':'samsung:zfold4',
 '4156608880226841904':'samsung:zfold3','8407165726238433823':'vivo:xfold3',
 '5712174568987012357':'realme:11pro','8006237445825262825':'iqoo:9pro',
 # p/pm are ambiguous shorthand unless the same title also spells the variant.
 '1068548':'apple:16pro','1874553':'apple:15pro','2872105':'apple:12promax',
 '5989522':'apple:15promax','5416504025157712880':'apple:8plus',
}
ALIASES={'samsung:galaxyzflip4':'samsung:zflip4','samsung:galaxyzflip5':'samsung:zflip5',
         'samsung:galaxyzfold6':'samsung:zfold6','huawei:p70pro':'huawei:pura70pro'}
BRANDS={'apple':'Apple iPhone','huawei':'华为','honor':'荣耀','oppo':'OPPO','vivo':'vivo',
        'iqoo':'iQOO','redmi':'Redmi','xiaomi':'小米','samsung':'Samsung Galaxy',
        'oneplus':'一加','realme':'realme','blackshark':'黑鲨','nubia':'努比亚'}
DOMAINS={'apple':'apple.com','huawei':'consumer.huawei.com','honor':'honor.com','oppo':'oppo.com',
         'vivo':'vivo.com.cn','iqoo':'iqoo.com','redmi':'mi.com','xiaomi':'mi.com','samsung':'samsung.com',
         'oneplus':'oneplus.com','realme':'realme.com','blackshark':'blackshark.com','nubia':'nubia.com'}

def audit():
    OUT.mkdir(parents=True,exist_ok=True)
    if (OUT/'listings.jsonl').exists(): raise FileExistsError('Audit already built; use a new revision')
    cat=read(CAT)
    previous=read(ROOT/'evaluation/context-multiagent-v2-feasibility-v2/title-model-claims-v2.jsonl')
    old={str(r['itemId']):r for r in previous}
    result=[]
    for row in cat:
        iid=row['itemId']; t=row['title']; before=old[iid]
        assert before['title']==t
        claims=sorted({ALIASES.get(c['canonicalModelClaim'],c['canonicalModelClaim']) for c in before['claims']})
        notes=[]
        if iid in OVERRIDES:
            claims=[OVERRIDES[iid]]; notes.append('逐字标题审阅：补充旧规则遗漏或合并明确同义简称')
        status='CLEAR' if len(claims)==1 else 'AMBIGUOUS' if claims else 'UNKNOWN'
        if len(claims)==1:
            family,model=claims[0].split(':',1)
            if family != before['catalogBrandFamily'] and {family,before['catalogBrandFamily']}!={'xiaomi','redmi'}:
                status='CONFLICT'; notes.append('目录品牌与标题型号家族冲突，不静默改写品牌')
            if model in {'14p','w223','红魔5','红魔6','a5a'}:
                status='AMBIGUOUS'; notes.append('型号简称/变体不足以唯一对应官方型号')
        if re.search(r'定制|升级款|升级心系|已root|已ROOT|root面具|WIFI版|WiFi版|wifi版|WIFI机|WiFi机|wifi机',t):
            status='CONFLICT'; notes.append('标题含改装、升级或功能受限声明，不将标准机规格绑定到实物')
        region='US' if '美版' in t else 'CN' if ('国行' in t or '国版' in t) else 'UNKNOWN'
        result.append(dict(itemId=iid,title=t,titleSha256=hashlib.sha256(t.encode()).hexdigest(),
            catalogBrand=row['brand'],modelKeys=claims,status=status,region=region,
            bindingAuthority='SELLER_MODEL_CLAIM_NOT_PHYSICAL_INSPECTION',listingIdentityVerified=False,
            notes=notes or ['核对原目录与已提取型号；地区未明时仅能提供注明适用版本的型号参考'],
            auditMethod='CODEX_TITLE_REVIEW_AND_RULE_SCREEN_NOT_HUMAN_GOLD'))
    dump(OUT/'listings.jsonl',result)
    groups=defaultdict(list)
    for r in result:
        if r['status']=='CLEAR': groups[r['modelKeys'][0]].append(r['itemId'])
    models=[]
    for key,ids in sorted(groups.items()):
        family,name=key.split(':',1)
        display=BRANDS[family]+' '+re.sub(r'(pro|max|plus|mini|ultra|turbo|fold|flip)',r' \1 ',name).strip()
        models.append(dict(modelKey=key,displayName=display,itemIds=ids,officialDomain=DOMAINS[family],
            investigationStatus='PENDING',missing=['chip','camera','screen','battery','charging','gaming_test','camera_test','battery_test']))
    dump(OUT/'models.jsonl',models)
    summary=dict(catalogSha256=hashlib.sha256(CAT.read_bytes()).hexdigest(),catalogRows=len(cat),
        statusCounts=dict(Counter(r['status'] for r in result)),modelCount=len(models),
        originalRowsPreserved=True,physicalIdentityVerified=False)
    (OUT/'audit-summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2)+'\n',encoding='utf8')
    print(json.dumps(summary,ensure_ascii=False))

if __name__=='__main__': audit()
