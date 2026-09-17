"""Freeze query-only policy and label-blind packets; this does not assign qrels."""
from __future__ import annotations
import hashlib
import json
from collections import defaultdict
from pathlib import Path
from bootstrap import ROOT, DEV, REPO, sha, write_once

# Explicit query interpretations authored before any v6 judgments or model comparison.
# Tuple: core type, non-substitutable core-purpose attributes, substitutable attributes.
SPECS = {
 '玫琳凯男士洗面奶': ('洗面奶/洁面乳', [], ['玫琳凯品牌','男士']),
 '卡骆驰真皮': ('卡骆驰名称指向的商品，不擅加鞋型/性别', [], ['卡骆驰名称','真皮']),
 'a5硬面笔记本插画': ('纸质笔记本', [], ['A5','硬面','插画']),
 '弹力绳手串打结': ('打结教程与绳材/工具意图未定', [], []),
 '鞋子女款秋季增高': ('鞋子', [], ['女款','秋季','增高']),
 '睡衣纯棉秋季': ('睡衣/家居睡衣', [], ['纯棉','秋季']),
 '卫时代车载吸尘器': ('吸尘器', ['车载使用'], ['卫时代品牌']),
 '实木傢俬椅': ('椅子', [], ['实木']),
 '电动玩具': ('玩具', [], ['电动']),
 'rbvc精华': ('精华类护肤商品', [], ['rbvc名称']),
 '小礼物便宜精美小学生党': ('小礼物', [], ['小学生适用','便宜','精美']),
 '木工用极细画线笔': ('划线/画线笔', ['木工用途'], ['极细线条']),
 '小车锁匙包蜡线': ('钥匙包成品与其制作线材本体未定', [], []),
 '叶医生纯手工健身拍': ('健身拍', [], ['叶医生名称','纯手工']),
 '船小雨海蜇丝': ('海蜇食品', [], ['船小雨名称','丝状']),
 '教室睡觉抱枕': ('抱枕', [], ['教室休息/睡觉场景']),
 'koko': ('实体/商品对象未定', [], []),
 '品胜充电宝主板': ('充电宝主板', ['品胜品牌或品胜充电宝适配'], []),
 '依依童装直播': ('童装商品与直播导航意图未定', [], []),
 '高尔夫球帽子女': ('帽子', [], ['高尔夫用途','女款']),
 '夹子晾衣夹木头': ('晾衣夹', ['晾衣夹持用途'], ['夹体木质']),
 '360全景主机盒': ('全景系统主机盒/控制主机', ['360全景系统用途'], []),
 '蕾丝边溜肩短袖t恤女': ('T恤', [], ['女款','短袖','蕾丝边','溜肩相关版型']),
 '全手织透明网底假发套': ('假发套', [], ['全手织','透明网底']),
 '老年穿的鞋子女': ('鞋子', [], ['女款','老年人穿用']),
 '本田金峰锐摩托车': ('摩托车本体', [], ['本田品牌','金峰锐名称/型号']),
 'bioisand婴儿dha': ('DHA类商品', [], ['bioisand原名称','婴儿适用']),
 '九月的第一天心情': ('心情表达/内容意图，商品对象未定', [], []),
 '球球直播外套': ('外套', [], ['球球实体/直播来源关系']),
 '孕妇控制体重的食物': ('食品', [], ['孕妇人群','体重管理用途关联']),
 '一杯芝士奶酪': ('芝士/奶酪食品', [], ['杯装/一杯份量']),
 '柜子拉篮': ('柜用拉篮', ['柜子内抽拉收纳用途'], []),
 '荼托盘': ('茶用托盘；保留荼字原文', [], []),
 '泰迪狗粮': ('狗粮', [], ['泰迪适用或明确覆盖的小型犬范围']),
 '三角地插固定器': ('地插固定器', [], ['三角形/三角结构']),
 '太阳能后尾灯爆闪': ('后尾灯', ['后尾警示用途'], ['太阳能','爆闪']),
 '小孩打针玩具': ('模拟打针/医疗扮演玩具', ['玩具而非真实医疗注射用品'], ['小孩使用']),
 '乘龙h5卧铺垫加厚': ('卧铺垫', ['乘龙H5适配'], ['加厚']),
 '三只松鼠零食': ('零食', [], ['三只松鼠品牌']),
 '喜来顺月饼': ('月饼', [], ['喜来顺品牌']),
}
AMBIGUOUS = {'九月的第一天心情','koko','弹力绳手串打结','小车锁匙包蜡线','依依童装直播'}

def jsonl_once(path, rows):
    path = Path(path); path.parent.mkdir(parents=True, exist_ok=True)
    raw = ''.join(json.dumps(r,ensure_ascii=False,sort_keys=True)+'\n' for r in rows).encode('utf-8')
    if path.exists():
        if path.read_bytes() != raw: raise ValueError(f'Immutable output differs: {path}')
    else:
        with path.open('xb') as f: f.write(raw)

