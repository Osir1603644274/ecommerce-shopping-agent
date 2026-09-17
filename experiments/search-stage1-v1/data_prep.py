"""Query-family isolation, training adapters, and bounded SQLite lexical indexes."""
from __future__ import annotations

import argparse
import hashlib
import heapq
import json
import os
import re
import sqlite3
from collections import Counter, defaultdict, deque
from pathlib import Path

from stage1 import SEED, clean, connect, digest, dump, key, log, status
from integrity import seal_lexical

REPO = Path(__file__).resolve().parents[2]


class NearIndex:
    def __init__(self):
        self.values = {}
        self.postings = defaultdict(set)

    @staticmethod
    def grams(value):
        return {value[i:i+3] for i in range(max(0, len(value)-2))}

    def add(self, value):
        if value in self.values:
            return
        grams = self.grams(value)
        self.values[value] = grams
        for gram in grams:
            self.postings[gram].add(value)

    def match(self, value):
        if value in self.values:
            return value
        if len(value) < 6:
            return None
        grams = self.grams(value)
        counts = Counter(candidate for gram in grams for candidate in self.postings.get(gram, ()))
        for candidate, intersection in counts.items():
            denominator = len(grams) + len(self.values[candidate]) - intersection
            if denominator and intersection / denominator >= .85:
                return candidate
        return None


def old_queries(root):
    # Query-only artifacts: never load old labels or sealed benchmark contents.
    paths = [REPO / "datasets/current/kuaisearch/queries.jsonl"]
    for base in (REPO / "evaluation", REPO / ".agents/evaluation-assets"):
        for current, dirs, files in os.walk(base):
            dirs[:] = [d for d in dirs if not any(w in d.casefold() for w in ("sealed", "hidden", "source_snapshot", "snapshots", "node_modules"))]
            for name in files:
                if name in {"queries.jsonl", "adapted_seed_queries.jsonl"}:
                    paths.append(Path(current) / name)
    near = NearIndex()
    receipts = []
    for path in sorted(set(paths)):
        if not path.is_file():
            continue
        rows = 0
        with path.open(encoding="utf-8-sig") as f:
            for line in f:
                if not line.strip():
                    continue
                row = json.loads(line)
                text = row.get("query") or row.get("query_text") or row.get("text")
                if isinstance(text, str) and key(text):
                    near.add(key(text))
                    rows += 1
        receipts.append({"path": str(path), "sha256": digest(path), "query_rows": rows})
    dump(root / "provenance/previous-query-exclusions.json", {"query_keys": sorted(near.values), "files": receipts,
         "scope": "Existing current KuaiSearch and query-only pool artifacts; not a claim of every historical interaction."})
    return near


CATEGORY_PATTERNS = {
    "服饰鞋包": r"衣|裤|裙|衫|鞋|靴|包|袜|帽|羽绒|围巾|皮带",
    "数码家电": r"手机|电脑|耳机|充电|相机|键盘|鼠标|电器|电视|空调|电池|音箱|灯|冰箱|电饭",
    "家居日用": r"床|被|枕|桌|椅|柜|杯|碗|锅|收纳|纸巾|毛巾|牙刷|垃圾|窗帘|水壶|菜刀",
    "食品饮料": r"糖|饼|茶|咖啡|奶|酒|面包|零食|巧克力|坚果|米|油|调料|肉|虾|枣|蜂蜜",
    "美妆护理": r"护肤|面膜|口红|眼影|眉笔|洗发|沐浴|香水|精华|防晒|乳液|护手|卸妆",
    "母婴玩具": r"宝宝|婴|儿童|玩具|积木|尿|奶瓶|童车|绘本",
    "运动户外": r"球|瑜伽|健身|跑步|钓鱼|帐篷|自行车|户外|泳|登山",
    "文教办公": r"笔|本子|文具|教材|书籍|打印|文件|计算器|乐器",
    "车品工具": r"汽车|车载|车灯|螺丝|扳手|电钻|五金|摩托",
    "宠物园艺": r"宠物|猫粮|狗粮|鱼缸|花盆|种子|园艺",
}


