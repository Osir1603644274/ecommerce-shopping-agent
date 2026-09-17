"""Render the actual immutable heldout result, retaining undefined queries explicitly."""
from pathlib import Path
from collections import Counter
from statistics import mean
from retrieval_runtime import read_json,sha,write_once
from reviews import rows
ROOT=Path('D:/agent-datasets/search-closure-v1')
SOURCES=('kuaisearch','multicpr')

def fmt(x):return '未定义' if x is None else f'{x:.4f}'
def interval(v):return '['+', '.join(fmt(x) for x in v)+']' if v is not None else '未定义'
def aggregate(values,key):
    by={s:[key(r) for r in values if r['source']==s and key(r) is not None] for s in SOURCES}
    return {'value':mean(mean(v) for v in by.values()) if all(by.values()) else None,
            'eligible_counts':{s:len(v) for s,v in by.items()},'source_means':{s:mean(v) if v else None for s,v in by.items()}}

def main():
    directory=ROOT/'evaluation/final-test-v1';complete=read_json(directory/'COMPLETE.json')
    if complete['report_sha256']!=sha(directory/'report.json'):raise ValueError('Actual final test report changed')
    report=read_json(directory/'report.json');per=rows(directory/'per-query.jsonl')
    assert report['status']=='FINAL_TEST_DESCRIPTIVE_EVALUATION_COMPLETE' and report['query_count']==80 and len(per)==160
    manifest=read_json(ROOT/'test-review/frozen/MANIFEST.json')
    assert manifest['qrels_sha256']==complete['qrels_sha256']==sha(ROOT/'test-review/frozen/qrels.jsonl')
    common=report['common_conditional']['main'];comparison=common['comparisons']['winner'];eligible=set(common['query_ids'])
    by_query={}
    for r in per:by_query.setdefault(r['query_id'],{})[r['method']]=r
    assert len(by_query)==80 and all(set(v)=={'baseline','winner'} for v in by_query.values())
    lines=['# 独立项目留出测试：冻结后的真实结果','',
        '80 条原始 native train 查询构成本项目独立留出，两来源各 40 条；不是数据集官方测试集。每条 80 个候选，共 6400 对模型银标。模型选择与 query 解释均在首次测试检索前冻结。',
        '32 份 A/B 评审及 15 份分歧复核已完成。6180 对 A/B 同级，220 对交第三方；最终按多数规则采纳，三方各异保留 UNKNOWN。没有根据测试结果改标、换模型、重抽 query 或删除候选。',
        f'最终等级分布：{manifest["grade_counts"]}；裁决分布：{manifest["resolution_counts"]}。',
        '',f'共同可评分查询为 {len(eligible)}/80。以下先报告所有原查询的汇总是否可定义，再列共同可评分部分的条件均值；不可评分 query 仍在原数据与逐条报告中。',
        '', '| 方案 | 全部原查询 pooled nDCG@10 界限 | 共同可评分部分界限 |', '|---|---|---|']
    for arm in ('baseline','winner'):
        all_values=report['summary']['main'][arm];v=common['methods'][arm]
        lines.append(f'| {arm}: {report["arms"][arm]["method"]} | {interval([all_values["equal_source_mean_lower"],all_values["equal_source_mean_upper"]])} | {interval([v["equal_source_mean_lower"],v["equal_source_mean_upper"]])} |')
    lines += ['',f'共同可评分部分的新减旧差值区间：{interval(comparison.get("equal_source_mean_difference_interval"))}；10000 次按来源配对 bootstrap 的 95% 外包区间：{interval(comparison.get("ci95_outer_envelope"))}。',
        f'预定统计规则是否支持正向提升：{comparison.get("positive_improvement_supported")}。这是有限候选池、模型银标下的结论，不能替代人工金标或证明全库所有相关商品均已找到。Bootstrap 不覆盖模型裁判共同偏差。',
        '', '| 方案 | 来源 | 共同可评分 query 数 | nDCG@10 界限 |', '|---|---|---:|---|']
    for arm in ('baseline','winner'):
        for source,v in common['methods'][arm]['by_source'].items():
            lo=v.get('mean_lower',v.get('conditional_mean_lower'));hi=v.get('mean_upper',v.get('conditional_mean_upper'))
            lines.append(f'| {arm} | {source} | {v["query_count"]} | {interval([lo,hi])} |')
    auxiliary={};lines += ['', '| 方案 | 已知等级覆盖 judged@10 | Top10 池外文档总数 | Top10 UNKNOWN 总数 |', '|---|---:|---:|---:|']
    for arm in ('baseline','winner'):
        values=[r for r in per if r['method']==arm];auxiliary[arm]={'judged_at_10':aggregate(values,lambda r:r['judged_at_10']),
            'outside_qrel_pool_top10':sum(r['outside_qrel_pool_top10'] for r in values),'unknown_top10':sum(r['unknown_top10'] for r in values),
            'status_counts':dict(Counter(r['status'] for r in values)),'known_pooled_recall':{}}
        a=auxiliary[arm];lines.append(f'| {arm} | {fmt(a["judged_at_10"]["value"])} | {a["outside_qrel_pool_top10"]} | {a["unknown_top10"]} |')
    lines += ['', '| 方案 | 已知相关等级 | 深度 | 两来源等权池内召回 | 有分母 query 数 Kuai/Multi |','|---|---|---:|---:|---|']
    for arm in ('baseline','winner'):
        values=[r for r in per if r['method']==arm]
        for grade in ('grade_ge_2','grade_eq_3'):
            for depth in ('10','100','300'):
                a=aggregate(values,lambda r:r['known_pooled_recall'][grade][depth]['value'])
                auxiliary[arm]['known_pooled_recall'][grade+'@'+depth]=a
                lines.append(f'| {arm} | {grade} | {depth} | {fmt(a["value"])} | {a["eligible_counts"]["kuaisearch"]}/{a["eligible_counts"]["multicpr"]} |')
    lines += ['', '这些召回率的分母仅为已判相关的池内文档；没有已知正例时为未定义，不填零，不外推为全库召回率。','', '## 未进入共同可评分部分的原查询','', '| Query | 来源 | baseline 状态 | winner 状态 |','|---|---|---|---|']
    excluded=[]
    for qid,v in by_query.items():
        if qid in eligible:continue
        row={'query_id':qid,'query':v['baseline']['query'],'source':v['baseline']['source'],'baseline':v['baseline']['status'],'winner':v['winner']['status']};excluded.append(row)
        lines.append('| '+' | '.join(str(row[k]).replace('|','\\|') for k in ['query','source','baseline','winner'])+' |')
    if not excluded:lines.append('| 无 | — | — | — |')
    lines += ['',f'完整逐查询指标：[per-query.jsonl]({(directory/"per-query.jsonl").as_posix()})；正式报告：[report.json]({(directory/"report.json").as_posix()})。',
        '不根据这份测试追加调参。Agent 回答、商城实际重排和生产启用门分别核验；本报告不自动启用新策略。']
    out=ROOT/'reports/final-test.md';raw='\n'.join(lines)+'\n'
    if out.exists():assert out.read_text(encoding='utf-8')==raw
    else:out.write_text(raw,encoding='utf-8')
    write_once(ROOT/'reports/final-test.sources.json',{'report_sha256':sha(out),'generator_sha256':sha(Path(__file__)),
        'auxiliary_metrics':auxiliary,'excluded_from_common_eligibility':excluded,'all_original_queries_retained':80,
        'inputs':[{'path':str(p),'sha256':sha(p)} for p in [directory/'COMPLETE.json',directory/'report.json',directory/'per-query.jsonl',ROOT/'test-review/frozen/MANIFEST.json']],
        'production_activation':False})
    print({'status':'ACTUAL_FINAL_TEST_REPORT_WRITTEN','common_queries':len(eligible),'positive_improvement_supported':comparison.get('positive_improvement_supported'),'sha256':sha(out)})

if __name__=='__main__':main()
