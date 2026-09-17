"""Integrity and query isolation boundary tests; not semantic accuracy tests."""
import tempfile
from pathlib import Path
import unittest
from collections import Counter
import hashlib
import json
import unicodedata

from selection import NearIndex, QueryNeighbors, key, priority, write_new


def audit_frozen(root=Path("D:/agent-datasets/search-closure-v1/selection")):
    """Independent all-selected source-line and brute-force lexical audit."""
    def sha(path):
        h = hashlib.sha256()
        with Path(path).open("rb") as source:
            for block in iter(lambda: source.read(4 * 1024 * 1024), b""):
                h.update(block)
        return h.hexdigest()

    def read(path):
        return [json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines() if line.strip()]

    frozen = json.loads((root / "FROZEN.json").read_text(encoding="utf-8"))
    approval = json.loads((root / "ROOT_QUERY_REVIEW.json").read_text(encoding="utf-8"))
    assert frozen["root_review_sha256"] == sha(root / "ROOT_QUERY_REVIEW.json")
    assert approval["approved_review_sha256"] == frozen["review_sha256"] == sha(root / "query-only-review.jsonl")
    assert approval["approved_selected_draft_sha256"] == sha(root / approval["approved_selected_draft"])
    selected = []
    for split, count in (("test", 80), ("train", 200)):
        path = root / "frozen" / f"{split}.queries.jsonl"
        assert sha(path) == frozen["files"][path.name]
        part = read(path)
        assert len(part) == count and all(r["split"] == split for r in part)
        selected.extend(part)
    assert len({r["query_id"] for r in selected}) == 280
    assert len({r["candidate_id"] for r in selected}) == 280
    assert len({r["query_key"] for r in selected}) == 280
    assert Counter(f"{r['source']}:{r['split']}" for r in selected) == {
        "kuaisearch:test": 40, "multicpr:test": 40, "kuaisearch:train": 100, "multicpr:train": 100}
    approved = read(root / approval["approved_selected_draft"])
    assert {(r["candidate_id"], r["source"], r["split"], r["query"]) for r in approved} == {
        (r["candidate_id"], r["source"], r["split"], r["text"]) for r in selected}
    reviews = {r["candidate_id"]: r for r in read(root / "query-only-review.jsonl")}
    for r in selected:
        assert r["native_split"] == r["native_origin"]["native_split"] == "train"
        assert r["review_decision"] == reviews[r["candidate_id"]]
        assert r["review_decision"]["decision"] == "accept"
        norm = "".join(c for c in unicodedata.normalize("NFKC", r["text"]).casefold()
                       if not c.isspace() and not unicodedata.category(c).startswith("P"))
        assert norm == r["query_key"]
    native_counts = {}
    for source in ("kuaisearch", "multicpr"):
        part = [r for r in selected if r["source"] == source]
        paths = {r["native_origin"]["path"] for r in part}
        assert len(paths) == 1
        path = Path(next(iter(paths)))
        targets = {r["native_origin"]["source_line"]: r for r in part}
        assert len(targets) == 140
        seen = set()
        file_hash = hashlib.sha256()
        with path.open("rb") as file:
            for line_no, raw in enumerate(file, 1):
                file_hash.update(raw)
                if line_no not in targets:
                    continue
                r = targets[line_no]
                origin = r["native_origin"]
                assert hashlib.sha256(raw).hexdigest() == origin["source_row_sha256"]
                decoded = raw.decode("utf-8-sig" if line_no == 1 else "utf-8").rstrip("\r\n")
                if source == "kuaisearch":
                    row = json.loads(decoded)
                    assert row["split"] == "train"
                    assert str(row["session_id"]) == origin["native_id"]
                    assert {k: row[k] for k in ("session_id", "user_id", "time_index")} == origin["native_identity"]
                    text = row["query"]
                else:
                    native_id, text = decoded.split("\t", 1)
                    assert native_id == origin["native_id"] == origin["native_identity"]["native_query_id"]
                assert text == origin["original_query"]
                assert " ".join(text.split()) == r["text"]
                seen.add(line_no)
        assert seen == set(targets)
        assert all(r["native_origin"]["source_sha256"] == file_hash.hexdigest() for r in part)
        native_counts[source] = {"matched_selected_source_rows": len(seen), "scanned_rows_for_full_hash": line_no,
                                 "source_sha256": file_hash.hexdigest()}
    old_keys = [r["query_key"] for r in read(root / "exclusions.query-only.jsonl")]
    old_set = set(old_keys)
    # Tuple grams and all-pairs scan do not reuse selection.NearIndex.
    def grams(s):
        return set(zip(s, s[1:], s[2:]))
    all_grams = [(s, grams(s)) for s in old_keys]
    for i, r in enumerate(selected):
        q, g = r["query_key"], grams(r["query_key"])
        assert q not in old_set
        if len(q) >= 6:
            for old, og in all_grams:
                if min(len(g), len(og)) < .85 * max(len(g), len(og)):
                    continue
                union = g | og
                assert not union or len(g & og) / len(union) < .85, (q, old)
        for other in selected[:i]:
            oq, og = other["query_key"], grams(other["query_key"])
            union = g | og
            assert q != oq
            assert len(q) < 6 or not union or len(g & og) / len(union) < .85, (q, oq)
    result = {"status": "PASS_FROZEN_QUERY_ONLY_AUDIT", "selected_queries": len(selected),
              "counts": dict(Counter(f"{r['source']}:{r['split']}" for r in selected)),
              "source_verification": native_counts, "old_keys_bruteforce_checked": len(old_keys),
              "root_review_binding_verified": True, "selected_set_matches_approved_v2_draft": True,
              "candidate_reviews_match_final_rows": True, "cross_split_exact_or_threshold_violations": 0,
              "test_product_retrieval_labels_scores_executed": False,
              "frozen_manifest_sha256": sha(root / "FROZEN.json"),
              "audit_code_sha256": sha(Path(__file__)),
              "limits": "Integrity and lexical isolation checks do not establish semantic perfection, relevance quality, or full historical/pretraining isolation."}
    write_new(root / "VALIDATION.json", result)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return result


