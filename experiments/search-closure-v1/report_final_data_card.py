"""Data card for completed frozen search labels, independently of Agent gates."""
from collections import Counter
from pathlib import Path
from retrieval_runtime import read_json,sha,write_once
from reviews import rows
ROOT=Path('D:/agent-datasets/search-closure-v1')
DEV=Path('D:/agent-datasets/search-stage1-dev-revision-v6')

def main():
    specifications=[('开发',DEV/'frozen',40,3936),('训练',ROOT/'training-review/frozen',200,8000),('独立项目测试',ROOT/'test-review/frozen',80,6400)]
    summaries=[];inputs=[]
    for label,folder,queries,pairs in specifications:
        complete=read_json(folder/'COMPLETE.json');manifest=read_json(folder/'MANIFEST.json');qpath=folder/'qrels.jsonl'
        assert sha(qpath)==complete['qrels_sha256'] and sha(folder/'MANIFEST.json')==complete['manifest_sha256']
        data=rows(qpath);ids={r['query_id'] for r in data};grades=dict(Counter(str(r['grade']) for r in data))
        assert len(data)==pairs and len(ids)==queries and grades==manifest['grade_counts']
        summaries.append({'name':label,'queries':queries,'pairs':pairs,'grade_counts':grades,'qrels_path':str(qpath),'qrels_sha256':sha(qpath)})
        inputs.extend([folder/'COMPLETE.json',folder/'MANIFEST.json',qpath])
    isolation=read_json(ROOT/'selection/VALIDATION.json');training=read_json(ROOT/'training-preparation/prepared/pairwise-v1/MANIFEST.json')
    assert isolation['status']=='PASS_FROZEN_QUERY_ONLY_AUDIT' and training['valid_query_count']==182 and training['pair_count']==5823
    assert read_json(ROOT/'training-preparation/runs/pairwise-lora-v1/training-complete.json')['status']=='FIXED_THREE_EPOCH_TRAINING_COMPLETE'
    lines=['# 最终搜索数据卡','',
        '本数据卡覆盖已冻结的搜索开发、训练、项目留出标签。Agent 回答质量与商城实际重排另行验收，不由数据卡代替。',
        '', '| 用途 | 查询数 | 候选对数 | 0 | 1 | 2 | 3 | UNKNOWN |','|---|---:|---:|---:|---:|---:|---:|---:|']
    for r in summaries:
        lines.append('| '+' | '.join(str(v) for v in [r['name'],r['queries'],r['pairs'],*[r['grade_counts'].get(str(g),0) for g in [0,1,2,3,'UNKNOWN']]])+' |')
    lines += ['', '开发两来源各 20 query，保留 33 主评、7 诊断；训练各 100；测试各 40，全部 80 条保留为主评。开发原有 3674 对重新审查后，一次共同追加 262 对，全配置 Top10 入池。训练每 query 40 候选，测试每 query 80 候选。',
        '', '搜索范围为 KuaiSearch 6,634,118 条、MultiCPR 电商 1,002,822 条原文档，共 7,636,940 条。送审候选池大小不等于文档库大小。',
        '', '## 查询与隔离',
        '', '开发查询保留项目历史原文，不将其称为新采集的真实用户日志。新训练和测试 query 直接来自本地核验的原始 native train 文件，保留原文、来源行和行哈希；是本项目重新划分的留出集，不是官方测试集。',
        f'实际隔离核验包含 {isolation["old_keys_bruteforce_checked"]} 个既有排除键，跨划分原文/规范化及既定阈值违规数为 {isolation["cross_split_exact_or_threshold_violations"]}。另有逐 query 解释与语义复核记录；不能据此保证没有任何潜在近义重合或基础模型预训练接触。',
        '测试候选在最终模型选择及 80 条 query 解释冻结后生成。评分没有用于改标、换参数、重抽查询或追加有利候选。',
        '', '## 标签与训练',
        '', 'A/B 在独立上下文逐对阅读原始展示字段；分歧交第三个独立上下文，2/3 多数采纳，三方不同保留 UNKNOWN。标签是模型银标，不是人工金标；上下文隔离不消除同模型共同偏差。',
        '等级：3=本体及全部显式要求有支持；2=核心明确、无明确违背，部分其他属性缺证；1=仍有实际替代关系的属性不符商品或直接配件；0=无关或核心不适用。UNKNOWN 无序，绝不转为训练负例。',
        '训练得到 182 个有至少两个不同已知等级的有效 query、5823 个明确有序对，每 query 最多 32 对；相同编码文本不构成训练对。真实执行固定 3 epoch LoRA pairwise softplus(score_low-score_high)，保留所有三个 checkpoint。数据和目标同时改变，不能将差异单独归因于其中一个因素。',
        '', '## 字段与评测边界',
        '', '判定基于已有标题、品牌、类别、卖家字段及原 query。空字段不补造；不由店名推定官方身份或制造品牌，不由食品名称推断医疗效果。原始价格单位未获确认时不换算成人民币，也不把公开语料文档当作可购买商品。',
        '采用有限池 pooled nDCG@10、UNKNOWN 上下界、逐来源指标、池外与已知等级覆盖，以及 grade≥2/grade=3 的池内召回。它不等于全库完整 qrel 或真实全库召回率。',
        '开发用于有限配置选择。一次独立测试中，新旧差值区间仍跨零，不能宣称已证明提升；生产默认未切换。',
        '', '## 冻结文件', '']
    for r in summaries:lines.append(f'- {r["name"]}：[qrels.jsonl]({Path(r["qrels_path"]).as_posix()})；SHA256 `{r["qrels_sha256"]}`。')
    out=ROOT/'reports/final-search-data-card.md';raw='\n'.join(lines)+'\n'
    if out.exists():assert out.read_text(encoding='utf-8')==raw
    else:out.write_text(raw,encoding='utf-8')
    inputs.extend([ROOT/'selection/VALIDATION.json',ROOT/'assets/COMPLETE.json',ROOT/'training-preparation/prepared/pairwise-v1/MANIFEST.json',ROOT/'training-preparation/runs/pairwise-lora-v1/training-complete.json'])
    write_once(ROOT/'reports/final-search-data-card.sources.json',{'report_sha256':sha(out),'generator_sha256':sha(Path(__file__)),'datasets':summaries,
        'inputs':[{'path':str(p),'sha256':sha(p)} for p in inputs],'human_gold':False,'production_activation':False})
    print({'status':'FINAL_SEARCH_DATA_CARD_WRITTEN','datasets':3,'pairs':sum(r['pairs'] for r in summaries),'sha256':sha(out)})

if __name__=='__main__':main()
