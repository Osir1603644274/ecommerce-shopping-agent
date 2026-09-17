import json
import pytest
import reviews
import review_workspace as rw
from test_review_workspace import prepare,author

def overwrite(path,value):
    path.write_text(json.dumps(value,ensure_ascii=False),encoding='utf-8')

def test_self_consistent_collected_forgery_rejected(tmp_path,monkeypatch):
    root=prepare(tmp_path,monkeypatch);author(root,'A')
    target=root/'reviews/A01'
    values=rw.rows(target/'judgments.jsonl');values[0]['reason']='自行改写但不来自实际作者'
    overwrite(target/'judgments.jsonl',values[0])
    record=rw.read_json(target/'COLLECTED.json');record['judgments_sha256']=rw.sha(target/'judgments.jsonl')
    overwrite(target/'COLLECTED.json',record)
    with pytest.raises(ValueError,match='actual author'):rw.read_review(root,'A01','train-01')

def test_rehashed_packet_rejected_by_original_anchor(tmp_path,monkeypatch):
    root=prepare(tmp_path,monkeypatch);author(root,'A')
    packet=root/'packets/train-01'
    with (packet/'pairs.jsonl').open('a') as f:f.write(' ')
    manifest=rw.read_json(packet/'INPUT_MANIFEST.json');manifest['files']['pairs.jsonl']=rw.sha(packet/'pairs.jsonl')
    overwrite(packet/'INPUT_MANIFEST.json',manifest)
    with pytest.raises(ValueError,match='Prebound'):rw.read_review(root,'A01','train-01')

@pytest.mark.parametrize('field',['source','document_id','catalog_text_sha256'])
def test_rehashed_private_mapping_rejected(tmp_path,monkeypatch,field):
    root=prepare(tmp_path,monkeypatch);author(root,'A');author(root,'B')
    mapping=root/'private/pair-mapping.jsonl';row=rw.rows(mapping)[0];row[field]='forged'
    overwrite(mapping,row)
    path=root/'packets/MANIFEST.json';manifest=rw.read_json(path);manifest['pair_mapping_sha256']=rw.sha(mapping)
    overwrite(path,manifest)
    with pytest.raises(ValueError,match='actual original candidates'):rw.freeze(root,label_version='fixture',status='FIXTURE')

def test_repair_requires_new_completed_turn_and_archived_bytes(tmp_path,monkeypatch):
    root=prepare(tmp_path,monkeypatch);author(root,'A')
    archive=root/'repairs/A01/original';source=root/'authors/A'
    for name in ('judgments.jsonl','receipt.json'):rw.exact_copy(source/name,archive/name)
    initial=root/'completion-events/A01.json'
    request={'author_thread_id':'A','initial_completion_sha256':rw.sha(initial),'errors':[{'pair_id':'fixture'}],
        'original_archive':str(archive),'original_judgments_sha256':rw.sha(archive/'judgments.jsonl'),
        'original_receipt_sha256':rw.sha(archive/'receipt.json')}
    rw.write_once(root/'repair-requests/A01.json',request)
    assert reviews.recompute_collected('A01',root)['status']=='WAITING_TOOL_CONFIRMED_COMPLETION'
    event=rw.read_json(initial);revised=root/'completion-events/A01-r1.json';overwrite(revised,event)
    with pytest.raises(ValueError,match='initial completion turn'):reviews.recompute_collected('A01',root)
    event['turn_id']='new-repair';overwrite(revised,event)
    assert 'binding' in reviews.recompute_collected('A01',root)
    (archive/'judgments.jsonl').write_text('changed',encoding='utf-8')
    with pytest.raises(ValueError,match='Archived original'):reviews.recompute_collected('A01',root)

def test_candidate_mutation_after_build_rejected(tmp_path,monkeypatch):
    root=prepare(tmp_path,monkeypatch);author(root,'A');author(root,'B')
    with (tmp_path/'candidates.jsonl').open('a') as f:f.write(' ')
    with pytest.raises(ValueError,match='Original pool input changed'):
        rw.freeze(root,label_version='fixture',status='FIXTURE')
