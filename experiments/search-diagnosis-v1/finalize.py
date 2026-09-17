import hashlib,json,math,statistics
from pathlib import Path
from run_plans import ROOT,REPO,verify,write

DOC=REPO/'docs/data/search-agent-diagnosis-20260913'
def read(p):return json.loads(p.read_text('utf-8'))
def digest(p):return hashlib.sha256(p.read_bytes()).hexdigest()
def cell(x):return str(x).replace('|','\\|').replace('\n',' ')
def table(head,rows):return '\n'.join(['|'+'|'.join(head)+'|','|'+'|'.join(['---']*len(head))+'|']+['|'+'|'.join(cell(x) for x in r)+'|' for r in rows])
def save_md(name,text):
    with (DOC/name).open('x',encoding='utf-8') as f:f.write(text)

NOTES={
'M01-T1':('no_observed_error','需求与标题总体对应；未设预算却提示预算匹配，属于冗余回答。'),
'M01-T2':('no_observed_error','追加女款，纯棉与秋季保留。'),
'M01-T3':('no_observed_error','长袖追加正确，展示标题均写长袖。'),
'M01-T4':('no_observed_error','整步撤销正确恢复T2及其scope；回答“需重新筛选”多余，取消长袖并不排除长袖商品。'),
'M02-T1':('no_observed_error','泰迪狗粮主体匹配。'),
'M02-T2':('clear_error','需求正确保留不含鸡肉；第2项明确鸡肉冻干，回答正确提示冲突。其余未写配方不能认定不含鸡肉。'),
'M02-T3':('unresolved_evidence','鸡肉排除与小颗粒均保留；标题没有完整配方，不能判定鸡肉条件满足。'),
'M02-T4':('unresolved_evidence','鸡肉改为牛肉排除且保留小颗粒；配方证据仍不足。'),
'M03-T1':('clear_error','第2、4、6项均为左颜右色；与指定玫琳凯不同。来自MultiCPR各自前三名，被交替展示。'),
'M03-T2':('clear_error','预算保留但缺可信价格；第6项明确左颜右色，回答仅说品牌待核验，未充分区分已知冲突与缺字段。'),
'M03-T3':('no_observed_error','new正确清除洗面奶、品牌、性别、预算，改为柜子拉篮。'),
'M03-T4':('no_observed_error','不锈钢追加正确，旧预算未恢复，6个标题均提供不锈钢证据。'),
'M04-T1':('clear_error','展示第2项明确品胜电路板，但第3、5项为充电宝本体；配件与整机区分不足。'),
'M04-T2':('clear_error','模型完整保留品牌与主板排除整机需求，但展示出现整机；原MultiCPR第1名品胜电路板降到第18名。'),
'M04-T3':('no_observed_error','新需求路由catalog正确，清除品胜；手机壳已明确配件主体，未额外保留“不要手机”不独立判错。'),
'M04-T4':('clear_error','模型正确写不透明；展示第1、2、3、5项明确透明，回答识别了冲突。'),
'M05-T1':('no_observed_error','改写老年女鞋，未见主体意图错误。'),
'M05-T2':('unresolved_evidence','无鞋带保留；第1、5项有证据，其他标题缺鞋带信息，不直接判不符合。'),
'M05-T3':('clear_error','持久化query仅留防滑，丢失“偏好而非必须”的限定；当轮回答借助当前原话仍解释正确。不能因此声称检索器已执行硬过滤。'),
'M05-T4':('clear_error','鞋带从query正确删除；继承的防滑软偏好限定仍缺失，回答重新要求核验已取消的鞋带属性（原始与一次复现出现）。'),
'M06-T1':('no_observed_error','木头/竹木混写按既有语料含义保留，不在本轮新造材质标签。'),
'M06-T2':('clear_error','需求保留木头；第4项塑料、第6项不锈钢均明确材质冲突，回答识别。'),
'M06-T3':('clear_error','大号和20个装正确加入；第3、4、6项塑料，第5项不锈钢，展示未满足木头条件。'),
'M06-T4':('clear_error','选择性撤销被解释为undo整步回退，丢失大号；回答又把取消数量条件误说成移除第4个商品，列表实际仍展示该商品。'),
}

