"""Preserve the completed regression before simultaneous-error diagnostics."""
import shutil
from .common import HERE, ROOT, file_sha, json_new, now
out=HERE/'p0/repair004_sources';out.mkdir(parents=True,exist_ok=False)
sources=list((ROOT/'agent/app').rglob('*.py'))+list(HERE.glob('*.py'))
sources += [HERE/'p0/contract.json', *list((ROOT/'agent/tests').glob('test_context_*repairs.py'))]
records=[]
for source in sources:
    rel=source.relative_to(ROOT);target=out/rel;target.parent.mkdir(parents=True,exist_ok=True)
    shutil.copyfile(source,target);records.append({'path':rel.as_posix(),'sha256':file_sha(source)})
json_new(out/'sources.json',{'at':now(),'sources':records,'secretsCopied':False})
json_new(HERE/'p8/full004/intentional_stop.json',{'at':now(),'ownedChildPid':16092,
    'reason':'P4 regression003 exposed simultaneous goal and pending-question errors; new bounded diagnostic repair requires a fresh full suite',
    'notAPass':True,'originalLogRetained':True})
print('Archived regression003 source and full004 stop reason')
