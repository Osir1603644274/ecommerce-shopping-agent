"""Export inspected actual grade changes; never change the frozen semantic labels."""
from pathlib import Path
from collections import Counter
from retrieval_runtime import sha,write_once
from reviews import rows
ROOT=Path('D:/agent-datasets/search-closure-v1')
DEV=Path('D:/agent-datasets/search-stage1-dev-revision-v6')
GROUPS={
 '核心明确、其他属性缺证：UNKNOWN → 2':['012ab16555888173aaccf2d6','016273a2cd7df34eb93d3833'],
 '保留本体不明或字段冲突：已知等级 → UNKNOWN':['03204a5c1cad6148115e48bf','0fdc2969fdc83dfc1a9d45c7'],
 '裁判认为有具体替代关系：0 → 1':['00e9a8754ae5829c79763e2c','0e14fff24637d3eb839091fe'],
 '裁判认为核心用途无直接关联：1 → 0':['05dd5eaf66cab06a5965b2f7','0afe21d8889ba2c8532af562'],
 '裁判认为全部要求有支持：UNKNOWN → 3':['0682b37b788b61ead75fc156','0d26a96565c8746b1ee702c8']}

def main():
    changes=DEV/'frozen/base/changes.jsonl';frozen=DEV/'frozen/qrels.jsonl'
    values=rows(changes);by={r['pair_id']:r for r in values};final={r['pair_id']:r for r in rows(frozen)}
    changes_count=sum(r['grade_changed'] for r in values);assert changes_count==1246
    lines=['# 开发集相关性修订：真实前后样例','',
        '以下逐行来自已冻结的 v5 → v6 修订记录，共 1246 对等级发生变化。它们展示模型裁判实际采用的解释，不能作为人工确认正确率。',
        '本次没有修改这些 query 的原文或商品标题，因此没有虚构“query 归一化前后”结果。最终共同补审只新增 262 对，未重写原有 3674 对。',
        '出现 UNKNOWN 不等于没有相关商品；它表示当前这对的意图、本体或证据仍不能稳定定级。','']
    selected=[]
    def cell(v):return str(v).replace('|','\\|').replace('\n',' ')
    for group,ids in GROUPS.items():
        lines += ['## '+group,'','| 原 query | 原商品标题 | 旧等级与理由 | 实际新等级与理由 | 对应记录 |','|---|---|---|---|---|']
        for pid in ids:
            r=by[pid];assert final[pid]['grade']==r['new_grade'] and r['grade_changed']
            fields=[r['query'],r['title'],f"{r['old_grade']}：{r['old_reason']}",f"{r['new_grade']}：{r['new_reason']}",r['document_id']]
            lines.append('| '+' | '.join(map(cell,fields))+' |');selected.append(r)
        lines.append('')
    lines += ['这些例子也保留了可争议判断：近邻替代关系、服装版型同义以及饱腹用途的文本解释仍可能有模型共同偏差。食品示例不证明减重效果或孕期安全。独立上下文一致不等于人工金标。',
        f'完整前后记录：[changes.jsonl]({changes.as_posix()})；最终标签：[qrels.jsonl]({frozen.as_posix()})。']
    out=ROOT/'reports/revision-examples.md';raw='\n'.join(lines)+'\n'
    if out.exists():assert out.read_text(encoding='utf-8')==raw
    else:out.write_text(raw,encoding='utf-8')
    write_once(ROOT/'reports/revision-examples.sources.json',{'report_sha256':sha(out),'generator_sha256':sha(Path(__file__)),
        'changed_pairs':changes_count,'transition_counts':dict(Counter(str(r['old_grade'])+'->'+str(r['new_grade']) for r in values if r['grade_changed'])),
        'examples':selected,'inputs':[{'path':str(p),'sha256':sha(p)} for p in [changes,frozen]],'semantic_labels_changed_by_report':0})
    print({'status':'ACTUAL_REVISION_EXAMPLES_EXPORTED','examples':len(selected),'sha256':sha(out)})

if __name__=='__main__':main()