def query_category(text):
    hits = [(len(re.findall(pattern, text)), category) for category, pattern in CATEGORY_PATTERNS.items()]
    count, category = max(hits)
    return category if count else "其他"


def split(root):
    if not (root / "normalization-report.json").exists():
        raise ValueError("Normalization must complete first")
    if (root / "queries.selected.jsonl").exists():
        raise ValueError("Selected query snapshot already exists; do not silently resample")
    db = connect(root)
    near = old_queries(root)
    selected = []
    inventories = []
    for source, native_split, project_split, budget in (
        ("kuaisearch", "test", "test", 80), ("multicpr", "dev", "test", 80),
        ("kuaisearch", "train", "dev", 20), ("multicpr", "train", "dev", 20),
    ):
        heaps = defaultdict(list)
        counts, rejected = Counter(), Counter()
        cursor = db.execute("""SELECT q.query_key,q.query_text,q.frequency,coalesce(d.category,'')
          FROM query_inventory q LEFT JOIN documents d ON d.docid=q.example_docid
          WHERE q.source=? AND q.native_split=? ORDER BY q.query_key""", (source, native_split))
        for qkey, text, frequency, category in cursor:
            if near.match(qkey):
                rejected["old_or_selected_query_family"] += 1
                continue
            category = (category or "其他") if source == "kuaisearch" else query_category(text)
            counts[category] += 1
            priority = int(hashlib.sha256(f"{SEED}:{source}:{project_split}:{qkey}".encode()).hexdigest(), 16)
            entry = (-priority, qkey, text, frequency)
            heap = heaps[category]
            if len(heap) < 300:
                heapq.heappush(heap, entry)
            elif entry > heap[0]:
                heapq.heapreplace(heap, entry)
        queues = {c: deque(sorted(entries, reverse=True)) for c, entries in heaps.items()}
        categories = sorted(queues, key=lambda c: (-counts[c], c))
        chosen = 0
        while chosen < budget:
            advanced = False
            for category in categories:
                while queues[category]:
                    _, qkey, text, frequency = queues[category].popleft()
                    if near.match(qkey):
                        rejected["near_duplicate_during_selection"] += 1
                        continue
                    qid = f"s1-{source[:2]}-{project_split}-" + hashlib.sha256(qkey.encode()).hexdigest()[:16]
                    selected.append({"query_id": qid, "text": text, "query_key": qkey, "source": source,
                                     "native_split": native_split, "split": project_split, "stratum": category,
                                     "stratum_method": "native_exposure_item_category" if source == "kuaisearch" else "query_lexicon_heuristic_not_gold",
                                     "frequency": frequency, "variants": [], "structured": {}})
                    near.add(qkey)
                    chosen += 1
                    advanced = True
                    break
                if chosen == budget:
                    break
            if not advanced:
                raise ValueError(f"Insufficient eligible queries: {source}/{project_split}")
        inventories.append({"source": source, "native_split": native_split, "project_split": project_split,
                            "eligible_by_stratum": dict(counts), "rejected": dict(rejected), "selected": chosen})
    selected.sort(key=lambda r: (r["source"], r["split"], r["query_id"]))
    with (root / "queries.selected.jsonl").open("x", encoding="utf-8") as out:
        for row in selected:
            out.write(json.dumps(row, ensure_ascii=False) + "\n")
    dump(root / "split-report.json", {"seed": SEED, "query_count": len(selected), "queries_sha256": digest(root / "queries.selected.jsonl"),
         "near_duplicate_rule": "NFKC+casefold+remove whitespace/punctuation; exact or char-trigram Jaccard>=0.85 for queries length>=6",
         "inventory": inventories, "selected_strata": dict(Counter(f"{r['source']}:{r['split']}:{r['stratum']}" for r in selected)),
         "native_test_claim": "MultiCPR project test is sampled from native dev; not an official held-out test set."})
    db.close()
    training(root, near)
    status(root, "SPLITS_READY", splits_complete=True)
    log("split_complete", queries=len(selected))


