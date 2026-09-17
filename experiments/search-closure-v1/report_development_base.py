"""Generate an evidence-linked interim report from actual frozen base results."""
from pathlib import Path
from collections import Counter
import json
from retrieval_runtime import read_json,sha,write_once
from reviews import rows

ROOT=Path('D:/agent-datasets/search-closure-v1')
DEV=Path('D:/agent-datasets/search-stage1-dev-revision-v6')

def escape(value):return str(value).replace('|','／').replace('\n',' ')

def main():
    base=DEV/'frozen/base';gate_path=ROOT/'training-preparation/gates/v6-grid-r2/gate.json'
    report_path=ROOT/'evaluation/dev-grid-r2-base/report.json'
    stats=read_json(base/'RESULTS.json');complete=read_json(base/'COMPLETE.json')
    gate=read_json(gate_path);report=read_json(report_path);values=rows(base/'qrels.jsonl')
    if sha(base/'qrels.jsonl')!=complete['qrels_sha256'] or report['qrels']['sha256']!=complete['qrels_sha256']:
        raise ValueError('Interim report label binding differs')
    lookup={(r['query_id'],r['document_id']):r for r in values}
    lines=['# 开发集基础修订与训练触发：真实阶段结果','',
        '这是完成全配置共同补审之前的阶段结果，不能作为新模型提升、独立测试通过或上线结论。','',
        f"40 条原查询、3,674 对原候选完成独立 A/B 评审；94 对分歧交第三窗口。3,580 对 A/B 同级、87 对三方多数、7 对三方不同保留 UNKNOWN。标签为模型银标，非人工金标。",
        f"新等级数量：{stats['grade_counts']}。1,246 对与 v5 等级不同；551 对 UNKNOWN 未充当负例。33 条 main 全部有可用于比较的增益，没有删掉无效查询来选最强配置。",'',
        '| 配置 | 主评两来源等权 nDCG@10 下界 | 上界 |','|---|---:|---:|']
    for method,summary in sorted(report['summary']['main'].items(),key=lambda x:-x[1]['equal_source_mean_lower']):
        lines.append(f"| {method} | {summary['equal_source_mean_lower']:.6f} | {summary['equal_source_mean_upper']:.6f} |")
    lines+=['','界限包含未判/UNKNOWN 带来的不确定性；下界差不能当真实提升。完整逐查询结果、来源分组、共同分母与配对 bootstrap 见原始报告。','',
        f"训练门选中最强既有 CE 配置 `{gate['selected_method']}`，发现 {gate['selected_error_query_count']} 条 main 查询具有高等级商品在 Top10 外、低等级商品占据 Top10 的证据，超过预定 5 条阈值。因此启动一次新训练数据构建。以下列出全部实际证据。",'',
        '| 查询 | 当前 Top10 中较低等级结果 | 池内更高等级结果 |','|---|---|---|']
    for witness in gate['selected_witnesses']:
        cells=[escape(witness['query'])]
        for side in ('lower','higher'):
            value=witness[side];row=lookup[witness['query_id'],value['document_id']]
            cells.append(escape(f"第{value['rank']}名，{value['grade']}分：{row['document']['title']}；{row['reason']}（{value['document_id']}）"))
        lines.append('| '+' | '.join(cells)+' |')
    lines+=['','下一阶段：200 条已隔离真实训练查询 × 40 候选，独立银标；至少 50 条有效有序等级查询才实际训练。开发标签不用于梯度训练。新的 80 条测试查询保持封存。','',
        f'[冻结标签]({(base/"qrels.jsonl").as_posix()}) · [逐查询报告]({report_path.as_posix()}) · [训练触发证据]({gate_path.as_posix()})',
        '',f"基础 qrels SHA256：`{complete['qrels_sha256']}`"]
    target=ROOT/'reports/development-base.md';target.parent.mkdir(parents=True,exist_ok=True)
    payload=('\n'.join(lines)+'\n').encode('utf-8')
    if target.exists() and target.read_bytes()!=payload:raise ValueError('Interim report changed')
    if not target.exists():target.write_bytes(payload)
    write_once(ROOT/'reports/development-base-evidence.json',{'report_sha256':sha(target),
        'inputs':[{ 'path':str(p),'sha256':sha(p)} for p in [base/'qrels.jsonl',base/'RESULTS.json',gate_path,report_path]],
        'scope':'Actual completed intermediate development only','code_sha256':sha(Path(__file__))})
    print(str(target))

if __name__=='__main__':main()
