"""Two independent fresh judgments per case; no study labels reach the judges."""
import argparse
from collections import Counter
from copy import deepcopy
import random
import subprocess
import sys
import tempfile
import time
from .common import *
from .runner import execute, check_code
from .audit import audit, require

JROOT=HERE/'attempt001/review001'
RUBRIC='''请独立评审每个匿名回答，不推测来源，不选择更喜欢的系统。逐个依据相同证据评分；回答之间的相似性不是正确性证据。
四维整数 0–4 分：correctness=事实/状态语义正确性；constraints=仍有效需求与阶段契约遵守；relevance=切中当前问题；usefulness=必要信息完整性和可操作性。
共同锚点：4=无实质问题且充分；3=仅轻微问题、不影响主要用途；2=明显遗漏/缺陷但部分可用；1=严重错误或大部分不可用；0=无回答、离题或核心完全错误。篇幅长短、Markdown、措辞风格本身不奖惩。
seriousError 单列：捏造关键事实/证据、推荐明显违反有效硬条件却未说明、撤销/修改反向执行、核心问题未处理等。缺少次要格式不算严重。issues 给出可复核的原话短引文及材料字段/历史轮次，不能只写笼统印象。每个分数至少在 rationale 中说明主因；无问题允许 issues=[]。
完整历史是本题所知的原始用户话语，不含未知的历史助手回答。当前展示由 providedEvidence.products 的数组顺序定义；不要自行想象其它候选。状态是受控参考快照，不代表其已覆盖用户所有偏好；需要同时理解完整历史与本轮修改。出现歧义或缺失证据时可以认可合理澄清/承认未知，不能要求猜测不存在的事实。
价格注明 synthetic/budget_and_ranking 的只能按题设作为模拟价格分析，不得当成实时成交价；未验证库存/售后/游戏性能不得当确定事实。原始记录的 description/specs 与 structured 字段都属于材料，冲突需要谨慎；不要因引用方式不同否定有据事实。
final 阶段评审面向用户正文；state 阶段评审 JSON 增量的含义而非文风：null 表示不更新，upsertRequirements 按 key 更新，removeRequirementKeys 删除，未修改条件保留；不得要求重复写入已有事实。输出结构的确定性校验另有自动程序，你仍要评价语义。是否需澄清只依据原话和给定契约。
insufficientEvidence 表示材料不足使该回答核心质量不能可靠裁决，解释原因。不要用它掩盖明确错误。输出必须包含每个给定 answerId 恰好一次，不添加不存在的回答。'''

def object_schema(properties):
    return {'type':'object','properties':properties,'required':list(properties),'additionalProperties':False}

SCORE_SCHEMA=object_schema({'answerId':{'type':'string'},
    **{k:{'type':'integer','minimum':0,'maximum':4} for k in ('correctness','constraints','relevance','usefulness')},
    'seriousError':{'type':'boolean'},'insufficientEvidence':{'type':'boolean'},
    'issues':{'type':'array','items':{'type':'string'}},'rationale':{'type':'string'}})
SCHEMA=object_schema({'packetId':{'type':'string'},'scores':{'type':'array','items':SCORE_SCHEMA}})

def make_packet(f,rows,reviewer):
    rng=random.Random(SEED+reviewer*10000+int(f['id'][-3:]));shuffled=list(rows);rng.shuffle(shuffled)
    pid='packet-'+sha([SEED,reviewer,f['id']])[:16];answers=[];mapping=[]
    for row in shuffled:
        aid='answer-'+f'{rng.getrandbits(64):016x}'
        answers.append({'answerId':aid,'text':row['answer']})
        mapping.append({'packetId':pid,'answerId':aid,'reviewer':reviewer,
            'caseId':f['id'],'ordinal':row['ordinal'],'arm':row['arm'],'repeat':row['repeat'],'answerHash':sha(row['answer'])})
    common_instruction=f['prompts']['A_RAW'].split('\n\n<context>',1)[0]
    packet={'packetId':pid,'stage':f['stage'],'rubric':RUBRIC,
        'task':{'completeUserHistory':f['history'],'currentUserRequest':f['query'],
            'sharedStageInstructionsForTheAnswerNotForJudge':common_instruction,
            'providedReferenceState':f['state'],'referenceStateTiming':'after_current_request' if f['stage']=='final' else 'before_current_request',
            'providedEvidence':{'products':f['products'],'evidenceRefs':f['evidenceRefs']},
            'stateOutputSchema':f['schema']},'anonymousAnswers':answers}
    encoded=canonical(packet)
    for forbidden in ARMS+('sameApplicationInputBC','applicationInputTokens','cliWallMs','compilerReceipt','expectedHard','sourceRowHash','quality_protocol_blind'):
        require(forbidden not in encoded,'judge_label_leak:'+forbidden)
    require(len(answers)==9 and len({x['answerId'] for x in answers})==9,'nine_anonymous_answers')
    return packet,mapping

