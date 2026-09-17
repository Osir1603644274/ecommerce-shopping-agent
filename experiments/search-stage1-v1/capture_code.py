"""Freeze task code bytes and the delta relative to preserved pre-task core."""
import difflib,json,shutil,subprocess
from pathlib import Path
from stage1 import dump,digest

ROOT=Path("D:/agent-datasets/search-stage1-v1")
REPO=Path("F:/agent")
CODE=Path(__file__).resolve().parent

def capture():
    destination=ROOT/"provenance/code-snapshot"
    records=[]
    files=[p for p in CODE.iterdir() if p.is_file() and p.suffix in (".py",".ps1",".md")]
    files += [p for p in (REPO/"retrieval_judgment_pool_core").rglob("*") if p.is_file() and p.suffix in (".py",".json") and "__pycache__" not in p.parts]
    for source in sorted(files):
        target=destination/source.relative_to(REPO)
        target.parent.mkdir(parents=True,exist_ok=True)
        shutil.copyfile(source,target)
        records.append({"source":str(source),"snapshot":str(target),"sha256":digest(source),"bytes":source.stat().st_size})
    for old,new,name in [(ROOT/"provenance/pool-core-before.py",REPO/"retrieval_judgment_pool_core/core.py","pool-core-task.patch"),
                         (ROOT/"provenance/pool-config-schema-before.json",REPO/"retrieval_judgment_pool_core/schemas/config.schema.json","pool-schema-task.patch")]:
        if new.exists():
            patch="".join(difflib.unified_diff(old.read_text(encoding="utf-8-sig").splitlines(True),new.read_text(encoding="utf-8-sig").splitlines(True),fromfile="before/"+new.name,tofile="after/"+new.name))
            (ROOT/"provenance"/name).write_text(patch,encoding="utf-8")
    head=subprocess.check_output(["git","rev-parse","HEAD"],cwd=REPO,text=True).strip()
    if head!=(ROOT/"provenance/git-head-before.txt").read_text(encoding="utf-8-sig").strip():raise ValueError("Repository HEAD changed")
    dump(ROOT/"provenance/code-manifest.json",{"git_head":head,"scope":"New experiment files and shared pool snapshot; core patch compares preserved pre-task bytes, not all existing dirty changes", "files":records})
    print(f"Captured {len(records)} code files")

if __name__=="__main__":capture()
