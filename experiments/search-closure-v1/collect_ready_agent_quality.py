"""Collect completed original authors only; never write semantic answer judgments."""
import json
import argparse
import reviews
import session_completion
import agent_quality_review as quality

def main():
    global quality
    p=argparse.ArgumentParser();p.add_argument('--version',choices=['v1','v2'],default='v1');args=p.parse_args()
    if args.version=='v2':
        import agent_quality_review_v2 as quality
    root=quality.ROOT
    for path in sorted((root/'dispatch').glob('*.json')):
        d=quality.read_json(path);rid=d['review_id']
        if (root/'reviews'/rid/'COLLECTED.json').exists():continue
        try:
            event=root/'completion-events'/(rid+'.json')
            if not event.exists():
                metadata=reviews.first_model_metadata(d['threadId'])
                completed=session_completion.capture(metadata['session_path'],d['threadId'])
                quality.write_once(event,completed)
            _,receipt=quality.collect(rid)
            print(json.dumps({'id':rid,'collected':True,'answers':receipt['answers']}))
        except Exception as exc:
            print(json.dumps({'id':rid,'pending':str(exc)}))

if __name__=='__main__':main()
