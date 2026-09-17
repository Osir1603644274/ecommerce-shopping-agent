"""Build a guarded file payload against captured F:/agent hashes; never modify F:/agent."""
import difflib, hashlib, json, pathlib, zipfile

ROOT=pathlib.Path(__file__).resolve().parents[2]
EVIDENCE=ROOT/'docs/messaging-migration/evidence'
def sha(p): return hashlib.sha256(p.read_bytes()).hexdigest().upper() if p.is_file() else None
baseline={r['path']:r for r in json.loads((EVIDENCE/'source-manifest.json').read_text(encoding='utf-8-sig'))}
paths=set(baseline)
for directory in ('backend/src','scripts/messaging-migration','docs/messaging-migration'):
    paths.update(p.relative_to(ROOT).as_posix() for p in (ROOT/directory).rglob('*') if p.is_file()
                 and '__pycache__' not in p.parts and 'evidence' not in p.parts)
paths.update(['backend/Dockerfile.messaging-lab','backend/Dockerfile.messaging-lab.dockerignore'])
changes=[]
for name in sorted(paths):
    expected=baseline.get(name,{}).get('sha256')
    current=sha(ROOT/name)
    if current==expected: continue
    changes.append({'path':name,'expected_source_sha256':expected,'result_sha256':current})
(EVIDENCE/'task-changes.json').write_text(json.dumps(changes,indent=2),encoding='utf-8')
diff=[]
for row in changes:
    source=pathlib.Path('F:/agent')/row['path']
    if sha(source)!=row['expected_source_sha256']: raise SystemExit('Source drift: '+row['path'])
    before=source.read_text(encoding='utf-8-sig').splitlines(keepends=True) if source.is_file() else []
    after=(ROOT/row['path']).read_text(encoding='utf-8-sig').splitlines(keepends=True) if row['result_sha256'] else []
    diff.extend(difflib.unified_diff(before,after,fromfile='a/'+row['path'],tofile='b/'+row['path']))
(EVIDENCE/'task.diff').write_text(''.join(diff),encoding='utf-8')
with zipfile.ZipFile(EVIDENCE/'task-changes.zip','w',zipfile.ZIP_DEFLATED) as z:
    z.write(EVIDENCE/'task-changes.json','task-changes.json')
    for row in changes:
        if row['result_sha256']: z.write(ROOT/row['path'],'files/'+row['path'])
print(json.dumps({'changed_paths':len(changes),'zip_sha256':sha(EVIDENCE/'task-changes.zip')}))
with zipfile.ZipFile(EVIDENCE/'evidence-bundle.zip','w',zipfile.ZIP_DEFLATED) as z:
    for p in EVIDENCE.rglob('*'):
        if p.is_file() and p.suffix!='.zip': z.write(p,p.relative_to(EVIDENCE))
print('Evidence bundle:',EVIDENCE/'evidence-bundle.zip')
