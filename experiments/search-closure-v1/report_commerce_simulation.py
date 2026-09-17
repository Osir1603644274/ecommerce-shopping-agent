"""Audit actual simulation receipts and render results without another model call."""
from pathlib import Path
from collections import Counter
import json
import hashlib
from retrieval_runtime import read_json,sha,write_once
from prepare_commerce_simulation import verify as verify_build

ROOT=Path('D:/agent-datasets/search-closure-v1')
RUN=ROOT/'commerce-component-simulated-v1'

def main():
    verify_build();complete=read_json(RUN/'COMPLETE.json');started=read_json(RUN/'STARTED.json')
    assert complete['component_checks_pass'] and complete['actual_ce_calls']==3 and complete['search_slots']==9
    assert complete['price_data_nature']=='synthetic' and complete['production_activation'] is False
    for relative,wanted in complete['files'].items():
        p=(RUN/relative).resolve();assert p.is_relative_to(RUN.resolve()) and sha(p)==wanted,relative
    for path,wanted in started['code'].items():assert sha(Path(path))==wanted,path
    prices={int(x['productId']):x['referencePriceMinor'] for x in map(json.loads,(ROOT/'commerce-simulated-prices-v1/prices.jsonl').read_text(encoding='utf-8').splitlines())}
    events=[json.loads(s) for s in (RUN/'events.jsonl').read_text(encoding='utf-8').splitlines()]
    counts=Counter(e['kind'] for e in events)
    assert counts=={'HTTP_REQUEST':37,'HTTP_RESPONSE':37,'MODEL_START':3,'MODEL_COMPLETE':3,'CONTROLLED_PROVIDER_FAILURE':3}
    requests=[e for e in events if e['kind']=='HTTP_REQUEST'];responses=[e for e in events if e['kind']=='HTTP_RESPONSE']
    assert all(e['status_code']==200 for e in responses)
    assert all(e['url'].startswith('http://localhost:8080/api/products') for e in requests)
    slots={};summary=[]
    for p in sorted((RUN/'slots').glob('*/RECEIPT.json')):
        row=read_json(p);slots[p.parent.name]=row;assert all(row['checks'].values())
        detail=row['trace']['detail'];assert row['trace']['ok']
        assert set(detail['candidatePoolIds'])==set(prices)
        for candidate in detail['candidates']:
            ident=candidate['id'];quote=candidate['syntheticReferencePrice']
            assert candidate['priceStatus']=='unverified' and candidate['snapshotPriceMinor'] is None
            assert quote['referencePriceMinor']==prices[ident] and quote['priceStatus']=='synthetic'
            assert candidate['facts']['priceStatus']=='synthetic'
        ce=detail['retrievalTrace']['crossEncoder']
        if ce['status']=='active':
            observations=[e for e in events if e['kind']=='MODEL_COMPLETE' and e['slot']==p.parent.name]
            assert len(observations)==1 and row['model_events'][0]['logits']==observations[0]['logits']
            logits={int(k):v for k,v in observations[0]['logits'].items()}
            order=sorted(logits,key=lambda i:(-logits[i],i));assert ce['orderedProductIds']==order
            for index,score in enumerate(ce['scores']):
                assert score['productId']==order[index] and score['rawLogit']==logits[order[index]]
                assert score['rankScore']==(len(order)-index)/len(order)
            start=next(e for e in events if e['kind']=='MODEL_START' and e['slot']==p.parent.name)
            assert {int(i):hashlib.sha256(text.encode()).hexdigest() for i,text in start['pairs']}=={int(k):v for k,v in ce['inputTextSha256'].items()}
        summary.append({'slot':p.parent.name,'candidate_count':len(detail['candidatePoolIds']),'result_count':len(detail['rankedItemIds']),'ce_status':ce['status'],'checks_pass':True})
    cases=read_json(RUN/'CASES.json')['cases']
    for name in cases:
        original=slots[name+'-original'];failed=slots[name+'-forced-failure']
        assert original['authority']==failed['authority']
        a=original['trace']['detail'];b=failed['trace']['detail']
        assert a['rankedItemIds']==b['rankedItemIds']
        assert [p['scoreBreakdown'] for p in a['candidates']]==[p['scoreBreakdown'] for p in b['candidates']]
        assert not failed['model_events'] and read_json(RUN/'fallback'/f'{name}.json')['rank_and_scores_preserved']
    budget=cases['budget'][0]['value']
    for suffix in ['original','ce','forced-failure']:
        assert all(prices[p['id']]<=budget for p in slots['budget-'+suffix]['trace']['detail']['candidates'])
    comparison=read_json(RUN/'comparison.json')['trace']
    expected=read_json(RUN/'comparison-STARTED.json')['product_ids']
    assert comparison['ok'] and len(expected)==2
    assert {p['product']['id'] for p in comparison['detail']['products']}==set(expected)
    assert all(p['facts']['priceStatus']=='synthetic' and p['product']['snapshotPriceMinor'] is None for p in comparison['detail']['products'])
    assert read_json(RUN/'SETTINGS_RESTORED.json')==started['settings_before']
    report={'status':'ACTUAL_SIMULATED_COMMERCE_RECEIPTS_VERIFIED','http_responses':37,'actual_ce_calls':3,'actual_ce_pairs':75,
        'slot_summary':summary,'fallback_cases_passed':3,'comparison_product_ids':expected,'budget_minor':budget,
        'synthetic_prices_only':True,'original_verified_price_fields_preserved':True,'source_build_verified':True,
        'complete_sha256':sha(RUN/'COMPLETE.json'),'real_quote_verification':False,'production_activation':False,
        'inputs':[{'path':str(p),'sha256':sha(p)} for p in [RUN/'COMPLETE.json',RUN/'STARTED.json',RUN/'events.jsonl',ROOT/'commerce-simulation-build-v1/MANIFEST.json',ROOT/'commerce-simulation-build-v1/targeted-tests.xml',ROOT/'commerce-simulation-build-v1/USER_SCOPE_AMENDMENT.json']]}
    write_once(ROOT/'commerce-simulation-build-v1/RECEIPT_VERIFICATION.json',report)
    lines=['# 商城剩余链路：模拟价格下实际验证完成','',
        '依据用户明确授权，为原25个候选随机合成价格，并在独立应用副本中完成商城组件实验。Java召回/详情、交叉编码模型推理和比较工具均实际执行；价格为模拟数据。',
        '', '| 场景 | 方式 | 候选数 | 返回数 | CE状态 | 检查 |','|---|---|---:|---:|---|---|']
    for row in summary:
        name=row['slot'];case,method=next((c,name[len(c)+1:]) for c in ('hard-brand','budget','plain') if name.startswith(c+'-'))
        lines.append(f'| {case} | {method} | {row["candidate_count"]} | {row["result_count"]} | {row["ce_status"]} | PASS |')
    lines += ['', '共37个成功的本地只读HTTP响应、9个搜索槽位、3次真实CE推理（75个query-document输入对）。未调用生成式大模型、未新训练或重复冻结检索评测。',
        '三组受控模型失败均保持对应原搜索的排序和scoreBreakdown；预算组返回结果均不超模拟预算。最终两个重排结果实际进入比较工具，比较结果保留synthetic价格状态。',
        '', '19项针对性测试通过，覆盖模拟价格身份/数值篡改、跨商品引用、display_only/disabled资格、售罄过滤、权威价格优先及原只读接口边界。测试使用模拟依赖；本节上方实际调用证据另行保存，两者不混称。',
        '', '## 数据与应用边界','',
        '原180个应用源文件未修改。独立副本只增加价格适配器、保存原价格适配器，并将显式模拟价格资格加入实验搜索入口；生命周期、库存、ID/version条件保留。真实snapshotPriceMinor仍为空、原priceStatus仍unverified，模拟价格位于独立字段。',
        '商城数据库未写入模拟价格，生产默认未切换。该结论适用于用户批准的模拟价格条件，不是实际市场报价验证、完整网页购物Agent验收或检索质量提升证明。既有搜索/Agent质量结论不变，保留原默认策略。',
        '',f'- [原始运行收据]({(RUN/"COMPLETE.json").as_posix()})',
        f'- [逐项复核]({(ROOT/"commerce-simulation-build-v1/RECEIPT_VERIFICATION.json").as_posix()})',
        f'- [应用改动]({(ROOT/"commerce-simulation-build-v1/tools-change.diff").as_posix()})',
        f'- [25个模拟价格]({(ROOT/"commerce-simulated-prices-v1/prices.md").as_posix()})']
    target=ROOT/'reports/commerce-simulated-completion.md';raw='\n'.join(lines)+'\n'
    if target.exists():assert target.read_text(encoding='utf-8')==raw
    else:target.write_text(raw,encoding='utf-8')
    write_once(ROOT/'reports/commerce-simulated-completion.sources.json',{'report_sha256':sha(target),'generator_sha256':sha(Path(__file__)),
        'verification_sha256':sha(ROOT/'commerce-simulation-build-v1/RECEIPT_VERIFICATION.json'),'complete_sha256':sha(RUN/'COMPLETE.json'),'production_activation':False})
    print({'status':report['status'],'search_slots':9,'actual_ce_calls':3,'fallback_cases':3,'comparison_ok':True,'report':str(target)})

if __name__=='__main__':main()
