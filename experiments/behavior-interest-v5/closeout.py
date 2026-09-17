"""Close the bounded offline experiment sequence, without deploying a behavior ranker."""
import json,hashlib,sys,html,sqlite3
from pathlib import Path
import numpy as np
HERE=Path(__file__).resolve().parent;ROOT=Path('F:/agent');DATA=Path('D:/agent-datasets');D=ROOT/'docs/experiments/behavior-closeout-20260916';D.mkdir(parents=True,exist_ok=True)
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
 versions={'v3':'behavior-history-ablation-v3','v4':'behavior-interest-v4','v5':'behavior-interest-v5'};allrows=[];checks=[]
 for v,folder in versions.items():
  o=DATA/folder;a=read(o/'ANALYSIS.json');assert read(o/'FINAL_VALIDATION.json')['status']=='PASS'
  report=ROOT/f'docs/experiments/{folder}-20260916'
  for entry in read(report/'MANIFEST.json')['files']:assert sha(Path(entry['path']))==entry['sha256'],entry['path']
  checks.append({'version':v,'manifest':'PASS','files':len(read(report/'MANIFEST.json')['files'])})
  for arm in a['summary']['dev']:
   allrows.append({'version':v,'arm':arm,'dev':a['summary']['dev'][arm],'test':a['summary']['test'][arm]})
 winner=min(allrows,key=lambda r:r['dev']['logloss']);save(D/'SUMMARY.json',{'fits_this_round':24,'epochs_this_round':192,'mean_dev_logloss_winner':winner,'rows':allrows,'manifests':checks,'production_changed':False,'scope':'completed bounded offline history and interest experiments; not all search algorithm optimization'})
 lines=['# 搜索算法主线：本轮实验闭环','','2026-09-16。本轮完成24次训练、192epochs：历史新旧对照6次；兴趣聚合对照9次；低频属性收缩修复9次。现有检索、相关性精排、Agent和Java服务均未切换。','','## 当前可用结论','','1. 修复历史时序与覆盖：同MLP的回归AUC由0.5911提高至0.6045，LogLoss由0.159322降至0.158928。该差值的用户簇95%区间不跨0。','2. 请求内nDCG由0.179801到0.179567，差值区间跨0；不能宣称排序变好。','3. 验证了近期属性均值与候选条件注意力，并根据开发曲线诊断低频属性；第二版只合并低频词表，没有叠加其他调参。',f"4. 全部方案按开发LogLoss比较，保留离线候选 **{winner['version']}/{winner['arm']}**。注意力贡献只看同版本 attention−mean；跨主干差值不能全算作注意力收益。",'','## 所有冻结方案（三seed均值）','','| 版本/方案 | 开发LogLoss↓ | 开发nDCG↑ | 回归AUC↑ | 回归LogLoss↓ | 回归nDCG↑ |','|---|---:|---:|---:|---:|---:|']
 for r in allrows:
  d,t=r['dev'],r['test'];dn=d.get('ndcg10_all_requests');tn=t.get('ndcg10_all_requests');lines.append(f"| {r['version']}/{r['arm']} | {d['logloss']:.6f} | {dn:.6f} | {t['auc']:.6f} | {t['logloss']:.6f} | {tn:.6f} |")
 lines+=['','v3：固定原MLP比较新旧历史。v4：加入品牌/类目embedding后固定主干比较stats、mean、attention。v5：仅将少于20个不同训练请求支持的属性并入UNK。各臂不是8套线上模型。','','## 已完成的验收','','- 50,214个请求的历史快照经过原始行、时间与集合核查；本轮重建旧全部5项历史特征，1,590,566行误差为0。','- 同输入哈希、样本/标签/顺序检查；train-only标准化与词表；同时间请求排除；开发/回归反馈未回流历史。','- 24个checkpoint均按各自开发LogLoss选取；组选择先冻结，再产生本轮历史回归预测。','- 无历史向量归零、重复属性权重等价、历史排列不变的模型检查通过；ROC面积、稳定LogLoss和显式理想排序另行核验。','- 保存逐seed模型、预测、开发曲线、分层指标、用户簇bootstrap区间和推理成本；逐文件哈希复验通过。','','## 还不能回答什么','','- 不能声称线上CTR/GMV上涨。离线只有曝光/点击，缺可靠展示位置与反馈到达时间；购买不能直接按标准点击转化漏斗解释。','- 已暴露回归不等于独立盲测；用户跨时间分区，不能称全冷启动测试。','- KuaiSearch匿名用户历史不是购物应用用户的真实历史；不能伪造ID映射或把公共点击日志直接当商城用户偏好。','- 尚无按真实用户、同候选与业务硬条件验证过的行为排序接入；线上指标、约束不违背和P95未验收。本轮完成的是第6步中的离线验收与复盘。','','## 为什么在这里停止自动加模型','','本轮预设对照和一次由开发证据支持的修复已执行完。继续试架构仍然可能有改进，但现有结果不支持直接接入个性化排序。下一项需要确定业务目标及取得真实用户日志/可靠独立评测数据；这与继续给已见回归集调参不同。具体接入要求已列在SERVING_CONTRACT.md，面试复习见INTERVIEW.md。','','## 原始报告','','- [新旧历史](../behavior-history-ablation-v3-20260916/RESULT.md)','- [兴趣聚合](../behavior-interest-v4-20260916/RESULT.md)','- [低频属性修复](../behavior-interest-v5-20260916/RESULT.md)','- [真实开发案例](./DEV_CASES.md)']
 (D/'RESULT.md').write_text('\n'.join(lines)+'\n',encoding='utf8')
 contract='''# 行为排序接入前的契约（未实施）

## 当前决策

保持现有搜索检索与精排默认。本次行为模型只作为离线研究产物；没有改前端、服务路由、数据库或用户偏好。无法把匿名公共用户ID映射到真实商城用户。

## 最小真实数据需求

- request_id、合法业务user/session标识、query、候选source+item_id、实际展示position、模型版本。
- impression_time、click_time、feedback_available_at、归因窗口；无点击必须在窗口成熟后形成训练标签。
- 当时可用的商品属性快照，避免后来更新属性回填历史；事件去重键。
- 明确隐私告知、留存与删除规则；不要从第三方数据推断用户身份。

## 受控接入顺序

1. 先确定优化目标：查询相关性还是曝光点击；强约束和商品身份仍由原链路保证。
2. 只记录真实显示和操作事件，不把“被召回”冒充曝光；建立未用于开发的新时间窗口。
3. 影子打分，不改变排序。冷用户/字段缺失回退现有排序；不得伪造品牌或用户历史。
4. 验证排序前后候选集合一致、硬条件不被打破、特征缺失比例、超时回退与P95；本轮未运行这类生产验收。
5. 只有经过新的离线验收与明确发布决策，才考虑小流量对照；报告真实曝光分母、点击归因、用户簇不确定性。

需要用户决定/提供的是：是否为真实应用采集行为、可用用户及数据授权、业务目标。不是再次批准本轮离线训练。
'''
 (D/'SERVING_CONTRACT.md').write_text(contract,encoding='utf8')
 interview='''# 面试复盘：问题 → 数据 → 对照 → 结论

## 1. 为什么相关性精排后还做点击模型？
相关性qrel回答“是否符合query”；点击日志回答“当时曝光条件下是否被点击”。标签和偏差不同，因此分开评测。不能把点击未发生解释成商品无关。

## 2. 这次真正解决了什么？
原历史统一冻结在早期前缀，回归有点击历史仅23.69%；按请求时间构建训练历史，并把开发/回归历史冻结在拟合训练期，覆盖变为54.05%。同MLP三seed对照AUC提高，但组内nDCG无可靠收益。

## 3. 为什么AUC涨、排序不涨？
全局AUC跨不同请求比较曝光对；同请求所有候选共享用户活跃度、CTR等统计，可能改善跨请求概率区分，却不改变这个请求内哪件商品排前。候选相关特征才有机会区分同组商品，但不保证收益。

## 4. 注意力到底做了什么？
输入当前商品品牌/类目向量和最近20请求的已点击属性，用[候选、历史、差、乘积]生成权重；同属性重复点击以次数保留。与固定权重均值对照，主干与预算相同。本实现没有真实点击顺序，不是完整DIN复现。

## 5. 如何发现并修复过拟合？
开发误差通常在1–2epoch最低，随后恶化。训练候选28,152品牌中23,374个只出现于不到5个请求。一次固定阈值20，把低频属性收缩到UNK，再跑相同三组对照。没有将低频收缩、正则、学习率同时修改。

## 6. 如何避免“挑个涨点就算成功”？
用开发LogLoss选checkpoint和方案；兴趣模型另要求开发nDCG不退步。冻结后报告历史回归，不按回归换胜者。三seed均值和用户簇配对bootstrap分开处理训练噪声与样本相关性；区间仍条件于这三个固定seed。

## 7. 最重要的限制？
没有精确展示位置、反馈到达时间；不能完整去偏，也不能证明每条历史点击在真实预测时已到达。回归已经见过，不能称未见盲测。匿名公共日志不能直接变成商城用户历史。负结果只能否定本轮配置，不能证明模型达到极限。
'''
 (D/'INTERVIEW.md').write_text(interview,encoding='utf8')
 # Inspect actual dev cases from v3, with aggregate selection and an explicitly named illustrative seed.
 old=DATA/'behavior-search-v1';v3=DATA/'behavior-history-ablation-v3';groups=read(old/'dev_groups.json');y=np.load(old/'dev_y.npy');ds=[]
 for seed in [17,29,43]:
  x=per_request(y,np.load(v3/f'old_seed{seed}_dev.npy'),groups)[1];z=per_request(y,np.load(v3/f'new_seed{seed}_dev.npy'),groups)[1];ds.append(z-x)
 delta=np.mean(ds,axis=0);chosen=np.r_[np.argsort(delta)[:3],np.argsort(-delta)[:3]];wanted=set(chosen.tolist());raw={}
 for i,line in enumerate((old/'dev.jsonl').open(encoding='utf8')):
  if i in wanted:raw[i]=json.loads(line)
 snaps={}
 for i,line in enumerate((DATA/'behavior-history-v2/dev_snapshots.jsonl').open(encoding='utf8')):
  if i in wanted:snaps[i]=json.loads(line)
 db=sqlite3.connect((old/'titles.sqlite3').as_uri()+'?mode=ro',uri=True);cases=[];case_lines=['# 真实开发请求：新旧历史的排序变化','','按三seed平均nDCG变化选出3条下降和3条上升；具体Top3展示固定seed17，不代表集成排序。标题来自旧实验的归一化标题表。点击只是日志观测结果，不是相关性金标。']
 pp={arm:np.load(v3/f'{arm}_seed17_dev.npy') for arm in ['old','new']}
 for i in chosen:
  i=int(i);g=groups[i];r=raw[i];s=snaps[i];case={'sid':g['sid'],'query':r['query'],'mean_seed_ndcg_delta':float(delta[i]),'old_history_clicks':s['old']['clicks'],'new_history_clicks':s['new']['clicks'],'shown_seed':17,'top3':{}}
  case_lines += ['',f"## sid {g['sid']}：{r['query']}",f"三seed平均ΔnDCG={delta[i]:+.4f}；历史点击 {s['old']['clicks']}→{s['new']['clicks']}。",'','| 历史 | 排名 | item_id | 归一化标题 | 点击 |','|---|---:|---:|---|---:|']
  for arm in ['old','new']:
   top=[]
   for rank,j in enumerate(np.argsort(-pp[arm][g['start']:g['end']],kind='stable')[:3],1):
    item=r['impressed_item_ids'][int(j)];title=db.execute('select title from titles where id=?',(item,)).fetchone()[0];clicked=int(item in r['clicked_item_ids']);top.append({'rank':rank,'item_id':item,'normalized_title':title,'clicked':clicked});case_lines.append(f"| {arm} | {rank} | {item} | {title.replace('|','/')} | {clicked} |")
   case['top3'][arm]=top
  cases.append(case)
 db.close();save(D/'DEV_CASES.json',cases);(D/'DEV_CASES.md').write_text('\n'.join(case_lines)+'\n',encoding='utf8')
 # Focused show-me progress artifact; earlier roadmap remains available.
 cards=''.join(f'<tr><td>{r["version"]} / {r["arm"]}</td><td>{r["dev"]["logloss"]:.6f}</td><td>{r["test"]["auc"]:.6f}</td><td>{r["test"]["ndcg10_all_requests"]:.6f}</td></tr>' for r in allrows)
 page='''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>搜索算法实验进度 · 2026-09-16</title><style>
*{box-sizing:border-box}body{margin:0;background:#f7f7f1;color:#203c33;font-family:"Microsoft YaHei",sans-serif;line-height:1.7}main{max-width:1100px;margin:auto;padding:32px 24px}h1{font-size:30px;line-height:1.4}h2{font-size:20px}p{margin:8px 0}.muted,small{color:#64746b}.hero{border-left:5px solid #e65b24;background:#fff0e4;padding:20px;border-radius:0 12px 12px 0}.route{display:grid;grid-template-columns:repeat(6,1fr);gap:10px;margin:24px 0}.node,.card{background:white;border:1px solid #dbe3db;padding:16px;border-radius:12px}.node b{display:block}.num{font-size:12px;color:#28714f}.grid{display:grid;grid-template-columns:1fr 1fr;gap:16px}.value{font-weight:bold;font-size:25px}table{border-collapse:collapse;width:100%;background:white}th,td{padding:10px;text-align:left;border-bottom:1px solid #dde5dc}th{background:#eaf1e7}section{margin:26px 0}details{background:white;padding:16px;border:1px solid #dbe3db;border-radius:10px;margin:12px 0}summary{cursor:pointer;font-weight:bold}a{color:#28714f}button{padding:8px 16px;border:1px solid #aebdaf;background:white;color:#203c33;border-radius:8px;cursor:pointer;margin:4px}button[aria-pressed=true]{background:#203c33;color:white}.explain{padding:16px;background:#eef3e9;border-radius:10px}ul{padding-left:22px}.scroll{overflow:auto}@media(max-width:780px){.route{grid-template-columns:repeat(3,1fr)}.grid{grid-template-columns:1fr}}@media(max-width:450px){.route{grid-template-columns:repeat(2,1fr)}main{padding:20px 14px}h1{font-size:25px}}</style></head><body><main>
<small>SHOW ME · 已执行结果 · 2026.09.16</small><h1>历史修好了，概率预测提高；排序收益仍需证据</h1><div class="hero"><b>本轮完成24次训练，进入第6/6步的离线验收与复盘。</b><p>不是“搜索算法全部完成”，也没有把实验模型切到你的购物应用。完整报告、逐seed结果和模型均已保存。</p></div>
<div class="route"><div class="node"><span class="num">1/6 · 已有</span><b>相关性排序</b><small>CE / LambdaMART</small></div><div class="node"><span class="num">2/6 · 已完成</span><b>行为审计</b><small>曝光、点击与时间</small></div><div class="node"><span class="num">3/6 · 已完成</span><b>点击基线</b><small>LR / MLP 消融</small></div><div class="node"><span class="num">4/6 · 本轮完成</span><b>修复历史</b><small>同MLP六次对照</small></div><div class="node"><span class="num">5/6 · 本轮完成</span><b>兴趣聚合</b><small>注意力及低频修复</small></div><div class="node"><span class="num">6/6 · 离线完成</span><b>冻结与复盘</b><small>线上验收未做</small></div></div>
<div class="grid"><article class="card"><small>同MLP · 旧历史 → 新历史</small><h2>点击概率区分改善</h2><div class="value">AUC 0.5911 → 0.6045</div><p>LogLoss 0.159322 → 0.158928</p><p>按用户簇抽样的ΔLogLoss 95%区间<br>[-0.000743，-0.000038]</p></article><article class="card"><small>同一对照 · 请求内排序</small><h2>没有可靠提升</h2><div class="value">nDCG 0.1798 → 0.1796</div><p>ΔnDCG 95%区间 [-0.001276，0.000852]</p><p>点击标签与相关性qrel不同，不能与旧精排0.83直接比较。</p></article></div>
<section><h2>为什么AUC提高，商品顺序却未改善？</h2><button aria-pressed="true" data-mode="prob">概率预测</button><button aria-pressed="false" data-mode="rank">请求内排序</button><div class="explain" id="explain">用户历史CTR能区分不同请求整体更容易或更难发生点击，因此改善跨请求概率预测。</div></section>
<section><h2>三轮实验，改变了什么？</h2><details open><summary>v3：只更新历史</summary><p>同样本、标签、模型及五项历史特征。训练只读过去请求；开发/回归统一冻结到拟合训练期。三个旧模型重跑完全复现。</p></details><details><summary>v4：固定主干，比较聚合方法</summary><p>统计特征 → 历史均值 → 候选条件注意力。三组候选品牌/类目embedding相同。注意力并未可靠胜出。</p></details><details><summary>v5：只收缩低频属性</summary><p>发现大部分品牌训练支持不足，将少于20个不同训练请求支持的属性并入UNK，再做同样三组对照。不是同时改学习率、正则和网络。</p></details></section>
<section><h2>完整对照（三seed均值）</h2><p class="muted">开发LogLoss用于选型；回归已暴露。v3与v4/v5主干不同，不能把跨版本差值都算成注意力贡献。</p><div class="scroll"><table><thead><tr><th>模型</th><th>开发LogLoss ↓</th><th>回归AUC ↑</th><th>回归nDCG ↑</th></tr></thead><tbody>'''+cards+'''</tbody></table></div></section>
<section class="hero"><h2>接下来需要哪种证据？</h2><p>真实用户的展示位置、点击与反馈到达时间，以及不参与开发的新窗口。公共匿名用户历史不能直接映射到你的商城用户。</p><p>现有检索默认保持。先明确真实行为采集与业务目标，再考虑影子打分或线上对照。</p></section>
<p><a href="../../experiments/behavior-closeout-20260916/RESULT.md">总报告</a> · <a href="../../experiments/behavior-closeout-20260916/INTERVIEW.md">面试复盘</a> · <a href="../../experiments/behavior-closeout-20260916/DEV_CASES.md">真实失败/改善案例</a> · <a href="../../experiments/behavior-closeout-20260916/SERVING_CONTRACT.md">接入边界</a></p><small>数据范围：20,192训练请求，13,481开发请求，16,541历史回归请求。未验证线上CTR、GMV、约束或P95。</small></main><script>const words={prob:'用户历史CTR能区分不同请求整体更容易或更难发生点击，因此改善跨请求概率预测。',rank:'同一请求内所有商品共享用户CTR。只有能够区分候选的属性匹配或兴趣信号，才可能改变这个请求中的商品先后；是否有效还必须实测。'};document.querySelectorAll('[data-mode]').forEach(b=>b.addEventListener('click',()=>{document.querySelectorAll('[data-mode]').forEach(x=>x.setAttribute('aria-pressed',String(x===b)));document.getElementById('explain').textContent=words[b.dataset.mode]}));</script></body></html>'''
 vis=ROOT/'docs/career/search-next-step-20260915/show-me-progress-20260916.html';vis.write_text(page,encoding='utf8')
 paths=[p for p in D.glob('*') if p.is_file() and p.name!='MANIFEST.json']+[vis,HERE/'closeout.py'];save(D/'MANIFEST.json',{'files':[{'path':str(p),'bytes':p.stat().st_size,'sha256':sha(p)} for p in paths]})
 print('CLOSEOUT',winner['version'],winner['arm'],'validated manifests',checks,'visual',vis)
if __name__=='__main__':main()
