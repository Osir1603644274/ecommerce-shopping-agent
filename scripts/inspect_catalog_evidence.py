"""Inspect repaired source metadata locally without LLM or retrieval inference."""
import argparse
import json
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'agent'))
from app.catalog_data import EvidenceStore,digest,plan_search

if __name__=='__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    p=argparse.ArgumentParser()
    p.add_argument('--metadata',type=Path,default=Path('D:/agent-datasets/integration-repair-20260913-v1/metadata'))
    p.add_argument('--docid',action='append',default=[])
    p.add_argument('--domain',choices=['used_phone','general','unknown'])
    a=p.parse_args()
    if a.domain:print(json.dumps(plan_search(a.domain,[]),ensure_ascii=False,indent=2))
    if a.docid:
        store=EvidenceStore(a.metadata,expected_manifest_sha256=digest(a.metadata/'MANIFEST.json'))
        try:
            for did in a.docid:
                record=store.record(did)
                print(json.dumps(store.describe_hit({'source':record['source'],'docid':did,'text':record['retrieval_text']}),ensure_ascii=False,indent=2))
        finally:store.close()
    if not a.docid and not a.domain:p.error('provide --docid or --domain')
