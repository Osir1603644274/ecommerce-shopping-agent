"""Assemble reviewed facts only; search hits never become facts automatically."""
from pathlib import Path
import hashlib
import json

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT/'datasets/knowledge/phone-v1'
def rows(path):
    return [json.loads(x) for x in path.read_text(encoding='utf8').splitlines() if x.strip()]
def write(name, value):
    (OUT/name).write_text(''.join(json.dumps(r,ensure_ascii=False,sort_keys=True)+'\n' for r in value),encoding='utf8')

def main():
    if (OUT/'manifest.json').exists():
        raise SystemExit('Snapshot already assembled; create a new revision instead of overwriting it')
    prior = rows(ROOT/'docs/analysis-briefs/product-knowledge-mcp-2026-09-07/phone-sources-20260908/sources.jsonl')
    sources=[]; facts=[]
    models=rows(OUT/'models.jsonl')
    # Reuse source receipt wording, with exact section/model boundaries.
    keys=['oppo:a96','oppo:findx6','oppo:a2','vivo:y3s','honor:80','honor:x40','apple:7','huawei:mate40pro']
    fields=[
        [('chip','芯片','骁龙695'),('battery','典型电池容量',4500,'mAh'),('battery','额定电池容量',4385,'mAh')],
        [('camera','后置摄像头','广角、超广角、潜望长焦各5000万像素；广角与长焦支持OIS；照片最高3倍光学变焦')],
        [('battery','典型电池容量',5000,'mAh'),('battery','额定电池容量',4880,'mAh'),('charging','充电功率',33,'W')],
        [('chip','芯片','MT6765'),('battery','典型电池容量',5000,'mAh'),('charging','充电规格','5V/2A')],
        [('camera','后置摄像头','1.6亿、800万、200万像素'),('chip','芯片','骁龙782G')],
        [('camera','后置摄像头','5000万+200万像素'),('chip','芯片','骁龙695'),('battery','典型电池容量',5100,'mAh')],
        [('camera','后置摄像头','1200万像素；支持4K 30fps；不能引用7 Plus的2倍光学变焦')],
        [],
    ]
    def add(model,source,group,label,value,unit=None,**extra):
        identity=json.dumps([model,source,group,label,value,unit],ensure_ascii=False,separators=(',',':'))
        eid='pk:'+hashlib.sha256(identity.encode()).hexdigest()[:24]
        facts.append(dict(evidenceId=eid,modelKey=model,sourceId=source,field=group,label=label,
            value=value,unit=unit,text=f'{model} {label}：{value}{unit or ""}',
            applicability='MODEL_REFERENCE_ONLY',listingIdentityVerified=False,**extra))
    for src,key,entries in zip(prior,keys,fields):
        sid='receipt-20260908-'+src['sourceId']
        sources.append({**src,'sourceId':sid,'modelKey':key,'region':'CN' if src['authority']=='OFFICIAL_SPEC' else 'TEST_SAMPLE',
            'originReceipt':'docs/analysis-briefs/product-knowledge-mcp-2026-09-07/phone-sources-20260908/sources.jsonl',
            'softwareVersion':None,'testConditions':None})
        for entry in entries: add(key,sid,*entry)
    for old in rows(ROOT/'evaluation/used-phone-model-facts-dev-v1/facts.jsonl'):
        sid='src:'+hashlib.sha256(old['sourceLocator'].encode()).hexdigest()[:20]
        sources.append(dict(sourceId=sid,url=old['sourceLocator'],title=old['publisher']+' '+old['canonicalModelClaim'],
            modelKey=old['canonicalModelClaim'],authority='INDEPENDENT_MODEL_TEST',region='TEST_SAMPLE',
            section=old['sourceSection'],checkedOn=old['observedAt'],verification='REUSED_FROZEN_FACT_RECEIPT',
            softwareVersion=old['browser'],testConditions=dict(protocolFamily=old['protocolFamily'],brightnessCdM2=old['testBrightnessCdM2']),
            limits='历史样机测试；部分软件/亮度未知；不是当前二手机续航保证，也不直接授权跨协议排名。',
            originReceipt='evaluation/used-phone-model-facts-dev-v1/facts.jsonl',originFactId=old['factId']))
        add(old['canonicalModelClaim'],sid,'battery_test','WiFi网页续航',old['value'],'minute',
            protocolFamily=old['protocolFamily'],softwareVersion=old['browser'],brightnessCdM2=old['testBrightnessCdM2'])
    sid='apple-support-111872-cn'
    sources.append(dict(sourceId=sid,url='https://support.apple.com/zh-cn/111872',title='iPhone 13 技术规格',
        modelKey='apple:13',region='CN',authority='OFFICIAL_SPEC',section='显示屏/芯片/摄像头/电源和电池',
        checkedOn='2026-09-08',verification='WEB_MANUAL_PAGE_READ',softwareVersion=None,testConditions=None,
        limits='标准型号资料；官方最长播放时间非独立实测，电源适配器额定功率不能当手机充电峰值。'))
    for entry in [('chip','芯片','A15，6核CPU、4核GPU'),('screen','屏幕','6.1英寸OLED，2532×1170'),
        ('camera','后置摄像头','1200万主摄与超广角；主摄传感器位移式防抖'),
        ('charging','快充条件','使用20W或更高功率适配器，官方标注约30分钟最多充至50%；非手机峰值功率')]:
        add('apple:13',sid,*entry)
    investigations={r['modelKey']:r for r in rows(OUT/'investigations.jsonl')}
    all_fields=['chip','camera','screen','battery','charging','gaming_test','camera_test','battery_test']
    for m in models:
        covered={f['field'] for f in facts if f['modelKey']==m['modelKey']}
        m['missing']=[f for f in all_fields if f not in covered]
        m['investigationStatus']='PARTIALLY_VERIFIED' if covered else 'SEARCHED_AWAITING_SOURCE_REVIEW'
        m['candidateSourceCount']=len(investigations[m['modelKey']]['candidates'])
        m['gaps']=[dict(field=f,reason='SOURCE_CANDIDATES_NOT_YET_VERIFIED' if m['candidateSourceCount'] else 'INITIAL_SEARCH_NO_USABLE_HIT') for f in m['missing']]
    write('models.jsonl',models);write('sources.jsonl',sources);write('facts.jsonl',facts)
    names=['models.jsonl','listings.jsonl','sources.jsonl','facts.jsonl','investigations.jsonl','audit-summary.json']
    hashes={n:hashlib.sha256((OUT/n).read_bytes()).hexdigest() for n in names}
    version='phone-v1-'+hashlib.sha256(json.dumps(hashes,sort_keys=True).encode()).hexdigest()[:12]
    manifest=dict(schemaVersion=1,version=version,files=hashes,status='DEVELOPMENT_PARTIAL_SOURCE_COVERAGE',
        listingCount=439,modelCount=len(models),factCount=len(facts),modelsWithFacts=len({f['modelKey'] for f in facts}),
        sourceInvestigationCount=len(investigations),notBenchmark=True,productionRankingAuthorized=False)
    (OUT/'manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2)+'\n',encoding='utf8')
    lines=['# 手机型号知识覆盖表','',f'版本：`{version}`。439件商品已登记标题审核状态；178型号均有初次搜索记录。',
        '','这是开发知识快照。搜索线索不等于已核查规格；未知不等于没有该功能。CLEAR仅指标题型号声明明确，不证明实物型号或地区。',
        '', '| 型号 | 商品数 | 来源线索数 | 已核查领域 | 缺口 |','|---|---:|---:|---|---|']
    for m in models:
        covered=sorted({f['field'] for f in facts if f['modelKey']==m['modelKey']})
        lines.append(f"| {m['displayName']} | {len(m['itemIds'])} | {m['candidateSourceCount']} | {','.join(covered) or '无'} | {','.join(m['missing'])} |")
    (OUT/'COVERAGE.md').write_text('\n'.join(lines)+'\n',encoding='utf8')
    print(json.dumps(manifest,ensure_ascii=False))

if __name__=='__main__': main()
