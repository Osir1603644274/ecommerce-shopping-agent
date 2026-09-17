"""Query-only, resumable split preparation. Never reads product or test judgments.

prepare creates deterministic candidates and native query provenance. freeze needs
explicit query-only review decisions and seals disjoint new train/test queries.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
import unicodedata

SEED = 20260909
OLD = Path("D:/agent-datasets/search-stage1-v1")
ROOT = Path("D:/agent-datasets/search-closure-v1/selection")
LITE = Path("D:/agent-datasets/kuaisearch-lite-09807c773ce67360ed8df30842e372182fcf7ad9")
SOURCES = {
    "kuaisearch": (LITE / "recall_lite.train.jsonl", "8949ed6b5cf685bb69067710ac70da12fd7999f66e66bafea4b9d8d78bfefe8a"),
    "multicpr": (OLD / "raw/multicpr/train.query.txt", "2a36f72063a3be3dea04944fb0174ea82473d66eb585b4fa3bcacc8c94c13008"),
}


def key(text):
    return "".join(c for c in unicodedata.normalize("NFKC", text).casefold()
                   if not c.isspace() and not unicodedata.category(c).startswith("P"))


def digest(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        for b in iter(lambda: f.read(4 * 1024 * 1024), b""):
            h.update(b)
    return h.hexdigest()


def read_rows(path):
    with Path(path).open(encoding="utf-8-sig") as f:
        for line in f:
            if line.strip():
                yield json.loads(line)


def write_new(path, value, lines=False):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = ("".join(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n" for r in value)
            if lines else json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    if path.exists():
        if path.read_text(encoding="utf-8") != data:
            raise ValueError(f"Refusing to replace prior artifact: {path}")
        return
    part = path.with_name(path.name + ".part")
    with part.open("w", encoding="utf-8", newline="\n") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    if path.exists():
        raise ValueError(f"Artifact appeared while preparing atomic write: {path}")
    os.replace(part, path)


class NearIndex:
    def __init__(self):
        self.values = {}
        self.postings = defaultdict(set)

    @staticmethod
    def grams(value, width=3):
        return {value[i:i + width] for i in range(max(0, len(value) - width + 1))}

    def add(self, value):
        if value in self.values:
            return
        grams = self.grams(value)
        self.values[value] = grams
        for gram in grams:
            self.postings[gram].add(value)

    def matches(self, value, threshold=.85):
        if value in self.values:
            return [(1.0, value)]
        if len(value) < 6:
            return []
        grams = self.grams(value)
        counts = Counter(c for g in grams for c in self.postings.get(g, ()))
        matches = []
        for candidate, inter in counts.items():
            den = len(grams) + len(self.values[candidate]) - inter
            if den and inter / den >= threshold:
                matches.append((inter / den, candidate))
        return sorted(matches, key=lambda r: (-r[0], r[1]))


class QueryNeighbors:
    """Bigram neighbours for reading assistance only, never semantic labels."""
    def __init__(self, keys):
        self.values = {k: NearIndex.grams(k, 2) for k in sorted(set(keys))}
        self.postings = defaultdict(set)
        for k, grams in self.values.items():
            for gram in grams:
                self.postings[gram].add(k)

    def nearest(self, query, limit=4):
        grams = NearIndex.grams(query, 2)
        counts = Counter(k for g in grams for k in self.postings.get(g, ()))
        rows = []
        for k, inter in counts.items():
            if k == query:
                continue
            score = inter / (len(grams) + len(self.values[k]) - inter)
            rows.append({"query_key": k, "bigram_jaccard": round(score, 6)})
        return sorted(rows, key=lambda r: (-r["bigram_jaccard"], r["query_key"]))[:limit]


def load_exclusions():
    records = defaultdict(list)
    receipts = []
    history = OLD / "provenance/previous-query-exclusions.json"
    value = json.loads(history.read_text(encoding="utf-8"))
    for text in value["query_keys"]:
        records[key(text)].append("historical_query_only_exclusion")
    receipts.append({"path": str(history), "sha256": digest(history), "query_keys": len(value["query_keys"]),
                     "access": "query-only exclusion keys"})
    for name, field, tag in (("queries.selected.jsonl", "text", "old_200_evaluation"),
                             ("training/kuaisearch.human.train.jsonl", "query", "old_reranker_training")):
        path = OLD / name
        count = 0
        for row in read_rows(path):
            count += 1
            records[key(row[field])].append(tag)
        receipts.append({"path": str(path), "sha256": digest(path), "query_rows": count,
                         "access": "query projection only; no grade/text/product values exported or used"})
    normalized = {k: sorted(set(v)) for k, v in sorted(records.items()) if k}
    near = NearIndex()
    for k in normalized:
        near.add(k)
    return normalized, near, receipts


def priority(source, qkey):
    return hashlib.sha256(f"{SEED}:search-closure-v1:{source}:{qkey}".encode()).hexdigest()


def native_bind(candidates, source):
    path, expected = SOURCES[source]
    wanted = {r["query_key"]: r for r in candidates}
    observed = defaultdict(list)
    h = hashlib.sha256()
    splits = Counter()
    rows = 0
    with path.open("rb") as f:
        for rows, raw in enumerate(f, 1):
            h.update(raw)
            text = raw.decode("utf-8-sig" if rows == 1 else "utf-8").rstrip("\r\n")
            if source == "kuaisearch":
                row = json.loads(text)
                split = row["split"]
                splits[split] += 1
                if split != "train":
                    continue
                qtext = row["query"]
                native_id = str(row["session_id"])
                identity = {"session_id": row["session_id"], "user_id": row["user_id"], "time_index": row["time_index"]}
            else:
                splits["train"] += 1
                native_id, qtext = text.split("\t", 1)
                identity = {"native_query_id": native_id}
            qkey = key(qtext)
            if qkey not in wanted:
                continue
            observed[qkey].append({"source_line": rows, "source_row_sha256": hashlib.sha256(raw).hexdigest(),
                                   "native_id": native_id, "native_split": "train", "original_query": qtext,
                                   "native_identity": identity})
    actual = h.hexdigest()
    if actual != expected:
        raise ValueError(f"Native source drift: {source}: {actual}")
    for r in candidates:
        bindings = observed[r["query_key"]]
        exact = [v for v in bindings if " ".join(v["original_query"].split()) == r["text"]]
        if not exact:
            raise ValueError(f"Inventory query lacks native train verification: {r['text']}")
        if len(bindings) != r["native_frequency"]:
            raise ValueError(f"Inventory frequency drift: {r['text']}")
        r["native_origin"] = {"path": str(path), "source_sha256": actual, **exact[0]}
        r["native_occurrence_count"] = len(bindings)
        r["native_query_variants"] = sorted(set(v["original_query"] for v in bindings))
    return {"source": source, "path": str(path), "sha256": actual, "bytes": path.stat().st_size,
            "scanned_rows": rows, "native_split_counts": dict(splits), "verified_candidate_queries": len(candidates),
            "access": "source bytes hashed; native split checked before query selection; exposure and label fields unused"}


def prepare(root=ROOT, budget=220):
    if budget < 140:
        raise ValueError("Candidate budget must cover 40 test + 100 train")
    root = Path(root)
    exclusions, near, input_receipts = load_exclusions()
    write_new(root / "exclusions.query-only.jsonl", [{"query_key": k, "reasons": v} for k, v in exclusions.items()], True)
    db = sqlite3.connect(f"file:{(OLD / 'catalog.sqlite').as_posix()}?mode=ro", uri=True)
    pools, stats, source_receipts = {}, [], []
    for source in SOURCES:
        eligible, rejected = [], Counter()
        total = 0
        # Intentionally no join to documents and no source example_docid access.
        for qkey, text, freq in db.execute("SELECT query_key,query_text,frequency FROM query_inventory WHERE source=? AND native_split='train' ORDER BY query_key", (source,)):
            total += 1
            if key(text) != qkey:
                raise ValueError("Inventory normalization drift")
            if near.matches(qkey):
                rejected["known_evaluation_or_training_family"] += 1
                continue
            eligible.append((priority(source, qkey), qkey, text, freq))
        selected = []
        own = NearIndex()
        for p, qkey, text, freq in sorted(eligible):
            if own.matches(qkey):
                rejected["candidate_family_duplicate"] += 1
                continue
            selected.append({"candidate_id": f"closure-{source[:2]}-{len(selected)+1:03d}", "source": source,
                             "text": text, "query_key": qkey, "priority_sha256": p,
                             "source_priority_rank": len(selected) + 1, "native_split": "train", "native_frequency": freq})
            own.add(qkey)
            if len(selected) == budget:
                break
        if len(selected) < budget:
            raise ValueError("Insufficient candidate inventory")
        pools[source] = selected
        source_receipts.append(native_bind(selected, source))
        stats.append({"source": source, "native_train_inventory_queries": total,
                      "eligible_before_candidate_dedup": len(eligible), "rejected": dict(rejected), "candidate_count": len(selected)})
    db.close()
    candidates = [r for source in SOURCES for r in pools[source]]
    old_neighbors = QueryNeighbors(exclusions)
    candidate_neighbors = QueryNeighbors(r["query_key"] for r in candidates)
    for r in candidates:
        r["review_aids"] = {"old_query_neighbors": old_neighbors.nearest(r["query_key"]),
                            "new_query_neighbors": candidate_neighbors.nearest(r["query_key"]),
                            "meaning": "lexical retrieval aids only, not semantic duplicate judgments"}
    write_new(root / "candidates.query-only.jsonl", candidates, True)
    report = {"status": "AWAITING_QUERY_ONLY_SEMANTIC_REVIEW", "seed": SEED,
              "selection": "sort sha256(seed:search-closure-v1:source:normalized_query); first 40 accepted per source test, next 100 accepted train",
              "rules": "NFKC casefold, whitespace and Unicode P category removal; exact or trigram Jaccard >=0.85 when query length >=6",
              "eligibility": "native train query only; no candidate retrieval, product availability, labels, positive count or scores used",
              "historical_exclusion_scope": "saved 510 query keys + old 200 query-only rows + all old 41306 training query projections; not every unrecorded historical interaction",
              "excluded_unique_query_keys": len(exclusions), "input_receipts": input_receipts,
              "source_receipts": source_receipts, "inventory": stats,
              "candidate_file_sha256": digest(root / "candidates.query-only.jsonl"),
              "exclusion_file_sha256": digest(root / "exclusions.query-only.jsonl"),
              "review_required": "Each considered candidate needs accept/reject and a query-only reason. Read nearest old/new query aids and flag remaining semantic ambiguity. Review does not certify exhaustive semantic isolation."}
    write_new(root / "PREPARATION.json", report)
    print(json.dumps(report, ensure_ascii=False, indent=2))


def freeze(root=ROOT):
    root = Path(root)
    prep = json.loads((root / "PREPARATION.json").read_text(encoding="utf-8"))
    candidates_path = root / "candidates.query-only.jsonl"
    if digest(candidates_path) != prep["candidate_file_sha256"]:
        raise ValueError("Candidates changed after preparation")
    if digest(root / "exclusions.query-only.jsonl") != prep["exclusion_file_sha256"]:
        raise ValueError("Exclusions changed after preparation")
    review_path = root / "query-only-review.jsonl"
    root_review_path = root / "ROOT_QUERY_REVIEW.json"
    root_review = json.loads(root_review_path.read_text(encoding="utf-8"))
    if root_review.get("status") != "ACCEPT_QUERY_ONLY_SPLIT_SELECTION":
        raise ValueError("Root query-only review is not accepted")
    approved_draft = root / root_review["approved_selected_draft"]
    if approved_draft.parent != root or digest(approved_draft) != root_review["approved_selected_draft_sha256"]:
        raise ValueError("Approved query draft binding mismatch")
    if digest(review_path) != root_review["approved_review_sha256"]:
        raise ValueError("Root-approved final review changed")
    decisions = list(read_rows(review_path))
    reviews = {r["candidate_id"]: r for r in decisions}
    if len(reviews) != len(decisions):
        raise ValueError("Duplicate review IDs")
    candidates = list(read_rows(candidates_path))
    ids = {r["candidate_id"] for r in candidates}
    if set(reviews) - ids:
        raise ValueError("Unknown candidate reviewed")
    exclusions, near, inputs = load_exclusions()
    if inputs != prep["input_receipts"]:
        raise ValueError("Historical query input drift")
    selected, reject, counts = [], [], Counter()
    for r in candidates:
        source = r["source"]
        if counts[source] == 140:
            continue
        decision = reviews.get(r["candidate_id"])
        if not decision:
            raise ValueError(f"Missing semantic review before target reached: {r['candidate_id']}")
        if decision.get("candidate_file_sha256") != prep["candidate_file_sha256"] or not decision.get("reason") or not decision.get("reviewer") or decision.get("query") != r["text"]:
            raise ValueError("Missing review binding/author/reason")
        if decision.get("decision") not in {"accept", "reject"}:
            raise ValueError("Unresolved review cannot freeze")
        if decision["decision"] == "reject":
            reject.append({"candidate_id": r["candidate_id"], "reason": decision["reason"]})
            continue
        match = near.matches(r["query_key"])
        if match:
            raise ValueError(f"Selected family collision {r['candidate_id']}: {match[:1]}")
        split = "test" if counts[source] < 40 else "train"
        qid = f"closure-{source[:2]}-{split}-" + hashlib.sha256(r["query_key"].encode()).hexdigest()[:16]
        selected.append({**{k: v for k, v in r.items() if k != "review_aids"}, "query_id": qid, "split": split,
                         "review_decision": decision, "heldout_access": "query-only until final search configuration freeze" if split == "test" else "training pool permitted after trigger"})
        near.add(r["query_key"])
        counts[source] += 1
    if any(counts[s] != 140 for s in SOURCES):
        raise ValueError(f"Insufficient accepted candidates: {counts}")
    draft_set = {(r["candidate_id"], r["source"], r["split"], r["query"]) for r in read_rows(approved_draft)}
    actual_set = {(r["candidate_id"], r["source"], r["split"], r["text"]) for r in selected}
    if len(draft_set) != 280 or draft_set != actual_set:
        raise ValueError("Freeze selected set differs from root-approved draft")
    for source in SOURCES:
        native_bind([r for r in selected if r["source"] == source], source)
    selected.sort(key=lambda r: (r["source"], r["split"], r["query_id"]))
    for split in ("test", "train"):
        write_new(root / "frozen" / f"{split}.queries.jsonl", [r for r in selected if r["split"] == split], True)
    report = {"status": "FROZEN_QUERY_SPLITS_ONLY", "seed": SEED, "counts": dict(Counter(f"{r['source']}:{r['split']}" for r in selected)),
              "candidate_sha256": digest(candidates_path), "review_sha256": digest(review_path), "rejected_considered": reject,
              "root_review_sha256": digest(root_review_path), "selection_code_sha256": digest(Path(__file__)),
              "native_bindings_reverified": True, "test_retrieval_or_labels_executed": False,
              "review_scope": "model query-only review; lexical neighbour aids are not proof of exhaustive semantic deduplication; no human review claimed",
              "files": {f"{s}.queries.jsonl": digest(root / "frozen" / f"{s}.queries.jsonl") for s in ("test", "train")}}
    write_new(root / "FROZEN.json", report)
    print(json.dumps(report, ensure_ascii=False, indent=2))


def draft(root=ROOT, revision=""):
    """Serialize explicitly authored semantic decisions; no heuristic labeling."""
    root = Path(root)
    prep = json.loads((root / "PREPARATION.json").read_text(encoding="utf-8"))
    candidate_path = root / "candidates.query-only.jsonl"
    if digest(candidate_path) != prep["candidate_file_sha256"]:
        raise ValueError("Candidate input drift")
    candidates = list(read_rows(candidate_path))
    if revision and not revision.isalnum():
        raise ValueError("Review revision must be alphanumeric")
    suffix = f".{revision}" if revision else ""
    manual_path = root / f"query-review-decisions{suffix}.tsv"
    manual = {}
    for line in manual_path.read_text(encoding="utf-8").splitlines():
        short_id, decision, reason = line.split("\t", 2)
        candidate_id = f"closure-{short_id[:2]}-{short_id[2:]}"
        if candidate_id in manual or decision not in {"accept", "reject"}:
            raise ValueError("Invalid manual semantic decision")
        manual[candidate_id] = (decision, reason)
    if set(manual) != {r["candidate_id"] for r in candidates}:
        raise ValueError("Manual review must explicitly cover all candidates")
    reviews = []
    for r in candidates:
        decision, reason = manual[r["candidate_id"]]
        reviews.append({"candidate_id": r["candidate_id"], "query": r["text"], "decision": decision,
                        "reason": reason, "reviewer": "/root/split_selection",
                        "review_type": "model_query_only_semantic_read_not_human_gold",
                        "candidate_file_sha256": prep["candidate_file_sha256"],
                        "manual_decisions_sha256": digest(manual_path), "reviewed_neighbors": r["review_aids"],
                        "review_scope": "Read actual query text and top three old plus top two new bigram neighbours; extra retrieved neighbour rows retained for transparency"})
    review_name = f"query-only-review{suffix}.draft.jsonl"
    selected_name = f"selected.query-only{suffix}.draft.jsonl"
    write_new(root / review_name, reviews, True)
    review_map = {r["candidate_id"]: r for r in reviews}
    counts, selected = Counter(), []
    exclusions, near, _ = load_exclusions()
    for r in candidates:
        if counts[r["source"]] >= 140 or review_map[r["candidate_id"]]["decision"] != "accept":
            continue
        if near.matches(r["query_key"]):
            raise ValueError(f"Draft collision: {r['candidate_id']}")
        split = "test" if counts[r["source"]] < 40 else "train"
        selected.append({"candidate_id": r["candidate_id"], "source": r["source"], "split": split,
                         "query": r["text"], "query_key": r["query_key"], "review_reason": review_map[r["candidate_id"]]["reason"]})
        counts[r["source"]] += 1
        near.add(r["query_key"])
    if any(counts[s] != 140 for s in SOURCES):
        raise ValueError(f"Not enough accepted candidates: {counts}")
    write_new(root / selected_name, selected, True)
    report = {"status": "AWAITING_ROOT_QUERY_ONLY_REVIEW_BEFORE_FREEZE", "reviewed_count": len(reviews),
              "all_candidate_decisions": dict(Counter(f"{r['candidate_id'][8:10]}:{r['decision']}" for r in reviews)),
              "selected_counts": dict(Counter(f"{r['source']}:{r['split']}" for r in selected)),
              "last_considered_ids": {s: [r["candidate_id"] for r in selected if r["source"] == s][-1] for s in SOURCES},
              "files": {name: digest(root / name) for name in (manual_path.name, review_name, selected_name)},
              "not_executed": ["product retrieval", "new test labels", "new test scores", "test configuration selection"],
              "limits": "Model judgment only. Lexical neighbour assistance and actual reading are not proof of exhaustive synonym/semantic isolation."}
    write_new(root / f"SEMANTIC_REVIEW{suffix.upper()}_DRAFT.json", report)
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("prepare", "draft", "freeze"))
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--budget", type=int, default=220)
    parser.add_argument("--review-revision", default="")
    args = parser.parse_args()
    if args.command == "prepare":
        prepare(args.root, args.budget)
    elif args.command == "draft":
        draft(args.root, args.review_revision)
    else:
        freeze(args.root)
