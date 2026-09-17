import hashlib,json,requests
from pathlib import Path
OUT=Path('D:/agent-datasets/companion-memory-v2/model');OUT.mkdir(parents=True,exist_ok=True)
client=requests.Session();client.proxies={'http':'http://127.0.0.1:17891','https':'http://127.0.0.1:17891'}
repo='sentence-transformers/all-MiniLM-L6-v2'
r=client.get(f'https://huggingface.co/api/models/{repo}?blobs=true',timeout=(8,30));r.raise_for_status();meta=r.json()
(OUT/'HF_METADATA.json').write_text(json.dumps(meta,indent=2),encoding='utf8')
rev=meta['sha'];files=[]
for name in ['config.json','tokenizer.json','tokenizer_config.json','special_tokens_map.json','vocab.txt','model.safetensors']:
 info=next(x for x in meta['siblings'] if x['rfilename']==name);p=OUT/name
 if not p.exists():
  r=client.get(f'https://huggingface.co/{repo}/resolve/{rev}/{name}',stream=True,timeout=(8,40));r.raise_for_status()
  with p.with_suffix(p.suffix+'.part').open('wb') as f:
   for chunk in r.iter_content(1024*1024):f.write(chunk)
  p.with_suffix(p.suffix+'.part').rename(p)
 actual=hashlib.sha256(p.read_bytes()).hexdigest();expected=info.get('lfs',{}).get('sha256')
 assert not expected or actual==expected
 assert p.stat().st_size==info['size']
 files.append(dict(name=name,bytes=p.stat().st_size,sha256=actual,lfs_sha256=expected))
 print('verified',name,p.stat().st_size,flush=True)
(OUT/'DOWNLOAD.json').write_text(json.dumps(dict(repo=repo,revision=rev,files=files),indent=2),encoding='utf8')
