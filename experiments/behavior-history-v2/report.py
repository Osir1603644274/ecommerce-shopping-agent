"""Write the human-readable audit and hash manifest from verified artifacts."""
import csv,json
from pathlib import Path
import snapshots as s

DOC=Path('F:/agent/docs/experiments/behavior-history-v2-20260915');DOC.mkdir(parents=True,exist_ok=True)
r=s.read(s.OUT/'REPORT.json');v=s.read(s.OUT/'VALIDATION.json');assert v['status']=='PASS'
examples=s.read(s.OUT/'RAW_EXAMPLES.json');table=[];csvrows=[]
names={'train':'训练抽样','dev':'开发抽样','test':'历史回归'}
for part,entry in r['partitions'].items():
    c=entry['counts'];n=c['tasks'];old=c['old_has_click_history'];new=c['new_has_click_history']
    table.append(f"| {names[part]} | {n:,} | {old:,}（{old/n:.2%}） | {new:,}（{new/n:.2%}） | +{new-old:,} |")
    csvrows.append({'partition':part,**c})
with (DOC/'coverage.csv').open('w',encoding='utf-8-sig',newline='') as f:
    fields=list(dict.fromkeys(k for row in csvrows for k in row));w=csv.DictWriter(f,fieldnames=fields);w.writeheader();w.writerows(csvrows)
length_rows=[]
for part,entry in r['partitions'].items():
    a=entry['lengths']
    length_rows.append(f"| {names[part]} | {a['old_requests']['p50']:g} → {a['new_requests']['p50']:g} | {a['old_clicks']['p50']:g} → {a['new_clicks']['p50']:g} | {a['new_requests']['p90']:g} | {a['new_clicks']['p90']:g} |")
example_rows=[]
for e in examples:
    t=e['target'];prior=', '.join(f"{x['session_id']}@{x['time_index']}" for x in e['last_history_requests']) or '无'
    excluded=', '.join(f"{x['session_id']}@{x['time_index']}" for x in e['excluded_same_time_peers']) or '无'
    example_rows.append(f"| {names[e['partition']]}／{e['type']} | {t['query'].replace('|','/')} | {t['session_id']} / {t['time_index']} | {e['old']['clicks']} → {e['new']['clicks']} | {prior} | {excluded} |")
