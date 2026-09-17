"""Validate and collect finished independent judgments; never infer semantic grades."""
from __future__ import annotations
import argparse
import hashlib
import json
import re
import shutil
from collections import Counter
from pathlib import Path
from bootstrap import DEV, sha, write_once
from prepare_development import jsonl_once

STATES={'supported','contradicted','missing','conflicting'}
RELATIONS={'same','accessory','related_substitute','unrelated','uncertain'}
FIELDS={'query','title','brand','seller_name','categories'}
REASONS={'query_intent_ambiguous','core_uncertain','conflicting_evidence','insufficient_core_evidence'}

def load(path): return json.loads(Path(path).read_text(encoding='utf-8-sig'))
def rows(path):
    with Path(path).open(encoding='utf-8-sig') as f:
        return [json.loads(x) for x in f if x.strip()]

def validate_judgments(entries,pairs,contracts):
    expected={p['pair_id']:p for p in pairs}
    if len(expected)!=len(pairs): raise ValueError('Duplicate input pair identity')
    by_query={q['query_id']:q for q in contracts}
    if len(by_query)!=len(contracts): raise ValueError('Duplicate input query contract')
    if {p['query_id'] for p in pairs}!=set(by_query): raise ValueError('Input query contract coverage differs')
    seen=set()
    for row in entries:
        pid=row.get('pair_id')
        if pid not in expected or pid in seen: raise ValueError(f'Duplicate/unexpected pair {pid}')
        seen.add(pid); pair=expected[pid]; contract=by_query[pair['query_id']]
        grade=row.get('grade')
        if not (type(grade) is int and 0<=grade<=3 or grade=='UNKNOWN'):
            raise ValueError(f'{pid}: invalid grade {grade!r}')
        if row.get('core_relation') not in RELATIONS: raise ValueError(f'{pid}: missing/invalid core relation')
        attrs=row.get('attributes')
        if not isinstance(attrs,dict) or set(attrs)!=set(contract['required_attribute_keys']):
            raise ValueError(f'{pid}: attribute contract mismatch')
        if any(v not in STATES for v in attrs.values()): raise ValueError(f'{pid}: invalid evidence state')
        if not isinstance(row.get('reason'),str) or len(row['reason'].strip())<4: raise ValueError(f'{pid}: no actual reason')
        evidence=row.get('evidence')
        if not isinstance(evidence,list) or not evidence: raise ValueError(f'{pid}: no traceable evidence')
        for item in evidence:
            if not isinstance(item,dict) or item.get('field') not in FIELDS:
                raise ValueError(f'{pid}: invalid evidence field')
            field=item['field']; quote=item.get('quote')
            value=pair['query'] if field=='query' else pair['document'].get(field,'')
            values=value if isinstance(value,list) else [value]
            if not isinstance(quote,str) or not quote or not any(isinstance(v,str) and quote in v for v in values):
                raise ValueError(f'{pid}: evidence is not an exact source substring: {item!r}')
        if grade=='UNKNOWN':
            if row.get('unknown_reason') not in REASONS: raise ValueError(f'{pid}: missing UNKNOWN cause')
        elif row.get('unknown_reason') is not None:
            raise ValueError(f'{pid}: numeric grade has UNKNOWN cause')
        if contract['intent_policy']=='query_intent_ambiguous' and not (
            grade=='UNKNOWN' and row['unknown_reason']=='query_intent_ambiguous'):
            raise ValueError(f'{pid}: violates frozen unresolved-query policy')
        if grade==3 and (row['core_relation']!='same' or set(attrs.values())!={'supported'}):
            raise ValueError(f'{pid}: grade3 contradicts declared evidence states')
        if grade==2 and (row['core_relation']!='same' or attrs['本体']!='supported'
                         or 'missing' not in attrs.values() or any(v in {'contradicted','conflicting'} for v in attrs.values())):
            raise ValueError(f'{pid}: grade2 contradicts partial-without-violation rule')
    if seen!=set(expected): raise ValueError(f'Missing {len(set(expected)-seen)} judgments')
    return {'pairs':len(entries),'grade_counts':dict(Counter(str(r['grade']) for r in entries)),
            'status':'STRUCTURE_AND_EXACT_EVIDENCE_PASS_NOT_ACCURACY'}

