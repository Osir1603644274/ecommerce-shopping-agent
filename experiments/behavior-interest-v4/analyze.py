"""Paired user-cluster analysis and sealed report for interest models."""
import json,hashlib,sys
from pathlib import Path
import numpy as np
HERE=Path(__file__).resolve().parent;O=Path('D:/agent-datasets/behavior-interest-v4');OLD=Path('D:/agent-datasets/behavior-search-v1');D=Path('F:/agent/docs/experiments/behavior-interest-v4-20260916')
sys.path.insert(0,str(HERE.parent/'behavior-history-ablation-v3'))
from run import per_request
def read(p):return json.loads(p.read_text(encoding='utf8'))
def save(p,o):p.write_text(json.dumps(o,ensure_ascii=False,indent=2),encoding='utf8')
def sha(p):
 h=hashlib.sha256()
 with p.open('rb') as f:
  for b in iter(lambda:f.read(1048576),b''):h.update(b)
 return h.hexdigest()
def main():
 r=read(O/'RESULT.json');runs=r['runs'];selection=read(O/'FROZEN_SELECTION.json');summary={};contrasts={};arrays={};gates={}
 for sp in ['dev','test']:
  groups=read(OLD/f'{sp}_groups.json');y=np.load(OLD/f'{sp}_y.npy',mmap_mode='r');tokens=np.load(O/f'{sp}_tokens.npy');uids=np.array([g['uid'] for g in groups]);counts=np.array([g['end']-g['start'] for g in groups]);arrays[sp]={}
  summary[sp]={arm:{key:float(np.mean([rr[sp][key] for rr in runs if rr['arm']==arm])) for key in ['auc','logloss','ndcg10_all_requests','ndcg10_positive_requests','brier','ece_10_equal_width']} for arm in ['stats','mean','attention']}
  for arm in ['stats','mean','attention']:
   vals=[per_request(y,np.load(O/f'{arm}_seed{s}_{sp}.npy'),groups) for s in [17,29,43]]
   arrays[sp][arm]=np.mean([v[0] for v in vals],axis=0),np.mean([v[1] for v in vals],axis=0)
  contrasts[sp]={}
  for left,right in [('mean','stats'),('attention','mean'),('attention','stats')]:
   subsets={}
   for co,mask in [('all',np.ones(len(groups),dtype=bool)),('no_recent_click',tokens==0),('recent_1_5',(tokens>0)&(tokens<=5)),('recent_6_plus',tokens>5)]:
    users,idx=np.unique(uids[mask],return_inverse=True);n=len(users);req=np.bincount(idx);exp=np.bincount(idx,weights=counts[mask]);dl=np.bincount(idx,weights=(arrays[sp][left][0]-arrays[sp][right][0])[mask]);dn=np.bincount(idx,weights=(arrays[sp][left][1]-arrays[sp][right][1])[mask]);rng=np.random.default_rng(20260916);boot=[]
    for _ in range(2000):
     draw=rng.integers(0,n,n);boot.append([dl[draw].sum()/exp[draw].sum(),dn[draw].sum()/req[draw].sum()])
    ci=np.percentile(boot,[2.5,97.5],axis=0);subsets[co]={'requests':int(mask.sum()),'users':n,'delta_logloss':float(dl.sum()/exp.sum()),'delta_logloss_ci95':ci[:,0].tolist(),'delta_ndcg10':float(dn.sum()/req.sum()),'delta_ndcg10_ci95':ci[:,1].tolist()}
   contrasts[sp][f'{left}-minus-{right}']=subsets
 # Freeze validation: source hashes, checkpoint selection, shapes, vocabulary bounds, numeric base unchanged.
 for path,h in read(O/'INPUTS.json').items():assert sha(Path(path))==h,path
 assert len(runs)==9;epochs=read(O/'EPOCHS.json');assert len(epochs)==72
 for rr in runs:
  ep=[e for e in epochs if e['arm']==rr['arm'] and e['seed']==rr['seed']];assert rr['epoch']==min(ep,key=lambda e:e['dev_logloss'])['epoch']
  for sp in ['dev','test']:
   p=np.load(O/f"{rr['name']}_{sp}.npy");assert p.shape==np.load(OLD/f'{sp}_y.npy',mmap_mode='r').shape and np.isfinite(p).all() and (p>=0).all() and (p<=1).all()
 assert selection['logloss_winner']==min(summary['dev'],key=lambda a:summary['dev'][a]['logloss'])
 eligible=[a for a in ['mean','attention'] if summary['dev'][a]['logloss']<summary['dev']['stats']['logloss'] and summary['dev'][a]['ndcg10_all_requests']>=summary['dev']['stats']['ndcg10_all_requests']]
 assert selection['candidate_after_ranking_gate']==(min(eligible,key=lambda a:summary['dev'][a]['logloss']) if eligible else 'stats')
 save(O/'FINAL_VALIDATION.json',{'status':'PASS','fits':9,'epochs':72,'input_hashes_unchanged':True,'dev_selection_verified':True,'no_production_switch':True})
 analysis={'summary':summary,'contrasts':contrasts,'selection':selection,'bootstrap':{'unit':'user_cluster','resamples':2000,'seed':20260916,'seed_aggregation':'average metrics, not prediction ensemble','limitation':'fixed three training seeds; exploratory subgroup intervals; historical regression only'}};save(O/'ANALYSIS.json',analysis)
 D.mkdir(parents=True,exist_ok=True)
 lines=['# 候选相关兴趣建模：固定数据的三组对照','','2026-09-16。9次训练、72epochs，完整结果已封存；未切换购物应用。','','## 结果','','| 分区 | 模型 | AUC↑ | LogLoss↓ | 请求内nDCG@10↑ |','|---|---|---:|---:|---:|']
 for sp,arms in summary.items():
  for arm,m in arms.items():lines.append(f"| {sp} | {arm} | {m['auc']:.6f} | {m['logloss']:.6f} | {m['ndcg10_all_requests']:.6f} |")
 lines+=['',f"开发LogLoss最优：**{selection['logloss_winner']}**；同时要求开发nDCG不低于stats后，保留候选：**{selection['candidate_after_ranking_gate']}**。选择文件先于本轮test预测。",'','## 差值及不确定性','','每列为前者减后者。LogLoss负数更好，nDCG正数更好；2000次按用户簇配对bootstrap，三seed的逐请求指标先取平均。','','| 分区 | 对照 | ΔLogLoss [95%区间] | ΔnDCG [95%区间] |','|---|---|---|---|']
 for sp,con in contrasts.items():
  for name,sub in con.items():
   m=sub['all'];a=m['delta_logloss_ci95'];b=m['delta_ndcg10_ci95'];lines.append(f"| {sp} | {name} | {m['delta_logloss']:+.6f} [{a[0]:+.6f}, {a[1]:+.6f}] | {m['delta_ndcg10']:+.6f} [{b[0]:+.6f}, {b[1]:+.6f}] |")
 lines+=['','## 固定了什么，改变了什么','','- 保持20,192训练请求/647,637曝光；13,481开发请求；16,541已暴露历史回归请求。','- 固定新历史16项数值特征和候选品牌/类目embedding、MLP主干、训练预算与seeds。','- stats不使用历史向量；mean增加近期点击属性均值；attention只替换mean的聚合权重，用当前候选决定关注哪些历史属性。','- 最近20请求包含零点击与边界同时间组；同属性合并保留次数。加权重复等价、历史排列不变、无历史归零三项模型不变量通过。','- 品牌/类目词表仅在train拟合；未知ID和字段缺失均映射UNK，不能用测试集扩词表。','- 这是品牌/类目候选条件注意力的小实现，不是原版DIN复现，不包含商品文本编码、点击顺序建模或CTR多目标训练。','- 新主干加入候选embedding，因此相对v3的总差值不能全部归因为注意力；注意力贡献只能看attention-vs-mean。','','## 各seed与推理成本','','推理为同机GPU分批处理494,271曝光，包含特征读取/传输，不是接口P95或线上请求延迟。','','| 模型/seed | epoch | dev LogLoss | 回归nDCG | 回归推理秒 | 总参数 |','|---|---:|---:|---:|---:|---:|']
 for rr in runs:lines.append(f"| {rr['name']} | {rr['epoch']} | {rr['dev']['logloss']:.6f} | {rr['test']['ndcg10_all_requests']:.6f} | {rr['test_inference_seconds']:.2f} | {rr['parameters_total']} |")
 lines+=['','## 解释与限制','','1. 预测目标是日志曝光下的点击，未点击不是人工判定不相关。无原始展示位置，不能纠正位置偏差；没有逐点击到达时间，历史可用性仍是离线假设。','2. nDCG按请求内二元点击计算，零点击请求记0。它与相关性qrel的0.83不同，不可横向比较。','3. 相同用户可跨时间分区，目标是时间外推而非全冷启动。历史回归已反复暴露，不称盲测。','4. 约20%候选缺失或未知三级类目，历史只含品牌/类目；负结果不代表所有兴趣模型无效。','5. 分层与置信区间见ANALYSIS.json；固定种子区间不包含训练随机性，未做多重比较校正。','','## 复现','','```powershell','$env:PYTHONIOENCODING="utf-8"','$env:CUBLAS_WORKSPACE_CONFIG=":4096:8"','.venv/Scripts/python.exe experiments/behavior-interest-v4/run.py build','.venv/Scripts/python.exe experiments/behavior-interest-v4/run.py train','.venv/Scripts/python.exe experiments/behavior-interest-v4/analyze.py','```','','禁止覆盖既有build/train。原始和派生产物以MANIFEST.json记录哈希；全量从头复跑需另开版本目录。']
 (D/'RESULT.md').write_text('\n'.join(lines)+'\n',encoding='utf8')
 paths=[p for root in [O,HERE] for p in root.glob('*') if p.is_file()]+[D/'RESULT.md'];save(D/'MANIFEST.json',{'files':[{'path':str(p),'bytes':p.stat().st_size,'sha256':sha(p)} for p in paths]})
 print(json.dumps({'summary':summary,'selection':selection,'test_contrasts':{k:v['all'] for k,v in contrasts['test'].items()}},ensure_ascii=False,indent=2))
if __name__=='__main__':main()
