"""Recompute fixed development rankings against new v6 labels, without a GPU.

Dry-run reads ranking IDs and fixed query metadata only. Formal evaluation
requires the dry-run binding and the externally supplied frozen-v6 qrel hash.
Neither mode substitutes an older qrel version. Input and code bytes are bound
and rechecked before producing immutable output.

Later retrieval adapters may emit the same row shape:
  {method, query_id, source, ranking: [document_id, ...], ranking_sha256}
or replace ranking with results: [{document_id, rank, score?}, ...]. Results
must already be in rank order; this evaluator never sorts using supplied score.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sys
import tempfile

from metrics_v2 import (VERSION, SOURCES, aggregate, build_common_pool, canonical_hash,
                        paired_interval_bootstrap, query_metrics, require,
                        shared_eligibility, validate_grade, validate_ranking)

HERE = Path(__file__).resolve().parent
ROOT = Path("D:/agent-datasets/search-closure-v1/evaluation")
V6 = Path("D:/agent-datasets/search-stage1-dev-revision-v6")
RANKINGS = Path("D:/agent-datasets/search-stage1-dev-revision-v3/evaluation/frozen-results/rankings.jsonl")
QUERIES = Path("D:/agent-datasets/search-stage1-dev-revision-v5/frozen/queries.jsonl")
FIXED_QUERY_METADATA_SHA256 = "462beddd9fe013710f98c6c9d8a6d83df621660852c7adbe3c6a94b10f78588c"
METHODS = ("bm25", "character", "dense", "rrf", "base_ce", "epoch1", "epoch2", "epoch3")


def sha(path):
    value = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def _no_duplicate_keys(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, f"Duplicate JSON key: {key}")
        result[key] = value
    return result


def parse_json(raw):
    return json.loads(raw, object_pairs_hook=_no_duplicate_keys,
                      parse_constant=lambda token: (_ for _ in ()).throw(ValueError(f"Nonfinite JSON: {token}")))


class Evidence:
    def __init__(self):
        self.files = {}

    def file(self, path, expected=None):
        path = Path(path).resolve()
        require(path.is_file(), f"Missing input: {path}")
        actual = sha(path)
        if expected is not None:
            require(type(expected) is str and re.fullmatch(r"[0-9a-f]{64}", expected), "Invalid expected SHA256")
            require(actual == expected, f"Input hash mismatch: {path}")
        item = {"path": str(path), "sha256": actual, "bytes": path.stat().st_size}
        require(str(path) not in self.files or self.files[str(path)] == item, f"Input changed during read: {path}")
        self.files[str(path)] = item
        return actual

    def json(self, path, expected=None):
        self.file(path, expected)
        return parse_json(Path(path).read_text(encoding="utf-8-sig"))

    def rows(self, path, expected=None):
        self.file(path, expected)
        rows = []
        with Path(path).open(encoding="utf-8-sig") as stream:
            for number, line in enumerate(stream, 1):
                if not line.strip():
                    continue
                row = parse_json(line)
                require(type(row) is dict, f"Nonobject row: {path}:{number}")
                rows.append(row)
        return rows

    def recheck(self):
        for item in list(self.files.values()):
            self.file(item["path"], item["sha256"])


def safe_input(path):
    path = Path(path).resolve()
    require(not any("test" in part.lower() for part in path.parts), f"Test input prohibited: {path}")
    return path


def write_once(path, value, *, jsonl=False):
    path = Path(path).resolve()
    require(path.is_relative_to(ROOT.resolve()), f"Output outside experiment evaluation root: {path}")
    if jsonl:
        raw = "".join(json.dumps(r, ensure_ascii=False, sort_keys=True, allow_nan=False) + "\n" for r in value)
    else:
        raw = json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n"
    payload = raw.encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        require(path.read_bytes() == payload, f"Immutable output already exists with different content: {path}")
        return
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(prefix=".pending-", dir=path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        # Windows rename fails if target exists, avoiding replacement of a frozen result.
        os.rename(temporary, path)
        temporary = None
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def query_id(row):
    values = [row[key] for key in ("qid", "query_id") if key in row]
    require(values and all(type(x) is str and x for x in values), "Missing/invalid query identity")
    require(len(set(values)) == 1, "Conflicting qid/query_id aliases")
    return values[0]


def source_from_qid(qid):
    match = re.fullmatch(r"s1-(ku|mu)-dev-[0-9a-f]{16}", qid)
    require(match is not None, f"Non-development or invalid query ID: {qid}")
    return "kuaisearch" if match.group(1) == "ku" else "multicpr"


def load_queries(rows, *, fixed_development=True):
    result = {}
    for row in rows:
        qid = query_id(row)
        require(qid not in result, f"Duplicate query identity: {qid}")
        source = source_from_qid(qid) if fixed_development else row["source"]
        require(source in SOURCES and row.get("source", source) == source, "Query source mismatch")
        text = row.get("query", row.get("text"))
        require(type(text) is str and text.strip(), "Missing query text")
        require("query" not in row or "text" not in row or row["query"] == row["text"], "Conflicting query text")
        cohort = row.get("query_cohort", row.get("cohort"))
        require(cohort in ("main", "diagnostic"), "Invalid query cohort")
        require("query_cohort" not in row or "cohort" not in row or row["query_cohort"] == row["cohort"], "Conflicting query cohort")
        result[qid] = {"query_id": qid, "query": text, "source": source, "query_cohort": cohort}
    if fixed_development:
        require(len(result) == 40, "Expected fixed 40 development queries")
        require(Counter(q["source"] for q in result.values()) == {"kuaisearch": 20, "multicpr": 20}, "Development source count drift")
        require(Counter(q["query_cohort"] for q in result.values()) == {"main": 33, "diagnostic": 7}, "Development cohort count drift")
    return result


def ranking_from_row(row):
    require(("ranking" in row) != ("results" in row), "Provide exactly one ranking representation")
    if "ranking" in row:
        ranking = row["ranking"]
    else:
        require(type(row["results"]) is list, "Invalid results array")
        ranking = []
        for index, result in enumerate(row["results"], 1):
            require(type(result) is dict, "Invalid ranked result")
            require(type(result.get("rank")) is int and result["rank"] == index, "Rank gap, duplicate, or unordered results")
            ids = [result[key] for key in ("document_id", "docid") if key in result]
            require(ids and len(set(ids)) == 1, "Missing/conflicting result document ID")
            if "score" in result:
                score = result["score"]
                require(type(score) in (int, float) and math.isfinite(score), "Invalid ranking score")
            ranking.append(ids[0])
    validate_ranking(ranking)
    require(row.get("ranking_sha256") == canonical_hash(ranking), "Ranking hash mismatch")
    return ranking


def load_rankings(rows, queries, *, methods=METHODS):
    require(methods and len(set(methods)) == len(methods), "Empty/duplicate methods")
    result = {method: {} for method in methods}
    for row in rows:
        qid, method = query_id(row), row.get("method")
        require(qid in queries, f"Ranking query outside fixed development cohort: {qid}")
        require(method in result, f"Unexpected retrieval method: {method}")
        require(qid not in result[method], f"Duplicate method/query ranking: {method}/{qid}")
        require(row.get("source") == queries[qid]["source"], "Ranking source mismatch")
        ranking = ranking_from_row(row)
        require(all(d.startswith(row["source"] + ":") for d in ranking), "Cross-source document ranking")
        result[method][qid] = ranking
    for method, rankings in result.items():
        require(set(rankings) == set(queries), f"Methods do not share identical query set: {method}")
    return result


def load_qrels(rows, queries):
    labels = {qid: {} for qid in queries}
    causes = {qid: {} for qid in queries}
    for row in rows:
        qid = query_id(row)
        require(qid in queries, f"Qrel query outside fixed cohort: {qid}")
        query = queries[qid]
        require(row.get("query") == query["query"], "Qrel query text mismatch")
        require(row.get("query_cohort") == query["query_cohort"], "Qrel cohort mismatch")
        require(row.get("source", query["source"]) == query["source"], "Qrel source mismatch")
        docid = row.get("document_id")
        require(type(docid) is str and docid.startswith(query["source"] + ":") and docid == docid.strip(), "Invalid qrel document identity")
        require(docid not in labels[qid], f"Duplicate qrel pair: {qid}/{docid}")
        grade = row.get("grade")
        validate_grade(grade)
        labels[qid][docid] = grade
        if grade == "UNKNOWN":
            values = row.get("unknown_causes", ["not_recorded"])
            require(type(values) is list and all(type(x) is str and x for x in values), "Invalid qrel unknown causes")
            causes[qid][docid] = values or ["not_recorded"]
    require(all(labels.values()), "One or more fixed queries has no qrels")
    return labels, causes


def evaluate(queries, rankings, labels, causes, *, repetitions=10000, seed=20260909, baseline="base_ce"):
    require(baseline in rankings, "Baseline method missing")
    require(set(labels) == set(queries), "Qrel query coverage mismatch")
    common = {qid: build_common_pool(labels[qid], {m: rankings[m][qid] for m in rankings}) for qid in queries}
    records = []
    for method in sorted(rankings):
        for qid in sorted(queries):
            records.append({**queries[qid], "method": method,
                            "ranking_sha256": canonical_hash(rankings[method][qid]),
                            **query_metrics(rankings[method][qid], labels[qid], common_pool=common[qid],
                                            unknown_causes=causes[qid])})
    summary, comparisons, eligibility, conditional = {}, {}, {}, {}
    for cohort in ("main", "diagnostic", "all"):
        selected = [r for r in records if cohort == "all" or r["query_cohort"] == cohort]
        eligibility[cohort] = shared_eligibility(selected, sorted(rankings))
        common_ids = set(eligibility[cohort]["eligible_query_ids"])
        conditional[cohort] = {
            "status": eligibility[cohort]["status"],
            "eligibility_manifest_sha256": eligibility[cohort]["manifest_sha256"],
            "query_ids": sorted(common_ids), "methods": {}, "comparisons": {},
        }
        summary[cohort] = {method: aggregate([r for r in selected if r["method"] == method]) for method in sorted(rankings)}
        base = {r["query_id"]: r for r in selected if r["method"] == baseline}
        comparisons[cohort] = {method: paired_interval_bootstrap(base,
            {r["query_id"]: r for r in selected if r["method"] == method}, repetitions=repetitions, seed=seed)
            for method in sorted(rankings) if method != baseline}
        if eligibility[cohort]["status"] != "NO_VALID_SELECTION":
            conditional_rows = [r for r in selected if r["query_id"] in common_ids]
            conditional[cohort]["methods"] = {
                method: aggregate([r for r in conditional_rows if r["method"] == method]) for method in sorted(rankings)}
            conditional_base = {r["query_id"]: r for r in conditional_rows if r["method"] == baseline}
            conditional[cohort]["comparisons"] = {method: paired_interval_bootstrap(conditional_base,
                {r["query_id"]: r for r in conditional_rows if r["method"] == method}, repetitions=repetitions, seed=seed)
                for method in sorted(rankings) if method != baseline}
    return {"per_query": records, "summary": summary, "comparisons": comparisons,
            "shared_eligibility": eligibility, "common_conditional": conditional,
            "common_pools": [{"query_id": qid, "document_ids": common[qid], "sha256": canonical_hash(common[qid])}
                             for qid in sorted(common)]}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("dry-run", "evaluate"))
    parser.add_argument("--rankings", type=Path, default=RANKINGS)
    parser.add_argument("--queries", type=Path, default=QUERIES)
    parser.add_argument("--methods", nargs="+", default=list(METHODS))
    parser.add_argument("--baseline", default="base_ce")
    parser.add_argument("--output-name", default="baseline-v6")
    parser.add_argument("--binding", type=Path)
    parser.add_argument("--qrels", type=Path, default=V6 / "frozen/qrels.jsonl")
    parser.add_argument("--qrels-sha256")
    args = parser.parse_args(argv)
    require(re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]*", args.output_name), "Invalid output name")
    evidence = Evidence()
    ranking_path, query_path = safe_input(args.rankings), safe_input(args.queries)
    code = {path.name: {"path": str(path), "sha256": evidence.file(path)}
            for path in (HERE / "baseline_eval.py", HERE / "metrics_v2.py")}
    if args.mode == "evaluate":
        require(args.binding is not None, "Formal evaluation requires --binding from dry-run")
        binding = evidence.json(safe_input(args.binding))
        require(binding["status"] == "DRY_RUN_INPUTS_VALIDATED_NO_LABELS_READ", "Invalid dry-run binding")
        require(binding["code"] == code and binding["metrics_version"] == VERSION, "Evaluator code/version hash mismatch")
        require(binding["methods"] == list(args.methods), "Method set changed after binding")
        require(binding["rankings"]["path"] == str(ranking_path) and binding["queries"]["path"] == str(query_path), "Bound input path mismatch")
        ranking_hash, query_hash = binding["rankings"]["sha256"], binding["queries"]["sha256"]
    else:
        require(args.binding is None and args.qrels_sha256 is None, "Dry-run does not consume label bindings")
        ranking_hash = query_hash = None
    queries = load_queries(evidence.rows(query_path, query_hash))
    fixed_queries = load_queries(evidence.rows(QUERIES, FIXED_QUERY_METADATA_SHA256))
    require(queries == fixed_queries, "Fixed development query identity, text, or cohort assignment drift")
    rankings = load_rankings(evidence.rows(ranking_path, ranking_hash), queries, methods=args.methods)
    inputs = {
        "metrics_version": VERSION, "methods": list(args.methods), "code": code,
        "queries": evidence.files[str(query_path)], "rankings": evidence.files[str(ranking_path)],
        "query_count": len(queries), "ranking_count": sum(len(v) for v in rankings.values()),
        "cohort_counts": dict(Counter(q["query_cohort"] for q in queries.values())),
        "source_counts": dict(Counter(q["source"] for q in queries.values())),
        "query_identity_sha256": canonical_hash([queries[qid] for qid in sorted(queries)]),
        "depth_counts": dict(sorted(Counter(str(len(r)) for values in rankings.values() for r in values.values()).items())),
        "seed": 20260909, "bootstrap_repetitions": 10000,
    }
    output = ROOT / args.output_name
    if args.mode == "dry-run":
        evidence.recheck()
        report = {**inputs, "status": "DRY_RUN_INPUTS_VALIDATED_NO_LABELS_READ", "qrels_read": False,
                  "formal_evaluation_run": False, "model_inference_run": False,
                  "scope": "Only dev query metadata and ranked document IDs were validated."}
        write_once(output / "dry-run.json", report)
        print(json.dumps({"status": report["status"], "path": str(output / "dry-run.json"),
                          "query_count": len(queries), "ranking_count": inputs["ranking_count"]}, ensure_ascii=False))
        return report
    qrel_path = safe_input(args.qrels)
    require(qrel_path.is_relative_to((V6 / "frozen").resolve()), "Only new v6 frozen qrels are authorized; old qrels cannot stand in")
    require(args.qrels_sha256 is not None, "Formal evaluation requires an externally verified --qrels-sha256")
    labels, causes = load_qrels(evidence.rows(qrel_path, args.qrels_sha256), queries)
    result = evaluate(queries, rankings, labels, causes, baseline=args.baseline)
    evidence.recheck()
    report = {**inputs, "status": "DEVELOPMENT_DESCRIPTIVE_EVALUATION_COMPLETE", "qrels_read": True,
              "formal_evaluation_run": True, "model_inference_run": False,
              "input_evidence": list(evidence.files.values()), "qrels": evidence.files[str(qrel_path)],
              "baseline": args.baseline, "label_kind": "model_silver", "new_test_read": False,
              "summary": result["summary"], "comparisons": result["comparisons"],
              "shared_eligibility": result["shared_eligibility"], "common_conditional": result["common_conditional"],
              "scope": "Fixed development cohort only. Lower-bound differences are not real improvement bounds. No test or GPU use."}
    write_once(output / "per-query.jsonl", result["per_query"], jsonl=True)
    write_once(output / "common-pools.jsonl", result["common_pools"], jsonl=True)
    write_once(output / "coverage-manifest.json", {"qrels": evidence.files[str(qrel_path)],
        "queries": inputs["queries"], "rankings": inputs["rankings"], "code": code,
        "cohorts": result["shared_eligibility"]})
    report["outputs"] = {name: {"sha256": sha(output / name), "bytes": (output / name).stat().st_size}
                         for name in ("per-query.jsonl", "common-pools.jsonl", "coverage-manifest.json")}
    write_once(output / "report.json", report)
    write_once(output / "complete.json", {"status": report["status"], "report_sha256": sha(output / "report.json"),
                                         "qrels_sha256": args.qrels_sha256, "code": code})
    print(json.dumps({"status": report["status"], "path": str(output / "report.json")}, ensure_ascii=False))
    return report


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    main()
