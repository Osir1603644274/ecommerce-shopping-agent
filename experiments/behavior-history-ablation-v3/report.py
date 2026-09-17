import json,hashlib
from pathlib import Path
O=Path('D:/agent-datasets/behavior-history-ablation-v3');D=Path('F:/agent/docs/experiments/behavior-history-ablation-v3-20260916');D.mkdir(parents=True,exist_ok=True)
a=json.loads((O/'ANALYSIS.json').read_text());s=json.loads((O/'FROZEN_SELECTION.json').read_text())
lines=['# 历史更新对照：概率预测改善，排序尚无可靠收益','','2026-09-16。第4/6步完成：6次MLP训练，48个epoch。原始实验与服务保持不变。','','## 结论','','新历史按开发集平均LogLoss胜出；三个旧方案复跑的epoch、开发/回归LogLoss与原实验完全一致。历史覆盖增加有助于点击概率预测，但不能据此宣称商品排序或线上CTR提升。','','| 分区 | 历史 | AUC↑ | LogLoss↓ | 请求内nDCG@10↑ |','|---|---|---:|---:|---:|']
for sp in ['dev','test']:
 for arm in ['old','new']:
  m=a['summary'][sp][arm];lines.append(f"| {'开发' if sp=='dev' else '已暴露历史回归'} | {arm} | {m['auc']:.6f} | {m['logloss']:.6f} | {m['ndcg10_all_requests']:.6f} |")
lines+=['','## 不确定性与分层','','以下差值均为新减旧，LogLoss负数更好，nDCG正数更好。2000次用户簇配对bootstrap；三个固定seed先取逐请求指标平均，不是预测集成，也不包含训练随机性的总体区间。分层为探索性分析。','','| 分区/群体 | 请求 | ΔLogLoss [95%区间] | ΔnDCG [95%区间] |','|---|---:|---|---|']
for sp,parts in a['cohorts'].items():
 for co,m in parts.items():
  l=m['delta_logloss_ci95'];n=m['delta_ndcg10_ci95'];lines.append(f"| {sp}/{co} | {m['requests']} | {m['delta_logloss']:+.6f} [{l[0]:+.6f}, {l[1]:+.6f}] | {m['delta_ndcg10']:+.6f} [{n[0]:+.6f}, {n[1]:+.6f}] |")
lines+=['','## 对照与校验','','- 同20,192训练请求、13,481开发请求、16,541历史回归请求；候选和标签不变。','- 原11项基础特征不变，只替换5项历史特征的可用历史；1,590,566行旧五项历史特征重建误差为0。','- 同MLP、BCE、Adam、8epochs、4096batch及seeds17/29/43；标准化均只拟合各臂train。','- 训练读取严格过去请求；dev/test只读取拟合训练期≤747693的原生train历史。未知反馈到达时间仍为数据限制。','- 按开发LogLoss选checkpoint和臂；冻结后才产生本轮test预测。已见test不能变成新盲测。','- nDCG是日志内二元点击标签、每请求独立计算，零点击请求记0；与相关性qrel实验的0.83不可直接比较。','- 无可核验展示位置，曝光偏差未纠正；同分沿原日志候选顺序，不能称真实位置。','','## 下一步与面试讲法','','开发集已有历史的7,997请求中，5,476条的品牌或类目份额能够区分候选，其余单靠累计统计难以个性化重排。这是建模容量的诊断线索，不证明注意力必然有效。按既定计划固定数据比较历史统计、近期平均聚合、候选条件注意力。','','可讲：先发现历史冻结过早，按请求时间修复历史并验证全量特征；同主干三seed对照使AUC提升，但排序收益不显著，因此继续拆解概率预测与候选匹配。不可讲：线上CTR提高、推荐系统已上线、模型已优化到极限。','','## 复现','','```powershell',' $env:PYTHONIOENCODING="utf-8"',' $env:CUBLAS_WORKSPACE_CONFIG=":4096:8"',' .venv/Scripts/python.exe experiments/behavior-history-ablation-v3/run.py build',' .venv/Scripts/python.exe experiments/behavior-history-ablation-v3/run.py train',' .venv/Scripts/python.exe experiments/behavior-history-ablation-v3/run.py analyze',' .venv/Scripts/python.exe experiments/behavior-history-ablation-v3/run.py verify','```','','build/train禁止覆盖已存在实验；完整重跑需新版本目录。只读检查可运行verify。','','数据与逐seed预测：`D:/agent-datasets/behavior-history-ablation-v3`。']
(D/'RESULT.md').write_text('\n'.join(lines)+'\n',encoding='utf8')
files=list(O.glob('*'))+list(Path(__file__).parent.glob('*'))+[D/'RESULT.md'];out=[]
for p in files:
 if p.is_file():out.append({'path':str(p),'bytes':p.stat().st_size,'sha256':hashlib.sha256(p.read_bytes()).hexdigest()})
(D/'MANIFEST.json').write_text(json.dumps({'files':out},ensure_ascii=False,indent=2),encoding='utf8');print('SEALED',len(out))