def first_model_metadata(thread_id):
    # Read only this known task's first turn metadata, not history/content from other tasks.
    session_root=Path('C:/Users/ming/.codex/sessions')
    matches=list(session_root.glob(f'2026/09/*/*{thread_id}.jsonl'))
    if len(matches)!=1: raise ValueError(f'Model metadata session not uniquely found for {thread_id}')
    with matches[0].open(encoding='utf-8') as f:
        for raw in f:
            record=json.loads(raw)
            if record.get('type')=='turn_context':
                payload=record['payload']
                return {'source_thread_id':thread_id,'model':payload.get('model'),
                        'reasoning_effort':payload.get('effort'), 'session_path':str(matches[0]),
                        'turn_context_line_sha256':hashlib.sha256(raw.rstrip('\r\n').encode()).hexdigest()}
    raise ValueError('No task turn metadata')

def find_output(directory,name):
    directory=Path(directory)
    candidates=[directory/name,directory.parent/name]
    existing=[p for p in candidates if p.is_file()]
    if not existing or len({sha(p) for p in existing}) != 1:
        raise ValueError(f'Expected one unambiguous {name}: {existing}')
    # Some independent tasks mirror their deliverable into outputs/. Accept
    # byte-identical copies only; conflicting versions still fail closed.
    return existing[0]

def receipt_hashes(value):
    if isinstance(value,dict):
        return set().union(*(receipt_hashes(v) for v in value.values())) if value else set()
    if isinstance(value,list):
        return set().union(*(receipt_hashes(v) for v in value)) if value else set()
    if isinstance(value,str) and re.fullmatch(r'[0-9a-fA-F]{64}',value): return {value.lower()}
    return set()

def validate_packet(workspace,packet_id):
    """Check the originally published packet receipt, then its actual bytes."""
    root=Path(workspace); packet=root/'packets'/packet_id
    if not re.fullmatch(r'(?:third-)?[a-zA-Z0-9_-]+-\d+',packet_id):
        raise ValueError('Invalid packet identity')
    top=load(root/'packets/MANIFEST.json'); items=top['packets']
    if len({p['packet_id'] for p in items})!=len(items) or sum(p['pairs'] for p in items)!=top['pairs']:
        raise ValueError('Duplicate/inconsistent initial packet manifest')
    if packet_id.startswith('third-'):
        parent=packet_id.removeprefix('third-')
        request=load(root/'adjudication/requests'/f'{parent}.json')
        if request.get('packet_id')!=parent: raise ValueError('Third request identity differs')
        reference=request.get('third_packet',{})
        expected=reference.get('input_manifest_sha256',request.get('third_input_manifest_sha256'))
        expected_id=reference.get('packet_id',request.get('third_packet_id'))
        count=request['disagreement_pairs']
        if expected_id!=packet_id: raise ValueError('Third packet not prebound by request')
    else:
        matches=[p for p in items if p['packet_id']==packet_id]
        if len(matches)!=1: raise ValueError('Packet is not in original manifest')
        reference=matches[0]; expected=reference['input_manifest_sha256'];count=reference['pairs']
    if sha(packet/'INPUT_MANIFEST.json')!=expected: raise ValueError('Prebound packet manifest changed')
    manifest=load(packet/'INPUT_MANIFEST.json')
    if (manifest.get('packet_id')!=packet_id or manifest.get('pairs')!=count
        or manifest.get('human_gold') is not False or manifest.get('no_old_labels_or_rankings') is not True
        or set(manifest['files'])!={'pairs.jsonl','query-contracts.jsonl','RUBRIC.md'}):
        raise ValueError('Packet manifest identity/schema differs')
    for name,wanted in manifest['files'].items():
        if sha(packet/name)!=wanted: raise ValueError('Actual review input changed')
    pairs=rows(packet/'pairs.jsonl');contracts=rows(packet/'query-contracts.jsonl')
    byquery={q['query_id']:q for q in contracts}
    if (len(pairs)!=count or len({p['pair_id'] for p in pairs})!=len(pairs)
        or len(byquery)!=len(contracts) or len(contracts)!=manifest['queries']
        or {p['query_id'] for p in pairs}!=set(byquery)
        or any(p['query']!=byquery[p['query_id']]['query'] for p in pairs)):
        raise ValueError('Packet pair/query identity or coverage differs')
    if packet_id.startswith('third-') and {p['pair_id'] for p in pairs}!=set(request['disagreement_pair_ids']):
        raise ValueError('Third packet coverage differs from request')
    return packet,manifest,pairs,contracts

