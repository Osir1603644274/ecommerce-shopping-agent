"""Collect the independent delta audit only after its actual native turn completes."""
from pathlib import Path
from retrieval_runtime import read_json,sha,write_once
from agent_quality_review import exact_copy
import reviews
import session_completion

ROOT=Path('D:/agent-datasets/search-closure-v1/delivery-audit/commerce-simulated-delta-v1')

def main():
    d=read_json(ROOT/'DISPATCH.json');metadata=reviews.first_model_metadata(d['threadId'])
    completion=session_completion.capture(metadata['session_path'],d['threadId'])
    out=Path(d['projectlessOutputDirectory']);hashes={}
    for name in ['delta-audit.md','delta-audit.json']:
        p=out/name;assert p.is_file()
        if name.endswith('.json'):read_json(p)
        exact_copy(p,ROOT/name);hashes[name]=sha(p)
    write_once(ROOT/'COMPLETION_EVENT.json',completion)
    write_once(ROOT/'COLLECTED.json',{'source_thread_id':d['threadId'],'fresh_context':True,
        'dispatch_sha256':sha(ROOT/'DISPATCH.json'),'model_metadata':metadata,'outputs':hashes,
        'completion_event_sha256':sha(ROOT/'COMPLETION_EVENT.json')})
    print({'status':'INDEPENDENT_COMMERCE_DELTA_AUDIT_COLLECTED','outputs':hashes})

if __name__=='__main__':main()
