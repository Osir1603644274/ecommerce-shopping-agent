import json
import pytest
from export_human_review import digest
from import_human_review import import_review


@pytest.fixture
def packet(tmp_path):
    source=tmp_path/'source';source.mkdir()
    (source/'fact.json').write_text('{"status":"pending"}')
    (source/'observations.json').write_text(json.dumps([{'caseId':'c','repetition':1,'humanReview':{'status':'UNREVIEWED'}}]))
    packet=tmp_path/'packet';packet.mkdir()
    row={'caseId':'c','repetition':1,'answers':[{'answer':'退款仍在处理中。'}],
         'evidence':[{'path':'fact.json','sha256':digest(source/'fact.json')}],
         'humanReview':{'status':'UNREVIEWED'}}
    (packet/'review.jsonl').write_text(json.dumps(row),encoding='utf-8')
    manifest={'sourceRun':str(source),'reviewTemplateSha256':digest(packet/'review.jsonl'),
              'observationsSha256':digest(source/'observations.json')}
    (packet/'MANIFEST.json').write_text(json.dumps(manifest))
    row['humanReview']={'status':'REVIEWED','reviewer':'synthetic unit-test identity',
        'reviewedAt':'2026-09-19T12:00:00+08:00','assertions':[
            {'quote':'退款仍在处理中','supported':None,'evidencePath':'fact.json','evidenceLocation':'status',
             'reason':'Synthetic unit test only; no real human review.'}]}
    completed=tmp_path/'completed.jsonl'
    completed.write_text(json.dumps(row),encoding='utf-8')
    return packet,completed,tmp_path/'output',row,source


def test_unknown_not_counted_supported_and_original_unchanged(packet):
    folder,completed,output,row,source=packet
    before=digest(source/'observations.json')
    import_review(folder,completed,output)
    review=json.loads((output/'observations.json').read_text())[0]['humanReview']
    assert (review['assertions'],review['supported'],review['unknown'])==(1,0,1)
    assert digest(source/'observations.json')==before


def test_agent_review_preserves_unreviewed_human_and_unknown_critical_fact(packet):
    folder,completed,output,row,source=packet
    row['agentReview']=row.pop('humanReview')
    row['agentReview']['reviewerType']='AGENT'
    row['agentReview']['assertions'][0]['critical']=True
    row['humanReview']={'status':'UNREVIEWED'}
    completed.write_text(json.dumps(row),encoding='utf-8')
    import_review(folder,completed,output,reviewer_type='AGENT')
    observed=json.loads((output/'observations.json').read_text())[0]
    assert observed['humanReview']=={'status':'UNREVIEWED'}
    assert observed['independentFactReview']['criticalTotal']==1
    assert observed['independentFactReview']['criticalSupported']==0
    assert observed['independentFactReview']['sourceReviewSha256']==digest(completed)


def test_agent_import_cannot_change_human_label(packet):
    folder,completed,output,row,source=packet
    row['agentReview']={**row['humanReview'],'reviewerType':'AGENT'}
    row['agentReview']['assertions'][0]['critical']=True
    completed.write_text(json.dumps(row),encoding='utf-8')
    with pytest.raises(ValueError,match='answer or evidence'):
        import_review(folder,completed,output,reviewer_type='AGENT')


@pytest.mark.parametrize('mutation',['answer','evidence','quote','support'])
def test_tampering_rejected_without_output(packet,mutation):
    folder,completed,output,row,source=packet
    if mutation=='answer':row['answers'][0]['answer']='已到账'
    elif mutation=='evidence':(source/'fact.json').write_text('{}')
    elif mutation=='quote':row['humanReview']['assertions'][0]['quote']='已到账'
    else:row['humanReview']['assertions'][0]['supported']=1
    completed.write_text(json.dumps(row),encoding='utf-8')
    with pytest.raises(ValueError):import_review(folder,completed,output)
    assert not output.exists()