def completion_binding(root,review_id,dispatch):
    initial=root/'completion-events'/f'{review_id}.json'
    if not initial.exists(): return None
    repair=root/'repair-requests'/f'{review_id}.json'
    def event_at(path,require_latest=True):
        event=load(path)
        if (event.get('thread_id')!=dispatch['threadId'] or event.get('status')!='completed'
            or not event.get('turn_id') or event.get('tool') not in ('mcp__codex_app__wait_threads','exec_command.read_codex_session')):
            raise ValueError('Completion event identity/status/tool mismatch')
        if event['tool']=='exec_command.read_codex_session':
            from session_completion import verify
            verify(event,first_model_metadata(dispatch['threadId'])['session_path'],require_latest=require_latest)
        return event
    initial_event=event_at(initial,require_latest=not repair.exists())
    if not repair.exists(): return initial,{}
    request=load(repair)
    if (request.get('author_thread_id')!=dispatch['threadId']
        or request.get('initial_completion_sha256')!=sha(initial)
        or not request.get('errors')
        or any(not re.fullmatch(r'[0-9a-f]{64}',str(request.get(k,'')))
               for k in ('original_judgments_sha256','original_receipt_sha256'))):
        raise ValueError('Repair request original author/input binding differs')
    revised=root/'completion-events'/f'{review_id}-r1.json'
    # An initial completion never authorizes an output under active revision.
    if not revised.exists(): return None
    event=event_at(revised)
    if event['turn_id']==initial_event['turn_id']: raise ValueError('Repair reuses initial completion turn')
    for key in ('original_judgments','original_receipt'):
        suffix='judgments.jsonl' if key=='original_judgments' else 'receipt.json'
        archived=request.get(key+'_path') or str(Path(request['original_archive'])/suffix)
        if sha(archived)!=request[key+'_sha256']:
            raise ValueError('Archived original repair artifact changed')
    return revised,{'initial_completion_event_sha256':sha(initial),'repair_request_sha256':sha(repair)}

