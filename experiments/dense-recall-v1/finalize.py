"""Publish bounded results and actual examples without altering judgments."""
import csv, hashlib, json, math, statistics
from pathlib import Path

P=Path('D:/agent-datasets/dense-recall-20260915-v1')
D=Path('F:/agent/docs/experiments/dense-recall-audit-20260915')
def read(p):return json.loads(p.read_text(encoding='utf8'))
def rows(p):return [json.loads(s) for s in p.read_text(encoding='utf8').splitlines()]
def sha(p):
    h=hashlib.sha256()
    with p.open('rb') as f:
        for b in iter(lambda:f.read(8*1024*1024),b''):h.update(b)
    return h.hexdigest()
diag=read(P/'DIAGNOSTIC.json');obj=read(P/'TRAINING_OBJECTIVE_CHECK.json')
assert not diag['gate']['advance_full_corpus'],'Do not finalize a stopped pilot if it passed'
qs=rows(P/'diagnostic-per-query.jsonl');data={x['query_id']:x for x in rows(P/'dev.jsonl')}
docs={x['document_id']:x['text'] for x in rows(P/'dev-documents.jsonl')}
rankings={}
for f in (P/'evaluation').glob('*/rankings.jsonl'):
    rankings[f.parent.name]={x['query_id']:{d['document_id']:i+1 for i,d in enumerate(x['ranking'])} for x in rows(f)}
examples=[]
for q in sorted(qs,key=lambda x:x['hard_delta'])[:3]+sorted(qs,key=lambda x:x['hard_delta'],reverse=True)[:3]:
    qid=q['query_id'];labels=data[qid]['qrels'];before=rankings['base'][qid]
    changes=[]
    for did,grade in labels.items():
        if not isinstance(grade,int) or grade<2:continue
        after=[rankings['hard-'+str(seed)][qid].get(did) for seed in [20260915,20260916,20260917]]
        if any((r is not None)!=(did in before) for r in after):
            changes.append({'document_id':did,'text':docs[did],'grade':grade,'base_rank':before.get(did),'hard_seed_ranks':after})
    examples.append({**q,'boundary_changes':changes})
(P/'boundary-examples.json').write_text(json.dumps(examples,ensure_ascii=False,indent=2),encoding='utf8')
receipts=[read(f) for f in (P/'runs').glob('*/COMPLETE.json') if f.parent.name!='smoke']
assert len(receipts)==6 and all(x['steps']==51 for x in receipts)
contract=read(P/'RUN_CONTRACT.json')
for f,h in contract['hashes'].items():assert sha(P/f)==h
assert sha(Path('F:/agent/experiments/dense-recall-v1/run.py'))==contract['code_sha256']
oldq=Path('D:/agent-datasets/search-closure-v1/cross-encoder-final-v1/evaluation/dev/qrels-final.jsonl')
assert sha(oldq)==read(P/'PREPARED.json')['qrel_sha256']
trainq=Path('D:/agent-datasets/search-closure-v1/training-preparation/repair-v2/qrels-expanded.jsonl')
assert sha(trainq)=='dedee1b98cab9092021dd2bc64b804a398368db3751f93abe2bea9cc5e4bd574'
with (D/'results.csv').open('w',encoding='utf-8-sig',newline='') as f:
    w=csv.writer(f);w.writerow(['model','source_equal_pool_known_recall100','source_equal_pool_known_recall300','unjudged_top10'])
    for name in ['base']+[f'{a}-{s}' for a in ['random','hard'] for s in [20260915,20260916,20260917]]:
        rr=read(P/'evaluation'/name/'REPORT.json');w.writerow([name,rr['summary']['recall100']['equal_source'],rr['summary']['recall300']['equal_source'],rr['unjudged_top10']])
