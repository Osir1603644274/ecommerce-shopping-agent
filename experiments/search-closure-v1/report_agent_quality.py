"""Summarize completed v2 quality judgments without rejudging or rerunning answers."""
from pathlib import Path
from collections import Counter
from retrieval_runtime import read_json, sha, write_once
from reviews import rows
import agent_quality_review_v2 as quality

ROOT=Path('D:/agent-datasets/search-closure-v1')

def main():
    quality.verify_projection()
    folder=ROOT/'agent-quality-review-v2/frozen'
    complete=read_json(folder/'COMPLETE.json'); report=read_json(folder/'report.json')
    assert sha(folder/'report.json')==complete['report_sha256']
    assert sha(folder/'answers.jsonl')==report['answers_sha256']
    answers=rows(folder/'answers.jsonl')
    assert len(answers)==40
    lines=['# Agent 真实回答质量：v2 独立盲审结果','',
        '本报告对应原有 20 对、40 个真实运行槽位。未新增模型调用或检索；失败回答保留 NOT_ASSESSABLE。标签为独立上下文模型银标，不是人工金标。',
        '40 次最终模型响应均触及 512 completion tokens 上限，finish_reason=length；39 条非空正文的控制器成功不能等同完整回答。质量审查仅覆盖实际输出，不能外推充分输出预算下的能力。',
        '', '原 v1 送审视图遗漏了生成模型实际看见的 score。v2 从各自原始模型请求恢复全部 400 个原始分数，逐条验证与实际工具记录一致；回答、文档文本、评分规则及原始运行均未改变。v1 评审已标明不作为最终依据。',
        '', '| 检查项 | 基线 20 条 | 新方案 20 条 |','|---|---|---|']
    for check in quality.engine.CHECKS:
        lines.append('| '+check+' | '+' | '.join(str(report['check_counts'][arm][check]) for arm in ('baseline','winner'))+' |')
    lines += ['', '| 方案 | 有用性等级分布 |', '|---|---|']
    for arm in ('baseline','winner'):
        counts=dict(Counter(str(r['useful_grade']) for r in answers if r['arm']==arm))
        lines.append(f'| {arm} | {counts} |')
    lines += ['',f'配对有用性结论：{report["usefulness"]["status"]}。不删除失败或 UNKNOWN 条目凑齐可计算的配对提升。',
        f'延迟门通过：{report["latency_gate"]}；新方案必需检查全部通过：{report["winner_required_checks_all_pass"]}；新方案没有显式要求 FAIL：{report["no_winner_hard_constraint_failures"]}。',
        f'Agent 启用门通过：{report["agent_gate_passed"]}。生产默认未切换。',
        '', '## 已判失败的实际例子', '', '| 方案 | Query ID | 检查项 | 原回答摘录 | 复核解释 |', '|---|---|---|---|---|']
    for arm in ('baseline','winner'):
        shown=0
        for row in answers:
            if row['arm']!=arm:continue
            failures={c for c,v in row['checks'].items() if v=='FAIL'}
            if not failures:continue
            finding=next((f for judge in ('primary','review','third') if row.get(judge) for f in row[judge]['findings'] if f['criterion'] in failures and row[judge]['checks'][f['criterion']]=='FAIL'),None)
            if finding:
                values=[arm,row['query_id'],finding['criterion'],finding['answer_quote'],finding['explanation']]
                lines.append('| '+' | '.join(str(v).replace('|','\\|').replace('\n',' ') for v in values)+' |')
                shown+=1
                if shown==3:break
    lines += ['', '示例摘录来自独立审查原文，未重新判分；完整逐回答结论、原始审查理由和作者绑定均在下列冻结文件中。',
        f'- [全部回答审查]({(folder/"answers.jsonl").as_posix()})',
        f'- [正式汇总]({(folder/"report.json").as_posix()})',
        '', '本次仅验证 catalog_evidence 路线。商城可购买商品、完整网页购物控制器、取消/新建需求的真实线上运行不能由此推定；商城重排仍受权威价格缺失影响。']
    target=ROOT/'reports/agent-quality-v2.md'
    raw='\n'.join(lines)+'\n'
    if target.exists():assert target.read_text(encoding='utf-8')==raw
    else:target.write_text(raw,encoding='utf-8')
    inputs=[folder/'COMPLETE.json',folder/'report.json',folder/'answers.jsonl',ROOT/'agent-quality-review-v2/PROJECTION_RECEIPT.json',ROOT/'reports/agent-output-budget.sources.json']
    write_once(ROOT/'reports/agent-quality-v2.sources.json',{'report_sha256':sha(target),'generator_sha256':sha(Path(__file__)),
        'inputs':[{'path':str(p),'sha256':sha(p)} for p in inputs], 'human_gold':False,'agent_gate_passed':report['agent_gate_passed'],'production_activation':False})
    print({'report':str(target),'sha256':sha(target),'agent_gate_passed':report['agent_gate_passed']})

if __name__=='__main__':main()