def main():
    old = Path('D:/agent-datasets/search-stage1-dev-revision-v5/frozen')
    queries = [json.loads(x) for x in (old/'queries.jsonl').read_text(encoding='utf-8-sig').splitlines()]
    if {q['query'] for q in queries} != set(SPECS): raise ValueError('Query specifications incomplete')
    policy = REPO / 'experiments/search-closure-v1/POLICY_V6.md'
    frozen_policy = DEV / 'policy/RUBRIC.md'
    frozen_policy.parent.mkdir(parents=True,exist_ok=True)
    if frozen_policy.exists():
        if frozen_policy.read_bytes()!=policy.read_bytes(): raise ValueError('Frozen policy changed')
    else:
        with frozen_policy.open('xb') as f: f.write(policy.read_bytes())
    contracts=[]
    for q in queries:
        core, hard, attrs = SPECS[q['query']]
        anon=hashlib.sha256(('v6-query:'+q['query_id']).encode()).hexdigest()[:16]
        contracts.append({**q, 'anonymous_query_id':anon, 'core_product':core,
            'core_purpose_requirements':hard, 'substitutable_attributes':attrs,
            'required_attribute_keys':['本体',*hard,*attrs],
            'intent_policy':'query_intent_ambiguous' if q['query'] in AMBIGUOUS else 'score_product_relevance',
            'no_added_requirements':True})
    jsonl_once(DEV/'policy/query-contracts.jsonl',contracts)
    byid={q['query_id']:q for q in contracts}
    original=[json.loads(x) for x in (old/'qrels.jsonl').read_text(encoding='utf-8-sig').splitlines()]
    if len(original)!=3674: raise ValueError('Unexpected source count')
    groups=defaultdict(list); mapping=[]
    for row in original:
        qid=row['query_id']; did=row['document_id']
        pid=hashlib.sha256(('v6-pair:'+qid+'\0'+did).encode()).hexdigest()[:24]
        groups[qid].append({'pair_id':pid, 'query_id':byid[qid]['anonymous_query_id'],
                           'query':row['query'],'document':row['document']})
        mapping.append({'pair_id':pid,'query_id':qid,'document_id':did,'query_cohort':row['query_cohort']})
    if len({r['pair_id'] for r in mapping})!=3674: raise ValueError('Duplicate pair id')
    jsonl_once(DEV/'private/pair-mapping.jsonl',mapping)
    # Query order and per-query document order are deterministic hashes, unrelated to rankings/grades.
    ordered=sorted(queries,key=lambda q:hashlib.sha256(('20260909:v6:'+q['query_id']).encode()).hexdigest())
    packets=[]
    for start in range(0,len(ordered),4):
        name=f'dev-{start//4+1:02d}'; directory=DEV/'packets'/name
        members=ordered[start:start+4]; entries=[]; public_contracts=[]
        for q in members:
            c=dict(byid[q['query_id']]); c['query_id']=c.pop('anonymous_query_id')
            public_contracts.append(c)
            entries.extend(sorted(groups[q['query_id']],key=lambda r:r['pair_id']))
        jsonl_once(directory/'pairs.jsonl',entries)
        jsonl_once(directory/'query-contracts.jsonl',public_contracts)
        rubric=directory/'RUBRIC.md'
        if not rubric.exists():
            with rubric.open('xb') as f: f.write(frozen_policy.read_bytes())
        elif rubric.read_bytes()!=frozen_policy.read_bytes(): raise ValueError('Packet rubric mismatch')
        manifest={'packet_id':name,'pairs':len(entries),'queries':len(members),
                  'files':{f:sha(directory/f) for f in ['pairs.jsonl','query-contracts.jsonl','RUBRIC.md']},
                  'no_old_labels_or_rankings':True,'human_gold':False}
        write_once(directory/'INPUT_MANIFEST.json',manifest)
        packets.append({'packet_id':name,'directory':str(directory),'pairs':len(entries),
                        'input_manifest_sha256':sha(directory/'INPUT_MANIFEST.json')})
    write_once(DEV/'policy/FROZEN.json',{'status':'POLICY_FROZEN_BEFORE_JUDGMENTS',
        'rule_sha256':sha(frozen_policy),'query_contracts_sha256':sha(DEV/'policy/query-contracts.jsonl'),
        'source_qrels_sha256':sha(old/'qrels.jsonl'), 'source_queries_sha256':sha(old/'queries.jsonl'),
        'query_count':40,'main':33,'diagnostic':7,'pair_count':3674,
        'resolution':'A=B else independent T; two of three majority; no majority UNKNOWN'})
    write_once(DEV/'packets/MANIFEST.json',{'packets':packets,'pairs':sum(p['pairs'] for p in packets)})
    print(json.dumps({'status':'BLIND_PACKETS_READY_NOT_LABELS','packets':packets},ensure_ascii=False))

if __name__=='__main__':
    main()
