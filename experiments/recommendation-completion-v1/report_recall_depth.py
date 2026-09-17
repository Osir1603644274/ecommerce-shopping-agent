"""Render the completed depth ablation without modifying frozen evidence."""
import json
from pathlib import Path

run=Path('D:/agent-datasets/recommendation-completion-v1/recall-depth-dev-20260917-001')
out=Path('F:/agent/docs/experiments/recommendation-depth-20260917')

def main():
    r=json.loads((run/'RESULT.json').read_text(encoding='utf-8'))
    v=json.loads((run/'VALIDATION.json').read_text(encoding='utf-8'))
    assert all(v.values())
    out.mkdir(parents=True,exist_ok=True)
    lines=['# 推荐召回深度开发对照','',
        '2026-09-17。固定992位开发用户、训练期历史、ItemCF/TF-IDF/MiniLM三路算法、向量与原LambdaMART模型，只改变每路候选深度。未训练、未更换BM25、未读取final留出集、未切换Agent默认模型。','',
        '每路Top100/200/300/600，经等权RRF（常数60）选至多600件，再用原12特征树模型排序，输出Top100。商品ID去重，不注入正确答案。对各深度轮换执行顺序，预热后测量本地CPU耗时。','',
        '## 全部开发用户结果','',
        '| 每路深度 | 实际候选中位数 | 池覆盖率 | 理想nDCG@10 | 树nDCG@10 | 树Recall@100 | 本地P50/P95 ms |',
        '|---|---:|---:|---:|---:|---:|---:|']
    for d in [100,200,300,600]:
        c=r['coverage'][str(d)]['all'];m=r['metrics'][f'tree_{d}']['all'];t=r['latency_ms'][str(d)]
        lines.append(f"| {d} | {r['pool_sizes'][str(d)]['capped']['median']:.0f} | {c['recall']:.2%} | {c['oracle_ndcg10']:.5f} | {m['ndcg_at_10']:.5f} | {m['recall_at_100']:.2%} | {t['p50']:.1f}/{t['p95']:.1f} |")
    lines += ['', '覆盖率与Recall均按用户宏平均；不是命中目标数除以总目标数。理想nDCG将池中目标放最前，分母保留全部真实目标，仅作上限诊断。这里是开发集，不能与此前925用户final表格直接对比。', '',
        '## 候选截断与冷暖商品','', '| 每路深度 | 截断前/后覆盖目标 | 暖目标入池/Top100/Top10 | 冷目标入池/Top100/Top10 |', '|---|---:|---:|---:|']
    # Exact target-level top10 counts, kept separate from macro metrics.
    truth={x['request_id']:set(x['target_item_ids']) for x in map(json.loads,Path('D:/agent-datasets/recommendation-unified-v1/amazon-luxury-dev-001/labels.private.jsonl').read_text(encoding='utf-8').splitlines())}
    fit=json.loads(Path('D:/agent-datasets/recommendation-unified-v1/amazon-luxury-dev-001/fit_artifacts.json').read_text(encoding='utf-8'))
    warm=set(fit['positive_item_user_counts'])
    predictions=list(map(json.loads,(run/'predictions.jsonl').read_text(encoding='utf-8').splitlines()))
    for d in [100,200,300,600]:
        c=r['coverage'][str(d)];m=r['metrics'][f'tree_{d}'];counts={}
        for name in ['warm','cold']:
            n=0
            for p in predictions:
                if p['arm']!=f'tree_{d}':continue
                t=truth[p['request_id']];t=t&warm if name=='warm' else t-warm
                n+=len(set(p['item_ids'][:10])&t)
            counts[name]=n
        lines.append(f"| {d} | {c['all']['pre_hit']}/{c['all']['hit']} | {c['warm']['hit']}/{m['warm_item']['retrieved_targets']}/{counts['warm']} | {c['cold']['hit']}/{m['cold_item']['retrieved_targets']}/{counts['cold']} |")
    lines += ['', '## 配对差值与选择','', '| 每路深度相对100 | nDCG@10差值 | 95%用户配对Bootstrap区间 | 胜/负/平用户 |', '|---|---:|---|---|']
    for c in r['comparisons']:
        m=c['metrics']['ndcg_at_10'];lines.append(f"| {c['left']} | {m['difference']:+.5f} | [{m['ci95'][0]:+.5f}, {m['ci95'][1]:+.5f}] | {m['win_users']}/{m['loss_users']}/{m['tie_users']} |")
    lines += ['', f"预先规定按开发nDCG最高、同分选更小深度，得到每路 **Top{r['selected_depth']}**。这是开发选择，不是新增独立验证结论。三个对照区间未做多重比较校正，不能据此宣称已证明新方案泛化胜出。", '',
        '## 解释与下一步','',
        '- 区分每路深度与最终候选600；较深三路并集可能超过600，RRF截断可能丢掉新覆盖目标。截断前后都报告，避免误把融合损失归给召回模型。',
        '- 特征公式不变，但候选新进入更深的某条召回路后，相应倒数排名特征会从0变为非0；这是召回深度变化引发的输入变化，不能把全部差值归为单纯候选数量收益。',
        '- 原树模型仍是Top100三路训练候选分布，本轮不重训。若扩大候选后精排不涨，需要单独检验训练与新候选分布的匹配。',
        '- 后续可固定开发选定深度，再做TF-IDF与BM25单变量对照；或对扩大候选后的训练分布与冷商品特征做独立实验。不能同时换召回、特征与训练再归因。',
        '- 用户历史仍固定在原训练截止日；本轮没有检验扩充完整Amazon或使用更近历史是否有收益。','',
        '## 复现与完整性','',
        '基线992条开发请求的候选列表、RRF Top100和树Top100均与已冻结原件完全一致；原nDCG重新计算一致。输入哈希读取前后不变。协议在预测前保存，先冻结全部预测再加载开发标签评分。',
        f'- 完整产物：`{run.as_posix()}`。',
        '- 脚本：`F:/agent/experiments/recommendation-completion-v1/recall_depth_dev.py`；固定输出目录，已有尝试会拒绝覆盖。',
        '- `PROTOCOL.json`、`PREDICTED.json`、`VALIDATION.json`、`MANIFEST.json`保存参数、哈希和验证。',
        '- `pools.jsonl`含截断前后候选；`predictions.jsonl`与`per_user.jsonl`保留逐请求结果。',
        '- 耗时包含本地召回、12特征与树推理，不含模型加载、HTTP、LLM；是单次预热顺序观测，不表示服务并发容量。']
    (out/'REPORT.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')
    print(out/'REPORT.md')

if __name__=='__main__':main()
