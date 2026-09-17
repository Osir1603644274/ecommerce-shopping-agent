"""Build a new read-only phone evidence audit; never run the shopping application.

Source annotations are analyst judgments, NOT qrels or model outputs.
Web facts below were manually checked on 2026-09-08; this script verifies LOCAL
bindings only. Re-running it does not refresh or verify the external websites.
"""
import argparse
import hashlib
import json
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
TOPIC = Path(__file__).resolve().parent
REPLAY = 'agent/evaluation/real_user_multiturn_replay_20260903_v7'
CATALOG = 'datasets/current/used-phone/catalog.jsonl'
DB = '.runtime/used-phone-demo-439/web-query-intake.sqlite3'

def sha(b):
    return hashlib.sha256(b).hexdigest()

def canonical(x):
    return json.dumps(x, ensure_ascii=False, sort_keys=True, separators=(',', ':'))

def rows(p):
    return [json.loads(x) for x in (ROOT / p).read_text(encoding='utf-8-sig').splitlines() if x.strip()]

# Short paraphrases only: no full article copies, no permission to redistribute inferred.
SOURCES = [
    ('S01', 'OPPO A96 中国版参数', 'https://www.oppo.com/cn/smartphones/series-a/a96/specs/', 'OFFICIAL_SPEC', '入网型号、芯片、电池', 'PFUM10；骁龙695；电池典型4500mAh，额定4385mAh。', '只有型号规格，不能保证这台二手机续航或游戏帧率。'),
    ('S02', 'OPPO Find X6 中国版参数', 'https://www.oppo.com/cn/smartphones/series-find-x/find-x6/specs/', 'OFFICIAL_SPEC', '入网型号、摄像头', 'PGFM10；广角、超广角、潜望长焦均为5000万像素；广角与潜望长焦支持OIS；照片最高3倍光学变焦。', '不是Find X6 Pro；硬件功能不能直接推出所有场景成像胜出。'),
    ('S03', 'OPPO A2 中国版参数', 'https://www.oppo.com/cn/smartphones/series-a/a2/specs/', 'OFFICIAL_SPEC', '入网型号、电池', 'PJB110；电池典型5000mAh、额定4880mAh；33W充电。', '容量与充电功率不能替代续航测试。'),
    ('S04', 'vivo Y3s 中国版参数', 'https://www.vivo.com.cn/vivo/param/y3s', 'OFFICIAL_SPEC', '建议零售价中的型号、处理器、电池信息', 'V1901A/V1901T；MT6765；电池典型5000mAh；5V/2A充电。', '需确认地区与入网型号；不使用官网首发价作为二手实价。'),
    ('S05', '荣耀80 官方帮助中心', 'https://www.honor.com/cn/shop/help/category-240.html', 'OFFICIAL_SPEC', '荣耀80的参数（非80 Pro/SE）', '该型号后置摄像头为1.6亿、800万、200万像素；芯片骁龙782G。', '必须限定荣耀80小节；像素不等于实拍质量，原目录品牌华为有绑定冲突。'),
    ('S06', '荣耀X40 官方帮助中心', 'https://www.honor.com/cn/shop/help/category-238.html', 'OFFICIAL_SPEC', '荣耀X40的参数', '后置5000万+200万像素；骁龙695；电池典型5100mAh。', '目录将荣耀写作华为，先审查身份；机况未知不能由官网补齐。'),
    ('S07', 'iPhone 7 技术规格', 'https://support.apple.com/zh-cn/111943', 'OFFICIAL_SPEC', '摄像头、视频拍摄、注释', '标准iPhone 7为1200万像素摄像头；视频支持4K 30fps；页面2倍光学变焦条目仅限7 Plus。', '不能将标准机功能背书给卖家WiFi版实物；不能混入7 Plus能力。'),
    ('S08', 'Notebookcheck Mate 40 Pro 原始评测', 'https://www.notebookcheck.net/Huawei-Mate-40-Pro-review-Top-smartphone-with-handicap.505829.0.html', 'INDEPENDENT_MODEL_TEST', 'Battery life / Battery Runtime；Games', 'WiFi网页测试为10小时9分钟；亮度150cd/m²，Huawei Browser 11。游戏表列PUBG Mobile 1.1.0平均39.8fps。', '历史测试样机和软件；游戏画质设置本轮未完整核对，不用于当前游戏承诺；不能与缺少同协议数据的畅享20直接排名。'),
]

