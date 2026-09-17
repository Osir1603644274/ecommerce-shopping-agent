"""Conservative title evidence, never product truth or model relevance labels."""
import re
from .catalog_data import available

MATERIALS={'wood':('木头','木质','木制','木夹','竹木','竹制','竹夹'),
           'plastic':('塑料',),'steel':('不锈钢',),'aluminium':('铝合金',)}
BOARD_TERMS=('主板','电路板','线路板','pcb板')
PART_TERMS=BOARD_TERMS+('模块','外壳','配件','芯片','保护壳','手机壳')

def evidence_terms(r):
    terms=list(dict.fromkeys([r['value']]+[t.strip() for t in r.get('terms',[]) if t.strip()]))
    terms=[t for t in terms if len(t)>1 or t==r['value']]
    if r['facet']=='商品' and any(t in r['value'].casefold() for t in BOARD_TERMS):
        terms+=list(BOARD_TERMS)
    if r['facet']=='材质':
        for aliases in MATERIALS.values():
            if any(a in r['value'] for a in aliases):terms+=list(aliases)
    return list(dict.fromkeys(terms))
def occurrences(text,term):
    positive=negative=False
    for m in re.finditer(re.escape(term.casefold()),text.casefold()):
        if term[0].isascii() and term[0].isalnum() and m.start() and text[m.start()-1].isascii() and text[m.start()-1].isalnum():continue
        if term[-1].isascii() and term[-1].isalnum() and m.end()<len(text) and text[m.end()].isascii() and text[m.end()].isalnum():continue
        # Negation must immediately modify this term: 无谷鸡肉 still contains 鸡肉.
        neg=bool(re.search(r'(?:不含|不带|没有|不用系?|无需系?|不必系?|不要|非|无|免|不)$',text[max(0,m.start()-4):m.start()]))
        negative|=neg;positive|=not neg
    return positive,negative

def assess(title,members,requirements):
    evidence=[]
    for r in requirements:
        terms=evidence_terms(r)
        hits=[(t,*occurrences(title,t)) for t in terms]
        yes=[t for t,p,n in hits if p];no=[t for t,p,n in hits if n]
        status='unknown';reason='标题未提供足够证据'
        if r['facet']=='预算':
            reason='没有经过核验的价格，标题数字不能当成交价'
        elif r['mode'] in {'exclude','avoid'}:
            device_terms=[re.sub(r'(整机|本体)$','',t) for t in terms]
            accessory=any(q['mode']=='require' and q['facet']=='商品'
                and any(b and b!=q['value'] and b in q['value'] for b in device_terms)
                and any(len(t)>1 and t not in device_terms and occurrences(title,t)[0]
                    for t in evidence_terms(q)+list(PART_TERMS)) for q in requirements) if r['facet']=='商品' else False
            if accessory:
                reason='设备名也是所需配件的适配对象，不能仅凭子串判为整机'
            elif yes and not no:status='conflict';reason='标题明确包含排除项：'+yes[0]
            elif no and not yes:status='supported';reason='标题否定描述：'+no[0]
        elif yes and not no:
            status='supported';reason='标题出现：'+yes[0]
        elif no and not yes:
            status='conflict';reason='标题否定所需属性：'+no[0]
        elif r['facet']=='品牌':
            brands=[str(m.get('brand')).strip() for m in members if available(m.get('brand')) and str(m.get('brand')).casefold() not in {'other','others'}]
            if brands and all(not any(t.casefold() in b.casefold() for t in terms) for b in brands):
                status='conflict';reason='来源品牌不同：'+'、'.join(sorted(set(brands)))
        elif r['facet']=='材质':
            expected={k for k,aliases in MATERIALS.items() if any(a in t for t in terms for a in aliases)}
            found={k for k,aliases in MATERIALS.items() if any(occurrences(title,a)[0] for a in aliases)}
            if expected and found and not expected&found:
                status='conflict';reason='标题明确写有其他材质'
        evidence.append({'facet':r['facet'],'mode':r['mode'],'value':r['value'],'status':status,'reason':reason})
    return evidence

def selection_key(g):
    evidence=g['constraintEvidence']
    # Subject / brand evidence takes precedence over incidental attributes.
    subject=sum(e['status']=='supported' for e in evidence if e['mode']=='require' and e['facet']=='商品')
    core=sum(e['status']=='supported' for e in evidence if e['mode']=='require' and e['facet'] in {'品牌','型号'})
    hard=sum(e['status']=='supported' for e in evidence if e['mode'] in {'require','exclude'})
    soft=sum(e['status']=='supported' for e in evidence if e['mode']=='prefer')
    soft-=sum(e['status']=='conflict' for e in evidence if e['mode']=='avoid')
    return (-subject,-core,-hard,-soft)


def select_groups(groups,requirements):
    """Evidence tiers across sources; existing local-rank order breaks ties."""
    eligible=[];excluded=[]
    for group in groups:
        checked=dict(group,constraintEvidence=assess(group['title'],group['members'],requirements))
        hard=[e for e in checked['constraintEvidence'] if e['mode'] in {'require','exclude'}]
        if any(e['status']=='conflict' for e in hard):
            excluded.append(checked);continue
        eligible.append(checked)
    eligible.sort(key=selection_key)
    return eligible,excluded
