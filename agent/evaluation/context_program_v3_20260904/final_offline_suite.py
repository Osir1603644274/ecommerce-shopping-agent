"""Freeze application AND tests for the final full-suite execution."""
from .common import HERE, ROOT, file_sha, json_new, now
from .offline_suite import run

if __name__=='__main__':
    sources=sorted((ROOT/'agent/app').rglob('*.py'))+sorted((ROOT/'agent/tests').rglob('*.py'))
    sources += [HERE/'offline_suite.py',HERE/'final_offline_suite.py']
    records=[{'path':p.relative_to(ROOT).as_posix(),'sha256':file_sha(p)} for p in sources]
    json_new(HERE/'p8/full005_sources.json',{'at':now(),'sources':records,'sourceSnapshotBeforeLaunch':True})
    run('full005')
    drift=[r['path'] for r in records if file_sha(ROOT/r['path'])!=r['sha256']]
    json_new(HERE/'p8/full005/source_verification.json',{'at':now(),'sourceFiles':len(records),'drift':drift,'passed':not drift})
    if drift:raise RuntimeError('full_suite_source_drift')
