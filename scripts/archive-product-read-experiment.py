"""Freeze selected experiment evidence; refuse overwrite, verify every archived byte."""
import hashlib
import json
from pathlib import Path
import zipfile

root = Path(__file__).resolve().parents[1]
source = root / '.runtime/product-read-experiment-20260910'
target = root / 'docs/experiments/product-read-20260910'
target.mkdir(parents=True, exist_ok=True)
archive = target / 'evidence.zip'
assert not archive.exists(), 'Archive exists; do not replace sealed evidence'
files = sorted(p for p in source.rglob('*') if p.is_file()
               and not any(part in {'unpacked', 'classes', '__pycache__'} for part in p.relative_to(source).parts)
               and p.suffix in {'.py', '.ps1', '.java', '.yaml', '.md', '.json'})
manifest = {}
with zipfile.ZipFile(archive, 'x', compression=zipfile.ZIP_DEFLATED) as z:
    for p in files:
        name = p.relative_to(source).as_posix()
        data = p.read_bytes()
        z.writestr(name, data)
        manifest[name] = hashlib.sha256(data).hexdigest()
with zipfile.ZipFile(archive) as z:
    assert all(hashlib.sha256(z.read(n)).hexdigest() == h for n, h in manifest.items())
(target / 'manifest.json').write_text(json.dumps({'archiveSha256': hashlib.sha256(archive.read_bytes()).hexdigest(),
    'files': manifest}, indent=2), encoding='utf-8')
print(f'Archived and verified {len(files)} files ({archive.stat().st_size} bytes). No runtime source changed.')