def recompute_collected(review_id,workspace=None):
    """Read-only reconstruction from actual author inputs, outputs and tool event."""
    root=Path(workspace) if workspace is not None else DEV
    if not re.fullmatch(r'[ABT]\d+',review_id): raise ValueError('Invalid review slot')
    dispatch=load(root/'dispatch'/f'{review_id}.json')
    if (dispatch.get('review_id')!=review_id or dispatch.get('role')!=review_id[0]
        or dispatch.get('fresh_context') is not True or not dispatch.get('threadId')
        or dispatch.get('status')!='DISPATCHED'
        or dispatch.get('packet_id','').rsplit('-',1)[-1]!=review_id[1:]
        or dispatch['packet_id'].startswith('third-')!=(review_id[0]=='T')):
        raise ValueError('Dispatch role/packet/context identity mismatch')
    completed=completion_binding(root,review_id,dispatch)
    if completed is None: return {'review_id':review_id,'status':'WAITING_TOOL_CONFIRMED_COMPLETION'}
    complete,repair_binding=completed
    packet,manifest,pairs,contracts=validate_packet(root,dispatch['packet_id'])
    if dispatch.get('expected_pairs')!=len(pairs): raise ValueError('Dispatch pair count differs')
    judgment=find_output(dispatch['projectlessOutputDirectory'],'judgments.jsonl')
    receipt=find_output(dispatch['projectlessOutputDirectory'],'receipt.json')
    local_dir=judgment.parent
    local_inputs=local_dir/'input'
    if not local_inputs.is_dir(): local_inputs=Path(dispatch['projectlessOutputDirectory']).parent/'input'
    for name,expected in manifest['files'].items():
        if sha(packet/name)!=expected or sha(local_inputs/name)!=expected: raise ValueError('Input bytes drifted')
    if sha(local_inputs/'INPUT_MANIFEST.json')!=sha(packet/'INPUT_MANIFEST.json'):
        raise ValueError('Input manifest drifted')
    authored=load(receipt)
    if authored.get('human_gold') is not False or authored.get('self_review_completed') is not True:
        raise ValueError('Missing author completion/silver disclosure')
    if authored.get('label_source')!='independent_context_model_silver_v6':
        raise ValueError('Author label provenance does not match this rubric version')
    required_hashes={*manifest['files'].values(),sha(packet/'INPUT_MANIFEST.json'),sha(judgment)}
    if not required_hashes <= receipt_hashes(authored):
        raise ValueError('Author receipt omits or misstates an input/output hash')
    values=rows(judgment)
    validation=validate_judgments(values,pairs,contracts)
    model=first_model_metadata(dispatch['threadId'])
    if not model['model'] or model.get('source_thread_id')!=dispatch['threadId']: raise ValueError('Unknown/mismatched author model')
    binding={'review_id':review_id,'packet_id':dispatch['packet_id'],'role':dispatch['role'],
             'context_isolation':'no_prior_conversation','human_gold':False,
             'model_metadata':model,'judgments_sha256':sha(judgment),'receipt_sha256':sha(receipt),
             'input_manifest_sha256':sha(packet/'INPUT_MANIFEST.json'),'validation':validation,
             'source_directory':str(local_dir),'completion_event_sha256':sha(complete)}
    binding.update(repair_binding)
    return {'binding':binding,'judgments':judgment,'receipt':receipt,'values':values}

def verify_collected(review_id,packet_id,workspace=None):
    root=Path(workspace) if workspace is not None else DEV
    actual=recompute_collected(review_id,root)
    if 'binding' not in actual: raise ValueError('Review is waiting for tool-confirmed completion')
    binding=actual['binding'];destination=root/'reviews'/review_id
    if binding['packet_id']!=packet_id or load(destination/'COLLECTED.json')!=binding:
        raise ValueError('COLLECTED does not match actual author/dispatch/completion/input chain')
    for name in ('judgments','receipt'):
        suffix='.jsonl' if name=='judgments' else '.json'
        if sha(destination/(name+suffix))!=sha(actual[name]): raise ValueError('Collected source output changed')
    return {r['pair_id']:r for r in actual['values']},binding

def collect(review_id):
    actual=recompute_collected(review_id)
    if 'binding' not in actual: return actual
    binding=actual['binding'];destination=DEV/'reviews'/review_id
    destination.mkdir(parents=True,exist_ok=True)
    for name in ('judgments','receipt'):
        source=actual[name];target=destination/(name+('.jsonl' if name=='judgments' else '.json'))
        if target.exists():
            if sha(target)!=sha(source): raise ValueError(f'Completed review changed: {review_id}')
        else: shutil.copyfile(source,target)
    write_once(destination/'COLLECTED.json',binding)
    return binding

def main():
    global DEV
    p=argparse.ArgumentParser();p.add_argument('command',choices=['collect','validate']);p.add_argument('--review-id');p.add_argument('--file');p.add_argument('--packet');p.add_argument('--workspace',type=Path)
    a=p.parse_args()
    if a.workspace is not None:
        value=a.workspace.resolve()
        allowed=Path('D:/agent-datasets/search-closure-v1').resolve()
        if not value.is_relative_to(allowed): raise ValueError('Additional review workspace must be in the new experiment')
        DEV=value
    if a.command=='collect':
        ids=[a.review_id] if a.review_id else [p.stem for p in sorted((DEV/'dispatch').glob('*.json'))]
        for review_id in ids:
            print(json.dumps(collect(review_id),ensure_ascii=False))
    else:
        packet=DEV/'packets'/a.packet
        print(json.dumps(validate_judgments(rows(a.file),rows(packet/'pairs.jsonl'),rows(packet/'query-contracts.jsonl')),ensure_ascii=False))

if __name__=='__main__':main()