def training(root, excluded=None):
    if excluded is None:
        excluded = NearIndex()
        data = json.loads((root / "provenance/previous-query-exclusions.json").read_text(encoding="utf-8"))
        for value in data["query_keys"]:
            excluded.add(value)
        for line in (root / "queries.selected.jsonl").read_text(encoding="utf-8").splitlines():
            excluded.add(json.loads(line)["query_key"])
    destination = root / "training"
    destination.mkdir(exist_ok=True)
    statistics = Counter()
    seen_pairs = {}
    raw = root / "raw/kuaisearch/relevance.jsonl"
    out_path = destination / "kuaisearch.human.train.jsonl"
    with raw.open(encoding="utf-8") as source, out_path.with_suffix(".part").open("w", encoding="utf-8") as out:
        for n, line in enumerate(source, 1):
            row = json.loads(line)
            statistics["native_" + row["split"]] += 1
            if row["split"] != "train":
                continue
            qkey = key(row["query"])
            if excluded.match(qkey):
                statistics["excluded_query_family"] += 1
                continue
            # Original annotations saw attr_value; preserve this native evidence.
            text = " ".join(dict.fromkeys(v for v in (clean(row["item_title"]), clean(row.get("brand")), clean(row.get("attr_value"))) if v))
            pair_key = (qkey, key(text))
            grade = row["score"]
            if type(grade) is not int or grade not in (0, 1, 2, 3):
                raise ValueError("Unexpected original grade")
            if pair_key in seen_pairs:
                if seen_pairs[pair_key] != grade:
                    raise ValueError("Conflicting native training labels require explicit handling")
                statistics["identical_duplicate_pair"] += 1
                continue
            seen_pairs[pair_key] = grade
            record = {"pair_id": f"ks-human-{n}", "query": row["query"], "query_key": qkey, "text": text,
                      "original_grade": grade, "target": int(grade == 3), "label_source": "kuaisearch_original_human",
                      "source_line": n, "native_split": "train", "original": row}
            out.write(json.dumps(record, ensure_ascii=False) + "\n")
            statistics["exported"] += 1
            statistics[f"grade_{grade}"] += 1
    os.replace(out_path.with_suffix(".part"), out_path)
    db = connect(root)
    cpr_counts = Counter()
    cpr_path = destination / "multicpr.native_positive_pairs.jsonl"
    with cpr_path.with_suffix(".part").open("w", encoding="utf-8") as out:
        for native_split in ("train", "dev"):
            query_map = {}
            with (root / f"raw/multicpr/{native_split}.query.txt").open(encoding="utf-8") as f:
                for line in f:
                    qid, query = line.rstrip("\r\n").split("\t", 1)
                    query_map[qid] = query
            with (root / f"raw/multicpr/qrels.{native_split}.tsv").open(encoding="utf-8") as f:
                for line in f:
                    qid, iteration, did, grade = line.rstrip().split("\t")
                    if qid not in query_map:
                        raise ValueError("Dangling MultiCPR query")
                    document = db.execute("SELECT text FROM documents WHERE docid=?", ("multicpr:" + did,)).fetchone()
                    if document is None:
                        raise ValueError("Dangling MultiCPR document")
                    query = query_map[qid]
                    blocked = bool(excluded.match(key(query)))
                    record = {"native_query_id": qid, "query": query, "document_id": "multicpr:" + did, "text": document[0],
                              "native_split": native_split, "original_label": grade, "label_source": "multicpr_original_positive",
                              "excluded_query_family": blocked, "first_round_training": False}
                    out.write(json.dumps(record, ensure_ascii=False) + "\n")
                    cpr_counts[native_split] += 1
                    cpr_counts["excluded_family"] += blocked
    os.replace(cpr_path.with_suffix(".part"), cpr_path)
    db.close()
    dump(root / "training-report.json", {"kuaisearch": dict(statistics), "multicpr": dict(cpr_counts),
         "training_sha256": digest(out_path), "cpr_pairs_sha256": digest(cpr_path),
         "original_binary_rule": "Only KuaiSearch native grade=3 is fully relevant; 0/1/2 are not fully relevant.",
         "feature_scope": "Native human pair training includes original attr_value; serving/pool text only includes available native catalog fields."})
    log("training_data_complete", kuaisearch=dict(statistics), multicpr=dict(cpr_counts))


