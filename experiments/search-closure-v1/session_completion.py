"""Read-only fallback when app task snapshots omit real resumed turns."""
import hashlib
import json
from pathlib import Path

def capture(path,thread_id,*,turn_id=None):
    path=Path(path).resolve();events=[];meta=None;starts={}
    with path.open('rb') as stream:
        for number,raw in enumerate(stream,1):
            row=json.loads(raw);payload=row.get('payload',{})
            anchor={'line':number,'sha256':hashlib.sha256(raw).hexdigest()}
            if row.get('type')=='session_meta':
                if meta is not None or payload.get('id')!=thread_id:raise ValueError('Completion session identity differs')
                meta=anchor
            if row.get('type')=='event_msg' and payload.get('type') in ('task_started','task_complete','turn_aborted'):
                events.append((row,anchor))
                if payload['type']=='task_started':starts[payload['turn_id']]=anchor
    if meta is None:raise ValueError('Missing actual session metadata')
    if turn_id is None:
        if not events or events[-1][0]['payload']['type']!='task_complete':raise ValueError('Actual latest session turn is not complete')
        row,anchor=events[-1]
    else:
        matched=[e for e in events if e[0]['payload'].get('turn_id')==turn_id and e[0]['payload']['type']=='task_complete']
        if len(matched)!=1:raise ValueError('Bound task completion absent or duplicate')
        row,anchor=matched[0]
    turn=row['payload']['turn_id']
    if turn not in starts or starts[turn]['line']>=anchor['line']:raise ValueError('Completion has no preceding start')
    return {'thread_id':thread_id,'turn_id':turn,'status':'completed','completed_at':row['timestamp'],
        'tool':'exec_command.read_codex_session','session_path':str(path),
        'session_meta':meta,'task_started':starts[turn],'task_complete':anchor,
        'final_message':row['payload'].get('last_agent_message'),
        'scope':'Actual local session events; app wait snapshot was stale. No log edits or inferred completion.'}

def verify(event,actual_session_path,*,require_latest=True):
    if Path(event['session_path']).resolve()!=Path(actual_session_path).resolve():raise ValueError('Completion uses another author session')
    actual=capture(actual_session_path,event['thread_id'],turn_id=None if require_latest else event['turn_id'])
    if actual!=event:raise ValueError('Actual session completion binding changed')
