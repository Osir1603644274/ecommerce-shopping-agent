"""Download an immutable, public encoder revision without modifying runtime dependencies."""
import hashlib, json, os, time, winreg
from pathlib import Path
import requests

ROOT = Path(os.environ.get('REC_COMPLETION_ROOT','D:/agent-datasets/recommendation-completion-v1'))
MODEL = 'sentence-transformers/all-MiniLM-L6-v2'
REV = '1110a243fdf4706b3f48f1d95db1a4f5529b4d41'
FILES = ['config.json', 'model.safetensors', 'special_tokens_map.json',
         'tokenizer.json', 'tokenizer_config.json', 'vocab.txt']

def main():
    dest = ROOT / 'minilm'; dest.mkdir(parents=True, exist_ok=True)
    session = requests.Session()
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r'Software\Microsoft\Windows\CurrentVersion\Internet Settings') as key:
        enabled = winreg.QueryValueEx(key, 'ProxyEnable')[0]
        proxy = winreg.QueryValueEx(key, 'ProxyServer')[0]
    if enabled:
        if '=' in proxy: proxy = proxy.split(';')[0].split('=', 1)[1]
        session.proxies.update({'http': 'http://' + proxy, 'https': 'http://' + proxy})
    records = []
    for name in FILES:
        path = dest / name
        url = f'https://huggingface.co/{MODEL}/resolve/{REV}/{name}'
        # Only resume verified files from a completed manifest.
        existing = json.loads((dest/'DOWNLOAD.json').read_text()) if (dest/'DOWNLOAD.json').exists() else {}
        expected = existing.get('files', {}).get(name, {}).get('sha256')
        valid = path.exists() and expected == hashlib.sha256(path.read_bytes()).hexdigest()
        if not valid:
            for attempt in range(3):
                try:
                    with session.get(url, stream=True, timeout=(20, 45)) as response:
                        response.raise_for_status()
                        with path.with_suffix(path.suffix+'.partial').open('wb') as out:
                            for chunk in response.iter_content(1024*1024): out.write(chunk)
                    os.replace(path.with_suffix(path.suffix+'.partial'), path)
                    break
                except requests.RequestException:
                    if attempt == 2: raise
                    time.sleep(2)
        records.append((name, {'url': url, 'bytes': path.stat().st_size,
                              'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}))
        print(json.dumps({'downloaded': name, 'bytes': path.stat().st_size}), flush=True)
    (dest/'DOWNLOAD.json').write_text(json.dumps({'model': MODEL, 'revision': REV,
        'license_from_model_card': 'Apache-2.0', 'files': dict(records)}, indent=2), encoding='utf-8')

if __name__ == '__main__': main()
