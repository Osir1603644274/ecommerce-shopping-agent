"""Audit saved live replies against their actual tool evidence; no model calls."""
import argparse
import hashlib
import json
import math
from pathlib import Path
import re
import statistics

ROOT=Path(__file__).resolve().parents[1]


def percentile(values, p):
    return sorted(values)[max(0,math.ceil(len(values)*p)-1)] if values else None


def audit(directory):
    directory=directory.resolve()
    manifest=json.loads((directory/'manifest.json').read_text(encoding='utf8'))
    data=[json.loads(s) for s in (directory/'turns.jsonl').read_text(encoding='utf8').splitlines()]
    supplement_path=directory/'model-receipts.json'
    supplement=json.loads(supplement_path.read_text(encoding='utf8')) if supplement_path.exists() else {}
    if supplement and supplement['sourceSha256']!=hashlib.sha256((directory/'turns.jsonl').read_bytes()).hexdigest():
        raise ValueError('receipt_supplement_source_mismatch')
    results=[]
    for row in data:
        response=row['response']; answer=response.get('answer','')
        comparisons=[t['detail'] for t in response.get('tool_trace',[]) if isinstance(t.get('detail'),dict)
                     and t['detail'].get('contractVersion')=='product-evidence-comparison-v1']
        violations=[];refs=re.findall(r'\[(pk:[a-z0-9]+)\]\(([^)]+)\)',answer)
        if comparisons:
            value=comparisons[-1]; knowledge=value['knowledge']
            by_id={e['evidenceId']:e for e in knowledge.get('evidence',[])}
            if knowledge.get('status') not in {'UNAVAILABLE','DISABLED'} and knowledge.get('knowledgeVersion')!=manifest['knowledgeVersion']:
                violations.append('wrong_knowledge_version')
            for eid,url in refs:
                if eid not in by_id or by_id[eid]['source']['url']!=url:violations.append('unknown_or_wrong_source_citation')
            candidates={str(c['productId']):c['decision'] for c in value['candidates']}
            bindings={b['itemId']:b for b in knowledge.get('bindings',[])}
            blocks=re.split(r'(?=^(?:条件内建议|事实备选，性能待核验|待核验备选（存在硬条件未知）)：)',answer,flags=re.M)
            for block in blocks:
                if not re.match(r'(?:条件内建议|事实备选，性能待核验|待核验备选（存在硬条件未知）)：',block):continue
                match=re.search(r'ID (\d+)）',block)
                if not match:violations.append('unparseable_recommendation');continue
                iid=match.group(1)
                if iid not in candidates:violations.append('outside_candidate_scope');continue
                checked=candidates[iid]['products']
                if not checked or checked[0].get('hardFailures'):violations.append('hard_failure_recommended')
                if checked and checked[0].get('hardUnknowns') and not block.startswith('待核验备选'):
                    violations.append('unknown_presented_as_satisfied')
                binding=bindings.get(iid,{})
                for eid in re.findall(r'\[(pk:[a-z0-9]+)\]',block):
                    if eid in by_id:
                        e=by_id[eid]
                        if binding.get('status')!='CLEAR' or e['modelKey'] not in binding.get('modelKeys',[]):
                            violations.append('citation_model_binding_mismatch')
                        region=binding.get('region','UNKNOWN'); source_region=e['source'].get('region','UNKNOWN')
                        if region!='UNKNOWN' and source_region not in {'UNKNOWN','TEST_SAMPLE',region}:
                            violations.append('citation_region_mismatch')
                ordinal=re.search(r'原卡片第(\d+)项',block)
                if ordinal:
                    cards=response.get('guideResult',{}).get('products',[])
                    n=int(ordinal.group(1))-1
                    if n>=len(cards) or str(cards[n]['product']['id'])!=iid:violations.append('wrong_display_ordinal')
        receipts=supplement.get('receipts',{}).get(row['arm']+':'+row['turnId'],row.get('modelCallReceipts',[]))
        call_count=sum(response.get('trace',{}).get('modelCallCounts',{}).values())
        if call_count!=len(receipts):violations.append('model_receipt_count_mismatch')
        summary=response.get('traceSummary',{})
        results.append(dict(arm=row['arm'],turnId=row['turnId'],query=row['request']['message'],seconds=row['seconds'],
            httpStatus=row['httpStatus'],agentStatus=summary.get('agentStatus'),failureCode=summary.get('failureCode') or (response.get('taskState') or {}).get('planningFailure'),
            comparison=bool(comparisons),sourceCitationCount=len(refs),violations=violations,
            conservativeFallback=answer.startswith(('现有证据不足以可靠','型号知识暂不可用')),
            observedModelCalls=len(receipts),inputTokens=sum(r['inputTokens'] or 0 for r in receipts),
            outputTokens=sum(r['outputTokens'] or 0 for r in receipts),
            missingUsage=sum(r.get('tokenStatus')!='OBSERVED' for r in receipts),
            answer=answer))
    arms={}
    for arm in manifest['arms']:
        rows=[r for r in results if r['arm']==arm]; times=[r['seconds'] for r in rows]
        arms[arm]=dict(turns=len(rows),http200=sum(r['httpStatus']==200 for r in rows),
            runtimeFailures=sum(bool(r['failureCode']) or r['agentStatus'] not in {'ok',None} for r in rows),
            comparisons=sum(r['comparison'] for r in rows),turnsWithSources=sum(r['sourceCitationCount']>0 for r in rows),
            conservativeFallbacks=sum(r['conservativeFallback'] for r in rows),
            structuralViolations=sum(len(r['violations']) for r in rows),
            modelCalls=sum(r['observedModelCalls'] for r in rows),inputTokens=sum(r['inputTokens'] for r in rows),
            outputTokens=sum(r['outputTokens'] for r in rows),missingUsage=sum(r['missingUsage'] for r in rows),
            latencyMedianSeconds=statistics.median(times),latencyP95Seconds=percentile(times,.95))
    changes=[p for p,digest in manifest['codeSha256'].items() if hashlib.sha256((ROOT/p).read_bytes()).hexdigest()!=digest]
    return dict(source=str(directory.relative_to(ROOT)),kind='DEVELOPMENT_STRUCTURAL_AUDIT_NOT_HUMAN_RELEVANCE_SCORE',
                sourceSha256=hashlib.sha256((directory/'turns.jsonl').read_bytes()).hexdigest(),
                codeChangesSinceReplay=changes,arms=arms,turns=results)


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('directory',type=Path);parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args(); result=audit(args.directory)
    with args.output.open('x',encoding='utf8') as f:json.dump(result,f,ensure_ascii=False,indent=2)
    print(json.dumps(result['arms'],ensure_ascii=False,indent=2))