def prepare():
    require(not JROOT.exists(),'review_directory_exists')
    report,rows=audit()
    require(read(HERE/'attempt001/audit001/report.json')['ledgerHash']==report['ledgerHash'],'audit_not_current')
    packets=[];mapping=[];schedule=[]
    for i in range(1,49):
        f=read(HERE/'inputs'/f'case-{i:03d}.json'); subset=[r for r in rows if r['kind']=='study' and r['caseId']==f['id']]
        for reviewer in (1,2):
            packet,bind=make_packet(f,subset,reviewer);packets.append(packet);mapping+=bind
            schedule.append({'packetId':packet['packetId'],'reviewer':reviewer})
    random.Random(SEED+90000).shuffle(schedule)
    for i,row in enumerate(schedule,1):row['ordinal']=i
    JROOT.mkdir()
    for packet in packets:write_new(JROOT/'packets'/(packet['packetId']+'.json'),packet)
    write_new(JROOT/'private_mapping.json',mapping);write_new(JROOT/'schedule.json',schedule);write_new(JROOT/'output.schema.json',SCHEMA)
    write_new(JROOT/'manifest.json',{'at':now(),'scheduledJudgments':96,'answersPerJudge':432,
        'sourceAuditHash':file_sha(HERE/'attempt001/audit001/report.json'),'sourceLedgerHash':file_sha(HERE/'attempt001/ledger.jsonl'),
        'codeFiles':{name:file_sha(HERE/name) for name in ('judging.py','judge_base.txt','audit.py')},
        'files':{str(x.relative_to(JROOT)):file_sha(x) for x in JROOT.rglob('*') if x.is_file()},
        'model':p.MODEL,'effort':p.EFFORT,'singleModelFamily':True,'priorDiscussionProvided':False,
        'protocol':'two independently permuted fresh ephemeral conversations per case; 96 total; no access to mapping, labels or other judge results',
        'timeoutSecondsPerJudgment':300,'batchTimeoutSeconds':21600,'reviewMetricsSeparateFromSUT':True,
        'uncertaintyPolicy':'preserve both judgments; serious disagreements unresolved unless separately independently adjudicated; no favorable reruns'})
    print(canonical({'status':'ANONYMOUS_PACKETS_FROZEN','packets':96,'ratingsExpected':864}),flush=True)

def verify_review():
    verify(); check_code(read(HERE/'attempt001/runtime.json'))
    m=read(JROOT/'manifest.json')
    for name,digest in m['codeFiles'].items():require(file_sha(HERE/name)==digest,'review_code_drift:'+name)
    for name,digest in m['files'].items():require(file_sha(JROOT/name)==digest,'review_input_drift:'+name)
    require(file_sha(HERE/'attempt001/ledger.jsonl')==m['sourceLedgerHash'],'study_ledger_drift')
    return m

