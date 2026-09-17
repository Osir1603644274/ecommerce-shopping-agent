"""Report actual frozen training artifacts; no inference or semantic labeling."""
import json
from pathlib import Path
from retrieval_runtime import read_json,sha,write_once

ROOT=Path('D:/agent-datasets/search-closure-v1')

def main():
    run=ROOT/'training-preparation/runs/pairwise-lora-v1'
    complete=read_json(run/'training-complete.json')
    cycle=read_json(ROOT/'training-preparation/TRAINING_CYCLE_DECIDED.json')
    if cycle.get('branch')!='three_epochs_completed' or cycle['training_complete']['sha256']!=sha(run/'training-complete.json'):
        raise ValueError('Actual training cycle is not verified against this completion')
    if complete.get('epochs')!=3 or complete.get('global_steps')!=1092:
        raise ValueError('Unexpected actual training schedule')
    config=read_json(run/'config.json');started=read_json(run/'STARTED.json')
    prepared=ROOT/'training-preparation/prepared/pairwise-v1/MANIFEST.json'
    if sha(prepared)!=config['prepared_manifest_sha256']:
        raise ValueError('Prepared input changed')
    data=read_json(prepared);labels=read_json(ROOT/'training-review/frozen/MANIFEST.json')
    records=[read_json(run/'checkpoints'/f'epoch-{n}'/'complete.json') for n in (1,2,3)]
    lines=['# 普通商品搜索：实际微调记录','',
        '状态：固定三轮训练已完成，训练产物验收通过；最终开发集选参、留出测试与 Agent 实验仍未完成。本报告不声明检索效果提升。','',
        f"训练标签：{labels['pairs']} 对独立模型银标，UNKNOWN {labels['grade_counts'].get('UNKNOWN',0)} 对；模型银标不是人工金标。",
        f"训练输入：200 个隔离真实查询中，{data['valid_query_count']} 个具有可用有序样本，共 {data['pair_count']} 对。未知标签不作为负例，文本完全相同的高低样本不进入有序对。",'',
        '模型：固定 bge-reranker-base，query/value LoRA 与分类器训练；每对输入 query、较高等级文档和较低等级文档，目标为 softplus(score_low-score_high)。',
        f"实际可训练参数：{started['trainable_parameters']:,}；总优化步 {complete['global_steps']}。",'',
        '| 轮次 | 累计优化步 | 训练平均损失 | 本轮优化循环耗时（秒） |','|---|---:|---:|---:|']
    for n,record in enumerate(records,1):
        s=record['summary'];lines.append(f"| {n} | {s['global_step']} | {s['mean_loss']:.6f} | {s['elapsed_seconds']:.3f} |")
    lines+=['','耗时仅指优化循环，不包含前置文件核验、依赖加载和后续评测。损失下降不等于 nDCG 提升。','',
        '本轮同时改变训练数据和训练目标，后续差异不能单独归因于其中一项。三个检查点均进入已限定的开发对照，不根据训练损失选最终模型。','',
        '首次系统 Python 启动在依赖导入阶段失败，未执行优化步。随后使用已有 F:/agent/.venv 环境完成训练；冻结数据和训练参数未改变。','',
        '复核入口：training-preparation/TRAINING_CYCLE_DECIDED.json、prepared/pairwise-v1/AUTHOR_CHAIN_VERIFIED.json、runs/pairwise-lora-v1/training-complete.json 及三个 checkpoints/epoch-*/complete.json。']
    out=ROOT/'reports/training-run.md'
    if out.exists() and out.read_text(encoding='utf8')!='\n'.join(lines)+'\n':raise ValueError('Training report changed')
    out.parent.mkdir(parents=True,exist_ok=True)
    if not out.exists():out.write_text('\n'.join(lines)+'\n',encoding='utf8')
    sources=[prepared,run/'config.json',run/'STARTED.json',run/'training-complete.json',ROOT/'training-preparation/TRAINING_CYCLE_DECIDED.json',ROOT/'training-review/frozen/MANIFEST.json']+[run/'checkpoints'/f'epoch-{n}'/'complete.json' for n in (1,2,3)]
    write_once(ROOT/'reports/training-run.sources.json',{'report_sha256':sha(out),'script_sha256':sha(Path(__file__)),'inputs':{str(p):sha(p) for p in sources},'semantic_judgments_generated':0,'inference_calls':0})
    print(json.dumps({'path':str(out),'sha256':sha(out),'epochs':3,'steps':1092}))

if __name__=='__main__':main()
