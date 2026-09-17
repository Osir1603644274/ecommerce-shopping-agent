"""Second title-by-title review, preserving the first immutable development snapshot.

This is an assisted source-title audit, never human gold or physical inspection.
"""
from collections import Counter, defaultdict
import hashlib
import json
from pathlib import Path

from build_product_knowledge_v1 import BRANDS, DOMAINS, dump, read

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'datasets/knowledge/phone-v2'

# Exact rows reviewed against the unchanged source catalog on 2026-09-09.
# Multiple model names in one listing cannot be resolved by picking the first.
CHANGES = {
    '1086995': ('AMBIGUOUS', ['apple:8','apple:8plus','apple:6s'], '8/8p及6S并列'),
    '1710698': ('AMBIGUOUS', ['oppo:reno7','oppo:reno8','oppo:reno6','oppo:reno5'], '斜杠列举多个Reno代际'),
    '1897492': ('AMBIGUOUS', ['oppo:reno6','oppo:reno7','oppo:reno8'], 'Reno6 7 8并列'),
    '1906599': ('AMBIGUOUS', ['apple:8','apple:8plus'], '8/8P并列'),
    '1951548': ('AMBIGUOUS', ['vivo:y93','vivo:y93s'], 'y93/s不能省略s变体'),
    '1968628': ('AMBIGUOUS', ['apple:8','apple:5s'], '苹果8与iPhone5s并列'),
    '291462': ('CLEAR', ['vivo:y35plus'], 'Y35+不是Y35；保留加号变体'),
    '4175990': ('CLEAR', ['honor:9x'], '荣耀9X不是荣耀9；保留X后缀'),
    '4563153': ('AMBIGUOUS', ['apple:6','apple:6s'], '6/6s并列'),
    '5011774': ('CONFLICT', ['vivo:xnote'], '标题是vivo X Note；7英寸不是Redmi Note7，目录品牌另有冲突'),
    '569202': ('CLEAR', ['huawei:nova5i'], 'Nova5i不是Nova5；保留i后缀'),
    '614304': ('CLEAR', ['huawei:p50pocket'], 'P50 Pocket不是P50直板机'),
    '6519282': ('AMBIGUOUS', ['nubia:红魔10pro','nubia:红魔10proplus'], '10Pro/10Pro+并列'),
    '737333': ('AMBIGUOUS', ['oppo:reno7','oppo:reno8','oppo:reno6','oppo:reno5','oppo:reno4'], '斜杠列举多个Reno代际'),
    '74980': ('AMBIGUOUS', ['samsung:zfold3','samsung:w22'], 'Fold3与W22是不同销售型号，不能静默合并'),
    '7970099883251406247': ('CONFLICT', ['samsung:zfold4','samsung:w25'], 'Fold4升级W25声明，不绑定标准W25规格'),
    '2181632328269076286': ('AMBIGUOUS', ['samsung:zfold3','samsung:zflip4'], 'Fold3与Flip4并列且折叠形态不同'),
    '8111366275491533476': ('AMBIGUOUS', ['apple:8','apple:8plus'], '8/8p并列；wifi是否功能受限待核实'),
    '1543339524778746315': ('AMBIGUOUS', ['apple:7','apple:6s','apple:6splus'], '7、6s、6sp并列'),
    '4025104132986547728': ('CLEAR', ['oppo:reno4se'], 'Reno4 SE不是Reno4'),
    '8585097571216542353': ('AMBIGUOUS', ['apple:8','apple:8plus'], 'iPhone8与8p并列'),
    '7039566577057208557': ('AMBIGUOUS', ['apple:16promax','apple:15promax'], '16promax与15promax并列'),
    '7441112016001245431': ('AMBIGUOUS', ['huawei:mate40pro'], '同标题同时声明5G与4G，地区/网络版本待确认'),
    '1903596919234922054': ('AMBIGUOUS', [], '华为智选Hi畅享60s的销售品牌和型号需核查，不能当华为畅享60'),
    '6955924241788205496': ('CONFLICT', ['samsung:zflip5','samsung:w25'], 'Flip5升级W25声明，不绑定标准W25规格'),
    '2387715': ('CONFLICT', ['redmi:10x'], '标题明确root/框架/面具用途，标准软件测试不保证适用'),
    '1444727988645405494': ('CONFLICT', ['redmi:k60'], '标题明确root机及已刷模块'),
    '1334236': ('CONFLICT', ['apple:14promax'], '国产替换屏、无面容声明，标准屏幕和功能不保证适用'),
    '3596437': ('AMBIGUOUS', ['apple:14pro'], '外版卡贴又声称双卡官方标配，地区和双卡形态待确认'),
    '4749720': ('AMBIGUOUS', ['honor:magicvs'], 'VS至臻版本待确认，不能直接绑定标准版'),
    '2429163289195259697': ('AMBIGUOUS', ['iqoo:pro'], 'iQOO Pro的4G/5G版本未确认'),
    '7519723262475557983': ('AMBIGUOUS', ['iqoo:neo','iqoo:pro'], 'Neo与Pro并列'),
    '2400689503905108374': ('AMBIGUOUS', ['apple:4s','apple:4'], '4s/4并列'),
    '4379700599406730668': ('AMBIGUOUS', ['apple:4','apple:4s','apple:5'], '4、4s、5并列'),
    '7356746980171657389': ('AMBIGUOUS', ['huawei:matexs2'], 'Mate Xs2标题可识别，但芯片和5G声明尚未与官方核查'),
}

