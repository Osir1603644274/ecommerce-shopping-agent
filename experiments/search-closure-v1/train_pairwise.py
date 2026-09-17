"""Conditional pairwise reranker training. No CUDA/model work at import time.

gate: select the strongest old CE on v6 dev using the shared pooled lower-bound
      denominator, then count main-query Top10/Top300 ranking-error witnesses.
prepare: validate isolated selected training queries, independently frozen new
         labels, original catalog.text, and produce <=32 ordered pairs/query.
train: the ONLY command that may initialize a real model/GPU. Requires a bound
       positive gate, >=50 valid training queries, immutable inputs/config, and
       verified checkpoints. No checkpoint is selected automatically.

Training annotation manifest contract (not created by this script):
  status=FROZEN_TRAINING_QRELS, label_version=search-closure-training-v1,
  label_kind=model_silver, qrels_sha256, selected_train_sha256, rubric_sha256,
  pair_mapping_path, pair_mapping_sha256,
  review_bindings=[{collected_path,collected_sha256,input_manifest_path,
                   input_manifest_sha256,context_isolation:no_prior_conversation}]
COLLECTED/receipt/judgments follow the existing search-closure review collector.
Each final training qrel needs pair_id,query_id,query,document_id,grade. Each of
the 200 selected training queries must have 40 independently reviewed qrels.
The private mapping binds pair_id/query_id/document_id plus catalog_text_sha256
and review_document_sha256, joining actual catalog text and anonymous evidence.

Resume replays only an uncommitted epoch from the last complete checkpoint;
abandoned attempt logs and incomplete checkpoint directories are preserved.
Optimizer and Python/NumPy/CPU/CUDA RNG states are saved at each epoch boundary.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import hashlib
import importlib.metadata
import json
import math
import os
from pathlib import Path
import random
import re
import sqlite3
import sys
import tempfile
import time
import uuid

import baseline_eval as evaluation
from metrics_v2 import aggregate, build_common_pool, canonical_hash, query_metrics, require, shared_eligibility, validate_grade
from selection import NearIndex, key as query_key

HERE = Path(__file__).resolve().parent
DATA = Path("D:/agent-datasets/search-closure-v1")
ROOT = DATA / "training-preparation"
OLD = Path("D:/agent-datasets/search-stage1-v1")
SEED = 20260909
MODEL = "BAAI/bge-reranker-base"
REVISION = "2cfc18c9415c912f9d8155881c133215df768a70"
MIN_VALID_QUERIES = 50
MAX_PAIRS_PER_QUERY = 32
CE_METHODS = ("base_ce", "epoch1", "epoch2", "epoch3")
GRID_PROFILES = ("w111", "w211", "w112", "no_dense")
GRID_CE_METHODS = tuple(f"{profile}/{model}" for model in ("base", "epoch1", "epoch2", "epoch3") for profile in GRID_PROFILES)
RANKINGS_SHA256 = "8c1b6d94e0fd1cb3441b8df786bf2e9f0cbd6675a5369fca1c1905871725c7fc"


def encoded(value, *, lines=False):
    text = ("".join(json.dumps(row, ensure_ascii=False, sort_keys=True, allow_nan=False) + "\n" for row in value)
            if lines else json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n")
    return text.encode("utf-8")


def write_once(path, value, *, lines=False):
    path = Path(path).resolve()
    require(path.is_relative_to(ROOT.resolve()), f"Output outside training-preparation root: {path}")
    raw = encoded(value, lines=lines)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        require(path.read_bytes() == raw, f"Immutable artifact differs: {path}")
        return
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(prefix=".pending-", dir=path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.rename(temporary, path)
        temporary = None
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def named_output(name, section):
    require(type(name) is str and re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]*", name), "Invalid output name")
    return ROOT / section / name


def runtime_code(evidence):
    paths = [HERE / name for name in ("train_pairwise.py", "metrics_v2.py", "baseline_eval.py", "selection.py")]
    return {path.name: {"path": str(path), "sha256": evidence.file(path)} for path in paths}


def new_training_input(path):
    path = evaluation.safe_input(path)
    require(path.is_relative_to(DATA.resolve()), f"New training input must be below the new experiment root: {path}")
    return path


def select_old_ce(queries, rankings, labels, candidate_methods=CE_METHODS):
    require(candidate_methods and len(set(candidate_methods))==len(candidate_methods)
            and set(candidate_methods) <= set(rankings), "Missing/duplicate old CE methods")
    records = []
    for qid, query in queries.items():
        if query["query_cohort"] != "main":
            continue
        pool = build_common_pool(labels[qid], {method: ranks[qid] for method, ranks in rankings.items()})
        for method in rankings:
            records.append({**query, "method": method,
                            **query_metrics(rankings[method][qid], labels[qid], common_pool=pool)})
    eligibility = shared_eligibility(records, sorted(rankings))
    selected_ids = set(eligibility["eligible_query_ids"])
    if eligibility["status"] == "NO_VALID_SELECTION":
        return {"status": "NO_VALID_SELECTION", "selected_method": None, "eligibility": eligibility, "candidates": []}
    candidates = []
    for order, method in enumerate(candidate_methods):
        summary = aggregate([r for r in records if r["method"] == method and r["query_id"] in selected_ids])
        require(summary["equal_source_mean_lower"] is not None, "Common selection denominator undefined")
        candidates.append({"method": method, "earliness_order": order, "summary": summary})
    winner = min(candidates, key=lambda row: (-row["summary"]["equal_source_mean_lower"], row["earliness_order"]))
    return {"status": "OLD_CE_SELECTED", "selected_method": winner["method"], "eligibility": eligibility,
            "candidates": candidates, "selection": "max equal-source pooled nDCG lower bound; ties choose earliest checkpoint",
            "scope": "Common declared dev subset only; lower-bound selection is not evidence of true improvement."}


def load_gate_rankings(args, evidence, queries):
    supplied=getattr(args,'rankings',None);expected=getattr(args,'rankings_sha256',None)
    require(bool(supplied)==bool(expected),'Provide rankings and its SHA256 together')
    if not supplied:
        return evaluation.load_rankings(evidence.rows(evaluation.RANKINGS,RANKINGS_SHA256),queries),CE_METHODS,{'kind':'historical_eight'}
    path=new_training_input(supplied);grid=path.parent
    require(path.name=='rankings.jsonl','Expected completed grid rankings.jsonl')
    complete=evidence.json(grid/'COMPLETE.json')
    binding=evidence.json(grid/'binding.json',complete['binding_sha256'])
    models={'base','epoch1','epoch2','epoch3'}
    methods={'bm25','character','dense'}|set(GRID_CE_METHODS)|{p+'/none' for p in GRID_PROFILES}
    require(complete.get('status')=='COMPLETE' and complete.get('query_count')==40
            and complete.get('test_access') is False and complete.get('labels_read') is False,
            'Gate requires label-free complete 40-query development grid')
    require(set(binding.get('models',{}))==models and set(complete.get('methods',[]))==methods
            and len(complete['methods'])==23,'Gate grid must contain exactly the original four models and 23 methods')
    require(binding.get('queries_sha256')==evaluation.FIXED_QUERY_METADATA_SHA256
            and binding.get('test_access') is False and binding.get('labels_read') is False
            and binding.get('depth')==300 and binding.get('rrf_k')==60
            and binding.get('weights')=={'w111':[1,1,1],'w211':[2,1,1],'w112':[1,1,2],'no_dense':[1,1,0]},
            'Gate grid query/config binding differs')
    require(complete['rankings_sha256']==expected,'Completed ranking SHA differs from supplied binding')
    for item in complete['files']:
        child=evaluation.safe_input(item['path'])
        require(child.is_relative_to(grid),'Grid evidence escapes output directory')
        evidence.file(child,item['sha256'])
    for code_path,wanted in binding['code'].items():evidence.file(code_path,wanted)
    for model,record in binding['models'].items():
        wanted=OLD/('models/reranker-'+REVISION) if model=='base' else OLD/'training/run-lora-v1'/('epoch-'+model[-1])
        require(Path(record['path']).resolve()==wanted.resolve(),'Gate uses an unregistered old model path')
    evidence.file(grid/'scores.jsonl',complete['scores_sha256'])
    rankings=evaluation.load_rankings(evidence.rows(path,expected),queries,methods=complete['methods'])
    return rankings,GRID_CE_METHODS,{'kind':'current_full_index_23_config_grid','directory':str(grid),
        'complete_sha256':evidence.file(grid/'COMPLETE.json'),'binding_sha256':complete['binding_sha256'],
        'rankings_sha256':expected,'selection_tie_order':list(GRID_CE_METHODS)}


def ranking_error_witnesses(queries, rankings, labels):
    """Count queries, never pairs; unknown/unjudged documents cannot witness errors."""
    witnesses = []
    for qid in sorted(queries):
        if queries[qid]["query_cohort"] != "main":
            continue
        ranking = rankings[qid][:300]
        top = [(rank, docid, labels[qid].get(docid)) for rank, docid in enumerate(ranking[:10], 1)
               if type(labels[qid].get(docid)) is int]
        outside = [(rank, docid, labels[qid].get(docid)) for rank, docid in enumerate(ranking[10:300], 11)
                   if type(labels[qid].get(docid)) is int]
        possible = [(high, low) for high in outside for low in top if high[2] > low[2]]
        if not possible:
            continue
        high, low = min(possible, key=lambda pair: (-pair[0][2], pair[1][2], pair[0][0], pair[1][0], pair[0][1], pair[1][1]))
        witnesses.append({"query_id": qid, "query": queries[qid]["query"], "source": queries[qid]["source"],
                          "higher": {"document_id": high[1], "rank": high[0], "grade": high[2]},
                          "lower": {"document_id": low[1], "rank": low[0], "grade": low[2]},
                          "ranking_sha256": canonical_hash(rankings[qid]), "qrel_mapping_sha256": canonical_hash(labels[qid]),
                          "witness_policy": "highest known outside grade, lowest inside grade, then earlier ranks/IDs"})
    return witnesses


def gate(args):
    evidence = evaluation.Evidence()
    code = runtime_code(evidence)
    qrel_path = evaluation.safe_input(args.qrels)
    require(qrel_path.is_relative_to((evaluation.V6 / "frozen").resolve()), "Gate requires actual frozen v6 qrels")
    queries = evaluation.load_queries(evidence.rows(evaluation.QUERIES, evaluation.FIXED_QUERY_METADATA_SHA256))
    rankings,candidate_methods,ranking_source = load_gate_rankings(args,evidence,queries)
    labels, _ = evaluation.load_qrels(evidence.rows(qrel_path, args.qrels_sha256), queries)
    selection = select_old_ce(queries, rankings, labels,candidate_methods)
    witnesses = {method: ranking_error_witnesses(queries, values, labels) for method, values in rankings.items()}
    selected = selection["selected_method"]
    selected_witnesses = witnesses[selected] if selected else []
    rubric_path = HERE / "POLICY_V6.md"
    rubric_sha = evidence.file(rubric_path)
    evidence.recheck()
    report = {"status": "TRAINING_TRIGGERED" if selected and len(selected_witnesses) >= 5 else "TRAINING_NOT_TRIGGERED",
              "triggered": bool(selected and len(selected_witnesses) >= 5), "threshold_query_count": 5,
              "selected_method": selected, "selected_error_query_count": len(selected_witnesses),
              "selected_witnesses": selected_witnesses, "baseline_selection": selection,
              "diagnostic_counts_by_method": {method: len(values) for method, values in witnesses.items()},
              "diagnostic_witnesses_by_method": witnesses, "main_query_count": 33, "diagnostic_queries_counted": False,
              "code": code, "inputs": list(evidence.files.values()), "rubric_sha256": rubric_sha,
              "ranking_source":ranking_source,
              "qrels_sha256": args.qrels_sha256, "gate_scope": "A dev error-based training trigger, not a test result or promised gain.",
              "training_started": False, "test_labels_or_scores_read": False}
    write_once(named_output(args.output_name, "gates") / "gate.json", report)
    return report


def selected_queries(rows, *, split, expected_per_source):
    result = {}
    for row in rows:
        qid = evaluation.query_id(row)
        require(qid not in result, f"Duplicate selected query: {qid}")
        require(row.get("split") == split and row.get("native_split") == "train", "Selected query split mismatch")
        source = row.get("source")
        require(source in evaluation.SOURCES, "Unknown selected source")
        require(qid.startswith(f"closure-{source[:2]}-{split}-"), "Selected query namespace mismatch")
        text = row.get("text")
        require(type(text) is str and text.strip() and row.get("query_key") == query_key(text), "Selected query normalization mismatch")
        require(row.get("native_origin", {}).get("native_split") == "train", "Missing native-train query provenance")
        result[qid] = row
    require(Counter(row["source"] for row in result.values()) == {source: expected_per_source for source in evaluation.SOURCES}, "Selected query counts differ from frozen plan")
    return result


def validate_isolation(training, heldout, exclusions, dev_queries):
    index = NearIndex()
    for row in exclusions:
        value = row.get("query_key")
        require(type(value) is str and value and value == query_key(value), "Malformed query-only exclusion")
        index.add(value)
    for row in heldout.values():
        index.add(row["query_key"])
    for row in dev_queries.values():
        index.add(query_key(row["query"]))
    for qid in sorted(training):
        value = training[qid]["query_key"]
        matches = index.matches(value)
        require(not matches, f"Training query family overlap: {qid}: {matches[:1]}")
        index.add(value)
    return {"status": "QUERY_FAMILY_ISOLATION_PASS", "training_query_count": len(training),
            "heldout_query_count": len(heldout), "historical_exclusion_count": len(exclusions), "dev_query_count": len(dev_queries),
            "policy": "NFKC/casefold/punctuation+whitespace removal; exact, or trigram Jaccard>=0.85 for length>=6; query-only review bound separately",
            "scope": "Known saved families and the frozen model query-only review; not proof of exhaustive semantic deduplication."}


def validate_training_qrels(rows, selected, *, expected_pool_size=40):
    labels, pair_ids = {qid: {} for qid in selected}, {}
    for row in rows:
        qid = evaluation.query_id(row)
        require(qid in selected, "Training qrel outside selected training queries")
        require(row.get("query") == selected[qid]["text"], "Training qrel query text mismatch")
        docid = row.get("document_id")
        require(type(docid) is str and docid.startswith(selected[qid]["source"] + ":") and docid == docid.strip(), "Training qrel document mismatch")
        require(docid not in labels[qid], "Duplicate training qrel pair")
        validate_grade(row.get("grade"))
        pid = row.get("pair_id")
        require(type(pid) is str and pid and pid not in pair_ids, "Missing/duplicate review pair identity")
        pair_ids[pid] = (qid, docid)
        labels[qid][docid] = row["grade"]
    require(all(len(values) == expected_pool_size for values in labels.values()), "Training qrel pool must cover exactly the frozen 40 candidates per query")
    return labels, pair_ids


def validate_training_mapping(rows, qrel_rows):
    expected = {row["pair_id"]: row for row in qrel_rows}
    result = {}
    for row in rows:
        pid = row.get("pair_id")
        require(pid in expected and pid not in result, "Duplicate/foreign training pair mapping")
        original = expected[pid]
        require(row.get("query_id") == original["query_id"] and row.get("document_id") == original["document_id"], "Training review-to-native mapping mismatch")
        require(all(type(row.get(field)) is str and re.fullmatch(r"[0-9a-f]{64}", row[field])
                    for field in ("catalog_text_sha256", "review_document_sha256")), "Training mapping lacks text/document hash")
        result[pid] = row
    require(set(result) == set(expected), "Incomplete training pair mapping")
    return result


def validate_review_majority(manifest, qrel_rows, pair_ids, evidence, mapping):
    votes = defaultdict(dict)
    authors_by_pair = defaultdict(set)
    expected = {row["pair_id"]: row["grade"] for row in qrel_rows}
    original = {row["pair_id"]: row for row in qrel_rows}
    bindings = manifest.get("review_bindings")
    require(type(bindings) is list and bindings, "Missing independent review bindings")
    for binding in bindings:
        require(binding.get("context_isolation") == "no_prior_conversation", "Review lacks independent-context binding")
        collected_path = new_training_input(binding["collected_path"])
        collected = evidence.json(collected_path, binding["collected_sha256"])
        role = collected.get("role")
        require(role in ("A", "B", "T"), "Invalid review role")
        metadata = collected.get("model_metadata", {})
        author = metadata.get("source_thread_id")
        require(type(author) is str and author and metadata.get("model"), "Unknown review author/model")
        require(collected.get("validation", {}).get("status") == "STRUCTURE_AND_EXACT_EVIDENCE_PASS_NOT_ACCURACY", "Review evidence validation missing")
        input_path = new_training_input(binding["input_manifest_path"])
        require(binding["input_manifest_sha256"] == collected.get("input_manifest_sha256"), "Review input binding mismatch")
        packet = evidence.json(input_path, binding["input_manifest_sha256"])
        require(packet.get("files", {}).get("RUBRIC.md") == manifest["rubric_sha256"], "Review rubric differs from frozen training rule")
        require("pairs.jsonl" in packet["files"], "Review lacks bound pair inputs")
        for name, expected_hash in packet["files"].items():
            path = (input_path.parent / name).resolve()
            require(path.is_relative_to(input_path.parent), "Review input path traversal")
            evidence.file(path, expected_hash)
        packet_pairs = evidence.rows(input_path.parent / "pairs.jsonl", packet["files"]["pairs.jsonl"])
        packet_ids = set()
        for pair in packet_pairs:
            pid = pair.get("pair_id")
            require(pid in pair_ids and pid not in packet_ids, "Foreign/duplicate review input pair")
            packet_ids.add(pid)
            require(pair.get("query") == original[pid]["query"], "Review query differs from original training query")
            require(canonical_hash(pair.get("document")) == mapping[pid]["review_document_sha256"], "Review document-to-native mapping hash mismatch")
        receipt = evidence.json(collected_path.parent / "receipt.json", collected["receipt_sha256"])
        require(receipt.get("human_gold") is False and receipt.get("self_review_completed") is True, "Review incomplete or mislabeled gold")
        judgments = evidence.rows(collected_path.parent / "judgments.jsonl", collected["judgments_sha256"])
        require({row.get("pair_id") for row in judgments} == packet_ids and len(judgments) == len(packet_ids), "Judgments do not exactly cover bound review packet")
        for row in judgments:
            pid, grade = row.get("pair_id"), row.get("grade")
            require(pid in pair_ids and role not in votes[pid], "Review contains foreign/duplicate pair-role")
            validate_grade(grade)
            require(author not in authors_by_pair[pid], "Same author reused across independent pair judgments")
            authors_by_pair[pid].add(author)
            votes[pid][role] = grade
    resolutions = Counter()
    for pid, final_grade in expected.items():
        judged = votes[pid]
        require("A" in judged and "B" in judged, f"Missing complete A/B coverage: {pid}")
        if judged["A"] == judged["B"]:
            resolved = judged["A"]
            resolutions["AB_agreement"] += 1
        else:
            require("T" in judged, f"Unadjudicated A/B disagreement: {pid}")
            count = Counter(judged.values())
            grade, number = count.most_common(1)[0]
            resolved = grade if number >= 2 else "UNKNOWN"
            resolutions["third_majority" if number >= 2 else "no_majority_unknown"] += 1
        require(final_grade == resolved, f"Final training qrel is not bound review consensus: {pid}")
    return {"status": "BOUND_INDEPENDENT_REVIEW_MAJORITY_PASS", "pairs": len(expected), "resolutions": dict(resolutions),
            "scope": "Provenance, coverage, and aggregation validation, not human gold or semantic accuracy certification."}


def build_pairs(selected, labels, documents, *, minimum_valid=MIN_VALID_QUERIES):
    require(set(selected) == set(labels), "Training label/query identity mismatch")
    pairs, statistics_rows = [], []
    for qid in sorted(selected):
        query = selected[qid]
        known = {}
        for docid, grade in labels[qid].items():
            validate_grade(grade)
            require(docid in documents, "Missing original catalog.text")
            document = documents[docid]
            require(document["source"] == query["source"] and type(document.get("text")) is str and document["text"].strip(), "Invalid original document text/source")
            if type(grade) is int:
                known[docid] = grade
        candidates, indistinguishable = [], 0
        for high in sorted(known):
            for low in sorted(known):
                if known[high] <= known[low]:
                    continue
                if documents[high]["text"] == documents[low]["text"]:
                    indistinguishable += 1
                    continue
                priority = canonical_hash([SEED, "search-closure-pairwise-v1", qid, high, low])
                candidates.append({"pair_id": "train-pair-" + priority, "sampling_priority_sha256": priority,
                                   "query_id": qid, "query": query["text"], "source": query["source"],
                                   "high_document_id": high, "high_grade": known[high], "high_text": documents[high]["text"],
                                   "low_document_id": low, "low_grade": known[low], "low_text": documents[low]["text"]})
        chosen = sorted(candidates, key=lambda row: row["sampling_priority_sha256"])[:MAX_PAIRS_PER_QUERY]
        pairs.extend(chosen)
        statistics_rows.append({"query_id": qid, "source": query["source"], "known_count": len(known),
                                "unknown_count": len(labels[qid]) - len(known), "known_grades": sorted(set(known.values())),
                                "ordered_pair_count": len(candidates), "identical_text_pairs_skipped": indistinguishable,
                                "selected_pair_count": len(chosen), "valid": bool(chosen)})
    require(len({row["pair_id"] for row in pairs}) == len(pairs), "Duplicate final training pairs")
    valid = sum(row["valid"] for row in statistics_rows)
    return {"status": "READY_FOR_EXPLICIT_TRAIN" if valid >= minimum_valid else "INSUFFICIENT_TRAINING_QUERIES",
            "valid_query_count": valid, "minimum_valid_query_count": minimum_valid, "pairs": pairs, "per_query": statistics_rows,
            "sampling": "lowest sha256(seed,namespace,query_id,high_id,low_id), at most32/query; no unlabeled negatives"}


def prepare(args):
    evidence = evaluation.Evidence()
    code = runtime_code(evidence)
    gate_path = new_training_input(args.gate)
    gate_report = evidence.json(gate_path, args.gate_sha256)
    require(gate_report.get("triggered") is True and gate_report.get("status") == "TRAINING_TRIGGERED", "Training gate is not positive")
    require(gate_report["code"] == code, "Gate/prepare runtime code changed")
    for item in gate_report["inputs"]:
        evidence.file(item["path"], item["sha256"])
    selection_path = DATA / "selection/FROZEN.json"
    selection = evidence.json(selection_path, args.selection_sha256)
    require(selection.get("status") == "FROZEN_QUERY_SPLITS_ONLY" and selection.get("native_bindings_reverified") is True, "Selection is not frozen/native verified")
    prep_path = selection_path.parent / "PREPARATION.json"
    prep = evidence.json(prep_path)
    require(prep["candidate_file_sha256"] == selection["candidate_sha256"], "Selection preparation/candidate binding mismatch")
    evidence.file(selection_path.parent / "candidates.query-only.jsonl", selection["candidate_sha256"])
    evidence.file(selection_path.parent / "query-only-review.jsonl", selection["review_sha256"])
    train_path = selection_path.parent / "frozen/train.queries.jsonl"
    heldout_path = selection_path.parent / "frozen/test.queries.jsonl"
    training = selected_queries(evidence.rows(train_path, selection["files"]["train.queries.jsonl"]), split="train", expected_per_source=100)
    # The only heldout access is its already-approved query-only isolation file.
    heldout = selected_queries(evidence.rows(heldout_path, selection["files"]["test.queries.jsonl"]), split="test", expected_per_source=40)
    exclusions = evidence.rows(selection_path.parent / "exclusions.query-only.jsonl", prep["exclusion_file_sha256"])
    dev = evaluation.load_queries(evidence.rows(evaluation.QUERIES, evaluation.FIXED_QUERY_METADATA_SHA256))
    isolation = validate_isolation(training, heldout, exclusions, dev)
    annotation_path = new_training_input(args.annotation_manifest)
    annotation = evidence.json(annotation_path, args.annotation_sha256)
    require(annotation.get("status") == "FROZEN_TRAINING_QRELS" and annotation.get("label_version") == "search-closure-training-v1"
            and annotation.get("label_kind") == "model_silver", "Not newly frozen independent training qrels")
    require(annotation.get("selected_train_sha256") == selection["files"]["train.queries.jsonl"], "Training labels/selected queries binding mismatch")
    require(annotation.get("rubric_sha256") == gate_report["rubric_sha256"], "Training labels use a different rubric")
    qrel_path = new_training_input(args.training_qrels)
    qrel_rows = evidence.rows(qrel_path, annotation["qrels_sha256"])
    labels, pair_ids = validate_training_qrels(qrel_rows, training)
    mapping = validate_training_mapping(evidence.rows(new_training_input(annotation["pair_mapping_path"]), annotation["pair_mapping_sha256"]), qrel_rows)
    review = validate_review_majority(annotation, qrel_rows, pair_ids, evidence, mapping)
    catalog_path = OLD / "catalog.sqlite"
    audit_path = OLD / "data-audit.json"
    audit = evidence.json(audit_path)
    evidence.file(catalog_path, audit["catalog_sha256"])
    documents = {}
    with sqlite3.connect("file:" + catalog_path.as_posix() + "?mode=ro", uri=True) as connection:
        for docid in sorted({d for values in labels.values() for d in values}):
            rows = connection.execute("SELECT docid,source,text FROM documents WHERE docid=?", (docid,)).fetchall()
            require(len(rows) == 1, f"Catalog document identity not unique: {docid}")
            did, source, text = rows[0]
            documents[did] = {"document_id": did, "source": source, "text": text,
                              "catalog_text_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest()}
    for row in mapping.values():
        require(documents[row["document_id"]]["catalog_text_sha256"] == row["catalog_text_sha256"], "Training review mapping differs from real catalog.text")
    data = build_pairs(training, labels, documents)
    evidence.recheck()
    out = named_output(args.output_name, "prepared")
    write_once(out / "pairs.jsonl", data["pairs"], lines=True)
    write_once(out / "documents.catalog.jsonl", [documents[d] for d in sorted(documents)], lines=True)
    write_once(out / "query-statistics.jsonl", data["per_query"], lines=True)
    manifest = {"status": data["status"], "seed": SEED, "gate_path": str(gate_path), "gate_sha256": args.gate_sha256,
                "selected_query_count": len(training), "valid_query_count": data["valid_query_count"],
                "minimum_valid_query_count": MIN_VALID_QUERIES, "pair_count": len(data["pairs"]),
                "sampling": data["sampling"], "isolation": isolation, "review_validation": review,
                "code": code, "inputs": list(evidence.files.values()), "training_started": False,
                "heldout_access": "frozen query-only isolation metadata; no retrieval, labels, or scores",
                "files": {name: evaluation.sha(out / name) for name in ("pairs.jsonl", "documents.catalog.jsonl", "query-statistics.jsonl")}}
    write_once(out / "MANIFEST.json", manifest)
    return manifest


def fixed_settings(prepared_sha256, pairs_sha256, gate_sha256, model_manifest_sha256, code):
    return {"model": MODEL, "revision": REVISION, "prepared_manifest_sha256": prepared_sha256,
            "pairs_sha256": pairs_sha256, "gate_sha256": gate_sha256, "model_manifest_sha256": model_manifest_sha256,
            "code": code, "seed": SEED, "epochs": 3, "microbatch_pairs": 2, "gradient_accumulation_steps": 8,
            "effective_batch_pairs": 16, "tail_policy": "use every remaining pair; normalize final partial group by actual pair count",
            "max_length": 256, "optimizer": "AdamW", "learning_rate": 2e-5, "weight_decay": .01,
            "betas": [.9, .999], "eps": 1e-8, "gradient_clip_norm": 1.0, "scheduler": "constant",
            "lora_rank": 16, "lora_alpha": 32, "lora_dropout": .05,
            "target_modules": ["query", "value"], "modules_to_save": ["classifier"],
            "loss": "mean softplus(score_low-score_high)", "precision": "FP32 trainable parameters; CUDA BF16 autocast",
            "tf32": False, "deterministic_algorithms": True, "attention_implementation": "eager",
            "checkpoint_selection": "none; all three epochs emitted; no dev/test data enters gradient updates"}


def assert_config_unchanged(saved, expected):
    require(saved == expected, "Immutable training configuration/input/code mismatch")


def validate_prepared_pairs(rows, *, minimum_valid=MIN_VALID_QUERIES):
    seen, by_query = set(), defaultdict(list)
    for row in rows:
        pid = row.get("pair_id")
        require(type(pid) is str and pid and pid not in seen, "Duplicate/missing training sample identity")
        seen.add(pid)
        qid = row.get("query_id")
        require(type(qid) is str and "-train-" in qid, "Nontraining query in gradient input")
        require(type(row.get("high_grade")) is int and type(row.get("low_grade")) is int
                and 0 <= row["low_grade"] < row["high_grade"] <= 3, "Pair direction/grade invalid")
        require(row.get("high_document_id") != row.get("low_document_id"), "Self pair")
        for field in ("query", "high_text", "low_text"):
            require(type(row.get(field)) is str and row[field].strip(), "Empty encoder input")
        require(row["high_text"] != row["low_text"], "Identical high/low encoder input")
        expected = canonical_hash([SEED, "search-closure-pairwise-v1", qid, row["high_document_id"], row["low_document_id"]])
        require(pid == "train-pair-" + expected and row.get("sampling_priority_sha256") == expected, "Pair sampling identity hash mismatch")
        by_query[qid].append(row)
    require(all(len(v) <= MAX_PAIRS_PER_QUERY for v in by_query.values()), "More than32 pairs per query")
    require(len(by_query) >= minimum_valid, "INSUFFICIENT_TRAINING_QUERIES")
    return {"pairs": len(rows), "valid_queries": len(by_query)}


def verify_model(info, evidence):
    require(info.get("model") == MODEL and info.get("revision") == REVISION, "Model/revision is not fixed BGE base")
    directory = Path(info["path"]).resolve()
    names = [row["name"] for row in info["files"]]
    require(len(names) == len(set(names)) and {"config.json", "model.safetensors", "tokenizer_config.json", "tokenizer.json"} <= set(names), "Incomplete or duplicate model files")
    for row in info["files"]:
        path = (directory / row["name"]).resolve()
        require(path.is_relative_to(directory), "Model manifest path traversal")
        evidence.file(path, row["sha256"])
        require(path.stat().st_size == row["bytes"], "Model byte length changed")
    fixed_files = {"model.safetensors": "ced967c45fd1902eb92716c9ceeca7c95a936770ea9db611f5a841b926e33fbd",
                   "config.json": "289adf7ada1eb6b4afa7589a48a032d45a076cf2e46dcdb3b4cabc33be14f708"}
    actual_files = {row["name"]: row["sha256"] for row in info["files"]}
    require(all(actual_files[name] == expected for name, expected in fixed_files.items()), "Pinned BGE base file hash differs from fixed revision")


def verify_checkpoint(path, config_sha256, *, expected_epoch=None):
    evidence = evaluation.Evidence()
    path = Path(path).resolve()
    receipt = evidence.json(path / "complete.json")
    require(receipt.get("config_sha256") == config_sha256, "Checkpoint configuration hash mismatch")
    epoch = receipt.get("epoch")
    require(type(epoch) is int and 1 <= epoch <= 3 and (expected_epoch is None or epoch == expected_epoch), "Checkpoint epoch mismatch")
    require(type(receipt.get("global_step")) is int and receipt["global_step"] > 0, "Checkpoint global step invalid")
    files = receipt.get("files")
    require(type(files) is dict and {"adapter_model.safetensors", "adapter_config.json", "optimizer.pt", "rng.pt"} <= set(files), "Checkpoint model/optimizer/RNG incomplete")
    actual = {p.relative_to(path).as_posix() for p in path.rglob("*") if p.is_file() and p.name not in ("complete.json", "inference-state.json")}
    require(actual == set(files), "Unexpected/missing checkpoint file")
    for name, item in files.items():
        target = (path / name).resolve()
        require(target.is_relative_to(path), "Checkpoint path traversal")
        evidence.file(target, item["sha256"])
        require(target.stat().st_size == item["bytes"], "Checkpoint file size changed")
    require(receipt.get("model_sha256") == files["adapter_model.safetensors"]["sha256"]
            and receipt.get("optimizer_sha256") == files["optimizer.pt"]["sha256"]
            and receipt.get("step") == receipt["global_step"], "Legacy inference aliases disagree with full checkpoint contract")
    inference = evidence.json(path / "inference-state.json")
    require(inference == checkpoint_inference_state(path, receipt), "Inference-state contract differs from complete checkpoint")
    evidence.recheck()
    return receipt


def resume_checkpoint(run, settings):
    run = Path(run)
    config_path = run / "config.json"
    if config_path.exists():
        assert_config_unchanged(evaluation.parse_json(config_path.read_text(encoding="utf-8")), settings)
    else:
        require(not any(run.glob("checkpoints/epoch-*")) and not (run / "training-complete.json").exists(), "Checkpoint exists without immutable config")
        return None
    config_sha = evaluation.sha(config_path)
    complete = []
    for path in sorted((run / "checkpoints").glob("epoch-*")):
        require(re.fullmatch(r"epoch-[123]", path.name), "Unexpected checkpoint epoch directory")
        epoch = int(path.name[-1])
        receipt = verify_checkpoint(path, config_sha, expected_epoch=epoch)
        complete.append((epoch, path, receipt))
    require([row[0] for row in complete] == list(range(1, len(complete) + 1)), "Noncontiguous committed epochs")
    if (run / "training-complete.json").exists():
        summary = evaluation.parse_json((run / "training-complete.json").read_text(encoding="utf-8"))
        require(len(complete) == 3 and summary.get("status") == "FIXED_THREE_EPOCH_TRAINING_COMPLETE"
                and summary.get("config_sha256") == config_sha, "Final training completion disagrees with committed checkpoints")
        require(summary.get("checkpoint_receipts") == {str(epoch): evaluation.sha(path / "complete.json")
                                                       for epoch, path, _ in complete}, "Final checkpoint receipt hash mismatch")
        require(summary.get("inference_states") == {str(epoch): evaluation.sha(path / "inference-state.json")
                                                    for epoch, path, _ in complete}, "Final inference-state hash mismatch")
        require(summary.get("global_steps") == complete[-1][2]["global_step"], "Final optimizer step binding mismatch")
    return complete[-1] if complete else None


def training_completion(run, config_sha256):
    receipts, inference_states = {}, {}
    last = None
    for epoch in (1, 2, 3):
        path = Path(run) / "checkpoints" / f"epoch-{epoch}"
        last = verify_checkpoint(path, config_sha256, expected_epoch=epoch)
        receipts[str(epoch)] = evaluation.sha(path / "complete.json")
        inference_states[str(epoch)] = evaluation.sha(path / "inference-state.json")
    return {"status": "FIXED_THREE_EPOCH_TRAINING_COMPLETE", "config_sha256": config_sha256,
            "epochs": 3, "global_steps": last["global_step"], "checkpoint_receipts": receipts, "inference_states": inference_states,
            "selected_checkpoint": None, "test_labels_or_scores_read": False}


def pairwise_loss(high_scores, low_scores, *, reduction="mean"):
    import torch
    require(high_scores.shape == low_scores.shape and high_scores.numel() > 0, "Pair score shape mismatch")
    require(bool(torch.isfinite(high_scores).all() and torch.isfinite(low_scores).all()), "Nonfinite pair scores")
    values = torch.nn.functional.softplus(low_scores.float() - high_scores.float())
    require(reduction in ("mean", "sum", "none"), "Invalid pairwise reduction")
    return values.mean() if reduction == "mean" else values.sum() if reduction == "sum" else values


def checkpoint_inference_state(path, receipt):
    """The existing read-only runtime's exact inference contract; no hash cycle."""
    return {"capture": "Search closure pairwise complete epoch",
            "training_complete_sha256": evaluation.sha(Path(path) / "complete.json"),
            "files": [{"name": name, **item} for name, item in sorted(receipt["files"].items())
                      if name not in ("optimizer.pt", "rng.pt", "README.md")]}