def lexical(root, source):
    import jieba
    if jieba.__version__!="0.42.1":raise ValueError("Frozen lexical profile requires jieba 0.42.1")
    jieba.setLogLevel(30)
    jieba.initialize()
    catalog = sqlite3.connect(f"file:{(root / 'catalog.sqlite').as_posix()}?mode=ro", uri=True)
    index_root = root / "indexes" / source
    index_root.mkdir(parents=True, exist_ok=True)
    binding_path = index_root / "lexical.binding.json"
    binding = {"normalization_sha256": digest(root / "normalization-report.json"), "source": source,
               "jieba": jieba.__version__, "hmm": False, "character_ngram": 2}
    if binding_path.exists() and json.loads(binding_path.read_text(encoding="utf-8")) != binding:
        raise ValueError("Lexical corpus/tokenization binding drift")
    dump(binding_path, binding)
    db = sqlite3.connect(index_root / "lexical.sqlite")
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA cache_size=-65536")
    db.execute("PRAGMA temp_store=FILE")
    db.executescript("""CREATE VIRTUAL TABLE IF NOT EXISTS words USING fts5(terms,content='',tokenize='unicode61 remove_diacritics 0');
      CREATE VIRTUAL TABLE IF NOT EXISTS chars USING fts5(terms,content='',tokenize='unicode61 remove_diacritics 0');
      CREATE TABLE IF NOT EXISTS progress(id INTEGER PRIMARY KEY CHECK(id=1),last_rowid INTEGER,count INTEGER,complete INTEGER);""")
    previous = db.execute("SELECT last_rowid,count,complete FROM progress WHERE id=1").fetchone() or (0,0,0)
    if previous[2]:
        expected_count=catalog.execute("SELECT count(*) FROM documents WHERE source=?",(source,)).fetchone()[0]
        if previous[1]!=expected_count:raise ValueError("Lexical completion count differs from source corpus")
        db.close();catalog.close()
        seal_lexical(root,source,previous[1])
        log("lexical_reuse", source=source, documents=previous[1]); return
    count, rowid = previous[1], previous[0]
    for rowid, text in catalog.execute("SELECT rowid,text FROM documents WHERE source=? AND rowid>? ORDER BY rowid", (source,rowid)):
        normalized = clean(text).casefold()
        tokens = [t for t in jieba.cut_for_search(normalized, HMM=False) if re.search(r"[\w\u3400-\u9fff]", t)]
        compact = key(normalized)
        grams = [compact[i:i+2] for i in range(len(compact)-1)] or [compact]
        db.execute("INSERT INTO words(rowid,terms) VALUES(?,?)", (rowid, " ".join(tokens)))
        db.execute("INSERT INTO chars(rowid,terms) VALUES(?,?)", (rowid, " ".join(grams)))
        count += 1
        if count % 10000 == 0:
            db.execute("INSERT OR REPLACE INTO progress VALUES(1,?,?,0)", (rowid,count)); db.commit()
            if count % 100000 == 0: log("lexical_progress", source=source, documents=count)
    db.execute("INSERT OR REPLACE INTO progress VALUES(1,?,?,1)", (rowid,count)); db.commit()
    db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    db.close(); catalog.close()
    seal_lexical(root,source,count)
    log("lexical_complete", source=source, documents=count)


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--root", type=Path, default=Path("D:/agent-datasets/search-stage1-v1"))
    p.add_argument("command", choices=("split", "training", "lexical"))
    p.add_argument("--source", choices=("kuaisearch", "multicpr"))
    a = p.parse_args()
    if a.command == "lexical":
        if not a.source: p.error("lexical requires --source")
        lexical(a.root,a.source)
    else: globals()[a.command](a.root)