def main():
    if OUT.exists():
        raise SystemExit('Revision directory exists; do not overwrite review evidence')
    previous = read(ROOT/'datasets/knowledge/phone-v1/listings.jsonl')
    catalog = read(ROOT/'datasets/current/used-phone/catalog.jsonl')
    assert len(catalog) == len(previous) == 439
    actual = {r['itemId']:r['title'] for r in catalog}
    ledger=[]
    for row in previous:
        assert row['title'] == actual[row['itemId']]
        before = {k:row[k] for k in ('status','modelKeys')}
        if row['itemId'] in CHANGES:
            row['status'], row['modelKeys'], reason = CHANGES[row['itemId']]
            row['notes'].append(reason)
        else:
            reason = '逐字复读：未发现需要变更的型号词；保留原有歧义、冲突及地区限制'
        row['reviewedOn']='2026-09-09'
        row['auditMethod']='CODEX_FULL_TITLE_REREVIEW_V2_NOT_HUMAN_GOLD'
        row['variantStatus']='UNVERIFIED_REGION_AND_GENERATION'
        ledger.append(dict(itemId=row['itemId'],titleSha256=row['titleSha256'],before=before,
            after={k:row[k] for k in ('status','modelKeys')},reason=reason,
            reviewedOn=row['reviewedOn'],reviewer='CODEX_ASSISTED_TITLE_REVIEW',humanGold=False))
    groups=defaultdict(list)
    for row in previous:
        if row['status']=='CLEAR': groups[row['modelKeys'][0]].append(row['itemId'])
    models=[]
    for key,ids in sorted(groups.items()):
        brand,name=key.split(':',1)
        models.append(dict(modelKey=key,displayName=BRANDS[brand]+' '+name,itemIds=ids,
            officialDomain=DOMAINS[brand],investigationStatus='PENDING',
            missing=['chip','camera','screen','battery','charging','gaming_test','camera_test','battery_test']))
    OUT.mkdir(parents=True)
    dump(OUT/'listings.jsonl',previous); dump(OUT/'models.jsonl',models); dump(OUT/'title-review.jsonl',ledger)
    summary=dict(catalogSha256=hashlib.sha256((ROOT/'datasets/current/used-phone/catalog.jsonl').read_bytes()).hexdigest(),
        catalogRows=439,statusCounts=dict(Counter(r['status'] for r in previous)),modelCount=len(models),
        reviewedRows=len(ledger),changedRows=len(CHANGES),physicalIdentityVerified=False,humanGold=False,
        supersedes='phone-v1-879ef066937c',supersessionReason='V1 missed model suffixes and multi-model title declarations')
    (OUT/'audit-summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2)+'\n',encoding='utf8')
    print(json.dumps(summary,ensure_ascii=False))

if __name__=='__main__': main()
