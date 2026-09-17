"""Bind tested runtime source and built Java image; never include local credentials."""
import hashlib,json,sys
from prepare import ROOT,OUT,cmd,write

manifest=OUT/'RUNTIME-CODE-MANIFEST.json'
files=[]
extensions={'.java','.py','.ts','.tsx','.css','.json','.yml','.yaml','.xml','.html','.txt','.md','.sql'}
for directory in ('backend/src/main','agent/app','frontend/src'):
    files += [p for p in (ROOT/directory).rglob('*') if p.is_file() and p.suffix in extensions and '__pycache__' not in p.parts]
files += [ROOT/p for p in ('backend/pom.xml','frontend/package.json','frontend/package-lock.json','frontend/vite.config.ts')]
hashes={p.relative_to(ROOT).as_posix():hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(files)}
image=json.loads(cmd('image','inspect','agent-backend:commerce-full-20260915-v3'))[0]['Id']
current=dict(files=hashes,javaImageId=image)
if sys.argv[1:]==['create']:
    assert not manifest.exists(),'existing accepted source manifest must not be overwritten'
    write(manifest.name,current);print('Runtime code/image manifest created:',len(files),'files')
elif sys.argv[1:]==['verify']:
    expected=json.loads(manifest.read_text())
    assert current==expected,'runtime source or Java image drifted after acceptance; rerun affected validation'
    print('Runtime code/image manifest matches:',len(files),'files')
else:raise ValueError('explicit create or verify required')