lines=['# BGE 召回训练：本轮结果', '',
'**状态：受控小样本试验已完成，未通过全库扩展门槛；保留原始 BGE。没有需要用户处理的执行故障。**', '',
'不是“召回训练已经做到极限”，也不是“新模型已上线”。本轮实际完成数据筛查、采样、资源冒烟、六次训练、七版本开发池评测、统计与误差核查。', '',
'## 1. 真实训练输入', '',
'- 原始候选 68 query；训练前隔离两条意图／正负界限不稳的 query，实际 **66 query（KuaiSearch 14、MultiCPR 52）**。原 qrel 未改。',
'- 324 个不同的 query–正例组合；每 query 每 epoch 8 次曝光，总 528 次。随机组涉及 340 个不同 query–负例组合；困难组为 66 个，每个 query 复用一个最高原始 BGE 得分的已知 0 分负例。',
'- 两组 439/528 次负例不同；正例曝光与其余超参数相同。3 个 epoch、3 个固定 seed，每次 51 个优化步。原模型权重重新初始化每个试验，不续接上一组。',
'- 只使用 3 分正例与 0 分负例；1/2/UNKNOWN 不充当负例；无跨 query in-batch negatives。所有标签仍是银标。', '',
'## 2. 开发池结果', '',
'以下为 **33 query 的冻结候选并集**，每 query 361～526 条，共 14,749 个不同文档。Recall 分母是已有 qrel 中等级 ≥2 的文档数，按 query 计算后两来源各占 50%。不是 763.7 万记录全库召回率。训练组展示三个 seed 的均值。', '',
'| 方案 | 池内 known-relevant Recall@100 | 池内 known-relevant Recall@300 | Recall@300 差值 95% 区间 |',
'|---|---:|---:|---|',
f"| 原始 BGE | {diag['arms']['hard']['recall100']['base']:.6f} | {diag['arms']['hard']['recall300']['base']:.6f} | 基线 |"]
for a,label in [('random','随机已知负例'),('hard','困难已知负例')]:
    ci=diag['comparisons']['recall300'][a+'_vs_base']['bootstrap95'];v=diag['arms'][a]
    lines.append(f"| {label} | {v['recall100']['mean']:.6f} | {v['recall300']['mean']:.6f} | [{ci[0]:+.6f}, {ci[1]:+.6f}] |")
lines+=['', '**困难组三个 seed 的 Recall@300：0.884287、0.887818、0.880911。** 两升一降，均值仅比基线高 0.000186；不能只挑 0.887818 报告提升。困难负例也未证明稳定优于随机负例。', '',
'置信区间：固定种子 20260915，按来源分层、query 配对抽样 10,000 次；先对每 query 的三个训练 seed 取均值，再与原模型作差。没有把 3×33 当作独立样本。这里使用开发集选型，区间只作描述性诊断。', '',
'## 3. 是没有训练成功，还是没有迁移收益？', '',
'所有曝光样本、关闭 dropout 的来源等权训练目标：', '',
'| 对照 | 原始权重的 loss | 三次训练后 loss |', '|---|---:|---|']
for a,label in [('random','随机负例'),('hard','困难负例')]:
    ls=[obj[f'{a}-{s}'][a]['source_equal_objective'] for s in [20260915,20260916,20260917]]
    lines.append(f"| {label} | {obj['base'][a]['source_equal_objective']:.6f} | {', '.join(f'{x:.6f}' for x in ls)} |")
