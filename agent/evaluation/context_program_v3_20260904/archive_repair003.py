"""Archive completed P4 revision before development-boundary repairs."""
import shutil
from .common import HERE, ROOT, file_sha, json_new, now
out=HERE/'p0/repair003_sources';out.mkdir(parents=True,exist_ok=False)
sources=list((ROOT/'agent/app').rglob('*.py'))+list(HERE.glob('*.py'))
sources += [HERE/'p0/contract.json',ROOT/'agent/tests/test_context_p4_repairs.py',ROOT/'agent/tests/test_run_agent.py']
records=[]
for source in sources:
    rel=source.relative_to(ROOT);target=out/rel;target.parent.mkdir(parents=True,exist_ok=True)
    shutil.copyfile(source,target);records.append({'path':rel.as_posix(),'sha256':file_sha(source)})
json_new(out/'sources.json',{'at':now(),'sources':records,'secretsCopied':False})
json_new(HERE/'p8/full003/intentional_stop.json',{'at':now(),'ownedChildPid':20724,
    'reason':'P4 dev001 found new product defects; superseded source will receive a fresh full-suite run after repair',
    'notAPass':True,'originalLogRetained':True})
print('Source archived; old suite stop reason recorded')