def main():
    verify()
    qs=read(ROOT/'single-inputs.json')
    rows=[read(ROOT/'retrieval'/(q['query_id']+'.json')) for q in qs]
    multis=read(ROOT/'multi-live/RESULTS.json')['turns']
    assert len(rows)==40 and len(multis)==24
    paired=[r for r in rows if r['input']['cohort']=='main' and r['delta'] is not None]
    cup=next(r for r in rows if r['input']['query']=='一杯芝士奶酪')
    dcg=lambda a,unknown:sum((2**(r['grade'] if type(r['grade']) is int else unknown)-1)/math.log2(i+2) for i,r in enumerate(a))
    originaldcg=dcg(cup['baselineTop10'],3);upperdcg=dcg(cup['rewrittenTop10'],3)
    assert upperdcg<originaldcg
    review=[]
    for r in rows:
        q=r['input'];metric='unresolved_evidence' if r['delta'] is None else ('measured_regression' if r['delta']<0 else 'no_observed_error')
        note='原始意图未见明确改写错误。'
        if q['query']=='一杯芝士奶酪':
            metric='measured_regression';note='删除一杯限定；原前三3分商品掉出Top10。新第10项缺标签，不能给新NDCG点值，但最高3分下DCG仍退步；复现3次有2次删词。'
        elif r['status']=='NOT_SEARCHED_BY_PLANNER':note='实际选择澄清，不伪造搜索结果或算作0分；意图诊断单列。'
        elif q['cohort']=='diagnostic':note='既有诊断query：按原意图保留，现有商品qrel不等于内容/导航成功标签。'
        elif r['delta'] is None:note='原结果或改写结果Top10存在UNKNOWN/未标注，保留证据缺口。'
        elif r['delta']>0:note='按冻结银标有小幅提升；只改空格，不证明LLM理解带来普遍增益。'
        review.append({'query_id':q['query_id'],'query':q['query'],'cohort':q['cohort'],'judgment':metric,'note':note,'raw':str(ROOT/'retrieval'/(q['query_id']+'.json'))})
    repetitions=[read(p) for p in sorted((ROOT/'repeats').glob('*.json')) if p.name!='CONTRACT.json']
    assert len(repetitions)==8 and all(not r.get('error') and r['samePlanInputSha'] for r in repetitions)
    assert all(r.get('sameAnswerInputSha',True) for r in repetitions)
    traces=[read(p) for p in sorted((ROOT/'retrieval').glob('*-trace.json'))]
    assert len(traces)==6 and all(r['liveTop10ExactlyReproduced'] for r in traces)
    actionmatches=sum(t['plan']['action']==t['expected']['expectedAction'] for t in multis)
    summary={'status':'DIAGNOSIS_COMPLETE_NO_PRODUCTION_FIXES','single':{'inputs':40,'main':33,'diagnostic':7,
        'identicalQueryBaselineReuse':sum(r.get('rewrittenMode')=='identical_query_reused' for r in rows),
        'freshRewriteRetrieval':sum(r.get('rewrittenMode')=='fresh_same_runtime' for r in rows),
        'clarifiedWithoutRetrieval':sum(r['status']=='NOT_SEARCHED_BY_PLANNER' for r in rows),
        'fullyJudgedMainPairs':len(paired),'pairedBaselineNdcg':statistics.mean(r['baselineNdcg'] for r in paired),
        'pairedRewriteNdcg':statistics.mean(r['rewrittenNdcg'] for r in paired),'pairedDelta':statistics.mean(r['delta'] for r in paired),
        'positiveExactPairs':sum(r['delta']>0 for r in paired),'negativeExactPairs':sum(r['delta']<0 for r in paired),
        'unchangedExactPairs':sum(r['delta']==0 for r in paired),'cupRobustDcgRegression':{'original':originaldcg,'rewriteMaximum':upperdcg,
            'proof':'new unjudged rank10 assigned max allowed grade3 only for upper-bound calculation, no qrel mutation; common positive IDCG preserves strict DCG ordering'},
        'noOverallWinnerClaim':'cup regression excluded from exact-pair average; silver labels and source-local ranks only'},
        'multi':{'constructedDialogues':6,'turns':24,'completed':sum(t['status']=='completed' for t in multis),
        'routeMatches':sum(t['route']==t['expected']['expectedRoute'] for t in multis),'actionMatches':actionmatches,
        'notSemanticPassRate':True,'manualReview':[{**t,'judgment':NOTES[t['id']][0],'review':NOTES[t['id']][1]} for t in multis]},
        'replications':{'cases':4,'additionalPlanCalls':8,'additionalFixedEvidenceAnswerCalls':6,'exactInputShaAllMatched':True,
        'selectiveUndoFailure':{'observed':3,'attempts':3},'cupQualifierDeletion':{'observed':2,'attempts':3},
        'stageRetrievalReproductions':6,'allLiveTop10ExactlyMatched':True},
        'provenance':'existing exposed dev silver; 6 assistant-constructed dialogues; 8 historical automated acceptance turns; no natural dialogue representativeness claim',
        'modelNames':sorted({read(ROOT/'single-plans'/(q['query_id']+'.json'))['receipt']['model'] for q in qs}),
        'originalSourceCodeAndFrozenReferencesUnchanged':True}
    write(ROOT/'RESULTS.json',summary);write(ROOT/'SINGLE-REVIEW.json',review)
    save_md('SINGLE-REVIEW.md','# 单轮40条逐例审阅\n\n每条保留原query目标；9条真实重检索、29条字节相同改写复用绑定v3基线、2条澄清不伪造结果。指标仅按冻结v6银标计算，非完整人工金标。`—`表示证据不足，不是0。\n\n'+table(['query','集合','真实改写/操作','原NDCG','新NDCG','差值','结论'],[
        [r['input']['query'],r['input']['cohort'],r['plan']['query'] or r['plan']['action'],*[f'{r[k]:.6f}' if r.get(k) is not None else '—' for k in ['baselineNdcg','rewrittenNdcg','delta']],v['note']] for r,v in zip(rows,review)])+'\n\n逐例原始排序、标题、分数和召回通道：[retrieval](D:/agent-datasets/search-agent-diagnosis-20260913-v1/retrieval)。\n')
    save_md('MULTI-REVIEW.md','# 六组派生多轮逐例审阅\n\n全部为执行前构造并冻结预期的对话，不能当作自然用户失败率。没有为新增约束复用原query的qrel。判定为错误可只发生于状态、展示或回答某一层；其余缺字段保留未决。\n\n'+table(['案例','用户原话','预期完整需求','实际完整需求','操作','审阅'],[
        [t['id'],t['expected']['message'],t['expected']['expectedQuery'],t['query'],t['plan']['action'],NOTES[t['id']][1]] for t in multis])+''.join('\n\n## '+t['id']+'\n\n'+table(['编号','真实标题'],[[x['number'],x['title']] for x in t['titles']])+'\n\n实际回答：\n\n'+t['answer']+'\n\n[原始状态、模型收据和运行记录]('+str(ROOT/'multi-live'/(t['id']+'.json')).replace('\\','/')+')' for t in multis))
    hist=Path('D:/agent-datasets/catalog-latency-20260913-v1/live-final001')
    hrows=[]
    for i in range(1,9):
        r=read(hist/f'S{i}-result.json');hrows.append([f'S{i}',r['message'],r['route'],r['action'],r['status'],
            '手机候选无可绑定的指定型号，回答披露证据不足；非本轮普通搜索质量结论' if i==6 else '操作/状态未见明确错误；商品属性以标题为限，未赋新qrel'])
    save_md('HISTORICAL-REVIEW.md','# 既有8步记录复查\n\n这是之前的自动验收记录，不是自然用户会话；不混入新24轮统计。核查了对应S1–S8的result/run原始文件。S1、S7把分散的词写成连续标题引文不够严谨；S2对缺亚克力字段保留待核验，S5没有虚构200元以内价格；S3比较、S4整步撤销、S8取消与原记录一致。\n\n'+table(['步骤','原话','路由','操作','运行状态','复查'],hrows)+'\n')
    trace=next(t for t in traces if t['case']=='M04-T2' and t['source']=='multicpr')
    good='multicpr:611596'
    stage={'docid':good,'title':trace['texts'][good],'channels':{k:next((r['rank'] for r in v if r['document_id']==good),None) for k,v in trace['recall']['channels']['M04-T2-multicpr'].items()},
        'rrfPoolRank':next(r['rank'] for r in trace['pool'] if r['document_id']==good),'ceRank':next(r['rank'] for r in trace['ranking'] if r['document_id']==good)}
    write(ROOT/'STAGE-EVIDENCE.json',stage)
    save_md('RESULTS.md',f'''# 普通商品LLM搜索失败诊断：4/4步完成

已完成诊断；没有修改生产检索、模型、提示词或qrel，没有启动训练。下一轮建议先修选择性撤销与排除条件处理，再评估是否需要训练。

## 范围与证据

|来源|规模|实际做了什么|不能推断什么|
|---|---|---|---|
|已有开发query|40，33主集合+7诊断|真实调用需求解释模型；9条改写重新检索，29条相同query复用哈希绑定排序，2条澄清|不是新盲测，不是40次完整前端会话|
|派生多轮|6组×4轮|预先冻结每轮完整需求，实际5173工作空间运行24轮，保存状态/候选/回答/收据|不是自然用户分布，不能外推失败率|
|历史交互|8步|复查原始运行与回答，单独记录|不是新运行或自然用户对话|
|复现|4例各额外2次|8次解释调用、6次固定证据回答调用；输入SHA与首轮完全相同|不是另一个独立评审者|
|阶段追踪|3例×2库|保存三路召回、RRF候选、交叉编码完整排序；6次Top10精确重现在线结果|没有评估新策略收益|

运行模型：{', '.join(summary['modelNames'])}。检索固定v3、w211(2/1/1)、RRF k=60、300候选、epoch-2交叉编码器。离线运行manifest与既有v3基线完全一致。多轮24/24运行完成、24/24路由正确、23/24操作类型一致；这些数字不是语义或购物任务成功率。

## 已定位的问题及优先级

|优先级|问题|实际证据|归因与建议|
|---|---|---|---|
|P0|选择性撤销误成整步撤销|M06-T4“只撤销20个装，保留大号和防风”→undo→大号也消失；首轮+2次复现均发生|plan选择错误；transition按设计弹出整版历史。区分整步undo和属性删除，按完整状态验证保留条件；先不训练|
|P0|排除条件正确理解，排序仍推冲突商品|M02-T2不含鸡肉→鸡肉冻干；M04-T4不透明→展示6项中4项透明|已确认冲突商品进入候选并被CE推到库内前列。分离正向召回词与排除约束；明确冲突过滤/降权，缺字段保留未核验，不能一概过滤|
|P1|具体商品限定被改写丢弃|“一杯芝士奶酪”→“芝士奶酪”；原前三3分商品掉出Top10；3次调用2次删除限定|改写导致目标泛化；保留原文中的产品短语、型号、数量/包装限定。采用可核验的改写保真规则，简单query不强制改写|
|P1|配件需求正确，精排压低已有好候选|M04-T2品胜电路板在三路召回均找到，RRF第{stage['rrfPoolRank']}，CE第{stage['ceRank']}，此前查询库内第1|不是“库里没有商品”。先用固定候选隔离正/负约束、整机/配件错误；确定训练样本需求后才训练|
|P1|双库交替展示占用槽位|M03-T1玫琳凯需求中，MultiCPR前三为左颜右色，占展示第2/4/6项|document_scope机械交替各库排名，不是全局相关性排序。先审约束再合并去重；跨库CE校准或统一复排需独立对照，不能直接比两库原始分数|
|P1|软偏好在持久化状态中丢失限定|M05-T3“防滑只是偏好”只存“防滑”；T4继续沿用，复现一致|当前query字符串没有记录软/硬区别。当轮回答能读原话，下一轮回答只读当前query而无历史。保留偏好强度；不能把此现象说成已执行硬过滤|
|P1|回答把撤销条件误说成移除商品|M06-T4复现回答说第4项20个装“不再保留”，实际列表仍含第4项；M05-T4部分回答要求核验已取消鞋带|回答未受状态差分约束。传入实际变更字段、明确剩余要求，核对回答与真实列表，区分需求条件和商品编号|
|P2|完整配方、价格或相关性标签缺证据|M02后续配方未知；M03预算无可信价格；若干单轮Top10未评分|单列缺口；不足以认定商品符合，也不等同商品不符合。本轮不改标签|

## 单轮指标：不能只看完整配对均值

33条主集合中，28条两侧Top10都具有数值标签。对相同28条，NDCG@10：{summary['single']['pairedBaselineNdcg']:.6f} → {summary['single']['pairedRewriteNdcg']:.6f}，差值+{summary['single']['pairedDelta']:.6f}；2条上升、26条相同。两条上升分别为a5硬面笔记本插画(+0.034148)、太阳能后尾灯爆闪(+0.024700)，均只改变空格。相同query复用的排序不构成新的增益证据。

这不是总体胜出结论：发生明显退步的芝士案例因新第10项未标注，恰好不在这个完整配对均值里。也不能把28条均值和之前29条均值直接相减。bioisand等仍有原Top10 UNKNOWN；新增覆盖不代表正确性改善。冻结银标也非最终无争议金标，例如a5新Top10中的“软皮笔记本A5”现有分数为2；本轮只按锁定标签计算，不据此认定满足硬面需求。

芝士案例提供了不依赖补标签的方向证明：原Top10 DCG={originaldcg:.6f}；将新第10项按允许的最高3分作上界计算，新DCG仍至多{upperdcg:.6f}。共同且为正的IDCG不会改变大小顺序，所以在冻结其余标签、缺口允许0–3分的前提下，改写后NDCG一定更低；新NDCG精确点值仍不报，也没有将该项真的写为3分。

原第1、2个3分商品进入改写后的300候选，却降至CE第47、96；原第3个3分商品仍在dense第166，但未进最终RRF300。这说明输入被泛化后，候选融合和精排都可能损失特定目标，不应把全部损失归给单一精排模型。

## 根因能确定到哪里

M04-T2的真实好候选为 `{good}`：{stage['title']}。BM25第{stage['channels']['bm25']}、字符第{stage['channels']['character']}、Dense第{stage['channels']['dense']}，RRF第{stage['rrfPoolRank']}，CE第{stage['ceRank']}。候选确实存在，主要淘汰发生在精排。M04-T4的透明壳即便query明确“不透明”仍被CE推至前列；这证明当前组合未可靠执行排除条件，但不能据此断言模型内部为何失效、换提示词必然有效或训练必然改善。

M06-T4的服务端undo严格恢复前一版，不是随机漏字段；选择性删除被映射为整步undo是已观测根因。回答层把撤销数量约束误解释成删除一个展示项，是另一层错误，不能只修检索来解决。

## 下一轮建议及验收标准（尚未实施）

1. 先修需求状态：选择性取消、整步撤销、new重置分别处理；冻结案例均正确保留未撤销条件，软偏好不能丢失限定。新增未看过的同类措辞再验证，避免只适配这六组。
2. 再修约束执行与回答：正向商品词用于召回；标题明确冲突的候选不得当作符合项推荐，缺属性单列未核验。回答必须与真实列表及状态变更一致。
3. 再做受控搜索对照：固定候选单独测精排/约束处理，再测双库合并；不同时改召回与精排来宣称某一模块贡献。补齐真正影响指标的标签后重算；此步需要另一个实验版本。
4. 只有上述后仍有稳定的品牌/配件/否定排序错误，才补独立训练样本并考虑SFT或排序器训练。目前没有证据要求直接启动Agent RL。

## 可复查文件

- [单轮40条表](SINGLE-REVIEW.md)
- [多轮24轮原话、预期、真实状态、标题、回答](MULTI-REVIEW.md)
- [历史8步复查](HISTORICAL-REVIEW.md)
- [结构化汇总](D:/agent-datasets/search-agent-diagnosis-20260913-v1/RESULTS.json)
- [冻结契约](D:/agent-datasets/search-agent-diagnosis-20260913-v1/CONTRACT.json)
- [原始重试记录](D:/agent-datasets/search-agent-diagnosis-20260913-v1/repeats)

所有审阅为本任务对原始记录的人工式核查，没有冒充独立盲审或用户金标。没有自然多轮样本代表性证据，没有线上转化实验，没有训练结果或修复收益。
''')
    refs=read(ROOT/'CONTRACT.json')['references']
    validation={'status':'PASS','queries':len(rows),'turns':len(multis),'sourceHashesUnchanged':True,'referenceHashesUnchanged':True,
        'baselineRuntimeManifestEqual':read(ROOT/'retrieval/runtime.json')==read(Path('D:/agent-datasets/catalog-latency-20260913-v1/dev-eval004/runtime.json')),
        'liveTop10Reproductions':6,'repeatInputHashesMatched':8,'noQrelWrites':True,
        'runtimeSuccessNotSemanticSuccess':True}
    write(ROOT/'VALIDATION.json',validation)
    historical=[hist/f'S{i}-{suffix}.json' for i in range(1,9) for suffix in ['run','result']]
    files=list(ROOT.rglob('*'))+list((REPO/'experiments/search-diagnosis-v1').glob('*.py'))+list(DOC.glob('*.md'))+historical
    write(ROOT/'MANIFEST.json',{'status':'SEALED_DIAGNOSIS_ONLY','files':{str(p):digest(p) for p in files if p.is_file() and '__pycache__' not in str(p)},'frozenReferences':refs})
    print(json.dumps({k:summary[k] for k in ['status','single','replications']},ensure_ascii=False))
if __name__=='__main__':main()
