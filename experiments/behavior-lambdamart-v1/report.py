"""Publish the completed, verified experiment without changing legacy reports."""
from run import *

assert read(OUT/'FINAL_VALIDATION.json')['status']=='PASS'
r=read(OUT/'RESULT.json');v=read(OUT/'VALIDATION.json');DOC.mkdir(parents=True,exist_ok=True)
names={'mlp16':'既有 MLP（16 特征）','binary16':'GBDT/BCE（16 特征）','rank16':'LambdaMART（16 特征）','rank11':'LambdaMART（11 特征，无用户历史）'}
lines=['# KuaiSearch 用户行为排序：同特征树模型对照','',
'2026-09-17。完成 9 次树模型训练，与已有 3 种子 MLP 预测对照。数据、候选和标签保持一致，应用默认排序未切换。','',
'## 结果（三随机种子指标均值）','',
'| 方案 | 开发 nDCG@10 | 历史回归 nDCG@10 | 回归正点击请求 nDCG@10 |',
'|---|---:|---:|---:|']
for a in names:
    d=r['splits']['dev']['models'][a];t=r['splits']['test']['models'][a]
    lines.append(f"| {names[a]} | {d['ndcg10_all']:.6f} | {t['ndcg10_all']:.6f} | {t['ndcg10_positive']:.6f} |")
lines+=['','## 固定对照与归因','',
'| 对照（新减旧） | 历史回归 ΔnDCG@10 | 用户簇 95% 区间 | 解释 |','|---|---:|---|---|']
explain={'binary16-minus-mlp16':'同特征方案对照；主干和选 checkpoint 口径不同',
'rank16-minus-binary16':'固定树配置与特征，比较排序目标和 BCE',
'rank16-minus-mlp16':'最终排序方案差值，不能全部归因于历史特征',
'rank16-minus-rank11':'固定 LambdaMART，检验 5 项用户历史特征增量'}
for k,c in r['splits']['test']['comparisons'].items():
    lines.append(f"| {k} | {c['delta']:+.6f} | [{c['ci95'][0]:+.6f}, {c['ci95'][1]:+.6f}] | {explain[k]} |")
chosen=r['selection']['selected_tree'];delta=r['splits']['test']['comparisons']['rank16-minus-rank11']
lines+=['','## 选择与下一步','',f"按开发集三种子平均 nDCG 选出的树方案：**{chosen}**。选择文件在本轮历史回归推理之前写入。",
'历史特征的增量区间'+('完全高于 0，支持本协议下的行为个性化收益。' if delta['ci95'][0]>0 else '未完全高于 0，尚不能宣称行为个性化收益稳定。'),
'下一项可独立增加近期点击商品与候选的关系特征；不得使用这次回归结果继续选择配置。真实应用接入仍需自己的行为历史、同候选与硬条件测试。','',
'## 数据与指标口径','',
'- train/dev/历史回归请求数：20,192 / 13,481 / 16,541；曝光数：647,637 / 448,658 / 494,271。',
'- 16 特征沿用已审计 v3/new：11 项文本匹配/前缀统计，加 5 项严格过去的用户历史；本轮没有新增语义模型分数或文本编码。',
'- 保留全部请求，零点击请求记 0；另列正点击请求结果。主指标是每请求 nDCG 算术均值，不是相关性 qrel 的 pooled nDCG。',
'- 按原始分数排序，同分稳定保留原候选顺序（并非可靠展示位置）；LambdaMART 分数不裁剪为概率、不计算未校准 LogLoss。',
'- LightGBM 自定义早停指标与本地口径一致；原 MLP 按 dev LogLoss 选 checkpoint，树按 dev nDCG 选，属于方案而非纯主干因果对照。',
'- 全部树使用相同 15 叶、min_data_in_leaf=100、学习率 0.05、L2=1、max_bin=63、80% bagging，最多 300 轮、早停 30 轮；种子 17/29/43。',
'- 2000 次用户簇配对 bootstrap，先平均三种子的逐请求指标。区间条件于这三种子；多项对比未进行多重校正。',
'- 训练前读取所有分区只做标签/候选对齐审计；训练和配置选择仅依赖 train/dev。历史回归已经暴露，不是新的独立盲测。',
'- 曝光点击不等同人工相关性，未处理展示位置偏差；不能由离线差值声称线上 CTR 或成交额收益。','',
'## 验证与产物','',
'输入 SHA256 不变、原实验输入链核验、请求分区隔离、逐请求候选标签对齐、全量独立 nDCG 实现对照和开发早停选择检查全部通过。',
f'- 数据、模型、逐请求分数、区间：`{OUT.as_posix()}`。',
'- 新脚本：`F:/agent/experiments/behavior-lambdamart-v1/`；业务代码未改。',
'- LightGBM 4.6.0 / NumPy 1.26.4；复用已存在的隔离依赖，没有修改业务环境。',
'- 参考开源方法：https://github.com/William-Huang274/kuaiSearch 。未直接复制源码，其公开分数不作为本实验基线。','',
'## 复现','',
'```powershell',
"$env:BEHAVIOR_MART_ROOT='D:/agent-datasets/behavior-lambdamart-reproduce-001'",
"foreach ($phase in 'prepare','train','evaluate','verify') {",
'  & F:/agent/.venv/Scripts/python.exe F:/agent/experiments/behavior-lambdamart-v1/run.py $phase',
'  if ($LASTEXITCODE -ne 0) { throw "Experiment failed: $phase" }','}','```',
'复现目录必须不存在；保留本次模型、结果和哈希。report.py 固定写本次报告，复现时不要覆盖这份文档。']
(DOC/'RESULT.md').write_text('\n'.join(lines)+'\n',encoding='utf8')
print(DOC/'RESULT.md')
