"""Explicit --apply only. Verify EVERY old hash first, then back up and copy task paths."""
import argparse, datetime, hashlib, json, pathlib, shutil, zipfile

def digest(p): return hashlib.sha256(p.read_bytes()).hexdigest().upper() if p.is_file() else None
parser=argparse.ArgumentParser()
parser.add_argument('payload',type=pathlib.Path); parser.add_argument('destination',type=pathlib.Path)
parser.add_argument('--apply',action='store_true'); args=parser.parse_args()
destination=args.destination.resolve()
with zipfile.ZipFile(args.payload) as z:
    changes=json.loads(z.read('task-changes.json'))
    pending=[]; conflicts=[]
    for row in changes:
        target=(destination/row['path']).resolve()
        if not target.is_relative_to(destination): raise ValueError('Path escapes destination')
        if target.exists() and not target.is_file(): raise ValueError('Expected file: '+str(target))
        current=digest(target)
        if current==row['result_sha256']: continue
        if current!=row['expected_source_sha256']: conflicts.append(row['path']); continue
        content=z.read('files/'+row['path']) if row['result_sha256'] else None
        if content is not None and hashlib.sha256(content).hexdigest().upper()!=row['result_sha256']:
            raise ValueError('Corrupt payload '+row['path'])
        pending.append((target,content,row))
    if conflicts: raise SystemExit('Source drift; no files written: '+json.dumps(conflicts,ensure_ascii=False))
    print(f'Checks passed; {len(pending)} files need changes; apply={args.apply}')
    if args.apply:
        backup=destination/'.migration-backups'/datetime.datetime.now().strftime('%Y%m%d-%H%M%S')
        for target,content,row in pending:
            if target.exists():
                saved=backup/row['path']; saved.parent.mkdir(parents=True,exist_ok=True); shutil.copy2(target,saved)
            target.parent.mkdir(parents=True,exist_ok=True)
            if content is None: target.unlink()
            else: target.write_bytes(content)
        backup.mkdir(parents=True,exist_ok=True)
        (backup/'manifest.json').write_text(json.dumps(changes,indent=2),encoding='utf-8')
        print('Backup:',backup)