def seal_checkpoint_files(path, epoch, global_step, config_sha, summary):
    """Shared production/test serializer for new training + old inference readers."""
    path = Path(path)
    require(not (path / "complete.json").exists() and not (path / "inference-state.json").exists(), "Checkpoint already sealed")
    files = {p.relative_to(path).as_posix(): {"sha256": evaluation.sha(p), "bytes": p.stat().st_size}
             for p in sorted(path.rglob("*")) if p.is_file()}
    require({"adapter_model.safetensors", "adapter_config.json", "optimizer.pt", "rng.pt"} <= set(files), "Checkpoint files missing before seal")
    receipt = {"epoch": epoch, "global_step": global_step, "step": global_step,
               "config_sha256": config_sha, "summary": summary, "files": files,
               "model_sha256": files["adapter_model.safetensors"]["sha256"],
               "optimizer_sha256": files["optimizer.pt"]["sha256"]}
    write_once(path / "complete.json", receipt)
    write_once(path / "inference-state.json", checkpoint_inference_state(path, receipt))
    return verify_checkpoint(path, config_sha, expected_epoch=epoch)


def save_checkpoint(run, epoch, model, tokenizer, optimizer, global_step, config_sha, summary):
    import numpy as np
    import torch
    run = Path(run)
    target = run / "checkpoints" / f"epoch-{epoch}"
    require(not target.exists(), "Committed epoch already exists")
    temporary = run / "checkpoints" / (f".pending-epoch-{epoch}-" + uuid.uuid4().hex)
    temporary.mkdir(parents=True)
    model.save_pretrained(temporary, safe_serialization=True)
    tokenizer.save_pretrained(temporary)
    torch.save({"optimizer": optimizer.state_dict(), "global_step": global_step, "epoch": epoch}, temporary / "optimizer.pt")
    state = np.random.get_state()
    rng = {"python": random.getstate(), "torch_cpu": torch.get_rng_state(), "torch_cuda": torch.cuda.get_rng_state_all(),
           "numpy": {"name": state[0], "keys": state[1].tolist(), "position": state[2], "has_gauss": state[3], "cached_gaussian": state[4]}}
    torch.save(rng, temporary / "rng.pt")
    seal_checkpoint_files(temporary, epoch, global_step, config_sha, summary)
    os.rename(temporary, target)


