import hashlib,json,statistics
from pathlib import Path

ROOT=Path('D:/agent-datasets/search-agent-repair-20260913-v1')
REPO=Path(__file__).resolve().parents[2]
DOC=REPO/'docs/data/search-agent-repair-20260913'
OLD=Path('D:/agent-datasets/search-agent-diagnosis-20260913-v1')
def read(p):return json.loads(p.read_text('utf8'))
def sha(p):return hashlib.sha256(p.read_bytes()).hexdigest()
def save(p,v):
    with p.open('x',encoding='utf8') as f:json.dump(v,f,ensure_ascii=False,indent=2)
def raw(attempt,cid):return read(ROOT/attempt/'multi-live'/(cid+'.json'))
def table(rows):return '\n'.join('|'+ '|'.join(str(v).replace('|','\\|').replace('\n',' ') for v in r)+'|' for r in rows)
def main():
    contract=read(ROOT/'live005/LIVE-CONTRACT.json')
    assert all(sha(REPO/p)==h for p,h in contract['code'].items())
    versions={}
    for a,count in [('live002',31),('live003',24),('live004',2),('live005',8)]:
        records=[read(p) for p in (ROOT/a/'multi-live').glob('*-T*.json')]
        assert len(records)==count and all(r['summary']['status']=='completed' for r in records)
        versions[a]={'requests':count,'completed':count,'medianSeconds':statistics.median(r['summary']['seconds'] for r in records),
                     'maxSeconds':max(r['summary']['seconds'] for r in records)}
    gates={}
    r=raw('live003','M06-T4');q=r['summary']['query'];req=r['after']['catalogSearch']['requirements']
    gates['selective_cancel_keeps_size']='大号' in q and '防风' in q and '20个' not in q and r['summary']['plan']['action']=='refine'
    t=raw('live003','N04-T3');first=raw('live003','N04-T1')
    gates['whole_undo_restores_scope']=t['summary']['plan']['action']=='undo' and t['after']['catalogSearch']['scope']==first['after']['catalogSearch']['scope']
    t=raw('live003','M05-T4');req=t['after']['catalogSearch']['requirements']
    gates['preference_preserved_and_laces_removed']=any(x['value']=='防滑' and x['mode']=='prefer' for x in req) and not any('鞋带' in x['value'] for x in req)
    t=raw('live003','N01-T3');q=t['summary']['query']
    gates['new_wording_cancel_capacity']='白色' in q and '陶瓷' in q and '350' not in q
    t=raw('live002','N02-T3');req=t['after']['catalogSearch']['requirements']
    gates['new_task_clears_old_requirements']='机械键盘' in t['summary']['query'] and not any(x['value'] in {'女款','防水','双肩包'} for x in req)
    t=raw('live003','M04-T4');titles=[g['title'] for g in t['after']['catalogSearch']['scope']['groups']]
    gates['no_transparent_titles_in_exclusion_case']=bool(titles) and all('透明' not in title for title in titles)
    t=raw('live005','M04-T2');scope=t['after']['catalogSearch']['scope']
    gates['board_survives_exclusion_and_is_first']=bool(scope['groups']) and any(m['docid']=='multicpr:611596' for m in scope['groups'][0]['members'])
    gates['only_negative_or_budget_changes_keep_positive_query']=all(
        raw('live005',d+'-T2')['summary']['plan']['retrievalQuery']==raw('live005',d+'-T1')['summary']['plan']['retrievalQuery']
        for d in ['M02','M03','M04','M05'])
    t=raw('live003','B01-T1');gates['brand_display']=all('玫琳凯' in x['title'] for x in t['after']['catalogSearch']['scope']['groups'])
    cup=raw('live003','N03-T1');s=cup['after']['catalogSearch']['scope']
    baseline=read(Path('D:/agent-datasets/catalog-latency-20260913-v1/dev-eval004/s1-mu-dev-124e3c1bbeaf4bf9.json'))
    ids=[h['docid'] for source in s['sources'] if source['source']=='multicpr' for h in source['hits']]
    gates['cup_original_retrieval_restored']=cup['summary']['plan']['retrievalQuery']=='一杯芝士奶酪' and ids==[h['document_id'] for h in baseline['ranking'][:10]]
    oldcontract=read(OLD/'CONTRACT.json')
    gates['qrels_and_retrieval_configuration_unchanged']=all(sha(Path(p))==h for p,h in oldcontract['references'].items())
    untouched={p:h for p,h in oldcontract['sourceCode'].items() if p not in contract['code']}
    gates['base_retrieval_code_unchanged']=all(sha(REPO/p)==h for p,h in untouched.items())
    replay=read(ROOT/'FINAL-POLICY-REPLAY-v3.json')
    gates['final_policy_replayed_55_saved_turns']=len(replay['rows'])==55 and replay['codeSha']==sha(REPO/'agent/app/catalog_requirements.py')
    assert all(gates.values()),gates
    result={'status':'REPAIR_VERIFIED_AND_RUNNING_LOCALLY','gates':gates,'testEvidence':{'passed':51,
       'command':'.venv/Scripts/python.exe -m pytest agent/tests/test_catalog_requirement_repair.py agent/tests/test_catalog_workspace.py agent/tests/test_catalog_model_client.py agent/tests/test_catalog_search_integration.py -q',
       'source':'observed executed command; includes live-discovered regression tests'},'liveAttempts':versions,
       'candidateReplays':55,'preStartupAttempt':'root multi-live has zero turns because HTTP502 during startup; preserved, not counted as semantic trial',
       'interpretation':'bounded constructed cases; request completion not search success rate; earlier faults retained; final8 calls check exclusion and budget edits with stable positive queries',
       'limitations':['No verified prices or complete ingredient fields added','Title-rule coverage is limited; missing attributes remain unknown','No full benchmark gain claim, no training or qrel edits','No natural-user traffic generalization claim'],
       'currentCode':contract['code']}
    save(ROOT/'RESULTS.json',result)
    original=read(OLD/'multi-live/M04-T4.json')['summary']
    board=raw('live005','M04-T2')['summary']
    final=raw('live003','M04-T4')['summary']
    allrows=[]
    for a in ['live002','live003','live004','live005']:
        for p in sorted((ROOT/a/'multi-live').glob('*-T*.json')):
            r=read(p)['summary'];allrows.append([a,r['id'],r['expected']['message'],r['query'],r['plan']['action'],r['status'],round(r['seconds'],2)])
    (DOC/'LIVE-REVIEW.md').write_text('# 实际执行记录\n\n共65次请求，包含失败修复后的复测，不是65个独立样本。原始模型收据、状态、标题和回答保存在各attempt/multi-live目录。\n\n'+table([['批次','案例','用户原话','实际完整需求','操作','运行状态','秒'],['---']*7]+allrows)+'\n',encoding='utf8')
    (DOC/'RESULTS.md').write_text(f'''# 搜索失败修复：4/4步完成

当前修复已运行于本地Agent，前端仍为 http://127.0.0.1:5173/ 。完成的是诊断中已复现问题的修复与限定验收，没有训练新模型或改qrel。

## 改动与实际结果

|问题|修复|真实验证|
|---|---|---|
|选择性取消丢失其他条件|区分refine和整步undo；保存完整requirements、retrievalQuery及历史|M06只取消20个装后保留大号/防风；N01取消350毫升仍保留白色/陶瓷；N04整步undo恢复原scope|
|软偏好遗失|require/exclude/prefer分别保存，模型输入包含完整当前需求|取消鞋带后防滑仍为prefer；换成机械键盘后不继承双肩包需求|
|否定词影响召回、冲突候选仍展示|正向召回query与需求约束分开；标题支持/冲突/未知分别处理|不透明手机壳的透明标题展示从原4/6降到0/6；鸡肉排除项不再放入召回词，配方未知仍明确待核验|
|具体商品词被删|简单短商品query使用原文保真兜底|“一杯芝士奶酪”恢复原MultiCPR Top10完整顺序，原前三3分商品恢复到展示前三|
|不同库轮流占位置|先检查冲突，再按商品主体、品牌/型号及条件证据排序，原库内次序用于同档排序|玫琳凯展示6项均出现玫琳凯；没有直接比较两库未经校准的原始分数|
|配件误过滤|整机名与配件适配对象分开；电子板件别名统一，避免依赖模型临时别名|最终M04-T2第1项：{board['titles'][0]['title']}|
|回答误说撤销某个商品|回答只读取当前需求、当前证据，状态变更前缀由程序生成|取消数量后不再声称移除了仍在列表里的第4项；鞋带取消后不再要求核验旧鞋带条件|

## 验证与过程中发现的问题

51项相关测试通过。先完成31次请求，发现“充电宝整机”误过滤配件和单字别名“大”误命中“大棚”；修正后复测24次。第二批发现模型漏写“电路板”别名仍可能误过滤；加入统一板件词表后，重放全部55份候选记录。接着2次实测显示，仅追加排除项时模型仍可能扩写正向检索词，导致正确板件掉出库内Top10；进一步让仅改排除条件/预算的refine复用原正向query。最后8次实测覆盖鸡肉、预算、主板整机、鞋带四类修改，通过验收。全部失败记录保留，不把65次请求完成率包装成语义成功率。

最后24轮批次耗时中位数{versions['live003']['medianSeconds']:.2f}秒，最慢{versions['live003']['maxSeconds']:.2f}秒；这是本轮观测，不是严格的速度对照。初次服务重启尚未就绪时一次bootstrap收到502，没有提交query，另存记录后重试。服务重启包含模型冷加载，不能混入在线query耗时。

同候选池消融分别保留了原展示、仅过滤、过滤后重排。玫琳凯例中仅过滤并未改变展示，按品牌证据重排才将另一品牌移出前6；因此该变化归于合并展示，不能算作交叉编码训练收益。输入正向化带来的变化另由真实请求检验，不和这项消融混为一个变量。

## 边界

标题缺配方、价格、精确规格时仍为未知，不能声称所有候选完全符合条件。标题规则目前只覆盖可明确识别的冲突与部分配件/材质词义；混合SKU、复杂型号兼容和品牌别名仍需更多证据，不能当成完整商品知识图谱。某些普通搜索仍可能展示证据不足的候选，回答应保留待核验说明。

本轮底层BM25/Dense/RRF、epoch-2模型与qrel均未改动。没有重新宣称整体NDCG上涨，也没有用这批构造案例开展SFT/RL。后续若继续优化，应把真实使用中新增的失败单独登记，再判断规则覆盖或独立训练数据需求。

## 复习与复现

流程：用户原话 → 模型解释操作和完整需求 → 保存require/exclude/prefer → 正向query调用原检索器 → 标题约束核查 → 按证据合并展示 → 基于当前状态回答。

- [逐次执行记录](F:/agent/docs/data/search-agent-repair-20260913/LIVE-REVIEW.md)
- [验收门槛及机器可读结果](D:/agent-datasets/search-agent-repair-20260913-v1/RESULTS.json)
- [同候选池消融](D:/agent-datasets/search-agent-repair-20260913-v1/CANDIDATE-ABLATION.json)
- [最终55份候选重放](D:/agent-datasets/search-agent-repair-20260913-v1/FINAL-POLICY-REPLAY-v3.json)
- [最终配件请求原始记录](D:/agent-datasets/search-agent-repair-20260913-v1/live005/multi-live/M04-T2.json)

代码备份在产物目录before，当前变更只涉及catalog_conversation、catalog_requirements、catalog_service、api/catalog_workspace及针对性测试。回退应按备份逐文件核对恢复并重启Agent，勿对脏工作区执行git reset/clean。
''',encoding='utf8')
    files=[p for p in ROOT.rglob('*') if p.is_file()]+list(DOC.glob('*.md'))
    files+=[REPO/p for p in contract['code']]+[REPO/'agent/tests/test_catalog_requirement_repair.py']
    save(ROOT/'MANIFEST.json',{'status':'FINAL_REPAIR_EVIDENCE','files':{str(p):sha(p) for p in files}})
    print(json.dumps({'status':result['status'],'gates':gates,'requests':sum(v['requests'] for v in versions.values())},ensure_ascii=False))
if __name__=='__main__':main()
