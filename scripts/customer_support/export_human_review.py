"""Prepare unreviewed, hash-bound review forms; never infer human judgments."""
import argparse
import hashlib
import json
import re
from pathlib import Path

def digest(path):return hashlib.sha256(path.read_bytes()).hexdigest()
def read(path):return json.loads(path.read_text(encoding='utf-8'))

def export(run,dataset,output):
    run=run.resolve();manifest=read(run/'RUN.json');observations=read(run/'observations.json')
    if digest(dataset)!=manifest['datasetSha256']:raise ValueError('dataset differs from executed manifest')
    planned=manifest['caseIds'];observed={r['caseId']:r for r in observations}
    if len(observed)!=len(observations) or set(observed)!=set(planned):raise ValueError('all planned observations required; no partial review packet')
    cases={c['id']:c for c in map(json.loads,dataset.read_text(encoding='utf-8').splitlines())}
    rows=[]
    for ident in planned:
        if not re.fullmatch(r'[A-Za-z0-9_-]+',ident):raise ValueError('invalid case identifier')
        folder=run/ident;case=cases[ident];record=observed[ident]
        histories=read(folder/'all-chat-observations.json') if (folder/'all-chat-observations.json').exists() else [read(folder/'http-and-trace.json')] if (folder/'http-and-trace.json').exists() else []
        answers=[];seen=set()
        for observation in histories:
            trace=observation.get('trace') or {};result=trace.get('result') or {}
            fingerprint=hashlib.sha256(json.dumps(result,ensure_ascii=False,sort_keys=True).encode()).hexdigest()
            key=(trace.get('requestId'),fingerprint)
            if key in seen:continue
            seen.add(key)
            answers.append({'requestId':trace.get('requestId'),'turnStatus':trace.get('status'),'answer':result.get('answer'),
                            'citations':result.get('citations',[]),'resultSha256':fingerprint})
        evidence=[]
        for name in ('before.json','after-chat.json','final.json','failure-state.json','http-and-trace.json','all-chat-observations.json','VERDICT.json','FINAL_VERDICT.json','driver-evidence.json','AUDIT_VERDICT.json','ALL_AUDIT_VERDICTS.json','preview-final.json','policy-snapshot.json','scenario-contract.md'):
            path=folder/name
            if path.exists():evidence.append({'path':str(path.relative_to(run)).replace('\\','/'),'sha256':digest(path)})
        rows.append({'caseId':ident,'repetition':record['repetition'],'familyId':case.get('familyId'),'split':case['split'],
            'userMessages':[s['message'] for s in case['steps'] if s.get('actor')=='customer' and s.get('message')],
            'answers':answers,'automatedVerdict':record['verdict'],'automatedReasons':record.get('reasons',[]),
            'evidence':evidence,'humanReview':{'status':'UNREVIEWED','reviewer':None,'reviewedAt':None,'assertions':[]},
            'instruction':'人工拆分每个可核验断言，记录原文、证据位置、支持/不支持/无法判断及理由。无回答也必须说明原因；自动PASS不是人工标签。'})
    output.mkdir(parents=True,exist_ok=False)
    content=''.join(json.dumps(row,ensure_ascii=False)+'\n' for row in rows)
    (output/'review.jsonl').write_text(content,encoding='utf-8')
    package={'status':'UNREVIEWED_NOT_HUMAN_GOLD','sourceRun':str(run),'caseCount':len(rows),
             'datasetSha256':digest(dataset),'observationsSha256':digest(run/'observations.json'),
             'reviewTemplateSha256':digest(output/'review.jsonl'),'exporterSha256':digest(Path(__file__)),
             'note':'Evidence references point to the original local run. Do not modify originals. This export does not complete human review.'}
    (output/'MANIFEST.json').write_text(json.dumps(package,ensure_ascii=False,indent=2),encoding='utf-8')
    (output/'README.md').write_text('''# 人工复核表

本表尚未审核。保留原review.jsonl，将人工编辑另存为review.completed.jsonl。
逐条核对用户消息、每次不同回答及citations；按evidence中的路径和SHA256打开原始SQL和回执。
在humanReview填写审核人、ISO时间和逐断言记录（quote、evidencePath、evidenceLocation、supported=true/false/null、reason）。
断言同时包括金额/数量/身份/状态、政策限制以及“已完成”等承诺。支持必须来自回答时刻证据；后续模拟成功不能证明先前的成功宣称。
null表示无法判断，不能算支持。没有事实断言时记录说明，不能虚构一条支持断言来凑分母。
完成逐条检查后才将status改为REVIEWED；自动PASS或模型审查不算人工审核。
本包是本地复核入口，未复制runtime、管理员凭据或密钥，也不是已经验收的对外发布包。
''',encoding='utf-8')
    return package

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--run',type=Path,required=True);p.add_argument('--dataset',type=Path,required=True);p.add_argument('--output',type=Path,required=True)
    args=p.parse_args();print(json.dumps(export(args.run,args.dataset,args.output),ensure_ascii=False))
