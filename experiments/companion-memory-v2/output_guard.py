"""Conservative offline output gate; not a factual relevance judge."""
import collections
import history as h

def gate(pred,quarantined):
    pid=pred.get('C');checks=pred.get('verification',{}).get('checks',[])
    if pid is None:return {'status':'abstained','approved_id':None}
    if pid in quarantined:return {'status':'source_conflict','approved_id':None,'reason':quarantined[pid]}
    if not checks or not pred.get('extraction',{}).get('preferences'):return {'status':'insufficient_checks','approved_id':None}
    states=[x.get('status') for x in checks]
    if 'contradicted' in states:return {'status':'model_reports_conflict','approved_id':None}
    if any(s!='supported' for s in states):return {'status':'insufficient_evidence','approved_id':None}
    # An omitted preference must not silently pass. Key aliases can cause conservative rejection.
    def norm(x):return ''.join(c for c in str(x).lower() if c.isalnum())
    wanted={(norm(x.get('attribute','')),norm(x.get('value',''))) for x in pred['extraction']['preferences']}
    checked={(norm(x.get('attribute','')),norm(x.get('value',''))) for x in checks}
    if not wanted<=checked:return {'status':'incomplete_preference_checks','approved_id':None}
    return {'status':'model_checks_pass_not_independent_verification','approved_id':pid}

def main():
    quarantined={'3896434581':'Title says Aluminum, structured material says Tabak Solid Wood. Actual material unresolved; do not choose a source by guessing.'}
    counts=collections.Counter();rows=[]
    for meta in h.read(h.OUT/'DATASET.json')['public_tasks']:
        pred=h.read(h.OUT/'semantic-pipeline'/f"{meta['id']}.prediction.json")
        result=gate(pred,quarantined);counts[result['status']]+=1
        rows.append({'id':meta['id'],'query':meta['query'],'original_selected_id':pred['C'],**result})
    assert next(r for r in rows if r['query']=='baby food')['approved_id'] is None
    assert next(r for r in rows if r['query']=='kitchen cabinet')['approved_id'] is None
    assert next(r for r in rows if r['query']=='ballpoint pen')['approved_id'] is None
    assert len(rows)==40
    h.write(h.OUT/'OUTPUT_GUARD.json',{'counts':dict(counts),'rows':rows,'quarantine':quarantined,'scope':'offline replay; preserves model output. Passing is not a gold success label. Model conflict can itself be wrong (e.g. absence of Cat is not proof of exclusion). No deployment.'})
    print(dict(counts),flush=True)

if __name__=='__main__':main()
