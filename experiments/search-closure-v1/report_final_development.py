"""Summarize the frozen actual development experiment without claiming heldout gains."""
from pathlib import Path
from retrieval_runtime import read_json,sha,write_once
ROOT=Path('D:/agent-datasets/search-closure-v1')

def main():
    report_path=ROOT/'evaluation/dev-final-grid-r3-strongest-old/report.json'
    selection_path=ROOT/'final-selection/SELECTION.json'
    report=read_json(report_path);selection=read_json(selection_path)
    main=report['common_conditional']['main'];winner=selection['selected']['method'];base=selection['strongest_old']['method']
    diff=main['comparisons'][winner]
    assert report['baseline']==base and diff['positive_improvement_supported'] is False
    def interval(lo,hi):return f'[{lo:.4f}, {hi:.4f}]'
    lines=['# 最终开发集实验：已冻结，尚不能证明新模型优于最强旧方案','',
        '40 条原查询全部保留：33 条主查询、7 条诊断查询。3936 对独立上下文模型银标，包含 600 个 UNKNOWN；不是人工金标。',
        '35 种配置使用同一最终 qrel。所有配置 Top10 均进入判断池，但进入判断池不代表等级已确定。',
        '主比较按来源等权，KuaiSearch 14 条、MultiCPR 19 条；诊断结果保留在原报告。指标是候选池内 pooled nDCG@10，不等于全库完整相关性。','',
        f'冻结的最强旧方案：`{base}`。冻结的新选择：`{winner}`。',
        f'新减旧差值区间：{interval(*diff["equal_source_mean_difference_interval"])}；10000 次配对分层 bootstrap 的 95% 外包区间：{interval(*diff["ci95_outer_envelope"])}。',
        '区间跨过零，不能写“已证明提升”。开发集用于选择，本结果也不能替代独立测试；同模型银标的系统偏差未被 bootstrap 覆盖。','',
        '| 配置 | 主查询 pooled nDCG@10 区间 | KuaiSearch 区间 | MultiCPR 区间 |',
        '|---|---|---|---|']
    compact=[]
    for name,v in sorted(main['methods'].items()):
        a=v['by_source']['kuaisearch'];b=v['by_source']['multicpr']
        # Source aggregates use the same mean-bound fields as the common cohort.
        al=a.get('mean_lower',a.get('conditional_mean_lower'));au=a.get('mean_upper',a.get('conditional_mean_upper'))
        bl=b.get('mean_lower',b.get('conditional_mean_lower'));bu=b.get('mean_upper',b.get('conditional_mean_upper'))
        lines.append(f'| {name} | {interval(v["equal_source_mean_lower"],v["equal_source_mean_upper"])} | {interval(al,au)} | {interval(bl,bu)} |')
        compact.append({'method':name,'lower':v['equal_source_mean_lower'],'upper':v['equal_source_mean_upper']})
    lines += ['', '配置解释：bm25/character/dense 为单路；w111/w211/w112 分别为三路 RRF 的 1:1:1、2:1:1、1:1:2 权重；no_dense 去掉 dense。none 无交叉编码重排，base 为原预训练模型，epoch1–3 为既有训练，new_epoch1–3 为本次固定三轮训练。',
        '这些配置支持通道、融合及模型阶段对照；新旧训练同时改变数据与训练目标，不能把差异归因于其中一个因素。',
        '无分数并列，因此未执行延迟决胜。批量 grid 时间不能充当在线 P95。生产默认未切换。','',
        f'完整逐查询证据：[report.json]({report_path.as_posix()})；选择凭据：[SELECTION.json]({selection_path.as_posix()})。']
    out=ROOT/'reports/final-development.md';out.parent.mkdir(parents=True,exist_ok=True)
    text='\n'.join(lines)+'\n'
    if out.exists():assert out.read_text(encoding='utf-8')==text
    else:out.write_text(text,encoding='utf-8')
    write_once(ROOT/'reports/final-development.sources.json',{'report_sha256':sha(out),'methods':compact,
        'inputs':[{'path':str(p),'sha256':sha(p)} for p in [report_path,selection_path]],'generator_sha256':sha(Path(__file__)),
        'development_only':True,'positive_improvement_supported':False,'production_activation':False})
    print({'status':'FINAL_DEVELOPMENT_REPORT_WRITTEN','methods':len(compact),'path':str(out),'sha256':sha(out)})

if __name__=='__main__':main()
