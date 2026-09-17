"""Freeze reviewed V2 facts and honest per-field gaps; never promote search hits."""
import hashlib
import json
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'datasets/knowledge/phone-v2'
OLD = ROOT / 'datasets/knowledge/phone-v1'
FIELDS = ['chip','camera','screen','battery','charging','gaming_test','camera_test','battery_test']
MAP = {'CPU型号':'chip','电池容量':'battery','充电规格':'charging','后置摄像头像素':'camera',
       '尺寸（英寸）':'screen','分辨率':'screen','屏幕材质':'screen'}
LABEL = dict(zip(FIELDS,['芯片','相机','屏幕','电池','充电','游戏实测','拍照实测','WiFi网页续航']))

def rows(p):
    return [json.loads(x) for x in p.read_text(encoding='utf8').splitlines() if x.strip()]

def write(name, values):
    (OUT/name).write_text(''.join(json.dumps(x,ensure_ascii=False,sort_keys=True)+'\n' for x in values),encoding='utf8')

def main():
    if (OUT/'manifest.json').exists(): raise SystemExit('Frozen V2 exists; use a new revision')
    sources={r['sourceId']:r for r in rows(OLD/'sources.jsonl')}
    facts={r['evidenceId']:r for r in rows(OLD/'facts.jsonl')}
    reviewed=[]
    for p in sorted(OUT.glob('reviewed-*.json')):
        for r in json.loads(p.read_text(encoding='utf8')):
            if r.get('exact') is False or not r.get('fields'): continue
            reviewed.append(r)
            sid='src:'+hashlib.sha256(r['url'].encode()).hexdigest()[:20]
            groups={}
            for label,value in r['fields'].items():
                if not value: continue
                field=MAP.get(label,label)
                assert field in FIELDS
                groups.setdefault(field,[]).append((label,value))
            source=dict(sourceId=sid,url=r['url'],modelKey=r['modelKey'],title=r['modelKey']+' 型号资料',
                region=r.get('region','CN'),authority=r.get('authority','OFFICIAL_SPEC'),
                section=r.get('section',' / '.join(r['fields'])),checkedOn=r.get('checkedOn','2026-09-09'),
                verification='CODEX_PAGE_FIELD_REVIEW_NOT_HUMAN_GOLD',softwareVersion=r.get('softwareVersion'),
                testConditions=r.get('testConditions'),originReceipt=str(p.relative_to(ROOT)).replace('\\','/'),
                limits=r.get('limits','地区和实物未核验；型号规格不是当前二手机性能保证。'),
                sourceConflict=r.get('sourceConflict'))
            if sid in sources and sources[sid]['modelKey']!=r['modelKey']: raise ValueError('multi_model_source_requires_explicit_section')
            sources[sid]=source
            for field,entries in groups.items():
                value=entries[0][1] if len(entries)==1 else '；'.join(f'{label}：{v}' for label,v in entries)
                unit='minute' if field=='battery_test' and isinstance(value,(int,float)) else None
                identity=json.dumps([r['modelKey'],sid,field,LABEL[field],value,unit],ensure_ascii=False,separators=(',',':'))
                eid='pk:'+hashlib.sha256(identity.encode()).hexdigest()[:24]
                facts[eid]=dict(evidenceId=eid,modelKey=r['modelKey'],sourceId=sid,field=field,label=LABEL[field],value=value,unit=unit,
                    text=f'{r["modelKey"]} {LABEL[field]}：{value}{unit or ""}',applicability='MODEL_REFERENCE_ONLY',listingIdentityVerified=False)
    models=rows(OUT/'models.jsonl')
    initial={r['modelKey']:r for r in rows(OLD/'investigations.jsonl')}
    searches=rows(OUT/'source-searches.jsonl')+rows(OUT/'new-model-searches.jsonl')
    investigations=[]
    for m in models:
        key=m['modelKey']; attempts=([initial[key]] if key in initial else [])+[r for r in searches if r['modelKey']==key]
        assert attempts, key
        candidates={r['url']:r for a in attempts for r in a['candidates']}
        refs=[s for s in sources.values() if s['modelKey']==key]
        covered={f['field'] for f in facts.values() if f['modelKey']==key}
        m['missing']=[f for f in FIELDS if f not in covered]
        m['gaps']=[]
        for field in m['missing']:
            if field.endswith('_test'):
                reason='NO_REVIEWED_MATCHING_INDEPENDENT_TEST_WITH_SUFFICIENT_CONDITIONS'
            elif key.startswith(('redmi:','honor:')):
                reason='OFFICIAL_SEARCH_FOUND_DYNAMIC_OR_NONMATCHING_PAGE_NO_VERIFIED_FIELD'
            elif key.startswith('apple:') and field=='battery':
                reason='OFFICIAL_SPEC_DOES_NOT_SUPPLY_VERIFIED_MAH_FIELD'
            else: reason='SEARCHED_BUT_NO_REVIEWED_EXACT_MODEL_REGION_FIELD'
            m['gaps'].append(dict(field=field,reason=reason))
        m['investigationStatus']='PARTIALLY_VERIFIED' if covered else 'SEARCHED_WITH_UNRESOLVED_GAPS'
        m['candidateSourceCount']=len(candidates)
        m['reviewedSourceCount']=len(refs)
        investigations.append(dict(modelKey=key,checkedOn='2026-09-09',attempts=attempts,reviewedSourceIds=[s['sourceId'] for s in refs],
            gaps=m['gaps'],verification='SEARCH_RECORDS_ARE_NOT_FACTS',exhaustiveSearch=False))
    write('models.jsonl',models); write('sources.jsonl',sources.values());write('facts.jsonl',facts.values());write('investigations.jsonl',investigations)
    audit=json.loads((OUT/'audit-summary.json').read_text(encoding='utf8'))
    names=['models.jsonl','listings.jsonl','sources.jsonl','facts.jsonl','investigations.jsonl','audit-summary.json','title-review.jsonl']
    names+=sorted(p.name for p in OUT.glob('reviewed-*.json'))+['source-searches.jsonl','new-model-searches.jsonl']
    hashes={n:hashlib.sha256((OUT/n).read_bytes()).hexdigest() for n in names}
    version='phone-v2-'+hashlib.sha256(json.dumps(hashes,sort_keys=True).encode()).hexdigest()[:12]
    covered_keys={f['modelKey'] for f in facts.values()}
    manifest=dict(schemaVersion=1,version=version,files=hashes,status='DEVELOPMENT_SOURCE_GAPS_EXPLICIT',listingCount=439,
        modelCount=len(models),factCount=len(facts),sourceCount=len(sources),modelsWithFacts=sum(m['modelKey'] in covered_keys for m in models),
        sourceInvestigationCount=len(investigations),titleAudit=audit['statusCounts'],notBenchmark=True,productionRankingAuthorized=False,
        supersedes=audit['supersedes'],supersessionReason=audit['supersessionReason'])
    (OUT/'manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2)+'\n',encoding='utf8')
    lines=['# 手机型号知识覆盖表','',f'版本 `{version}`；439件标题复审：明确288、冲突50、歧义89、未知12；可识别型号组171。',
        f'已收录 {len(facts)} 条事实；{manifest["modelsWithFacts"]}/171 个型号有事实。所有型号有调查记录，缺口仍存在。',
        '','CLEAR只代表卖家标题声明明确，不是实物鉴定。全部地区/代际仍需核验。官方规格不能替代游戏、拍照、续航实测；无事实不等于没有该能力。',
        '','| 型号 | 商品数 | 已核查来源 | 已收录领域 | 缺口 |','|---|---:|---:|---|---|']
    for m in models:
        covered=sorted({f['field'] for f in facts.values() if f['modelKey']==m['modelKey']})
        lines.append(f'| {m["displayName"]} | {len(m["itemIds"])} | {m["reviewedSourceCount"]} | {", ".join(covered) or "无"} | {", ".join(m["missing"])} |')
    lines+=['','## 439件商品审核明细','','| 商品ID | 原标题 | 审核状态 | 声明型号 |','|---|---|---|---|']
    for r in rows(OUT/'listings.jsonl'):
        lines.append('| '+r['itemId']+' | '+r['title'].replace('|','／')+' | '+r['status']+' | '+', '.join(r['modelKeys'])+' |')
    (OUT/'COVERAGE.md').write_text('\n'.join(lines)+'\n',encoding='utf8')
    print(json.dumps({k:v for k,v in manifest.items() if k!='files'},ensure_ascii=False))

if __name__=='__main__': main()
