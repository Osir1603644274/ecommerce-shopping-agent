"""Correct the probe's status lookup offline; preserve original failed scores."""
import json
from openai.types.chat import ChatCompletion
from agent.app import llm
from agent.app.task_state import TaskState
from .common import HERE, file_sha, json_new, rows, now

out=HERE/'p2/jointrepair001';source=HERE/'p4/regression003'
with (source/'private_states.jsonl').open(encoding='utf-8') as stream:
    state_row=next(json.loads(line) for line in stream if json.loads(line)['executionOrdinal']==27)
state=TaskState.model_validate(state_row['before']);before=state.model_dump_json()
request=next(r for r in rows(source/'private_requests.jsonl') if r['requestId']=='ctxv3-call-00965')
message=next(m['content'] for m in request['request']['messages'] if m['role']=='user')
checks=[]
for record in rows(out/'private_responses.jsonl'):
    response=ChatCompletion.model_validate(record['response'])
    parsed=llm._parse_task_state_arguments(response.choices[0].message.tool_calls[0],response=response,strict=True)
    payload,_=llm._build_validated_task_state_payload(state,parsed,message=message,require_status=True,allow_auto_ready=False)
    effective_status=payload.get('status',state.status)
    checks.append({'requestId':record['requestId'],
        'statusUnchangedAndLegal':effective_status=='collecting_information',
        'unresolvedBlockerPreserved':llm._effective_unknowns(state,payload)==state.unknowns,
        'pendingQuestionPreserved':payload.get('pendingQuestions',state.pending_questions)==state.pending_questions,
        'noGoalPlaceholder':payload.get('goal',state.goal)==state.goal,
        'noStateMutation':state.model_dump_json()==before})
result={'at':now(),'passed':len(checks)==4 and all(all(v for k,v in r.items() if k!='requestId') for r in checks),
    'checks':checks,'sourceResponsesSha256':file_sha(out/'private_responses.jsonl'),
    'originalScoresSha256':file_sha(out/'results.jsonl'),'correction':'pure builder omits unchanged status; effective status is payload.get(status,state.status)',
    'additionalModelCalls':0,'persistCalls':0,'oldScoresPreserved':True}
json_new(out/'verification.json',result);print(result)
