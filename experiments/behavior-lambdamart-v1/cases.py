"""Balanced development examples for explaining candidate-level personalization."""
import sqlite3
from run import *

assert read(OUT/'FINAL_VALIDATION.json')['status']=='PASS'
gs=groups('dev');y=np.load(BASE/'dev_y.npy');lo=Layout(y,gs)
# One explicitly identified seed: cases are not the three-seed aggregate.
a=np.load(OUT/'rank16_seed17_dev.npy');b=np.load(OUT/'rank11_seed17_dev.npy')
delta=lo.per(a)-lo.per(b);has=np.load(PREV/'dev_new_x.npy',mmap_mode='r')[lo.starts,15]>0
gain=np.flatnonzero(has&(delta>0));loss=np.flatnonzero(has&(delta<0))
selected=list(gain[np.argsort(-delta[gain])[:3]])+list(loss[np.argsort(delta[loss])[:3]])
needed=set(selected);requests={};snapshots={}
with (BASE/'dev.jsonl').open(encoding='utf8') as f:
    for i,line in enumerate(f):
        if i in needed:requests[i]=json.loads(line)
with Path('D:/agent-datasets/behavior-history-v2/dev_snapshots.jsonl').open(encoding='utf8') as f:
    for i,line in enumerate(f):
        if i in needed:snapshots[i]=json.loads(line)
db=sqlite3.connect('file:'+str(BASE/'titles.sqlite3').replace('\\','/')+'?mode=ro',uri=True)
def title(item):
    row=db.execute('select title from titles where id=?',(item,)).fetchone()
    return row[0] if row else '标题未缓存'
out=[];lines=['# 开发集中的真实行为排序案例','',
'固定 seed17，比较 rank16 与 rank11；各取 3 条提升及退步最大的有历史请求，用于诊断，不代表随机抽样或总体结果。标题来自旧实验规范化标题缓存。','']
for i in selected:
    g=gs[i];r=requests[i];s=snapshots[i];st,en=g['start'],g['end'];items=r['impressed_item_ids']
    entry={'sid':g['sid'],'uid':g['uid'],'query':r['query'],'delta_ndcg10':float(delta[i]),'history':s['new'],'without_history':[],'with_history':[]}
    lines += [f"## 请求 {g['sid']}：{r['query']}",'',f"历史累计点击 {s['new']['clicks']} 次；ΔnDCG@10 {delta[i]:+.6f}。",'','| 排序 | 商品 ID | 规范化标题 | 本次是否点击 |','|---|---|---|---|']
    for key,p,label in [('without_history',b,'无历史特征'),('with_history',a,'有历史特征')]:
        for j in np.argsort(-p[st:en],kind='stable')[:3]:
            item=items[int(j)];t=title(item);clicked=int(y[st+j]);entry[key].append({'item_id':item,'title':t,'clicked':clicked})
            lines.append(f"| {label} | {item} | {t.replace('|','/')} | {clicked} |")
    out.append(entry);lines+=['']
save('DEV_CASES.json',out);(DOC/'DEV_CASES.md').write_text('\n'.join(lines)+'\n',encoding='utf8')
print(DOC/'DEV_CASES.md')
