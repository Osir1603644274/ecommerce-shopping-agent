"""Close the full search experiment under the user's explicit simulated-price amendment."""
from pathlib import Path
from retrieval_runtime import read_json,sha,write_once
from prepare_commerce_simulation import verify as verify_build

ROOT=Path('D:/agent-datasets/search-closure-v1')

def main():
    prior=ROOT/'delivery-audit/final-independent-native-v1'
    delta=ROOT/'delivery-audit/commerce-simulated-delta-v1'
    bindings=[]
    for folder,names in [(prior,['final-audit.json','final-audit.md']),(delta,['delta-audit.json','delta-audit.md'])]:
        collected=read_json(folder/'COLLECTED.json')
        for name in names:assert sha(folder/name)==collected['outputs'][name]
        bindings.extend([folder/'COLLECTED.json',*[folder/name for name in names]])
    old=read_json(prior/'final-audit.json');audit=read_json(delta/'delta-audit.json')
    assert audit['status']=='ACCEPT_SIMULATED_COMMERCE_DELTA'
    assert audit['findings']=={'P0':'NONE','P1':'NONE','P2':'NONE'}
    assert all(x['status']=='PASS' for x in old['contract_items'] if x['id']<=9)
    assert {x['id'] for x in old['contract_items']}==set(range(1,12))
    verify_build()
    simulation=ROOT/'commerce-component-simulated-v1';complete=read_json(simulation/'COMPLETE.json')
    assert complete['component_checks_pass'] and complete['fallback_checks_pass'] and complete['comparison_ok']
    assert complete['actual_ce_calls']==3 and complete['search_slots']==9 and complete['comparison_prices_disclosed']
    assert complete['production_activation'] is False and complete['real_verified_price_experiment_completed'] is False
    receipt=read_json(ROOT/'commerce-simulation-build-v1/RECEIPT_VERIFICATION.json')
    assert receipt['complete_sha256']==sha(simulation/'COMPLETE.json')
    quality=read_json(ROOT/'agent-quality-review-v2/frozen/report.json')
    search=read_json(ROOT/'evaluation/final-test-v1/report.json')
    assert quality['agent_gate_passed'] is False and quality['production_activated'] is False
    assert search['common_conditional']['main']['comparisons']['winner']['positive_improvement_supported'] is False
    artifact_paths=[
        ROOT/'commerce-simulation-build-v1/USER_SCOPE_AMENDMENT.json',ROOT/'commerce-simulation-build-v1/MANIFEST.json',
        ROOT/'commerce-simulation-build-v1/targeted-tests.xml',ROOT/'commerce-simulation-build-v1/RECEIPT_VERIFICATION.json',
        simulation/'COMPLETE.json',ROOT/'commerce-simulated-prices-v1/MANIFEST.json',
        ROOT/'agent-quality-review-v2/frozen/COMPLETE.json',ROOT/'agent-quality-review-v2/frozen/report.json',
        ROOT/'evaluation/final-test-v1/COMPLETE.json',ROOT/'evaluation/final-test-v1/report.json',
        ROOT/'final-selection/SELECTION.json',ROOT/'reports/training-run-current.sources.json',
        ROOT/'reports/delivery-overview-current.sources.json',ROOT/'reports/commerce-simulated-completion.sources.json']
    for p in artifact_paths:assert p.is_file()
    bindings.extend(artifact_paths)
    ledger=[{'item':x['id'],'state':'FULFILLED','basis':'Prior independent full audit PASS','evidence':x['evidence']} for x in old['contract_items'] if x['id']<=9]
    ledger.extend([
        {'item':10,'state':'FULFILLED_WITH_USER_APPROVED_SIMULATED_PRICE','basis':'Original real40 catalog Agent calls retained; remaining commerce now has real Java/CE execution, 9slots, 3fallbacks and comparison with explicit synthetic prices.',
         'evidence':[str(simulation/'COMPLETE.json'),str(delta/'delta-audit.json'),str(ROOT/'commerce-simulation-build-v1/USER_SCOPE_AMENDMENT.json')]},
        {'item':11,'state':'FULFILLED_RETAIN_BASELINE','basis':'Prior regression/delivery evidence and19 scoped tests; quality and uncertainty gates reported. Since activation gates failed, existing default retained as required. No positive result manufactured.',
         'evidence':[str(ROOT/'agent-quality-review-v2/frozen/report.json'),str(ROOT/'commerce-simulation-build-v1/targeted-tests.xml'),str(delta/'delta-audit.json')]}])
    decision={'status':'SEARCH_CLOSURE_COMPLETE_WITH_USER_APPROVED_SIMULATED_PRICE',
        'complete_under_latest_user_scope':True,'original_real_price_branch_verified':False,
        'user_price_amendment_applied':True,'full_goal_scope_otherwise_preserved':True,
        'search_positive_improvement_supported':False,'agent_activation_gate_passed':False,'production_activated':False,
        'default_decision':'RETAIN_EXISTING_BASELINE','requirement_ledger':ledger,
        'independent_final_audit_thread_id':read_json(prior/'COLLECTED.json')['source_thread_id'],
        'independent_delta_audit_thread_id':read_json(delta/'COLLECTED.json')['source_thread_id'],
        'inputs':[{'path':str(p),'sha256':sha(p)} for p in bindings],'generator_sha256':sha(Path(__file__))}
    lines=['# 普通搜索算法闭环：最终交付','',
        '**已按你最新批准的“25个候选使用随机模拟价格”范围完成闭环。保留原默认搜索方案，不声明已证明稳定提升。**','',
        '| 部分 | 最终状态 |','|---|---|',
        '| 数据与训练 | 开发40条3936对、训练200条8000对、独立项目测试80条6400对；固定3epoch真实训练已完成 |',
        '| 搜索对照 | 35个固定配置、冻结选型、一次留出测试已完成；差值不确定区间跨零，保留基线 |',
        '| Agent | 20对40槽位、39非空正文和1失败；40最终响应均触及输出预算。独立盲审完成，启用门未过 |',
        '| 商城模拟实验 | 25价格已实际接入独立运行副本；9搜索、3真实CE推理、3失败回退、重排后比较通过 |',
        '| 验收 | 原完整独立总审＋本次增量独立验收；19项本次针对性测试通过；旧源码和冻结结果保留 |','',
        '## 结果含义','',
        '闭环完成表示约定的数据、训练、评测、实际调用和结果判断均已落实，不等于算法必然提升或生产启用。旧测试/盲审失败和不确定性全部保留，未通过改标、换测试、重跑回答追求正结果。',
        '商城中的商品文本、ID、库存和版本来自实际Java接口，CE真实推理；价格是用户批准的随机模拟数据。未把模拟价格伪装为verified，未写入商城数据库；该能力目前在独立实验副本中验证，生产默认不切换。真实市场价格验证、完整网页购物Agent及TaskState SFT/RL不由本次证据证明。','',
        '## 交付入口','']
    reports=[('最终数据卡','final-search-data-card.md'),('真实清理前后样例','revision-examples.md'),('实际训练','training-run-current.md'),('开发对照及消融','final-development.md'),('独立留出与逐query指标','final-test.md'),('实际Agent调用','agent-execution.md'),('Agent输出预算限制','agent-output-budget.md'),('最终回答盲审','agent-quality-v2.md'),('商城模拟链路实测','commerce-simulated-completion.md')]
    for title,name in reports:
        p=ROOT/'reports'/name;assert p.is_file();lines.append(f'- [{title}]({p.as_posix()})')
    lines += [f'- [完整独立总审]({(prior/"final-audit.md").as_posix()})',f'- [模拟价格增量验收]({(delta/"delta-audit.md").as_posix()})',
        f'- [逐合同完成证据]({(ROOT/"delivery-closure-v1/COMPLETE.json").as_posix()})',
        '', '此前“受阻/9通过2部分完成”的报告均保留为历史状态。用户随后授权模拟价格，本次新增实验及验收补齐剩余步骤；没有将旧失败报告改写为成功。']
    target=ROOT/'reports/delivery-final.md';raw='\n'.join(lines)+'\n'
    if target.exists():assert target.read_text(encoding='utf-8')==raw
    else:target.write_text(raw,encoding='utf-8')
    decision['final_report_sha256']=sha(target);write_once(ROOT/'delivery-closure-v1/COMPLETE.json',decision)
    print({'status':decision['status'],'contract_items':len(ledger),'production_activated':False,'report':str(target)})

if __name__=='__main__':main()
