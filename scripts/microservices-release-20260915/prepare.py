"""Read-only live baseline and code recovery snapshot; no service changes."""
import hashlib,json,subprocess,zipfile
from pathlib import Path
import httpx

ROOT=Path(__file__).resolve().parents[2]
OUT=Path('D:/agent-experiments/microservices-release-20260915-attempt001')

def main():
    assert not OUT.exists(),'existing attempt: inspect instead of overwriting'
    health=httpx.get('http://127.0.0.1:5173/',timeout=10)
    assert health.status_code==200
    release=json.loads((ROOT/'.runtime/merged-commerce/full-catalog-release.json').read_text())
    assert release['status']=='LIVE_VERIFIED'
    OUT.mkdir(parents=True)
    manifest={}
    files=[]
    for folder in ('agent/app','backend/src','backend-gateway/src','frontend/src','scripts'):
        files.extend(p for p in (ROOT/folder).rglob('*') if p.is_file() and '__pycache__' not in p.parts
                     and p.suffix in {'.java','.py','.ts','.tsx','.css','.yml','.yaml','.json','.xml','.sql','.ps1','.sh'})
    files += [ROOT/p for p in ('backend/pom.xml','backend-gateway/pom.xml','frontend/package.json','frontend/package-lock.json','frontend/vite.config.ts','docker-compose.yml','compose.merged-commerce.yml')]
    with zipfile.ZipFile(OUT/'pre-edit-code.zip','x',zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(set(files)):
            data=path.read_bytes();relative=path.relative_to(ROOT).as_posix()
            archive.writestr(relative,data);manifest[relative]=hashlib.sha256(data).hexdigest()
    names=subprocess.check_output(['docker','ps','--format','{{.Names}}'],text=True).splitlines()
    baseline=dict(status='BASELINE_SAVED',liveRelease=release,httpStatus=200,containers=names,codeSha256=manifest)
    (OUT/'BASELINE.json').write_text(json.dumps(baseline,ensure_ascii=False,indent=2)+'\n',encoding='utf8')
    print('Live baseline and pre-edit code snapshot saved:',len(manifest),'files')

if __name__=='__main__':main()
