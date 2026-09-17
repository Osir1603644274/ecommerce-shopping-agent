"""Reconcile independent findings without modifying frozen experiments or audit text."""
from pathlib import Path
from retrieval_runtime import read_json,sha,write_once
ROOT=Path('D:/agent-datasets/search-closure-v1')

def main():
    auditdir=ROOT/'delivery-audit/final-independent-native-v1'
    collected=read_json(auditdir/'COLLECTED.json')
    for name,digest in collected['outputs'].items():assert sha(auditdir/name)==digest
    audit=read_json(auditdir/'final-audit.json')
    assert {x['id'] for x in audit['findings']}=={'F1','F2','F3'}
    training=ROOT/'reports/training-run-current.md'
    tr=read_json(ROOT/'reports/training-run-current.sources.json')
    assert sha(training)==tr['report_sha256']
    quality=read_json(ROOT/'agent-quality-review-v2/frozen/report.json')
    assert quality['agent_gate_passed'] is False
    commerce=read_json(ROOT/'commerce-component-v1/DIAGNOSIS.json')
    assert commerce['actual_ce_inferences']==0
    evidence=[auditdir/'COLLECTED.json',auditdir/'final-audit.md',auditdir/'final-audit.json',training,
        ROOT/'reports/training-run-current.sources.json',ROOT/'reports/delivery-overview.md',
        ROOT/'agent-quality-review-v2/frozen/report.json',ROOT/'commerce-component-v1/DIAGNOSIS.json',
        ROOT/'commerce-prerequisite-audit-v1/REPORT.json']
    resolutions=[{'finding':'F1','state':'OPEN_EXTERNAL_PRICE_PREREQUISITE','action':'Require authoritative verified price evidence before actual commerce CE positive/fallback/compare; no invented prices or loosened eligibility.'},
        {'finding':'F2','state':'RETAIN_BASELINE_GATE_NOT_MET','action':'Keep existing default. Frozen quality failure is retained; do not rerun, tune, or relabel to force a positive result. This is an activation blocker, not a requirement to manufacture improvement.'},
        {'finding':'F3','state':'CORRECTED_IN_NEW_REPORT_VERSION','action':'training-run-current.md updates only the stage paragraph; original report, source bindings and numerical training facts preserved.'}]
    result={'status':'INDEPENDENT_AUDIT_RECONCILED_FULL_GOAL_PARTIAL','full_goal_complete':False,
        'search_experiment_deliverable':True,'production_activation':False,'resolutions':resolutions,
        'remaining_required_execution':['Actual commerce CE positive path','Actual controlled provider failure fallback comparison','Actual compare after CE'],
        'inputs':[{'path':str(p),'sha256':sha(p)} for p in evidence],'generator_sha256':sha(Path(__file__))}
    write_once(ROOT/'delivery-audit/FINAL_RECONCILIATION.json',result)
    previous=ROOT/'reports/delivery-overview.md';raw=previous.read_text(encoding='utf-8')
    raw=raw.replace('**搜索数据、固定训练、开发选择和一次留出测试已完成；整体目标仍有商城实测缺口。保留原默认策略。**',
        '**独立总审已完成：11项合同中9项通过、2项部分完成。搜索实验可交付；整体目标仍有商城实测缺口。保留原默认策略。**')
    raw=raw.replace('[训练记录](D:/agent-datasets/search-closure-v1/reports/training-run.md)',
        '[训练记录（当前阶段说明）](D:/agent-datasets/search-closure-v1/reports/training-run-current.md)')
    raw=raw.replace('独立总审需对照最初11条合同核验现有证据，并处理真实发现。',
        '独立总审已按最初11条合同完成。旧训练报告的阶段措辞已在新版本修正，原文件保留；质量门失败继续保留为启用阻塞，商城实测缺口仍未解决。')
    raw+='\n## 独立总审与发现处理\n\n'
    raw+=f'- [完整独立审计]({(auditdir/"final-audit.md").as_posix()})\n'
    raw+=f'- [逐项处理与未完成清单]({(ROOT/"delivery-audit/FINAL_RECONCILIATION.json").as_posix()})\n'
    raw+='- 总审未重新运行模型或测试；重新核验了来源行、标签多数逻辑、17个资产哈希、运行绑定、历史文件及已记录测试。通过范围和未独立重做的事项见审计原文。\n'
    raw+='- 本地另一报价路径明确为 local_simulated，不能作为本次真实权威价格的替代。原搜索测试与Agent回答均已消费，不继续调参或补跑。\n'
    target=ROOT/'reports/delivery-overview-current.md'
    if target.exists():assert target.read_text(encoding='utf-8')==raw
    else:target.write_text(raw,encoding='utf-8')
    write_once(ROOT/'reports/delivery-overview-current.sources.json',{'report_sha256':sha(target),
        'inputs':[{'path':str(p),'sha256':sha(p)} for p in [previous,ROOT/'reports/delivery-overview.sources.json',ROOT/'delivery-audit/FINAL_RECONCILIATION.json']],
        'generator_sha256':sha(Path(__file__)),'old_reports_preserved':True,'full_goal_complete':False})
    print({'status':result['status'],'report':str(target),'findings_resolved_in_documentation':['F3'],'remaining_execution_items':3})

if __name__=='__main__':main()