class SelectionTest(unittest.TestCase):
    def test_normalization_unicode_and_punctuation(self):
        self.assertEqual(key(" ＡＢＣ，女 鞋！"), "abc女鞋")
        self.assertEqual(key("原神【流萤】抱枕"), "原神流萤抱枕")

    def test_short_queries_only_exact(self):
        near = NearIndex()
        near.add("女鞋")
        self.assertTrue(near.matches("女鞋"))
        self.assertFalse(near.matches("女凉鞋"))

    def test_long_query_near_boundary(self):
        near = NearIndex()
        near.add("abcdefghijklmnopqrst")
        self.assertTrue(near.matches("abcdefghijklmnopqrstu"))
        self.assertTrue(near.matches("abcdefghijklmnopqrstxyz"))  # 18 / 21
        self.assertFalse(near.matches("abcdefghijklmnopqrstxyza"))  # 18 / 22

    def test_priority_determinism_and_source_separation(self):
        self.assertEqual(priority("kuaisearch", "女鞋"), priority("kuaisearch", "女鞋"))
        self.assertNotEqual(priority("kuaisearch", "女鞋"), priority("multicpr", "女鞋"))

    def test_artifact_resume_and_drift(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "a.json"
            write_new(path, {"v": 1})
            write_new(path, {"v": 1})
            with self.assertRaises(ValueError):
                write_new(path, {"v": 2})

    def test_neighbors_are_aids_no_self(self):
        neighbors = QueryNeighbors(["女鞋冬季", "女鞋秋季", "手机"])
        result = neighbors.nearest("女鞋冬季")
        self.assertEqual(result[0]["query_key"], "女鞋秋季")
        self.assertTrue(all(r["query_key"] != "女鞋冬季" for r in result))


if __name__ == "__main__":
    unittest.main()
