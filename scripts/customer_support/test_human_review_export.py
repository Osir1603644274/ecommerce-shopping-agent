import hashlib,json
import pytest
from export_human_review import export

def test_partial_runs_cannot_be_presented_as_complete_review_packets(tmp_path):
    run=tmp_path/'run';run.mkdir();data=tmp_path/'dataset.jsonl';data.write_text('{}\n')
    (run/'RUN.json').write_text(json.dumps({'caseIds':['a','b'],'datasetSha256':hashlib.sha256(data.read_bytes()).hexdigest()}))
    (run/'observations.json').write_text(json.dumps([{'caseId':'a'}]))
    with pytest.raises(ValueError,match='all planned observations'):export(run,data,tmp_path/'out')
    assert not (tmp_path/'out').exists()

def test_export_never_converts_automated_pass_into_human_label(tmp_path):
    run=tmp_path/'run';run.mkdir();(run/'a').mkdir();data=tmp_path/'dataset.jsonl'
    data.write_text(json.dumps({'id':'a','split':'dev','steps':[{'actor':'customer','message':'query'}]})+'\n')
    (run/'RUN.json').write_text(json.dumps({'caseIds':['a'],'datasetSha256':hashlib.sha256(data.read_bytes()).hexdigest()}))
    (run/'observations.json').write_text(json.dumps([{'caseId':'a','repetition':1,'verdict':'PASS'}]))
    export(run,data,tmp_path/'out')
    row=json.loads((tmp_path/'out/review.jsonl').read_text(encoding='utf-8'))
    assert row['humanReview']=={'status':'UNREVIEWED','reviewer':None,'reviewedAt':None,'assertions':[]}
    assert row['answers']==[] and row['automatedVerdict']=='PASS'
