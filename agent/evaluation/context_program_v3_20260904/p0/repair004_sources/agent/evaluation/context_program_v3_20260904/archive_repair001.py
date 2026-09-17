"""Archive the first repair source and reallocate unused budget without increasing it."""
import json
import shutil
from .common import HERE, ROOT, file_sha, json_new, now

out = HERE / 'p0/repair001_sources'
out.mkdir(parents=True, exist_ok=False)
sources = list((ROOT / 'agent/app').rglob('*.py')) + list(HERE.glob('*.py'))
sources += [HERE / 'p0/contract.json']
records = []
for source in sources:
    relative = source.relative_to(ROOT)
    target = out / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, target)
    records.append({'path': str(relative).replace('\\','/'), 'sha256': file_sha(source)})
json_new(out / 'sources.json', {'at': now(), 'sources': records, 'secretsCopied': False})
print('First repair source archived before further edits')
