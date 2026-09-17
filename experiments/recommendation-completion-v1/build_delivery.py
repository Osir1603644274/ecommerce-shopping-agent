"""Build reviewable evidence notes and a resume variant from verified artifacts."""
import html, re
from common import *

DOC=Path(__file__).resolve().parents[2]/'docs/experiments/recommendation-completion-20260916'
CAREER=Path('C:/Users/ming/Desktop/袁明珠_简历/推荐算法_20260916')

def main():
    result=read(ROOT/'final-001/RESULT.json');metrics=result['metrics'];verify=read(ROOT/'SERVING_VERIFIED.json')
    http=read(ROOT/'http-acceptance-003/RESULT.json');training=read(ROOT/'sequence-dev-001/seed-17/PROTOCOL.json')
    DOC.mkdir(parents=True,exist_ok=True);CAREER.mkdir(exist_ok=True)
    table='| 模型 | 留出 nDCG@10 | Recall@100 | 命中目标/1,109 |\n|---|---:|---:|---:|\n'
    names={'popular':'热门','itemcf':'ItemCF','content_tfidf':'TF-IDF 内容','equal_rrf':'CF＋TF-IDF 等权 RRF',
           'fixed_union_rrf':'固定三路候选 RRF','lambdamart_0':'固定三路候选 LambdaMART',
           'mean_seed17':'无序篮子 ID（seed17）','mean_seed29':'无序篮子 ID（seed29）','mean_seed43':'无序篮子 ID（seed43）',
           'causal_seed17':'因果篮子 Transformer（seed17）','causal_seed29':'因果篮子 Transformer（seed29）','causal_seed43':'因果篮子 Transformer（seed43）'}
    for arm,name in names.items():
        m=metrics[arm]['all'];table+=f"| {name} | {m['ndcg_at_10']:.5f} | {m['recall_at_100']:.2%} | {m['retrieved_targets']} |\n"
    cold=metrics['lambdamart_0']['cold_item'];warm=metrics['lambdamart_0']['warm_item']
    report=f'''# 电商推荐闭环：实际完成与边界

日期：2026-09-16。仍是同一电商 Search Agent 项目，增加推荐数据与算法分支；没有新建另一个商城。

## 核心结果

在 925 位用户、1,109 个未来正反馈目标的时间留出集上，固定相同的三路候选池，LambdaMART 相比 RRF：
- nDCG@10：**0.01953 → 0.03014**，绝对差 0.01062，相对约 54.37%。
- Recall@100：**8.36% → 10.59%**。
- 10,000 次用户配对 Bootstrap，nDCG 差值 95% 区间 **[0.00376, 0.01808]**。
- 925 人中 nDCG 提升 28 人、下降 18 人、持平 879 人；不能把显著区间说成每个用户都有提升。

{table}

## 数据是什么

Amazon Reviews 2018 Luxury Beauty：原始 34,278 条评价、3,819 个用户、1,581 个被评价 ASIN；清洗后 28,879 条评价事件，静态商品目录 12,111 件。18,336 等旧搜索 qrel 数字与本实验无关。

标签是未来评分 ≥4 的评价，不是曝光、点击或购买记录。商品标题、品牌和类目与行为用 ASIN 连接；仅用静态元数据，不使用 also_buy/also_view、全时期销量、未来评价文本。

处理 3,965 条完全重复记录；同用户/商品/日期出现评分冲突的整组隔离，共180组、509条原始记录；同评分重复表达合并；不同日期的评价仍保留。元数据188条完全重复记录合并；同标题不同ASIN继续作为不同商品。

同日事件是无序篮子，不能编造日内先后。训练/开发/最终按统一时间边界划分；开发992用户、1,164目标，最终925用户、1,109目标。当前实验历史固定在原训练截止日，不用中间开发行为更新历史；“新目标”是相对训练期已评价商品而言。所有训练期评价（含低评分）均排除。

静态元数据没有验证历史快照时间，原始5-core文件也按全时期活动预筛选，因此这是离线静态目录实验，不是严格的历史库存回放。最终行曾经被清洗与计数，但此次是首次模型预测和计分，不能称“从未接触过原文的盲测”。

## 真正训练了什么

1. 固定 MiniLM-L6-v2：12,111×384 商品向量。只替换内容表示的开发对照未超过 TF-IDF，保留负结果。
2. LambdaMART：三组较早时间快照，514个有可学习正负对的请求组、107,827条候选训练记录、12个特征。特征只读目标日期前的历史和共现图，不把未来正例注入候选。未观察项用于隐式排序对比，不能称已确认负反馈。三组开发参数择优，冻结模型0。
3. 神经 ID 推荐：{training['training_examples']:,}个下一新正反馈训练样本，{training['training_users']:,}位训练用户，{training['known_ids']:,}个有训练正反馈且能连接目录的商品。PyTorch BPR，32个采样负例，64维，三个种子17/29/43。
4. 顺序对照：无序篮子聚合与2层、2头因果 Transformer。序列实现采用同日平均池化，属于 SASRec 思路的日篮子适配，不声称逐条复现标准 SASRec。三种种子中序列均弱于无序模型，未作为部署方案。

开发集上通用语义向量表现弱，不能推出所有神经向量都无效。序列弱可能与历史短、日期粒度粗和样本稀疏有关；本轮未做能单独证明原因的消融，不将推测写成结论。

## 为什么绝对分数低

全部12,111件目录竞争，并且不只在已命中的候选上算分。最终冷商品目标 {cold['targets']} 个，树排序命中 {cold['retrieved_targets']}；暖商品目标 {warm['targets']} 个，命中 {warm['retrieved_targets']}。用户未来正评价具有稀疏性，没被评价不代表讨厌。不能与旧搜索候选池 pooled nDCG≈0.8 横向比较。

## 接入与实测

- 5173 同一购物页面新增“推荐实验室”，从服务端12个公开历史样例或无历史冷启动中选择；演示身份不写入登录用户长期记忆。
- 同目录支持推荐、英文商品检索（中文需求由现有模型翻译路由）、按编号喜欢/排除、比较、撤销和清除演示偏好。
- 页面每行仅一个来源ASIN；没有价格、库存与商城SKU，因此无购买按钮或交易授权。
- 部署导出纯树推理，业务环境没有安装 LightGBM。全部992条开发Top100与离线LightGBM结果一致。
- 纯推荐工具暖请求 P50 {verify['latency_ms_p50']:.1f}ms、P95 {verify['latency_ms_p95']:.1f}ms；这是992次顺序本地调用，不是生产并发容量。
- {http['turn_count']}轮真实HTTP操作，{http['check_count']}项身份、版本、幂等和工具效果检查；全链路耗时范围 {min(http['seconds']):.2f}–{max(http['seconds']):.2f}秒。浏览器验证另计，不能将两者加成一个模型成功率。
- 只刷新已登记的Agent进程并保留原环境；现有Java、搜索、知识库进程没有重启。

## 尚未实现，不写入简历的能力

- 没有真实曝光日志，因此未训练/验证真实CTR、CVR、多目标点击购买模型；评价监督不能冒充这些。
- 没有真实用户线上A/B、商业收益和生产容量验证。
- 公开美妆源不与 KuaiSearch 商品或登录用户强行实体合并；不是跨域协同迁移实验。
- 当前受控推荐模式没有分步暂停/恢复，UI禁用了该开关；不声称GraphV2推荐持久执行已验收。
- 尚未启用“在搜索当前候选内个性化重排”、任意商品属性硬约束或跨源统一打分；不支持的语言意图请求澄清。
- 原24项探索清单仍有更广场景未实现，本轮按已发布受控模式验收，不把24项全部打勾。

## 投递判断

项目现已形成真实推荐数据 → 协同/内容召回 → 特征工程 → 学习排序 → 神经/序列对照 → 时间留出统计评测 → 同应用工具接入的链路，可作为推荐算法实习项目证据。岗位录用仍取决于基础题与本人能否讲清实现。下一步应以投递、复习和面试反馈为主，不再为了新增名词立即堆DPO/RL。
'''
    (DOC/'RESULT.md').write_text(report,encoding='utf-8')
    skills='''<section class="section skills-section"><h2 class="section-title">专业技能</h2>
<p><strong>推荐与学习排序：</strong>理解协同过滤、内容推荐、隐式反馈及序列建模，具备 <b>ItemCF、BPR、LambdaMART</b> 实践；使用 PyTorch 训练兴趣聚合与因果 Transformer，完成时间切分、困难候选构建和多种子对照。</p>
<p><strong>搜索与模型训练：</strong>熟悉 BM25、稠密召回、RRF 与交叉编码精排；使用 <b>Transformers / PEFT</b> 完成 LoRA、Pairwise 排序训练，具备向量索引压缩和模型推理优化经验。</p>
<p><strong>实验与工程：</strong>熟悉 <b>Python、Java、SQL</b>；使用 nDCG、Recall、用户配对 Bootstrap 评估收益，区分曝光、点击、评价与未观察反馈；使用 FastAPI、MySQL、Redis 构建服务，具备 RAG、MCP、工具调用和多轮状态管理实践。</p></section>'''
    project='''<article id="search-project"><div class="company"><div class="company-name">电商搜索推荐与 Shopping Agent<span class="project-role"> · 个人项目</span></div><div class="company-meta">2026.07—至今</div></div>
<p class="project-intro">围绕同一购物 Agent 搭建搜索与推荐链路：以 KuaiSearch、MultiCPR 的 <b>763.7 万条商品记录</b>开展搜索，以 Amazon 真实评价行为开展个性化推荐；分别建立监督与评测口径，统一接入商品展示、需求修改和证据问答。</p>
<ul class="project-points">
<li><strong>推荐数据与时间隔离：</strong>针对搜索相关性标签无法刻画用户偏好的问题，引入 Amazon 电商行为，清洗得到 <b>28,879 条评价事件、12,111 件目录商品</b>；以 ASIN 连接行为与商品内容，隔离冲突评分，保留冷商品和未命中目标，按时间构建训练、开发与留出集。</li>
<li><strong>多路召回与个性化排序：</strong>针对行为稀疏和内容噪声，组合 <b>ItemCF、TF-IDF、MiniLM</b> 候选，构造共现、内容相似度、历史和冷启动等 <b>12 个特征</b>；使用更早时间快照生成 <b>514 组请求、107,827 条候选记录</b>，训练 LambdaMART，避免目标行为泄漏至共现特征。</li>
<li><strong>固定候选评测：</strong>开发选参后冻结方案，在 <b>925 位用户、1,109 个未来正评价目标</b>的时间留出集上，相同候选下 nDCG@10 <b>0.0195→0.0301</b>，Recall@100 <b>8.36%→10.59%</b>；10,000 次用户配对 Bootstrap，nDCG 差值95%区间 <b>[0.0038, 0.0181]</b>。</li>
<li><strong>神经推荐与序列对照：</strong>针对同日行为无可靠先后顺序的问题，以日篮子聚合构造 <b>8,493 个训练样本</b>，基于 PyTorch / BPR 对比无序兴趣模型与 SASRec 思路的因果 Transformer；完成 <b>3 个随机种子</b>训练，分层检查暖/冷商品和短历史表现，按效果保留更稳妥方案。</li>
<li><strong>搜索召回与精排微调：</strong>构建 BM25、字符二元组与 BGE 三路召回，经加权 RRF 后使用交叉编码精排；对 <b>bge-reranker-base</b> 进行 LoRA / Pairwise 训练，使用 <b>5,823 组有序文档对</b>。固定 Top300 候选，33 条开发主查询 pooled <b>nDCG@10：0.736→0.830</b>。</li>
<li><strong>模型部署与 Agent 接入：</strong>将推荐树模型导出为轻量CPU推理，<b>992 条开发请求 Top100 与离线结果全部一致</b>，暖工具调用 P50/P95 为 <b>138/162ms</b>；接入同一购物页面，支持公开历史推荐、同目录搜索、喜欢/排除、比较与撤销，隔离匿名样例与登录身份，保留来源及模型版本。</li>
<li><strong>知识、上下文与协作：</strong>针对标题缺少商品规格证据，整理 <b>425 条可追溯事实、覆盖99个手机型号</b>，通过混合检索及只读MCP支撑问答；结合 LangGraph / TaskState 管理跨轮约束，使用 Context Pack/View 和 MySQL/Redis 版本化偏好管理上下文，证据不足时委派只读研究子 Agent。</li>
</ul></article>'''
    original=Path('C:/Users/ming/Desktop/袁明珠_简历/袁明珠_北航28届_搜索算法_SearchAgent_更新版.html')
    document=original.read_text(encoding='utf-8')
    document=re.sub(r'<section class="section skills-section">.*?</section>',skills,document,count=1,flags=re.S)
    document=re.sub(r'<article id="search-project">.*?</article>',project,document,count=1,flags=re.S)
    document=document.replace('搜索排序算法 / Search Agent 实习','推荐算法 / 搜索算法实习')
    document=document.replace('ymz-search-20260914-v4','ymz-recommendation-20260916-v1')
    document=document.replace('选填照片','')
    document=document.replace('</style>','\n.profile-photo-slot:has(.photo-placeholder){display:none;}\n</style>',1)
    resume=CAREER/'袁明珠_北航28届_搜索推荐算法实习.html';resume.write_text(document,encoding='utf-8')
    claim_rows=[('rec-data','真实评价数据与目录','28,879条清洗事件 / 12,111件静态目录',str(BASE/'SOURCE_AUDIT.json'),'不是曝光点击购买日志'),
                ('rec-rank','冻结固定候选排序提升','nDCG10 0.0195→0.0301',str(ROOT/'final-001/RESULT.json'),'925用户时间留出；不是线上商业收益'),
                ('rec-seq','日篮子神经推荐训练','8493样本、3种子、均值与因果Transformer对照',str(ROOT/'sequence-dev-001/seed-17/PROTOCOL.json'),'序列未胜出，不写SASRec提升'),
                ('rec-serve','模型导出与应用接入','992请求Top100一致',str(ROOT/'SERVING_VERIFIED.json'),'暖工具耗时不等于端到端/生产容量'),
                ('search-existing','旧搜索精排结果','33开发主查询0.736→0.830',str(original),'沿用用户已确认旧简历；本轮未重跑搜索模型')]
    ledger={'schema_version':1,'profile':{'candidate_id':'ymz','target_roles':['推荐算法实习','搜索算法实习'],'updated_at':'2026-09-16'},'claims':[{'id':i,'source_fact':fact,'candidate_wording':word,
        'sources':[{'type':'local_artifact','location':source,'public':False}],'responsibility_level':'项目负责人',
        'verification_status':'已确认','allowed_uses':['推荐算法实习简历','面试复盘'],
        'interview_details':{'decisions':['固定任务监督和评测口径'],'difficulties':['数据稀疏、归因与版本追溯'],
                             'verification':['见 RESULT.md、INTERVIEW.md 及 sources 中原始产物'],'result':word},
        'boundary':boundary,'risk_notes':['个人项目，使用AI辅助开发；本人须能独立解释代码和实验。'],'last_verified':'2026-09-16'}
        for i,fact,word,source,boundary in claim_rows]}
    (CAREER/'claim-evidence-ledger.json').write_text(json.dumps(ledger,ensure_ascii=False,indent=2),encoding='utf-8')
    write(DOC/'DELIVERY_PATHS.json',{'resume':str(resume),'report':str(DOC/'RESULT.md'),'data':str(ROOT)})
    # Standalone status view; values loaded from the actual frozen report only.
    table_html='<table><thead><tr><th>留出模型</th><th>nDCG@10</th><th>Recall@100</th></tr></thead><tbody>'+''.join(f"<tr><td>{names[a]}</td><td>{metrics[a]['all']['ndcg_at_10']:.5f}</td><td>{metrics[a]['all']['recall_at_100']:.2%}</td></tr>" for a in ['popular','equal_rrf','fixed_union_rrf','lambdamart_0','mean_seed17','causal_seed17'])+'</tbody></table>'
    visual='''<!doctype html><html lang="zh"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>搜索推荐 · 当前闭环</title><style>
body{font:16px/1.65 system-ui,'Microsoft YaHei';color:#21312a;background:#f6f7f4;max-width:1080px;margin:30px auto;padding:20px}h1{font-size:34px}h2{font-size:22px}section{background:white;padding:24px;margin:18px 0;border:1px solid #e2e5dc;border-radius:14px}.flow{display:flex;flex-wrap:wrap;gap:12px}.flow span{padding:12px;background:#edf4ee;border-radius:10px}.metric{font-size:28px;color:#205a41;font-weight:bold}table{border-collapse:collapse;width:100%}td,th{text-align:left;border-bottom:1px solid #e2e5dc;padding:10px}.muted{color:#697269}a{color:#246847}summary{cursor:pointer;font-weight:bold}@media(max-width:650px){body{margin:0;padding:12px}section{padding:16px}h1{font-size:27px}}</style>
<p class="muted">2026-09-16 · 同一电商项目 · 真实产物驱动</p><h1>搜索已有基础，推荐现在也有完整实验链路。</h1>
<section><div class="flow"><span>真实评价＋商品</span><span>→ 时间隔离</span><span>→ 多路候选</span><span>→ 学习排序</span><span>→ 留出评测</span><span>→ 同一Agent页面</span></div><p>推荐与搜索各用匹配的监督信号；商品身份和用户身份保持来源边界。</p></section>
<section><h2>最值得讲的结果：固定候选的排序收益</h2><p class="metric">nDCG@10　0.0195 → 0.0301</p><p>925 用户 / 1,109 个未来正评价目标；差值95%区间 [0.0038, 0.0181]。Recall@100：8.36% → 10.59%。</p>'''+table_html+'''<p class="muted">这是全目录推荐，不能与旧搜索 pooled nDCG≈0.8 横向比较。区间支持此设置下的平均提升，不能推出线上业务收益。</p></section>
<section><h2>已经训练并核查</h2><ul><li>LambdaMART：514组请求、107,827条候选、12个特征；三个较早时间快照。</li><li>BPR神经推荐：8,493个训练样本；无序兴趣与因果日篮子模型，三种随机种子。</li><li>通用MiniLM没有超过TF-IDF；因果序列没有超过无序模型。负结果保留。</li><li>部署模型992条Top100与离线一致；真实HTTP和浏览器均已验证受控推荐操作。</li></ul></section>
<section><h2>回来后直接体验</h2><p>打开 <a href="http://127.0.0.1:5173/">购物页面</a> → 展开“推荐实验室” → 加载公开样例 → 选历史 → 输入“推荐”。然后试“喜欢第一个”“不要第二个”“比较前两个”“撤销”。</p><p>这是公开美妆评价历史演示；不代表你的真实偏好，也不支持购买。</p></section>
<section><h2>下一步：开始投递与复习</h2><p>项目证据可以支持推荐算法实习投递。本人还需能讲清：BPR负采样、LambdaMART梯度、时间泄漏、冷启动、序列为何没赢、如何做真实曝光实验。</p><details><summary>还不能宣称什么？</summary><p>真实CTR/CVR、多目标线上优化、A/B商业收益、跨数据集协同迁移、生产并发容量。没有把评价伪装成点击，也没有把未通过的探索清单算作验收成功。</p></details><p><a href="RESULT.md">完整结果与边界</a> · <a href="INTERVIEW.md">面试复习笔记</a> · <a href="RUNBOOK.md">复现与启动</a></p></section></html>'''
    (DOC/'show-me-recommendation-ready.html').write_text(visual,encoding='utf-8')
    print(json.dumps({'resume':str(resume),'report':str(DOC/'RESULT.md')},ensure_ascii=False))

if __name__=='__main__':main()
