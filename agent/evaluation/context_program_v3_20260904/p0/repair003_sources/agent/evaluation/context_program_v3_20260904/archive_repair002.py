"""Preserve current sources before the P4 repair, never overwriting evidence."""
import shutil
from .common import HERE, ROOT, file_sha, json_new, now

out = HERE / 'p0/repair002_sources'
out.mkdir(parents=True, exist_ok=False)
sources = list((ROOT / 'agent/app').rglob('*.py')) + list(HERE.glob('*.py'))
sources += [HERE / 'p0/contract.json']
records = []
for source in sources:
    relative = source.relative_to(ROOT)
    target = out / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, target)
    records.append({'path': relative.as_posix(), 'sha256': file_sha(source)})
json_new(out / 'sources.json', {'at': now(), 'sources': records, 'secretsCopied': False})
print('P4 pre-repair sources archived:', len(records))