def preflight():
    verify_review(); out=JROOT/'judge_attempt001';out.mkdir(exist_ok=False)
    cwd=Path(tempfile.mkdtemp(prefix='blind-answer-review-'));overrides=p.effective_overrides(cwd)
    overrides['model_instructions_file']=str(HERE/'judge_base.txt')
    runtime=deepcopy(read(HERE/'attempt001/runtime.json'))
    runtime.update({'at':now(),'cwd':str(cwd),'overrides':overrides,'reviewManifestHash':file_sha(JROOT/'manifest.json')})
    login=subprocess.run([p.CODEX,'login','status'],capture_output=True,text=True,env=p.clean_env())
    require(login.returncode==0 and 'Logged in using ChatGPT' in login.stdout+login.stderr,'native_subscription_auth')
    write_new(out/'runtime.json',runtime)
    prefixes=[]
    for i in range(2):
        marker=f'INDEPENDENT_JUDGE_PROBE_{i}_ONLY_OK'
        proc=subprocess.run([p.CODEX,*p.cli_options(overrides),'debug','prompt-input',marker],cwd=cwd,env=p.clean_env(),
            capture_output=True,encoding='utf-8',timeout=120,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        write_new(out/f'probe{i}.json',{'exitCode':proc.returncode,'stdout':proc.stdout,'stderr':proc.stderr})
        require(proc.returncode==0,'review_prompt_export')
        exported=json.loads(proc.stdout);encoded=canonical(exported)
        for forbidden in ('memory_summary','MEMORY.md','<response-annotations>','0904.md','<subagents>',*ARMS,'quality_protocol_blind'):
            require(forbidden not in encoded,'review_context_leak:'+forbidden)
        if i:require('INDEPENDENT_JUDGE_PROBE_0' not in encoded,'review_history_leak')
        prefixes.append([{'role':x.get('role'),'content':x.get('content')} for x in exported[:-1]])
    write_new(out/'preflight.json',{'at':now(),'status':'READY','sampledModelCalls':0,'prefixHashes':[sha(x) for x in prefixes],
        'prefixEqual':prefixes[0]==prefixes[1],'nativeFullWireVisible':False,'discussionAndArmLabelsAbsentInProbe':True})
    print(canonical(read(out/'preflight.json')),flush=True)

def validate_judgment(packet,answer):
    import jsonschema
    value=json.loads(answer);jsonschema.validate(value,SCHEMA)
    require(value['packetId']==packet['packetId'],'judgment_packet_id')
    require(Counter(s['answerId'] for s in value['scores'])==Counter(a['answerId'] for a in packet['anonymousAnswers']),'judgment_answer_ids')
    require(all(s['rationale'].strip() for s in value['scores']),'empty_rationale')
    return value

def run():
    manifest=verify_review();out=JROOT/'judge_attempt001';runtime=read(out/'runtime.json')
    require(read(out/'preflight.json')['status']=='READY','review_preflight')
    require(not (out/'ledger.jsonl').exists(),'review_no_implicit_resume')
    require(file_sha(JROOT/'manifest.json')==runtime['reviewManifestHash'],'review_manifest_drift')
    started=time.monotonic();consecutive_failures=0
    try:
        for row in read(JROOT/'schedule.json'):
            verify_review()
            if time.monotonic()-started>manifest['batchTimeoutSeconds']:raise RuntimeError('review_batch_timeout')
            packet=read(JROOT/'packets'/(row['packetId']+'.json'));prompt=canonical(packet)
            d=out/'calls'/f"{row['ordinal']:03d}-{row['packetId']}";d.parent.mkdir(exist_ok=True)
            append(out/'ledger.jsonl',{'event':'START',**row,'at':now(),'packetHash':sha(packet)})
            print(canonical({'event':'START',**row}),flush=True)
            result=execute(prompt,runtime,d,SCHEMA,timeout=manifest['timeoutSecondsPerJudgment'])
            try:value=validate_judgment(packet,result['answer']);validation={'valid':True,'ratings':len(value['scores'])}
            except Exception as exc:value=None;validation={'valid':False,'errorType':type(exc).__name__,'error':str(exc)[:500]}
            if value:write_new(d/'judgment.json',value)
            result.update({**row,'packetHash':sha(packet),'validation':validation,'scope':'BLIND_REVIEW_NOT_SUT_EXECUTION'})
            write_new(d/'result.json',result)
            append(out/'ledger.jsonl',{'event':'END',**row,'at':now(),'resultHash':file_sha(d/'result.json'),
                'requestHash':file_sha(d/'request.json'),'judgmentHash':file_sha(d/'judgment.json') if value else None})
            print(canonical({'event':'END',**row,'status':result['status'],'validation':validation}),flush=True)
            if result['toolEvents'] or result['malformedLines']:raise RuntimeError('review_execution_boundary')
            consecutive_failures=consecutive_failures+1 if result['status']=='FAILED' else 0
            if consecutive_failures>=3:raise RuntimeError('three_consecutive_review_provider_failures')
            if any(x in canonical(result['errors']).lower() for x in ('unauthorized','usage limit','quota exceeded','insufficient_quota','rate limit reached')):
                raise RuntimeError('review_account_or_quota_no_reset')
        write_new(out/'complete.json',{'at':now(),'status':'96_JUDGMENTS_ATTEMPTED','durationSeconds':time.monotonic()-started})
    except Exception as exc:
        write_new(out/'stop.json',{'at':now(),'status':'STOPPED_WITH_EVIDENCE','errorType':type(exc).__name__,'error':str(exc)})
        raise

def launch():
    verify_review();out=JROOT/'judge_attempt001'
    require(read(out/'preflight.json')['status']=='READY','review_preflight')
    for name in ('launch.json','ledger.jsonl','batch.stdout.txt','batch.stderr.txt'):require(not (out/name).exists(),'duplicate_review_launch')
    args=[sys.executable,'-B','-m','agent.evaluation.context_codex_abc_study_v1_20260904.judging','run']
    with (out/'batch.stdout.txt').open('x',encoding='utf-8') as stdout,(out/'batch.stderr.txt').open('x',encoding='utf-8') as stderr:
        proc=subprocess.Popen(args,cwd=ROOT,stdin=subprocess.DEVNULL,stdout=stdout,stderr=stderr,
            creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0)|getattr(subprocess,'CREATE_NEW_PROCESS_GROUP',0))
    receipt={'at':now(),'pid':proc.pid,'args':args,'cwd':str(ROOT),'reviewManifestHash':file_sha(JROOT/'manifest.json')}
    write_new(out/'launch.json',receipt);print(canonical(receipt),flush=True)

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('phase',choices=['prepare','preflight','run','launch']);args=parser.parse_args()
    {'prepare':prepare,'preflight':preflight,'run':run,'launch':launch}[args.phase]()
