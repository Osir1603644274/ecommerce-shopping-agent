"""Compact operational collection; never authors relevance judgments."""
import argparse
import json
from pathlib import Path
import reviews
import session_completion

def main():
    p=argparse.ArgumentParser()
    p.add_argument('--workspace',type=Path,required=True)
    args=p.parse_args()
    root=args.workspace.resolve()
    if not root.is_relative_to(Path('D:/agent-datasets/search-closure-v1').resolve()):
        raise ValueError('Workspace outside current experiment')
    reviews.DEV=root
    for path in sorted((root/'dispatch').glob('*.json')):
        d=json.loads(path.read_text(encoding='utf-8'));rid=d['review_id']
        if (root/'reviews'/rid/'COLLECTED.json').exists():continue
        try:
            event=root/'completion-events'/(rid+'.json')
            if not event.exists():
                m=reviews.first_model_metadata(d['threadId'])
                e=session_completion.capture(m['session_path'],d['threadId'])
                reviews.write_once(event,e)
            b=reviews.collect(rid)
            print(json.dumps({'id':rid,'collected':b.get('validation')},ensure_ascii=False))
        except Exception as e:
            print(json.dumps({'id':rid,'pending':str(e)},ensure_ascii=False))

if __name__=='__main__':main()
