"""Add only the tested read-aggregation class to the exact deployed JAR; preserve all other bytes."""
import hashlib,json,zipfile
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2]
base=ROOT/'.runtime/product-read-experiment-20260910/backend.jar'
assert hashlib.sha256(base.read_bytes()).hexdigest()=='64179f480e781e09d8aa27204202cc2f79f9480a0849c90ba2550329f994fb76'
dest=ROOT/'.runtime/product-read-release-20260910'
dest.mkdir(exist_ok=False)
classes=ROOT/'backend/target/classes/com/example/locallife/product'
members={f'BOOT-INF/classes/com/example/locallife/product/{name}':(classes/name).read_bytes()
         for name in ('ProductPurchaseViewController.class','ProductPurchaseViewController$View.class')}
source=ROOT/'backend/src/main/java/com/example/locallife/product/ProductPurchaseViewController.java'
members['BOOT-INF/classes/observer-source/com/example/locallife/product/ProductPurchaseViewController.java']=source.read_bytes()
with zipfile.ZipFile(base) as src,zipfile.ZipFile(dest/'backend.jar','x') as out:
    assert not set(members).intersection(src.namelist())
    for info in src.infolist():out.writestr(info,src.read(info.filename))
    for name,data in members.items():out.writestr(name,data)
with zipfile.ZipFile(base) as src,zipfile.ZipFile(dest/'backend.jar') as out:
    assert all(src.read(name)==out.read(name) for name in src.namelist())
(dest/'manifest.json').write_text(json.dumps({'baseJarSha256':hashlib.sha256(base.read_bytes()).hexdigest(),
    'releaseJarSha256':hashlib.sha256((dest/'backend.jar').read_bytes()).hexdigest(),
    'added':{n:hashlib.sha256(b).hexdigest() for n,b in members.items()},'unchangedOldMembers':True},indent=2),encoding='utf-8')
print(dest)