lines+=['',
'**训练有效，当前监督未表现出稳定的开发池召回收益。** 梯度、模型参数更新、损失方向、输入正文哈希、样本隔离与两组曝光一致性检查通过。没有证据支持通过改学习率／增加 epoch 就能解决，所以没有临时追加网格搜索。',
f"六次训练记录内合计 {sum(x['seconds'] for x in receipts):.1f} 秒，单次 {min(x['seconds'] for x in receipts):.1f}～{max(x['seconds'] for x in receipts):.1f} 秒；峰值已分配 CUDA 显存 {max(x['peak_allocated_bytes'] for x in receipts)/1024**2:.1f} MiB。此耗时不含进程启动、模型加载和评测。", '',
'## 4. 可见收益、退步及评测边界', '',
'| Query | 原模型 Recall@300 | 困难组三 seed 均值 | 差值 |', '|---|---:|---:|---:|']
for q in examples:lines.append(f"| {q['query']} | {q['base']:.4f} | {q['hard']:.4f} | {q['hard_delta']:+.4f} |")
gaps=rows(P/'coverage-gaps.jsonl')
lines+=['',f"七个版本的实际 Top10 合并后，有 **{len(gaps)} 对缺少标签，涉及 {len({x['query_id'] for x in gaps})} 个 query**；原模型 4 对，各训练版本 35～45 对。已输出 `coverage-gaps.jsonl`，保留原文与来源 ID，未将缺失标签赋成 0。", '',
'这使现有标签可能更有利于旧检索器。例如“教室睡觉抱枕”→“午睡枕办公室午睡神器小学生教室午休趴在桌上睡觉抱枕儿童趴睡枕”也在缺口中。**所以本轮只能说“未通过当前预设门槛”，不能断言新模型真实相关性一定更差。**', '',
'辅助字段 `judged_only_ndcg10` 会先移除未判文档，只用于内部诊断，不能当作实际 Top10 nDCG，更不能与简历中交叉编码精排 nDCG 直接比较。本报告没有据此选模型。', '',
'`boundary-examples.json` 保存六条升降示例对应的真实商品、原等级、原排名和三个 seed 的位置。位置 null 表示未进入 Top300，不代表无关。', '',
'## 5. 停止原因与下一轮入口', '',
'1. 已触发训练前冻结的停止规则：最佳组并非三个 seed 均改善，差值区间跨 0；保留当前应用中的原始 BGE、CE、MART 与索引。',
'2. 未启动 763.7 万记录重编码、全库召回、145 历史查询评测、蒸馏或应用切换。这些是条件步骤，本轮未达到其触发条件。',
'3. 下一轮优先处理评测覆盖：已有 73 对缺口清单，采用独立且隐藏模型排名的复核，再派生新 qrel；原冻结结果保留。新标签下的重算属于补充敏感性分析，不能覆盖本次主结果或称新盲测。',
'4. 数据方面，本轮仅覆盖 66 query，且困难组每 query 仅一个负例。更丰富的同类商品／属性冲突样本是后续可验证假设；当前没有完成“样本不足是唯一原因”的因果证明。',
'5. 不因为负结果自动改用 DPO、增加训练轮数或临时换指标。下一轮应先明确覆盖修订和样本扩展，再冻结新对照；不沿开发失败 query 直接制造训练样本。', '',
'## 6. 复现与产物', '',
'- 脚本：`F:/agent/experiments/dense-recall-v1/`。原始 `run.py` 与训练前代码哈希保持一致。',
'- 运行：`F:/agent/.venv/Scripts/python.exe experiments/dense-recall-v1/batch.py`；只跳过哈希一致且有完整回执的任务，拒绝覆盖未完成目录。',
'- 统计：`analyze.py`；完整训练目标核查：`check_training.py`；发布报告与原始输入复核：`finalize.py`。',
'- 大产物：`D:/agent-datasets/dense-recall-20260915-v1/`，包括六份模型、固定采样表、原始日志、逐 query 排名、统计区间、缺口与案例。',
'- `results.csv` 为七个版本的可编辑数值表；原计划见 PLAN.md，执行修订见 EXECUTION.md。']
(D/'RESULT.md').write_text('\n'.join(lines)+'\n',encoding='utf8')
status={'status':'PILOT_COMPLETE_NO_GO_FULL_CORPUS','training_runs_completed':6,'models_evaluated':7,'full_corpus_executed':False,'online_assets_changed':False,'human_action_required_to_finish_pilot':False,
        'original_training_qrels_sha256':sha(trainq),'original_dev_qrels_sha256':sha(oldq),'next_round_top10_label_gaps':len(gaps)}
(P/'STATUS.json').write_text(json.dumps(status,indent=2),encoding='utf8')
files=[f for f in P.rglob('*') if f.is_file() and f.name!='ARTIFACT_MANIFEST.json']
manifest={'files':{str(f.relative_to(P)).replace('\\','/'):sha(f) for f in files},'report_sha256':sha(D/'RESULT.md'),'csv_sha256':sha(D/'results.csv')}
(P/'ARTIFACT_MANIFEST.json').write_text(json.dumps(manifest,indent=2),encoding='utf8')
print(json.dumps({**status,'training_seconds':sum(x['seconds'] for x in receipts),'peak_mib':max(x['peak_allocated_bytes'] for x in receipts)/1024**2,'manifest_files':len(files),'report':str(D/'RESULT.md')},ensure_ascii=False),flush=True)
