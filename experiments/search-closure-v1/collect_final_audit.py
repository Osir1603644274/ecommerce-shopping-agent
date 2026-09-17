"""Preserve an independently authored final audit with actual native turn completion."""
from pathlib import Path
from retrieval_runtime import read_json, sha, write_once
from agent_quality_review import exact_copy
import reviews
import session_completion

ROOT=Path('D:/agent-datasets/search-closure-v1/delivery-audit/final-independent-native-v1')

def main():
    dispatch=read_json(ROOT/'DISPATCH.json')
    metadata=reviews.first_model_metadata(dispatch['threadId'])
    completion=session_completion.capture(metadata['session_path'],dispatch['threadId'])
    source=Path(dispatch['projectlessOutputDirectory'])
    outputs={}
    for name in ('final-audit.md','final-audit.json'):
        path=source/name
        if not path.is_file():raise ValueError(f'Independent author output missing: {path}')
        if name.endswith('.json'):read_json(path)
        exact_copy(path,ROOT/name);outputs[name]=sha(path)
    write_once(ROOT/'COMPLETION_EVENT.json',completion)
    write_once(ROOT/'COLLECTED.json',{'source_thread_id':dispatch['threadId'],'fresh_context':dispatch['fresh_context'],
        'dispatch_sha256':sha(ROOT/'DISPATCH.json'),'model_metadata':metadata,'outputs':outputs,
        'completion_event_sha256':sha(ROOT/'COMPLETION_EVENT.json'),
        'scope':'Author output collected; findings require root reconciliation. Not a goal completion declaration.'})
    print({'status':'INDEPENDENT_FINAL_AUDIT_COLLECTED','thread_id':dispatch['threadId'],'outputs':outputs})

if __name__=='__main__':main()
