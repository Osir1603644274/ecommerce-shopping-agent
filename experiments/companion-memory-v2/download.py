"""Download a pinned public upstream, stream and verify LFS hash; no credentials."""
import hashlib, json, time, argparse
from pathlib import Path
import requests

OUT = Path('D:/agent-datasets/companion-memory-v2/upstream')
OUT.mkdir(parents=True, exist_ok=True)
repo = 'xiaowu0162/longmemeval-cleaned'
filename = 'longmemeval_s_cleaned.json'
p = argparse.ArgumentParser(); p.add_argument('--host', default='https://huggingface.co'); p.add_argument('--proxy'); args=p.parse_args()
client=requests.Session()
if args.proxy: client.proxies={'http':args.proxy,'https':args.proxy}
meta = client.get(f'{args.host}/api/datasets/{repo}?blobs=true', timeout=(8,20))
meta.raise_for_status()
info = meta.json()
revision = info['sha']
file_info = next(x for x in info['siblings'] if x['rfilename'] == filename)
(OUT/'hf_metadata.json').write_text(json.dumps(info, indent=2), encoding='utf8')
url = f'{args.host}/datasets/{repo}/resolve/{revision}/{filename}'
dest = OUT/filename
expected = file_info.get('lfs', {}).get('sha256')
if not dest.exists():
    part = dest.with_suffix('.json.part')
    start = time.time()
    with client.get(url, stream=True, timeout=(8, 40)) as response:
        response.raise_for_status()
        with part.open('wb') as f:
            total = 0; last = 0
            for chunk in response.iter_content(1024*1024):
                if not chunk: continue
                f.write(chunk); total += len(chunk)
                if total-last >= 25*1024*1024:
                    print('downloaded_MB', round(total/1e6, 1), flush=True); last=total
    part.rename(dest)
h = hashlib.sha256()
with dest.open('rb') as f:
    for chunk in iter(lambda:f.read(1024*1024), b''): h.update(chunk)
actual = h.hexdigest()
assert expected and actual == expected, (actual, expected)
assert dest.stat().st_size == file_info['size']
receipt = dict(repo=repo, revision=revision, filename=filename, url=url,
               bytes=dest.stat().st_size, sha256=actual, expected_sha256=expected,
               verified=True)
(OUT/'DOWNLOAD.json').write_text(json.dumps(receipt, indent=2), encoding='utf8')
print(json.dumps(receipt), flush=True)