class RunLock:
    """OS-released Windows advisory lock; stale files do not block safe resume."""
    def __init__(self, path):
        self.path = Path(path)
        self.stream = None

    def __enter__(self):
        import msvcrt
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.stream = self.path.open("a+b")
        self.stream.seek(0, 2)
        if not self.stream.tell():
            self.stream.write(b"0")
            self.stream.flush()
        self.stream.seek(0)
        try:
            msvcrt.locking(self.stream.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            self.stream.close()
            self.stream = None
            raise RuntimeError("Another pairwise training command owns the GPU lock")
        return self

    def __exit__(self, *exc):
        import msvcrt
        if self.stream is not None:
            self.stream.seek(0)
            msvcrt.locking(self.stream.fileno(), msvcrt.LK_UNLCK, 1)
            self.stream.close()


def train(args):
    evidence = evaluation.Evidence()
    code = runtime_code(evidence)
    manifest_path = new_training_input(args.prepared_manifest)
    manifest = evidence.json(manifest_path, args.prepared_sha256)
    require(manifest.get("status") == "READY_FOR_EXPLICIT_TRAIN" and manifest.get("valid_query_count", 0) >= MIN_VALID_QUERIES, "Prepared training is insufficient")
    require(manifest["code"] == code, "Prepared/runtime code mismatch")
    for item in manifest["inputs"]:
        evidence.file(item["path"], item["sha256"])
    gate_report = evidence.json(manifest["gate_path"], manifest["gate_sha256"])
    require(gate_report.get("triggered") is True and gate_report.get("status") == "TRAINING_TRIGGERED", "Training gate lost authorization")
    for name, expected_hash in manifest["files"].items():
        path = (manifest_path.parent / name).resolve()
        require(path.is_relative_to(manifest_path.parent), "Prepared file path traversal")
        evidence.file(path, expected_hash)
    pairs = evidence.rows(manifest_path.parent / "pairs.jsonl", manifest["files"]["pairs.jsonl"])
    checked = validate_prepared_pairs(pairs)
    require(checked["pairs"] == manifest["pair_count"] and checked["valid_queries"] == manifest["valid_query_count"], "Prepared count mismatch")
    model_manifest = OLD / "models/manifest.json"
    info = evidence.json(model_manifest)["reranker"]
    verify_model(info, evidence)
    settings = fixed_settings(args.prepared_sha256, manifest["files"]["pairs.jsonl"], manifest["gate_sha256"], evaluation.sha(model_manifest), code)
    run = named_output(args.run_name, "runs")
    evidence.recheck()
    # Import and initialize CUDA only after every authorization/data binding above passed.
    with RunLock(ROOT / ".pairwise-gpu.lock"):
        os.environ.setdefault("CUBLAS_WORKSPACE_CONFIG", ":4096:8")
        import numpy as np
        import torch
        from transformers import AutoModelForSequenceClassification, AutoTokenizer
        from peft import LoraConfig, PeftModel, TaskType, get_peft_model
        require(torch.cuda.is_available() and torch.cuda.is_bf16_supported(), "Fixed training profile requires CUDA BF16")
        settings["runtime"] = {name: importlib.metadata.version(name) for name in ("torch", "transformers", "peft", "numpy", "safetensors")}
        settings["runtime"].update({"cuda": torch.version.cuda, "device_name": torch.cuda.get_device_name(0)})
        resume = resume_checkpoint(run, settings)
        if resume:
            require(resume[2]["global_step"] == resume[0] * math.ceil(len(pairs) / 16)
                    and resume[2].get("summary", {}).get("pair_count") == len(pairs), "Committed checkpoint does not match fixed sample/epoch schedule")
        write_once(run / "config.json", settings)
        config_sha = evaluation.sha(run / "config.json")
        if resume and resume[0] == 3:
            write_once(run / "training-complete.json", training_completion(run, config_sha))
            return {"status": "ALREADY_COMPLETE_NO_TRAINING_REPEATED", "run": str(run)}
        torch.set_num_threads(2)
        torch.manual_seed(SEED)
        torch.cuda.manual_seed_all(SEED)
        random.seed(SEED)
        np.random.seed(SEED)
        torch.backends.cuda.matmul.allow_tf32 = False
        torch.backends.cudnn.allow_tf32 = False
        torch.backends.cudnn.benchmark = False
        torch.use_deterministic_algorithms(True)
        tokenizer = AutoTokenizer.from_pretrained(info["path"], local_files_only=True, trust_remote_code=False)
        base = AutoModelForSequenceClassification.from_pretrained(info["path"], local_files_only=True,
                    trust_remote_code=False, attn_implementation="eager")
        if resume:
            model = PeftModel.from_pretrained(base, str(resume[1]), is_trainable=True, local_files_only=True)
        else:
            model = get_peft_model(base, LoraConfig(task_type=TaskType.SEQ_CLS, r=16, lora_alpha=32, lora_dropout=.05,
                                                   target_modules=["query", "value"], modules_to_save=["classifier"]))
        model = model.to(device="cuda", dtype=torch.float32)
        trainable = [(name, value) for name, value in model.named_parameters() if value.requires_grad]
        require(trainable and all(".lora_" in name or ".classifier.modules_to_save." in name for name, _ in trainable), "Unexpected trainable parameter outside LoRA/classifier")
        require(any(".query.lora_" in name for name, _ in trainable) and any(".value.lora_" in name for name, _ in trainable)
                and any(".classifier.modules_to_save." in name for name, _ in trainable), "Required LoRA/classifier modules absent")
        optimizer = torch.optim.AdamW([value for _, value in trainable], lr=2e-5, weight_decay=.01, betas=(.9, .999), eps=1e-8, foreach=False)
        global_step, last_epoch = 0, 0
        if resume:
            last_epoch = resume[0]
            saved = torch.load(resume[1] / "optimizer.pt", map_location="cpu", weights_only=True)
            require(saved["epoch"] == last_epoch and saved["global_step"] == resume[2]["global_step"], "Optimizer resume step mismatch")
            require(saved["global_step"] == last_epoch * math.ceil(len(pairs) / 16)
                    and resume[2]["summary"]["pair_count"] == len(pairs), "Resume checkpoint did not complete the fixed epoch schedule")
            optimizer.load_state_dict(saved["optimizer"])
            global_step = saved["global_step"]
            rng = torch.load(resume[1] / "rng.pt", map_location="cpu", weights_only=True)
            random.setstate(rng["python"])
            npstate = rng["numpy"]
            np.random.set_state((npstate["name"], np.array(npstate["keys"], dtype=np.uint32), npstate["position"], npstate["has_gauss"], npstate["cached_gaussian"]))
            torch.set_rng_state(rng["torch_cpu"])
            torch.cuda.set_rng_state_all(rng["torch_cuda"])
        write_once(run / "STARTED.json", {"status": "TRAINING_STARTED", "config_sha256": config_sha,
                   "prepared_manifest_sha256": args.prepared_sha256, "trainable_parameters": sum(v.numel() for _, v in trainable),
                   "trainable_parameter_names": [name for name, _ in trainable], "checkpoint_selection": None})
        for epoch in range(last_epoch + 1, 4):
            attempts = run / "attempts"
            attempts.mkdir(parents=True, exist_ok=True)
            attempt = attempts / f"epoch-{epoch}-attempt-{uuid.uuid4().hex}"
            attempt.mkdir()
            order = list(range(len(pairs)))
            random.Random(SEED + epoch).shuffle(order)
            write_once(attempt / "STARTED.json", {"epoch": epoch, "global_step_start": global_step,
                       "config_sha256": config_sha, "order_sha256": canonical_hash(order),
                       "resume_policy": "replay uncommitted epoch from previous complete checkpoint; preserve prior attempt artifacts"})
            model.train()
            started, total_loss, seen = time.perf_counter(), 0.0, 0
            try:
                with (attempt / "steps.jsonl").open("x", encoding="utf-8") as log:
                    for offset in range(0, len(order), 16):
                        group = order[offset:offset + 16]
                        optimizer.zero_grad(set_to_none=True)
                        group_loss = 0.0
                        for begin in range(0, len(group), 2):
                            batch = [pairs[i] for i in group[begin:begin + 2]]
                            texts = [[row["query"], row[field]] for row in batch for field in ("high_text", "low_text")]
                            inputs = tokenizer(texts, padding=True, truncation=True, max_length=256, return_tensors="pt").to("cuda")
                            with torch.autocast("cuda", dtype=torch.bfloat16):
                                scores = model(**inputs).logits.view(-1)
                                require(scores.numel() == 2 * len(batch), "Reranker must emit one score per query-document")
                                loss = pairwise_loss(scores[0::2], scores[1::2], reduction="sum") / len(group)
                            require(bool(torch.isfinite(loss)), "Nonfinite training loss")
                            loss.backward()
                            group_loss += float(loss.detach())
                        grad_norm = torch.nn.utils.clip_grad_norm_([v for _, v in trainable], 1.0, error_if_nonfinite=True)
                        optimizer.step()
                        require(all(bool(torch.isfinite(v).all()) for _, v in trainable), "Nonfinite parameter after optimizer step")
                        require(all(not torch.is_tensor(value) or bool(torch.isfinite(value).all())
                                    for state in optimizer.state.values() for value in state.values()), "Nonfinite optimizer state")
                        global_step += 1
                        seen += len(group)
                        total_loss += group_loss * len(group)
                        row = {"epoch": epoch, "global_step": global_step, "pairs_in_step": len(group), "seen_pairs": seen,
                               "loss": group_loss, "mean_loss": total_loss / seen, "grad_norm": float(grad_norm),
                               "elapsed_seconds": time.perf_counter() - started, "peak_cuda_bytes": torch.cuda.max_memory_allocated()}
                        log.write(json.dumps(row, allow_nan=False) + "\n")
                        log.flush()
                        os.fsync(log.fileno())
                summary = {"pair_count": seen, "mean_loss": total_loss / seen, "global_step": global_step,
                           "attempt": str(attempt), "steps_sha256": evaluation.sha(attempt / "steps.jsonl"),
                           "elapsed_seconds": time.perf_counter() - started}
                save_checkpoint(run, epoch, model, tokenizer, optimizer, global_step, config_sha, summary)
                write_once(attempt / "COMPLETE.json", summary)
            except BaseException as error:
                write_once(attempt / "INTERRUPTED.json", {"epoch": epoch, "global_step_observed": global_step,
                           "exception_type": type(error).__name__, "message": str(error), "committed_checkpoint_advanced": (run / "checkpoints" / f"epoch-{epoch}").exists(),
                           "resume": "verify complete checkpoints; preserve and replay only uncommitted epoch"})
                raise
        result = training_completion(run, config_sha)
        write_once(run / "training-complete.json", result)
        return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    gate_parser = commands.add_parser("gate", help="CPU: select strongest old CE and count exact main-query ranking witnesses")
    gate_parser.add_argument("--qrels", type=Path, default=evaluation.V6 / "frozen/qrels.jsonl")
    gate_parser.add_argument("--qrels-sha256", required=True)
    gate_parser.add_argument("--output-name", default="v6-old-ce")
    gate_parser.add_argument("--rankings",type=Path)
    gate_parser.add_argument("--rankings-sha256")
    prep_parser = commands.add_parser("prepare", help="CPU: validate new independent labels and prepare isolated pairwise samples")
    prep_parser.add_argument("--gate", type=Path, required=True)
    prep_parser.add_argument("--gate-sha256", required=True)
    prep_parser.add_argument("--selection-sha256", required=True)
    prep_parser.add_argument("--annotation-manifest", type=Path, required=True)
    prep_parser.add_argument("--annotation-sha256", required=True)
    prep_parser.add_argument("--training-qrels", type=Path, required=True)
    prep_parser.add_argument("--output-name", default="pairwise-v1")
    train_parser = commands.add_parser("train", help="EXPLICIT CUDA execution; all gates and artifact bindings required")
    train_parser.add_argument("--prepared-manifest", type=Path, required=True)
    train_parser.add_argument("--prepared-sha256", required=True)
    train_parser.add_argument("--run-name", default="pairwise-lora-v1")
    args = parser.parse_args(argv)
    result = {"gate": gate, "prepare": prepare, "train": train}[args.command](args)
    print(json.dumps({key: result[key] for key in ("status", "triggered", "selected_method", "selected_error_query_count", "valid_query_count", "pair_count") if key in result}, ensure_ascii=False))
    return result


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    main()
