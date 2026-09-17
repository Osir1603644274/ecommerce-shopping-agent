import json
import pytest
from session_completion import capture,verify

def append(path,kind,payload):
    with path.open('a',encoding='utf-8') as f:f.write(json.dumps({'timestamp':'fixed','type':kind,'payload':payload})+'\n')

def sample(path):
    append(path,'session_meta',{'id':'author'})
    append(path,'event_msg',{'type':'task_started','turn_id':'original'})
    append(path,'event_msg',{'type':'turn_aborted','turn_id':'original'})
    append(path,'event_msg',{'type':'task_started','turn_id':'resume'})
    append(path,'event_msg',{'type':'task_complete','turn_id':'resume','last_agent_message':'done'})

def test_actual_resumed_completion_and_byte_tampering(tmp_path):
    path=tmp_path/'session.jsonl';sample(path)
    event=capture(path,'author');assert event['turn_id']=='resume'
    verify(event,path)
    path.write_text(path.read_text().replace('done','changed'))
    with pytest.raises(ValueError,match='binding changed'):verify(event,path)

def test_new_running_turn_prevents_collect_but_old_event_supports_repair(tmp_path):
    path=tmp_path/'session.jsonl';sample(path);event=capture(path,'author')
    append(path,'event_msg',{'type':'task_started','turn_id':'repair'})
    with pytest.raises(ValueError,match='not complete'):verify(event,path)
    verify(event,path,require_latest=False)

def test_wrong_author_and_aborted_not_accepted(tmp_path):
    path=tmp_path/'session.jsonl';sample(path)
    with pytest.raises(ValueError,match='identity'):capture(path,'other')
    append(path,'event_msg',{'type':'turn_aborted','turn_id':'later'})
    with pytest.raises(ValueError,match='not complete'):capture(path,'author')