# ids = intended/actually displayed listings, never freshly retrieved candidates.
CASES = [
    ('c001-t01', [], [], 'NO_RECORDED_CANDIDATES', '已有确认的学生/便宜/游戏原文；本轮未取得可核验的原候选或完整诊断。', '先明确预算、游戏和画质目标，再绑定真实候选；不替该轮编造型号。', 'NO_CANDIDATE'),
    ('c002-t03', ['1795901','7441112016001245431','1597799'], ['S08'], 'PREVIOUS_TURN_SCOPE', '这里面原指两件Mate 40 Pro与一件畅享20；历史回复重新返回vivo Y35/Y3s/Y52s。', '已有Mate40Pro网页续航事实可复用；畅享20同协议游戏/续航实测未核验。还必须保留原候选与2000预算。', 'PARTIAL_AND_SCOPE'),
    ('c004-t02', ['8542161000270987328','320619'], ['S05','S06'], 'ACTUAL_RESPONSE', '历史返回荣耀80、X40；预算1200丢失，拍照否定游戏被路由为gaming_title_claim。', '相机规格可补，但不能仅按像素评胜负；两条目录均写华为，需处理品牌/型号绑定冲突。', 'PARTIAL_AND_IDENTITY'),
    ('c005-t03', [], [], 'NO_SEARCH_EXECUTED', '实际回复总执行超时；原逐字记录说明没有执行检索、没有展示候选。', '先解决运行与探索性推荐；续航知识需求存在，本轮没有可绑定型号。', 'RUNTIME_BLOCKED'),
    ('c007-t02', ['1710694','1276807','214508'], ['S01'], 'ACTUAL_RESPONSE', '续航被映射为电池健康偏好；返回A96及两件标题未给具体型号的安卓手机。', 'A96可取得型号容量/芯片；另两件身份不足；健康百分比不能直接比较不同型号续航。', 'PARTIAL_AND_IDENTITY'),
    ('c008-t07', ['6055970412849301893','4194616','2732031'], ['S03','S04'], 'ACTUAL_RESPONSE', '500预算未应用，实际合成参考价855/856/857；候选Y3s、A11、A2。', 'Y3s/A2容量资料可取得；A11可靠规格本轮未完成核验；不能以同为5000mAh判定同续航。', 'PARTIAL_AND_BUDGET'),
    ('c008-t08', ['2675122','4705189315400771947','8542161000270987328'], ['S02','S05','S07'], 'ACTUAL_RESPONSE', '仅标题相关性召回，返回iPhone7 WiFi版、Find X6、荣耀80；保留健康偏好但未保留500预算。', '可以增加镜头/变焦/防抖依据；WiFi版实物和荣耀品牌冲突需核验；不据此生成无条件拍照总排名。', 'PARTIAL_AND_IDENTITY'),
    ('c003-t02', [], [], 'NO_NEW_MAPPING_ASSERTED', '用户纠正1200预算，原事故材料显示预算没有保留。', '由约束提取、状态继承和价格过滤解决；不伪装成缺少知识。', 'ENGINEERING_CONTROL'),
    ('c004-t03', ['8542161000270987328','320619'], [], 'PREVIOUS_TURN_SCOPE', '第一个第二个指荣耀80和X40；比较时requirements清空，以字段完整度替代需求取舍。', '先保留候选序号、预算和拍照用途；新增相机资料属于上面c004-t02，不重复算独立知识需求。', 'ENGINEERING_CONTROL'),
    ('c005-t02', [], [], 'NO_CANDIDATES', '不怎么打游戏、也不要求拍照，却被追问不想要哪个品牌。', '否定语义和澄清策略问题；不能通过关键词把它统计为游戏/相机需求。', 'ENGINEERING_CONTROL'),
]

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--output', type=Path)
    args = ap.parse_args()
    paths = [CATALOG, f'{REPLAY}/conversations.jsonl', f'{REPLAY}/session_source_receipts.jsonl',
             'evaluation/used-phone-model-facts-dev-v1/facts.jsonl',
             'evaluation/used-phone-model-facts-dev-v1/resolved-catalog-rows-v1.jsonl',
             'docs/analysis-briefs/product-knowledge-mcp-2026-09-07/snapshot001/confirmed_web_turns.jsonl']
    receipts = rows(f'{REPLAY}/session_source_receipts.jsonl')
    for r in receipts:
        f = r['authorshipEvidenceFile']
        assert sha((ROOT/f['path']).read_bytes()) == f['sha256'], f['path']
        paths.append(f['path'])
    hashes = {p:sha((ROOT/p).read_bytes()) for p in dict.fromkeys(paths)}
    cat = {str(r['itemId']):r for r in rows(CATALOG)}
    assert len(cat) == 439
    conn = sqlite3.connect((ROOT/DB).as_uri()+'?mode=ro', uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA query_only=ON')
    conn.execute('BEGIN')
    raw = {sha(canonical(dict(r)).encode()):dict(r) for r in conn.execute('SELECT * FROM web_query_intake')}
    diag = {r['request_id']:dict(r) for r in conn.execute('SELECT * FROM web_query_diagnostic')}
    conn.close()
    confirmed = {}
    for c in rows(f'{REPLAY}/conversations.jsonl'):
        for t in c['turns']:
            r = raw[t['sourceRecordSha256']]
            assert r['message'] == t['rawUserText']
            assert sha(r['message'].encode()) == t['messageSha256']
            assert sha(('request:'+r['request_id']).encode()) == t['sourceRequestFingerprint']
            assert sha(('session:'+r['session_id']).encode()) == c['sourceSessionFingerprint']
            confirmed[t['turnId']] = (c,t,r)
    previous = {r['turnId']:r for r in rows(paths[5])}
    source_rows = [dict(zip(['sourceId','title','url','authority','section','verifiedFacts','limits'],r),
                        checkedOn='2026-09-08', verification='WEB_MANUAL_PAGE_READ',
                        listingIdentityVerified=False) for r in SOURCES]
    outrows = []
    for short, ids, sources, scope, observation, gap, status in CASES:
        tid = 'rumr-v1-'+short
        c,t,r = confirmed[tid]
        answer = None
        d = diag.get(r['request_id'])
        if d:
            assert sha(d['payload_json'].encode()) == d['payload_sha256']
            answer = json.loads(d['payload_json']).get('answer','')
        for iid in ids:
            assert iid in cat, iid
            if answer and scope == 'ACTUAL_RESPONSE':
                assert 'ID：'+iid in answer, (tid,iid)
        outrows.append(dict(turnId=tid,query=t['rawUserText'],messageSha256=t['messageSha256'],
            sourceRecordSha256=t['sourceRecordSha256'],database=DB,
            authorshipEvidenceLocator=c['authorshipEvidenceLocator'],
            diagnosticPayloadSha256=d['payload_sha256'] if d else None,
            originalAnswer=answer,knowledgeInterest=previous[tid]['knowledgeInterest'],
            scopeBasis=scope,itemIds=ids,sourceIds=sources,observation=observation,
            gap=gap,status=status,authority='AI_ANALYST_NOT_GOLD_NOT_CURRENT_RUNTIME_TEST'))
    selectedids = sorted({i for r in outrows for i in r['itemIds']})
    facts = rows(paths[3])
    resolved = rows(paths[4])
    matches = [r for r in resolved if str(r['itemId']) in selectedids]
    assert len(outrows)==10 and sum(r['knowledgeInterest'] for r in outrows)==7
    assert all(sha((ROOT/p).read_bytes())==h for p,h in hashes.items())
    summary = dict(status='PHONE_SOURCE_FEASIBILITY_AUDIT_COMPLETE_NOT_KB_IMPLEMENTED',
        sourceDate='2026-08-25 historical real developer cases',webCheckedOn='2026-09-08',
        confirmedTurnsReverified=len(confirmed),selectedTurns=10,knowledgeInterestTurns=7,
        engineeringControls=3,catalogRows=439,selectedCatalogRows=len(selectedids),
        verifiedExternalSources=len(source_rows),existingFacts=len(facts),existingResolutionRows=len(resolved),
        existingSelectedResolutions=matches,sourceFiles=hashes,sourceFilesUnchanged=True,
        modelCalls=0,retrievalRuns=0,businessWrites=0,
        caveat='No end-to-end answerability test, no new qrels, no source licence assessment for bulk ingestion.')
    if args.output:
        dest=args.output.resolve()
        if dest.parent!=TOPIC: raise ValueError('Output must be a new immediate child of topic')
        dest.mkdir(exist_ok=False)
        for name, data in [('cases.jsonl',outrows),('sources.jsonl',source_rows),
                           ('catalog-excerpts.jsonl',[cat[i] for i in selectedids])]:
            (dest/name).write_text(''.join(canonical(r)+'\n' for r in data),encoding='utf8')
        (dest/'summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2)+'\n',encoding='utf8')
        lines=['# 手机真实问题：型号与外部证据核查（2026-09-08）','',
            '本轮完成来源可行性审计。10条 = 7条正向知识需求 + 3条工程对照，**不是10条知识库可解决的失败**。',
            '已重新核验21轮原文与原数据库行/会话/请求指纹；表中为历史开发案例，不是当前系统失败率。未运行模型、检索实验或服务。',
            '', '## 逐条问题—商品—缺口—来源','',
            '7条知识需求中：5条有实际关联商品及部分外部资料，2条没有可用候选；没有做端到端回答实验，不能据此报成功率。','']
        sm={r['sourceId']:r for r in source_rows}
        for n,r in enumerate(outrows,1):
            lines += [f"### {n}. {r['query']}",'',f"- 原记录：`{r['turnId']}`；[原始佐证](../../../../{r['authorshipEvidenceLocator'].split('#')[0]})。",
                f"- 当时发生：{r['observation']}",f"- 缺口与处理：{r['gap']}"]
            for i in r['itemIds']:
                lines.append(f"- 商品 `{i}`：{cat[i]['title']}；目录品牌：{cat[i]['brand']}。")
            lines.append('- 可用来源：'+('；'.join(f"[{s} {sm[s]['title']}]({sm[s]['url']})" for s in r['sourceIds']) or '本条不指定型号知识来源；原因见上。'))
            lines+=['']
        lines += ['## 已核对的外部来源','',
                  '| ID | 来源 | 实际可取得的事实 | 使用边界 |','|---|---|---|---|']
        for r in source_rows:
            lines.append(f"| {r['sourceId']} | [{r['title']}]({r['url']})，{r['section']} | {r['verifiedFacts']} | {r['limits']} |")
        lines += ['', '## 首批范围与未完成项','',
            '1. 建议先整理 Mate 40 Pro、OPPO A96、Find X6、A2、vivo Y3s 的型号证据卡；这些型号直接来自上面的真实候选。先展示按型号限定的事实，不自动证明卖家实物身份。',
            '2. 荣耀80/X40先作为品牌冲突样例；无型号的两件安卓手机保持未知；iPhone7 WiFi版先查实物功能，不能照搬标准机。',
            '3. 不需要先扩充439商品库。需要新增的是证据和明确的商品—型号绑定；先做可读审查材料，再决定知识服务接线。',
            '4. 畅享20同协议续航/游戏评测、OPPO A11可靠规格，本轮未完成核验；不能说网上不存在。Mate40Pro中国官网规格地址读取失败，官方售后入口可找到，但未从中取得完整规格。',
            '5. 荣耀80/X40规格页本次文本解析出现空字段，改用官方帮助中心对应型号小节核验；不能把网页200或标题存在当成事实采集完成。',
            '6. 已有5条型号续航事实、22条解析切片位于 `evaluation/used-phone-model-facts-dev-v1/`；Mate40Pro的609分钟本次原网页复核一致，保留其非生产排序授权、实物身份未验证边界。',
            '7. 商品价格均为历史Demo合成参考价，不代表当前报价。目录七项机况是来源字段的受控解释，不是本轮实物检测；官方资料不能验证某台二手机维修史。',
            '', '## 核验文件','',
            '- [cases.jsonl](cases.jsonl)：逐字Query、原文哈希、来源、完整可用诊断回答、候选ID与分析。',
            '- [sources.jsonl](sources.jsonl)：8个核对过的外部来源、事实摘要、适用边界；不是已部署知识库。',
            '- [catalog-excerpts.jsonl](catalog-excerpts.jsonl)：13件相关商品的原行摘录，只是审计证据，不新增数据底座。',
            '- [summary.json](summary.json)：来源文件哈希、21轮回连结果、原有事实关联。',
            '- [审计脚本](../audit_phone_sources.py)：默认仅校验本地来源，指定新目录才写审计产物；不联网刷新来源、不跑实验。','']
        (dest/'README.md').write_text('\n'.join(lines),encoding='utf8')
        hashes_out={p.name:sha(p.read_bytes()) for p in dest.iterdir()}
        hashes_out['../audit_phone_sources.py']=sha(Path(__file__).read_bytes())
        (dest/'SHA256SUMS.json').write_text(json.dumps(hashes_out,ensure_ascii=False,indent=2)+'\n',encoding='utf8')
    print(json.dumps({k:v for k,v in summary.items() if k not in ['sourceFiles','existingSelectedResolutions']},ensure_ascii=False))
    print('existingSelectedResolutions='+canonical(matches))

if __name__=='__main__':
    main()
