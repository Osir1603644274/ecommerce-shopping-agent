"""Versioned, disk-first ordinary-search dataset pipeline. No production writes."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import sys
import time
import unicodedata
import urllib.request
from collections import Counter
from pathlib import Path

SEED = 20260909
KUAI_REV = "16f555af360f6a86209f60eb1f12462f9e25be2c"
CPR_REV = "a4e467182a3e2c110528a1575d79b33cc449d2c3"
LITE = Path("D:/agent-datasets/kuaisearch-lite-09807c773ce67360ed8df30842e372182fcf7ad9")


def dump(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    part = path.with_name(path.name + ".part")
    part.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(part, path)


def digest(path: Path):
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(4 * 1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def key(text):
    text = unicodedata.normalize("NFKC", str(text)).casefold()
    return "".join(c for c in text if not c.isspace() and not unicodedata.category(c).startswith("P"))


def clean(value):
    text = " ".join(str(value or "").split())
    return "" if text.upper() in {"UNKNOWN", "NULL", "NONE", "N/A"} else text


def log(event, **values):
    print(json.dumps({"time": time.strftime("%Y-%m-%dT%H:%M:%S"), "event": event, **values}, ensure_ascii=False), flush=True)


def status(root, phase, **values):
    path = root / "status.json"
    data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    data.update({"phase": phase, "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S"), **values})
    dump(path, data)


def acquire(root):
    proxy = os.environ.get("SEARCH_STAGE1_PROXY")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({"http": proxy, "https": proxy}) if proxy else urllib.request.ProxyHandler())
    sources = [
        {"id": "kuaisearch_relevance", "revision": KUAI_REV,
         "url": f"https://huggingface.co/datasets/benchen4395/KuaiSearch/resolve/{KUAI_REV}/relevance/train.jsonl",
         "relative": "raw/kuaisearch/relevance.jsonl", "expected_sha256": "b26c79a3ae6fde4aadc756c7b9eda9f6dcc6e112b725db603def868ab9f9c720"},
    ]
    for name in ("corpus.tsv", "train.query.txt", "qrels.train.tsv", "dev.query.txt", "qrels.dev.tsv"):
        sources.append({"id": "multicpr_" + name, "revision": CPR_REV,
                        "url": f"https://raw.githubusercontent.com/Alibaba-NLP/Multi-CPR/{CPR_REV}/data/ecom/{name}",
                        "relative": "raw/multicpr/" + name})
    manifest = []
    old_path = root / "sources.json"
    old = {r["id"]: r for r in json.loads(old_path.read_text(encoding="utf-8"))["sources"]} if old_path.exists() else {}
    for source in sources:
        path = root / source["relative"]
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            part = path.with_suffix(path.suffix + ".download")
            log("download_start", source=source["id"])
            with opener.open(source["url"], timeout=45) as response, part.open("wb") as out:
                for block in iter(lambda: response.read(1024 * 1024), b""):
                    out.write(block)
            os.replace(part, path)
        sha = digest(path)
        expected = source.get("expected_sha256") or old.get(source["id"], {}).get("sha256")
        if expected and expected != sha:
            raise ValueError(f"Source hash mismatch: {source['id']}")
        manifest.append({**source, "path": str(path), "bytes": path.stat().st_size, "sha256": sha})
        dump(old_path, {"complete": False, "sources": manifest})
        log("source_verified", source=source["id"], bytes=path.stat().st_size, sha256=sha)
    for name in ("items_lite.train.jsonl", "recall_lite.train.jsonl"):
        path = LITE / name
        sha = digest(path)
        if name.startswith("items") and sha != "5c04e031324a37636afb2f822a4d862378d7f54eada93875d686fa8f31bd2621":
            raise ValueError("KuaiSearch Lite item snapshot differs from verified official object")
        manifest.append({"id": "kuaisearch_" + name, "path": str(path), "bytes": path.stat().st_size,
                         "sha256": sha, "revision": LITE.name.removeprefix("kuaisearch-lite-"), "access": "read_only_existing"})
        log("source_verified", source=name, bytes=path.stat().st_size, sha256=sha)
    dump(old_path, {"complete": True, "sources": manifest})
    status(root, "SOURCES_READY", sources_complete=True, labels_complete=False, training_complete=False, cloud_spend_cny=0)


def connect(root):
    root.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(root / "catalog.sqlite")
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA synchronous=NORMAL")
    db.execute("PRAGMA cache_size=-65536")
    db.execute("PRAGMA temp_store=FILE")
    db.executescript("""
      CREATE TABLE IF NOT EXISTS documents(rowid INTEGER PRIMARY KEY, docid TEXT UNIQUE NOT NULL,
        source TEXT NOT NULL, title TEXT NOT NULL, brand TEXT NOT NULL, category TEXT NOT NULL,
        text TEXT NOT NULL, payload TEXT NOT NULL, source_line INTEGER NOT NULL);
      CREATE TABLE IF NOT EXISTS checkpoints(stage TEXT PRIMARY KEY, byte_offset INTEGER, lines INTEGER, complete INTEGER);
      CREATE TABLE IF NOT EXISTS query_inventory(source TEXT, native_split TEXT, query_key TEXT, query_text TEXT,
        frequency INTEGER, example_docid TEXT, PRIMARY KEY(source,native_split,query_key));
    """)
    return db


def resume_lines(db, stage, path):
    row = db.execute("SELECT byte_offset,lines,complete FROM checkpoints WHERE stage=?", (stage,)).fetchone()
    offset, lines, complete = row or (0, 0, 0)
    if complete:
        return
    with path.open("rb") as f:
        f.seek(offset)
        while block := f.readline():
            lines += 1
            yield lines, f.tell(), block.decode("utf-8-sig" if lines == 1 else "utf-8").rstrip("\r\n")


def checkpoint(db, stage, offset, lines, complete=0):
    db.execute("INSERT OR REPLACE INTO checkpoints VALUES(?,?,?,?)", (stage, offset, lines, complete))
    db.commit()


def normalize(root):
    sources = json.loads((root / "sources.json").read_text(encoding="utf-8"))
    if not sources["complete"]:
        raise ValueError("Acquire must complete first")
    binding = digest(root / "sources.json")
    binding_path = root / "normalization.source-binding.json"
    if binding_path.exists() and json.loads(binding_path.read_text(encoding="utf-8-sig"))["sources_sha256"] != binding:
        raise ValueError("Source manifest changed after normalization started")
    dump(binding_path, {"sources_sha256": binding})
    db = connect(root)
    status(root, "NORMALIZING")
    for source, path in (("kuaisearch", LITE / "items_lite.train.jsonl"), ("multicpr", root / "raw/multicpr/corpus.tsv")):
        stage = "documents_" + source
        last = db.execute("SELECT byte_offset,lines,complete FROM checkpoints WHERE stage=?", (stage,)).fetchone()
        if last and last[2]:
            continue
        lines, offset = (last[1], last[0]) if last else (0, 0)
        for lines, offset, line in resume_lines(db, stage, path):
            if source == "kuaisearch":
                row = json.loads(line)
                native_id, title, brand = str(row["item_id"]), clean(row["item_title"]), clean(row.get("brand_name"))
                categories = [clean(row.get(f"category_level{i}_name")) for i in (1, 2, 3)]
                category = categories[0]
                fields = {"title": title, "brand": brand, "categories": [v for v in categories if v]}
            else:
                native_id, title = line.split("\t", 1)
                title, brand, category = clean(title), "", ""
                fields = {"title": title, "brand": "", "categories": []}
            if not title or not native_id:
                raise ValueError(f"Missing document identity/text in {source}:{lines}")
            docid = source + ":" + native_id
            fields.update({"document_id": docid, "entity_type": "product", "source": source})
            text = " ".join(dict.fromkeys([v for v in (title, brand, *fields["categories"]) if v]))
            fields["text"] = text
            payload = json.dumps(fields, ensure_ascii=False, separators=(",", ":"))
            try:
                db.execute("INSERT INTO documents(docid,source,title,brand,category,text,payload,source_line) VALUES(?,?,?,?,?,?,?,?)",
                           (docid, source, title, brand, category, text, payload, lines))
            except sqlite3.IntegrityError:
                previous = db.execute("SELECT payload FROM documents WHERE docid=?", (docid,)).fetchone()
                if previous is None or previous[0] != payload:
                    raise ValueError(f"Conflicting duplicate document id: {docid}")
            if lines % 25000 == 0:
                checkpoint(db, stage, offset, lines)
                log("normalize_progress", source=source, lines=lines)
        checkpoint(db, stage, offset, lines, 1)
        log("normalize_source_complete", source=source, raw_lines=lines)
    query_sources = [("kuaisearch", None, LITE / "recall_lite.train.jsonl")]
    query_sources += [("multicpr", split, root / f"raw/multicpr/{split}.query.txt") for split in ("train", "dev")]
    for source, split, path in query_sources:
        stage = "queries_" + source + "_" + str(split)
        last = db.execute("SELECT byte_offset,lines,complete FROM checkpoints WHERE stage=?", (stage,)).fetchone()
        if last and last[2]:
            continue
        lines, offset = (last[1], last[0]) if last else (0, 0)
        for lines, offset, line in resume_lines(db, stage, path):
            if source == "kuaisearch":
                row = json.loads(line)
                query, native_split = clean(row["query"]), row["split"]
                candidates = row.get("clicked_item_ids") or row.get("impressed_item_ids") or []
                example = "kuaisearch:" + str(candidates[0]) if candidates else ""
            else:
                native_id, query = line.split("\t", 1)
                query, native_split, example = clean(query), split, ""
            normalized = key(query)
            if not normalized:
                continue
            db.execute("""INSERT INTO query_inventory VALUES(?,?,?,?,1,?)
              ON CONFLICT(source,native_split,query_key) DO UPDATE SET frequency=frequency+1""",
                       (source, native_split, normalized, query, example))
            if lines % 25000 == 0:
                checkpoint(db, stage, offset, lines)
                log("query_inventory_progress", source=source, lines=lines)
        checkpoint(db, stage, offset, lines, 1)
    db.execute("CREATE INDEX IF NOT EXISTS documents_source ON documents(source,rowid)")
    db.commit()
    stats = {"sources_sha256": binding,
             "documents": [dict(zip(("source", "count", "first_rowid", "last_rowid"), r)) for r in db.execute("SELECT source,count(*),min(rowid),max(rowid) FROM documents GROUP BY source")],
             "queries": [dict(zip(("source", "native_split", "unique_normalized_queries", "requests"), r)) for r in db.execute("SELECT source,native_split,count(*),sum(frequency) FROM query_inventory GROUP BY source,native_split")],
             "categories": [dict(zip(("category", "documents"), r)) for r in db.execute("SELECT category,count(*) FROM documents WHERE source='kuaisearch' GROUP BY category ORDER BY count(*) DESC")],
             "checkpoints": [dict(zip(("stage", "byte_offset", "lines", "complete"), r)) for r in db.execute("SELECT * FROM checkpoints")]}
    dump(root / "normalization-report.json", stats)
    db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    db.close()
    status(root, "NORMALIZED", normalization_complete=True)
    log("normalization_complete", documents=stats["documents"], queries=stats["queries"])


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path("D:/agent-datasets/search-stage1-v1"))
    parser.add_argument("command", choices=("acquire", "normalize"))
    args = parser.parse_args()
    args.root.mkdir(parents=True, exist_ok=True)
    globals()[args.command](args.root)


if __name__ == "__main__":
    main()