allowed=r['source_periods']['eligible_source_requests'];held=r['source_periods']['heldout_native_train']
doc='''# 行为历史快照：审计完成

2026-09-15。状态：**主线第4/6步中的历史快照准备与校验完成；尚未重训模型。**

## 1. 结论

旧实验把所有样本历史固定在time_index≤192005，导致可用历史覆盖有限。本轮保持原样本和标签不变，训练使用严格过去的请求，开发与回归使用同一拟合训练期截止747693的历史。历史覆盖明显增加，但这不是模型收益，也不能据此推断nDCG会涨。

## 2. 固定时间边界

| 预测分区 | 允许历史 | 明确排除 |
|---|---|---|
| train | 同用户，原生train，time_index严格小于当前请求 | 当前请求、同时间请求、未来请求 |
| dev | 同用户，原生train，time_index≤747693 | 开发期反馈和原生test反馈 |
| test历史回归 | 与dev相同，截止747693 | 原生train后20%的开发期反馈和全部test反馈 |

原生train约80%之前的全部请求可作历史，包括没有抽入训练样本的请求。原始train文件中的后20%已被用作开发，不能因为文件写着train就把它们的反馈加入开发特征。

旧抽样继续保留：训练20,192请求、开发13,481请求、回归16,541请求，共50,214。全局商品/品牌/类目流行度等11项基础特征未更新，未改变模型、候选或标签。

**不可验证的假设：** 更早请求的最终反馈在后续请求时已可用。源数据没有逐点击时间、反馈到达时间或归因窗口；本次可验证请求级时间隔离，不能证明反馈级绝无泄漏。因此不能称生产实时历史重建。

## 3. 真实覆盖变化

“有点击历史”指此前至少有一个已记录点击的请求—商品对，不代表该用户有长序列。

| 分区 | 请求数 | 旧历史有点击 | 新历史有点击 | 新增覆盖 |
|---|---:|---:|---:|---:|
'''+ '\n'.join(table)+'''

| 分区 | 历史请求数中位数：旧→新 | 历史点击对数中位数：旧→新 | 新历史请求数P90 | 新历史点击对数P90 |
|---|---:|---:|---:|---:|
'''+ '\n'.join(length_rows)+f'''

本轮允许的来源期共{allowed['requests']:,}请求、{allowed['clicks']:,}个点击对，涉及{allowed['users']:,}用户。原生train中另有{held['requests']:,}个开发期请求，其反馈全部隔离。此前“test有10,519请求可用历史”的统计使用完整原生train；本轮不能直接采用该数字，因为需排除开发期。

近期历史定义为最后20个请求，保留零点击请求。若第20个请求边界有同时间请求，整组纳入；不按sid伪造先后，不把time_index换算成天数。训练及开发各发现1个目标的近期窗口因此超过20条。

## 4. 实际样例

下表的sid@time均来自原始文件。历史只列最近3条作展示，快照文件保留全部允许历史。点击数为累计点击对数，允许同商品在不同请求重复出现。

| 分区／案例 | 原query | 当前sid / time | 累计历史点击：旧→新 | 最近历史sid@time | 被排除的同时间sid@time |
|---|---|---|---|---|---|
'''+ '\n'.join(example_rows)+f'''

完整当前请求、历史请求和同时间请求保存在 `D:/agent-datasets/behavior-history-v2/RAW_EXAMPLES.json`。这些是展示与验证资料，不进入未来预测输入。

## 5. 检查了什么

- 从原文件重新读取并比对全部 **{v['checks']['raw_index_rows']:,}** 条请求索引：用户、时间、split、曝光数、点击列表、字节位置及行哈希。
- 对 **{v['checks']['exact_history_set']:,}** 个样本逐一比较实际快照与全部允许来源的集合，共核对 **{v['checks']['history_memberships']:,}** 个历史请求引用；检查不只看最大时间，也检查没有漏记录或混入别的用户。
- 训练抽样中24个目标存在同时间同用户的其他请求，均未读取彼此反馈。
- 反算旧5项历史特征中的3项：user_log_clicks、user_ctr、has_click_history，覆盖 **{v['checks']['old_history_feature_rows']:,}** 条候选特征行，与旧矩阵一致；本阶段尚未重新构建品牌/类目份额特征。
- 原抽样文件、组索引与标签文件哈希保持不变；开发和test反馈未进入新快照；保留原实验结果。

## 6. 下一步：单独验证历史更新的贡献

现在具备准备同模型对照的条件，尚未执行训练：

1. 按新快照重建原有5项历史特征，其余11项基础特征不变；保持原请求、候选、标签及顺序。
2. 同MLP、同3个seed、同优化器与预算，比较旧历史与新历史；标准化仅拟合训练特征，开发选型。
3. 同时报概率指标及请求内排序指标，按有/无历史分层；原生test继续称已暴露历史回归。
4. 先完成这项对照，再分别判断新增近期特征、类别embedding或候选相关注意力是否值得做，避免同时改变多个因素。

没有充分依据要求用户重新提供数据，也没有启动DIN、SFT、DPO或更新应用。若需真实反馈时点或无偏点击估计，仍需数据作者补充日志定义/时间信息；这不阻止当前假设明确的离线实验。

## 7. 复现与文件

- 计划/代码：`F:/agent/experiments/behavior-history-v2/`
- 数据目录：`D:/agent-datasets/behavior-history-v2/`
- `requests.sqlite3`：只读原件的派生索引；`train/dev/test_snapshots.jsonl`：每请求历史分组与累计统计；`INDEX.json`：来源散列；`REPORT.json`：统计；`VALIDATION.json`：校验。
- 可从F:/agent运行 `.venv/Scripts/python.exe experiments/behavior-history-v2/validate.py` 复核，不会训练或调用LLM。
- `coverage.csv` 可直接查看覆盖统计；派生数据没有修改原始日志、旧qrel、模型或业务数据库。
'''
(DOC/'RESULT.md').write_text(doc,encoding='utf8')
files=[]
for root in [s.OUT,Path(__file__).resolve().parent,DOC]:
    for p in sorted(root.rglob('*')):
        if p.is_file() and '__pycache__' not in p.parts and p.name!='MANIFEST.json':files.append({'path':str(p),'bytes':p.stat().st_size,'sha256':s.digest(p)})
s.write(DOC/'MANIFEST.json',{'status':'HISTORY_SNAPSHOTS_AUDITED_NO_TRAINING','files':files})
print(str(DOC/'RESULT.md'),len(files),'files sealed',flush=True)
