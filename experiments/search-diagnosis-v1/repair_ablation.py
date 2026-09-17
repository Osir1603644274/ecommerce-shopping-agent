"""Same saved candidate pools: distinguish filter from evidence-tier merge."""
import json,sys
from pathlib import Path
from collections import OrderedDict
sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from agent.app.catalog_requirements import assess,select_groups
ROOT=Path('D:/agent-datasets/search-agent-repair-20260913-v1')
OLD=Path('D:/agent-datasets/search-agent-diagnosis-20260913-v1')
def req(f,v,terms):return dict(facet=f,value=v,mode='require',terms=terms)
rows=[]
for cid in ['M02-T2','M03-T1','M04-T2','M04-T4','M06-T4']:
    if cid=='M02-T2':rs=[req('商品','狗粮',['犬粮']),dict(facet='配方',value='鸡肉',mode='exclude',terms=['鸡肉'])]
    elif cid=='M03-T1':rs=[req('商品','洗面奶',['洁面乳','洗面乳']),req('品牌','玫琳凯',['玫琳凯'])]
    else:rs=json.loads((ROOT/'plan-check001'/(cid+'.json')).read_text('utf8'))['plan']['requirements']
    raw=json.loads((OLD/'multi-live'/(cid+'.json')).read_text('utf8'))
    scope=raw['after']['catalogSearch']['scope'];groups=OrderedDict()
    for rank in range(10):
        for s in scope['sources']:
            if rank>=len(s['hits']):continue
            h=s['hits'][rank];m=s['metadata'][rank];key=m['titleGroupKey']
            g=groups.setdefault(key,dict(title=m['fields']['title']['value'],members=[]))
            g['members'].append(dict(source=h['source'],docid=h['docid'],brand=m['fields']['brand']['value']))
    before=list(groups.values())
    checked=[dict(g,constraintEvidence=assess(g['title'],g['members'],rs)) for g in before]
    conflict=lambda g:any(e['mode']!='prefer' and e['status']=='conflict' for e in g['constraintEvidence'])
    filtered=[g for g in checked if not conflict(g)]
    merged,excluded=select_groups(before,rs)
    row=dict(id=cid,requirements=rs,original=checked[:6],filterOnly=filtered[:6],filterAndMerge=merged[:6],
             originalExplicitConflicts=sum(conflict(g) for g in checked[:6]),afterExplicitConflicts=sum(conflict(g) for g in merged[:6]),
             interpretation='same20candidate_pool_only; unknown retained; no qrel scoring or new retrieval')
    assert not row['afterExplicitConflicts']
    rows.append(row)
    print(cid,row['originalExplicitConflicts'],'->',row['afterExplicitConflicts'],[g['title'] for g in merged[:2]])
with (ROOT/'CANDIDATE-ABLATION.json').open('x',encoding='utf8') as f:json.dump(rows,f,ensure_ascii=False,indent=2)
