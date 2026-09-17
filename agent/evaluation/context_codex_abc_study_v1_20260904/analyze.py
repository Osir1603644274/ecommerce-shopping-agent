"""Audit the completed blind review and produce descriptive A/B and B/C reports.

This module is deliberately offline: it never invokes a model and it never
rewrites frozen study or review artifacts.
"""
from collections import Counter, defaultdict
import csv
import json
import math
import random
import statistics

from .common import *
from .audit import audit, require
from .judging import JROOT, SCHEMA, validate_judgment, verify_review
from .runner import NOTICE


REVIEW_OUT = JROOT / "judge_attempt001"
ANALYSIS_OUT = HERE / "attempt001" / "analysis001"
SCORE_KEYS = ("correctness", "constraints", "relevance", "usefulness")
USAGE_KEYS = (
    "input_tokens",
    "cached_input_tokens",
    "cache_write_input_tokens",
    "output_tokens",
    "reasoning_output_tokens",
)
BASE_METRICS = (
    "applicationInputTokens",
    "schemaTextTokens",
    "fixedTextTokens",
    "applicationOutputTokens",
    "contextBuildMs",
    "cliWallMs",
    "validationMs",
    "stageWallMs",
)
BOOTSTRAP_DRAWS = 10000


def percentile(values, q):
    values = sorted(float(v) for v in values)
    require(values, "percentile_empty")
    at = (len(values) - 1) * q
    lo, hi = math.floor(at), math.ceil(at)
    if lo == hi:
        return values[lo]
    return values[lo] * (hi - at) + values[hi] * (at - lo)


def numeric_summary(values):
    values = [float(v) for v in values if v is not None]
    if not values:
        return {"count": 0, "mean": None, "median": None, "p95": None,
                "min": None, "max": None, "stdev": None, "cv": None}
    mean = statistics.fmean(values)
    stdev = statistics.pstdev(values) if len(values) > 1 else 0.0
    return {
        "count": len(values),
        "mean": mean,
        "median": statistics.median(values),
        "p95": percentile(values, 0.95),
        "min": min(values),
        "max": max(values),
        "stdev": stdev,
        "cv": stdev / mean if mean else None,
    }


def fixed_row(row):
    result = dict(row)
    result["fixedTextTokens"] = row["applicationInputTokens"] + row["schemaTextTokens"]
    for key in USAGE_KEYS:
        result["native_" + key] = row.get("usage", {}).get(key) if row.get("usage") else None
    return result


def source_cluster(row):
    return "harness_all" if row["family"].startswith("harness:") else row["family"].split(":", 1)[-1]


def audit_review(sut_rows):
    """Recompute every review binding, including the private unblinding map."""
    manifest = verify_review()
    require(read(REVIEW_OUT / "complete.json")["status"] == "96_JUDGMENTS_ATTEMPTED", "review_not_complete")
    require(not (REVIEW_OUT / "stop.json").exists(), "review_stop_record")
    schedule = read(JROOT / "schedule.json")
    ledger = jsonl(REVIEW_OUT / "ledger.jsonl")
    mapping = read(JROOT / "private_mapping.json")
    runtime = read(REVIEW_OUT / "runtime.json")
    require(len(schedule) == 96 and len(ledger) == 192, "review_schedule_or_ledger_count")
    require(len(list((REVIEW_OUT / "calls").glob("*/result.json"))) == 96, "review_result_count")
    require(len(list((REVIEW_OUT / "calls").glob("*/judgment.json"))) == 96, "review_judgment_count")
    require(len(mapping) == 864, "review_mapping_count")
    require(len({(m["packetId"], m["answerId"]) for m in mapping}) == 864, "review_mapping_answer_identity")
    require(len({(m["reviewer"], m["caseId"], m["ordinal"]) for m in mapping}) == 864,
            "review_mapping_output_identity")

    map_by_answer = {(m["packetId"], m["answerId"]): m for m in mapping}
    sut_by_ordinal = {r["ordinal"]: r for r in sut_rows if r["kind"] == "study"}
    sut_thread_ids = {tid for r in sut_rows for tid in r["threadIds"]}
    ratings, review_rows, bindings, review_thread_ids = [], [], [], []

    for i, scheduled in enumerate(schedule):
        start, end = ledger[2 * i:2 * i + 2]
        require(start["event"] == "START" and end["event"] == "END", "review_ledger_order")
        for event in (start, end):
            require(all(event[k] == v for k, v in scheduled.items()), "review_ledger_binding")
        d = REVIEW_OUT / "calls" / f"{scheduled['ordinal']:03d}-{scheduled['packetId']}"
        packet_path = JROOT / "packets" / (scheduled["packetId"] + ".json")
        packet = read(packet_path)
        result = read(d / "result.json")
        request = read(d / "request.json")
        events = jsonl(d / "events.jsonl")
        judgment = read(d / "judgment.json")
        prompt = canonical(packet)

        require(all(result[k] == v for k, v in scheduled.items()), "review_result_binding")
        require(result["packetHash"] == start["packetHash"] == sha(packet), "review_packet_hash")
        require(file_sha(d / "result.json") == end["resultHash"], "review_result_hash")
        require(file_sha(d / "request.json") == end["requestHash"], "review_request_hash")
        require(file_sha(d / "judgment.json") == end["judgmentHash"], "review_judgment_hash")
        require((d / "prompt.txt").read_text(encoding="utf-8") == prompt, "review_prompt_text")
        require(request["promptHash"] == sha(prompt), "review_prompt_canonical_hash")
        require(request["applicationTextTokens"] == tokens(prompt), "review_input_tokens")
        require(request["schemaHash"] == sha(SCHEMA), "review_schema_hash")
        require(request["schemaTextTokens"] == tokens(canonical(SCHEMA)), "review_schema_tokens")
        require(read(d / "output.schema.json") == SCHEMA, "review_schema_content")
        expected_args = [runtime["codex"], *p.cli_options(runtime["overrides"]), "exec", "--ephemeral",
                         "--skip-git-repo-check", "--json", "--color", "never", "--output-schema",
                         str(d / "output.schema.json"), "-"]
        require(request["args"] == expected_args, "review_cli_args")

        finals = [e["item"]["text"] for e in events
                  if e.get("type") == "item.completed" and e.get("item", {}).get("type") == "agent_message"]
        terminals = [e for e in events if e.get("type") == "turn.completed"]
        require(result["answer"] == (finals[-1] if finals else ""), "review_answer_from_event")
        require(result["terminalCount"] == len(terminals) == 1, "review_terminal_count")
        require(result["usage"] == terminals[0].get("usage"), "review_usage_from_event")
        ids = [e["thread_id"] for e in events if e.get("type") == "thread.started"]
        require(result["threadIds"] == ids and len(ids) == 1, "review_thread_identity")
        review_thread_ids += ids
        notices = [e for e in events if e.get("item", {}).get("type") == "error"
                   and e["item"].get("message") == NOTICE]
        errors = [e for e in events if e.get("type") in {"error", "turn.failed"}
                  or (e.get("item", {}).get("type") == "error" and e not in notices)]
        tools = [e for e in events if e.get("type", "").startswith("item.")
                 and e.get("item", {}).get("type") not in {"agent_message", "reasoning", "error"}]
        require(errors == result["errors"] and notices == result["startupNotices"]
                and tools == result["toolEvents"], "review_event_classification")
        require(not tools and not result["malformedLines"] and not errors, "review_execution_boundary")
        require(result["status"] == "COMPLETED", "review_status")
        value = validate_judgment(packet, result["answer"])
        require(value == judgment, "review_judgment_content")
        require(result["validation"] == {"valid": True, "ratings": 9}, "review_local_validation")

        answer_text = {a["answerId"]: a["text"] for a in packet["anonymousAnswers"]}
        for score in value["scores"]:
            bind = map_by_answer[(packet["packetId"], score["answerId"])]
            require(bind["reviewer"] == scheduled["reviewer"], "mapping_reviewer")
            require(bind["caseId"] in packet["packetId"] or bind["caseId"] == sut_by_ordinal[bind["ordinal"]]["caseId"],
                    "mapping_case")
            require(bind["answerHash"] == sha(answer_text[score["answerId"]]), "mapping_packet_answer_hash")
            sut = sut_by_ordinal[bind["ordinal"]]
            require(bind["answerHash"] == sha(sut["answer"]), "mapping_sut_answer_hash")
            require(all(bind[k] == sut[k] for k in ("caseId", "arm", "repeat", "ordinal")), "mapping_sut_binding")
            ratings.append({**score, **{k: bind[k] for k in ("packetId", "reviewer", "caseId", "ordinal", "arm", "repeat")},
                            "stage": sut["stage"], "family": sut["family"], "sourceKind": sut["sourceKind"],
                            "lengthBin": sut["lengthBin"], "cluster": source_cluster(sut)})
        review_rows.append({**result, "applicationInputTokens": request["applicationTextTokens"],
                            "schemaTextTokens": request["schemaTextTokens"]})
        bindings.append({"ordinal": scheduled["ordinal"], "packetId": scheduled["packetId"],
                         "files": {x.name: file_sha(x) for x in d.iterdir() if x.is_file()}})

    require(len(ratings) == 864, "rating_count")
    require(len(review_thread_ids) == len(set(review_thread_ids)) == 96, "fresh_review_threads")
    require(not (set(review_thread_ids) & sut_thread_ids), "review_sut_thread_overlap")
    require(Counter((r["caseId"], r["reviewer"]) for r in ratings)
            == Counter({(f"case-{i:03d}", reviewer): 9 for i in range(1, 49) for reviewer in (1, 2)}),
            "review_case_reviewer_coverage")
    require(Counter((r["ordinal"], r["reviewer"]) for r in ratings)
            == Counter({(ordinal, reviewer): 1 for ordinal in sut_by_ordinal for reviewer in (1, 2)}),
            "review_output_coverage")

    report = {
        "at": now(),
        "status": "96_REVIEW_LEDGER_ARTIFACT_MAPPING_BINDINGS_PASS",
        "scheduledJudgments": 96,
        "ratings": 864,
        "distinctReviewThreadIds": 96,
        "distinctSutThreadIds": len(sut_thread_ids),
        "threadSetsDisjoint": True,
        "resultStatusCounts": dict(Counter(r["status"] for r in review_rows)),
        "validJudgmentCounts": dict(Counter(str(r["validation"]["valid"]).lower() for r in review_rows)),
        "toolEvents": sum(len(r["toolEvents"]) for r in review_rows),
        "malformedLines": sum(len(r["malformedLines"]) for r in review_rows),
        "reviewManifestHash": file_sha(JROOT / "manifest.json"),
        "reviewLedgerHash": file_sha(REVIEW_OUT / "ledger.jsonl"),
        "mappingHash": file_sha(JROOT / "private_mapping.json"),
        "sourceStudyLedgerHash": manifest["sourceLedgerHash"],
        "replayedModelCalls": 0,
        "bindings": bindings,
    }
    return report, ratings, review_rows


def metric_value(row, metric):
    if metric.startswith("native_"):
        return row.get("usage", {}).get(metric[len("native_"):]) if row.get("usage") else None
    return row[metric]


def arm_execution_summary(rows):
    result = {}
    for arm in ARMS:
        subset = [r for r in rows if r["kind"] == "study" and r["arm"] == arm]
        result[arm] = {
            "executions": len(subset),
            "cases": len({r["caseId"] for r in subset}),
            "metrics": {m: numeric_summary(metric_value(r, m) for r in subset)
                        for m in BASE_METRICS + tuple("native_" + x for x in USAGE_KEYS)},
        }
    return result


def case_arm_means(rows):
    grouped = defaultdict(list)
    for row in rows:
        if row["kind"] == "study":
            grouped[(row["caseId"], row["arm"])].append(row)
    result = {}
    for (case_id, arm), subset in grouped.items():
        require(len(subset) == 3, "three_repetitions")
        result[(case_id, arm)] = {
            m: statistics.fmean(v for r in subset if (v := metric_value(r, m)) is not None)
            if any(metric_value(r, m) is not None for r in subset) else None
            for m in BASE_METRICS + tuple("native_" + x for x in USAGE_KEYS)
        }
    return result


def bootstrap_cluster_pair(case_records, value_fn, seed_offset):
    clusters = defaultdict(list)
    for record in case_records:
        clusters[record["cluster"]].append(record)
    names = sorted(clusters)
    if len(names) < 2:
        return {
            "method": "source-family cluster percentile bootstrap",
            "draws": 0,
            "clusters": len(names),
            "seed": SEED + seed_offset,
            "p2_5": None,
            "p97_5": None,
            "status": "NOT_ESTIMABLE_WITH_FEWER_THAN_TWO_CLUSTERS",
        }
    rng = random.Random(SEED + seed_offset)
    estimates = []
    for _ in range(BOOTSTRAP_DRAWS):
        sampled = []
        for name in (rng.choice(names) for _ in names):
            sampled.extend(clusters[name])
        estimates.append(value_fn(sampled))
    return {
        "method": "source-family cluster percentile bootstrap",
        "draws": BOOTSTRAP_DRAWS,
        "clusters": len(names),
        "seed": SEED + seed_offset,
        "p2_5": percentile(estimates, 0.025),
        "p97_5": percentile(estimates, 0.975),
    }


def pair_block(rows, means, before, after, case_ids=None, seed_offset=0):
    case_ids = sorted(case_ids or {r["caseId"] for r in rows if r["kind"] == "study"})
    meta = {r["caseId"]: r for r in rows if r["kind"] == "study"}
    records = []
    for case_id in case_ids:
        records.append({"caseId": case_id, "cluster": source_cluster(meta[case_id]),
                        "before": means[(case_id, before)], "after": means[(case_id, after)]})
    metrics = {}
    all_metrics = BASE_METRICS + tuple("native_" + x for x in USAGE_KEYS)
    for index, metric in enumerate(all_metrics):
        available = [r for r in records if r["before"][metric] is not None and r["after"][metric] is not None]
        diffs = [r["after"][metric] - r["before"][metric] for r in available]
        entry = {
            "caseCount": len(available),
            "before": numeric_summary(r["before"][metric] for r in available),
            "after": numeric_summary(r["after"][metric] for r in available),
            "afterMinusBefore": numeric_summary(diffs),
        }
        if metric in {"applicationInputTokens", "fixedTextTokens"}:
            before_sum = sum(r["before"][metric] for r in available)
            after_sum = sum(r["after"][metric] for r in available)
            entry["totalRatioSavingsPct"] = (1 - after_sum / before_sum) * 100 if before_sum else None
            entry["clusterBootstrap95Pct"] = bootstrap_cluster_pair(
                available,
                lambda sample, m=metric: (1 - sum(x["after"][m] for x in sample)
                                          / sum(x["before"][m] for x in sample)) * 100,
                seed_offset + index,
            )
        elif metric in {"cliWallMs", "stageWallMs"}:
            entry["clusterBootstrap95AfterMinusBefore"] = bootstrap_cluster_pair(
                available,
                lambda sample, m=metric: statistics.fmean(x["after"][m] - x["before"][m] for x in sample),
                seed_offset + index,
            )
        metrics[metric] = entry
    return {"before": before, "after": after, "caseCount": len(records), "metrics": metrics}


def strata_blocks(rows, means, before, after, seed_offset):
    result = {}
    study = [r for r in rows if r["kind"] == "study"]
    case_meta = {r["caseId"]: r for r in study}
    for key in ("stage", "sourceKind", "lengthBin"):
        values = sorted({r[key] for r in study})
        result[key] = {}
        for index, value in enumerate(values):
            ids = [case_id for case_id, meta in case_meta.items() if meta[key] == value]
            result[key][value] = pair_block(rows, means, before, after, ids,
                                                   seed_offset + index * 100)
    return result


def score_summary(ratings):
    return {
        key: {**numeric_summary(r[key] for r in ratings),
              "distribution": {str(i): sum(r[key] == i for r in ratings) for i in range(5)}}
        for key in SCORE_KEYS
    }


def quality_block(ratings):
    outputs = defaultdict(list)
    for rating in ratings:
        outputs[rating["ordinal"]].append(rating)
    ratings_per_output = sorted({len(v) for v in outputs.values()})
    require(len(ratings_per_output) == 1 and ratings_per_output[0] in {1, 2}, "consistent_ratings_per_output")
    paired = ratings_per_output == [2]
    return {
        "ratings": len(ratings),
        "outputs": len(outputs),
        "ratingsPerOutput": ratings_per_output[0],
        "scores": score_summary(ratings),
        "seriousErrorRatings": sum(r["seriousError"] for r in ratings),
        "seriousErrorOutputsAtLeastOne": sum(any(r["seriousError"] for r in rs) for rs in outputs.values()),
        "seriousErrorOutputsBoth": sum(all(r["seriousError"] for r in rs) for rs in outputs.values()) if paired else None,
        "seriousErrorOutputsDisagreement": sum(len({r["seriousError"] for r in rs}) > 1 for rs in outputs.values()) if paired else None,
        "insufficientEvidenceRatings": sum(r["insufficientEvidence"] for r in ratings),
        "insufficientEvidenceOutputsAtLeastOne": sum(any(r["insufficientEvidence"] for r in rs) for rs in outputs.values()),
    }


def inter_rater(ratings):
    outputs = defaultdict(dict)
    for rating in ratings:
        outputs[rating["ordinal"]][rating["reviewer"]] = rating
    result = {"outputs": len(outputs), "scores": {}}
    for key in SCORE_KEYS:
        diffs = [abs(rs[1][key] - rs[2][key]) for rs in outputs.values()]
        result["scores"][key] = {
            "meanAbsoluteDifference": statistics.fmean(diffs),
            "exactAgreementCount": sum(x == 0 for x in diffs),
            "withinOneCount": sum(x <= 1 for x in diffs),
            "outputCount": len(diffs),
        }
    result["seriousErrorAgreementCount"] = sum(rs[1]["seriousError"] == rs[2]["seriousError"] for rs in outputs.values())
    result["insufficientEvidenceAgreementCount"] = sum(rs[1]["insufficientEvidence"] == rs[2]["insufficientEvidence"] for rs in outputs.values())
    return result


def quality_case_arm_means(ratings):
    grouped = defaultdict(list)
    for rating in ratings:
        grouped[(rating["caseId"], rating["arm"])].append(rating)
    result = {}
    for key, subset in grouped.items():
        require(len(subset) == 6, "six_quality_ratings")
        result[key] = {score: statistics.fmean(r[score] for r in subset) for score in SCORE_KEYS}
    return result


def quality_comparison(ratings, before, after, seed_offset, case_ids=None):
    means = quality_case_arm_means(ratings)
    meta = {r["caseId"]: r for r in ratings}
    case_ids = sorted(case_ids or meta)
    records = [{"caseId": cid, "cluster": meta[cid]["cluster"],
                "before": means[(cid, before)], "after": means[(cid, after)]} for cid in case_ids]
    metrics = {}
    for index, key in enumerate(SCORE_KEYS):
        diffs = [r["after"][key] - r["before"][key] for r in records]
        metrics[key] = {
            "before": numeric_summary(r["before"][key] for r in records),
            "after": numeric_summary(r["after"][key] for r in records),
            "afterMinusBefore": numeric_summary(diffs),
            "clusterBootstrap95AfterMinusBefore": bootstrap_cluster_pair(
                records,
                lambda sample, k=key: statistics.fmean(x["after"][k] - x["before"][k] for x in sample),
                seed_offset + index,
            ),
        }
    return {"before": before, "after": after, "caseCount": len(records), "metrics": metrics}


def quality_strata(ratings, before, after, seed_offset):
    meta = {r["caseId"]: r for r in ratings}
    result = {}
    for key in ("stage", "sourceKind", "lengthBin"):
        result[key] = {}
        for index, value in enumerate(sorted({r[key] for r in ratings})):
            ids = [cid for cid, row in meta.items() if row[key] == value]
            result[key][value] = quality_comparison(ratings, before, after,
                                                           seed_offset + index * 100, ids)
    return result


def compiler_behavior():
    fixtures = [read(HERE / "inputs" / f"case-{i:03d}.json") for i in range(1, 49)]
    rejected = [item for f in fixtures for item in f["compilerReceipt"]["rejectedItems"]]
    changes = []
    for f in fixtures:
        b, c = f["appInputTokens"]["B_PACK"], f["appInputTokens"]["C_PACK_COMPILER"]
        changes.append({"caseId": f["id"], "sameApplicationInputBC": f["sameApplicationInputBC"],
                        "bTokens": b, "cTokens": c, "cMinusB": c - b,
                        "selectedItems": len(f["compilerReceipt"]["selectedItems"]),
                        "rejectedItems": len(f["compilerReceipt"]["rejectedItems"]),
                        "estimatedTokens": f["compilerReceipt"]["estimatedTokens"],
                        "modelViewBytes": f["compilerReceipt"]["modelViewBytes"],
                        "compileDurationMs": f["compilerReceipt"]["compileDurationMs"]})
    return {
        "configuredEstimatedTokenBudget": 20000,
        "exactApplicationInputSameCases": sum(x["sameApplicationInputBC"] for x in changes),
        "exactApplicationInputChangedCases": sum(not x["sameApplicationInputBC"] for x in changes),
        "equalApplicationTokenCountCases": sum(x["bTokens"] == x["cTokens"] for x in changes),
        "applicationTokenCountChangedCases": sum(x["bTokens"] != x["cTokens"] for x in changes),
        "tokenDeltaCMinusB": numeric_summary(x["cMinusB"] for x in changes),
        "selectedItemCount": numeric_summary(x["selectedItems"] for x in changes),
        "rejectedItemCount": numeric_summary(x["rejectedItems"] for x in changes),
        "rejectedReasonCounts": dict(Counter(x.get("reason", "MISSING") for x in rejected)),
        "estimatedCompiledTokens": numeric_summary(x["estimatedTokens"] for x in changes),
        "modelViewBytes": numeric_summary(x["modelViewBytes"] for x in changes),
        "compileDurationMs": numeric_summary(x["compileDurationMs"] for x in changes),
        "changedCases": [x for x in changes if not x["sameApplicationInputBC"]],
    }


def control_summary(rows):
    controls = [r for r in rows if r["kind"] == "identical_input_control"]
    require(len(controls) == 12, "twelve_controls")
    return {
        "executions": 12,
        "distinctContextHashes": len({r["contextHash"] for r in controls}),
        "distinctApplicationInputTokenCounts": sorted({r["applicationInputTokens"] for r in controls}),
        "metrics": {m: numeric_summary(metric_value(r, m) for r in controls)
                    for m in ("cliWallMs", "stageWallMs") + tuple("native_" + x for x in USAGE_KEYS)},
        "interpretation": "identical application input still shows platform/native-usage variation; no constant correction applied",
    }


def review_cost_summary(rows):
    return {
        "judgments": len(rows),
        "metrics": {
            "applicationInputTokens": numeric_summary(r["applicationInputTokens"] for r in rows),
            "schemaTextTokens": numeric_summary(r["schemaTextTokens"] for r in rows),
            "cliWallMs": numeric_summary(r["cliWallMs"] for r in rows),
            **{"native_" + key: numeric_summary(r["usage"][key] for r in rows) for key in USAGE_KEYS},
        },
        "scope": "blind-review overhead only; excluded from SUT metrics",
    }


def mechanical_state(rows):
    state_rows = [r for r in rows if r["kind"] == "study" and r["stage"] == "state"]
    failures = [
        {"ordinal": r["ordinal"], "caseId": r["caseId"], "arm": r["arm"], "repeat": r["repeat"],
         "validation": r["validation"], "answerHash": sha(r["answer"])}
        for r in state_rows if not r["validation"].get("hardConditionsCorrect")
    ]
    return {
        "executions": len(state_rows),
        "byArm": {arm: dict(Counter("correct" if r["validation"].get("hardConditionsCorrect") else "incorrect_or_invalid"
                                           for r in state_rows if r["arm"] == arm)) for arm in ARMS},
        "failures": failures,
    }


def fallacy_scan(analysis):
    return [
        {"name": "Simpson paradox", "status": "CHECKED", "note": "overall directions are shown beside stage/source/length strata; strata are descriptive because stage and length are associated"},
        {"name": "ecological fallacy", "status": "LIMIT", "note": "results describe this 48-case corpus, not individual users or production traffic"},
        {"name": "Berkson bias", "status": "LIMIT", "note": "cases were selected from existing public/dev materials rather than sampled from deployment traffic"},
        {"name": "collider bias", "status": "LOW_OBSERVED", "note": "all scheduled SUT and review calls are retained; no quality-success conditioning was applied"},
        {"name": "base-rate neglect", "status": "LIMIT", "note": "serious-error rates use this corpus denominator only; production prevalence is unknown"},
        {"name": "regression to the mean", "status": "LOW_OBSERVED", "note": "treatments are interleaved repeated executions, not before/after selection on extreme outcomes"},
        {"name": "survivorship bias", "status": "CHECKED", "note": "444/444 SUT executions and 864/864 ratings are included; none were filtered for favorable results"},
        {"name": "look-elsewhere effect", "status": "LIMIT", "note": "many strata and dimensions are reported without p-value selection; all are exploratory"},
        {"name": "researcher degrees of freedom", "status": "MITIGATED_NOT_ELIMINATED", "note": "inputs, schedule, metrics and review protocol were frozen before results; no confirmatory margin or power plan was preregistered"},
        {"name": "causal overreach", "status": "LIMIT", "note": "interleaving supports a conditional fixed-stage chain comparison only, not full-Agent or production causality"},
        {"name": "reverse causality/order", "status": "MITIGATED_NOT_ELIMINATED", "note": "arm order was interleaved; identical-input controls still show platform drift, so latency remains noisy"},
    ]


def fnum(value, digits=2):
    return "—" if value is None else f"{value:.{digits}f}"


def table(headers, rows):
    lines = ["| " + " | ".join(headers) + " |", "|" + "|".join("---" for _ in headers) + "|"]
    lines += ["| " + " | ".join(str(x) for x in row) + " |" for row in rows]
    return "\n".join(lines)


def render_report(a):
    arm_labels = {"A_RAW": "A 全量原始上下文", "B_PACK": "B ContextPack+View", "C_PACK_COMPILER": "C Pack+Compiler+View"}
    quality_rows = []
    for arm in ARMS:
        q = a["quality"]["byArm"][arm]
        state = a["mechanicalState"]["byArm"][arm]
        quality_rows.append([arm_labels[arm], q["outputs"],
                             *[fnum(q["scores"][k]["mean"], 3) for k in SCORE_KEYS],
                             f"{q['seriousErrorOutputsBoth']}/{q['seriousErrorOutputsAtLeastOne']}",
                             f"{state.get('correct', 0)}/36"])
    token_rows = []
    latency_rows = []
    native_rows = []
    for arm in ARMS:
        metrics = a["executionSummary"][arm]["metrics"]
        token_rows.append([arm_labels[arm], int(round(metrics["applicationInputTokens"]["mean"])),
                           int(round(metrics["applicationInputTokens"]["mean"] * 144)),
                           int(round(metrics["schemaTextTokens"]["mean"])),
                           int(round(metrics["applicationOutputTokens"]["mean"]))])
        latency_rows.append([arm_labels[arm], fnum(metrics["contextBuildMs"]["mean"]),
                             fnum(metrics["cliWallMs"]["mean"] / 1000),
                             fnum(metrics["cliWallMs"]["median"] / 1000),
                             fnum(metrics["cliWallMs"]["p95"] / 1000),
                             fnum(metrics["stageWallMs"]["mean"] / 1000)])
        native_rows.append([arm_labels[arm], fnum(metrics["native_input_tokens"]["mean"]),
                            fnum(metrics["native_cached_input_tokens"]["mean"]),
                            fnum(metrics["native_output_tokens"]["mean"]),
                            fnum(metrics["native_reasoning_output_tokens"]["mean"])])

    comp_rows = []
    for key, label in (("AB", "A→B"), ("BC", "B→C")):
        pair = a["comparisons"][key]
        app = pair["metrics"]["applicationInputTokens"]
        fixed = pair["metrics"]["fixedTextTokens"]
        cli = pair["metrics"]["cliWallMs"]
        stage = pair["metrics"]["stageWallMs"]
        comp_rows.append([label, fnum(app["totalRatioSavingsPct"], 3), fnum(fixed["totalRatioSavingsPct"], 3),
                          fnum(cli["afterMinusBefore"]["mean"] / 1000, 3),
                          fnum(stage["afterMinusBefore"]["mean"] / 1000, 3)])

    quality_delta_rows = []
    for key, label in (("AB", "A→B"), ("BC", "B→C")):
        qc = a["quality"]["comparisons"][key]
        quality_delta_rows.append([label] + [fnum(qc["metrics"][k]["afterMinusBefore"]["mean"], 3) for k in SCORE_KEYS])

    strata_rows = []
    for comp in ("AB", "BC"):
        for dimension, groups in a["strata"][comp].items():
            for name, block in groups.items():
                strata_rows.append([comp, dimension, name, block["caseCount"],
                                    fnum(block["metrics"]["applicationInputTokens"]["totalRatioSavingsPct"], 2),
                                    fnum(block["metrics"]["cliWallMs"]["afterMinusBefore"]["mean"] / 1000, 2)])

    compiler = a["compiler"]
    control = a["identicalInputControls"]["metrics"]
    review = a["reviewCost"]["metrics"]
    inter = a["quality"]["interRater"]
    lines = [
        "# Context A/B/C 固定阶段实验报告",
        "",
        "## 结论",
        "",
        f"A→B 衡量的是 **全量原始上下文** 与 **ContextPack + 阶段 View 整体方案**，应用输入 token 总量变化为 {fnum(a['comparisons']['AB']['metrics']['applicationInputTokens']['totalRatioSavingsPct'], 3)}%；"
        f"CLI 墙钟的样本配对均值变化为 {fnum(a['comparisons']['AB']['metrics']['cliWallMs']['afterMinusBefore']['mean']/1000, 3)} 秒（负数更快）。",
        "",
        f"B→C 只衡量 Compiler 的增量作用。48 题中 {compiler['exactApplicationInputSameCases']} 题字节完全不变，"
        f"{compiler['equalApplicationTokenCountCases']} 题 token 数不变；应用输入 token 总量变化为 {fnum(a['comparisons']['BC']['metrics']['applicationInputTokens']['totalRatioSavingsPct'], 3)}%。"
        "在固定 20000 估算-token 预算下，Compiler 基本未获得可压缩空间。",
        "",
        "质量评分、机械状态校验和严重错误均完整保留。它们是同模型家族的两次独立匿名 AI 评审，不是人工 gold；本实验没有预注册非劣界值，因此只能给描述性比较，不能据此宣布质量等价或生产切换。",
        "",
        "## Material Passport",
        "",
        "- Origin Skill: academic-research-suite / experiment-agent",
        "- Origin Mode: run / validate / analyze",
        "- Origin Date: 2026-09-05 Asia/Shanghai",
        "- Verification Status: ANALYZED",
        "- Version Label: context_codex_abc_study_v1_analysis001",
        "",
        "## 完整性",
        "",
        f"- 被测：444/444 次完成；其中研究执行 432 次（48 题 × 3 组 × 3 重复）及相同输入对照 12 次。被测 threadId 444 个且互异。",
        f"- 盲评：96/96 个包完成并通过账本/请求/事件/schema/哈希审计，共 864/864 个评分；盲评 threadId 96 个且互异，并与被测 threadId 完全不重叠。",
        "- 所有预定输出均纳入，没有按质量筛选或补跑。应用输入 token 是冻结 o200k_base 文本长度；原生 usage 是整个 Codex turn，二者不可混称。",
        "",
        "## 质量护栏",
        "",
        table(["组", "输出数", "正确性", "约束", "相关性", "可用性", "严重错误 双方/至少一方", "状态机械正确"], quality_rows),
        "",
        "四维均值只是 0–4 序数评分的描述性摘要。严重错误用“双方都判定 / 至少一方判定”给出范围；分歧不由作者覆盖。",
        "",
        table(["比较", "Δ正确性", "Δ约束", "Δ相关性", "Δ可用性"], quality_delta_rows),
        "",
        f"两位评审在 432 个输出上的严重错误标记一致 {inter['seriousErrorAgreementCount']}/432；"
        f"正确性精确同分 {inter['scores']['correctness']['exactAgreementCount']}/432，平均绝对差 {fnum(inter['scores']['correctness']['meanAbsoluteDifference'], 3)}。",
        "",
        f"状态阶段严格机械校验为 {108-len(a['mechanicalState']['failures'])}/108 正确，失败或不合法 {len(a['mechanicalState']['failures'])}/108；明细已保留在 analysis.json。",
        "",
        "## Token",
        "",
        table(["组", "应用输入均值", "应用输入总量", "schema均值(单列)", "应用输出均值"], token_rows),
        "",
        table(["比较", "应用输入总量节省%", "应用+schema节省%", "ΔCLI秒", "Δ阶段秒"], comp_rows),
        "",
        "A/B 的 B 不是“只打开一个 Pack 开关”，而是当前代码中的 Pack + ContextProjector/View 整体链。B/C 的 schema 相同且单列；C 可能因 JSON 结构变化出现极小正负 token 波动。",
        "",
        "## 时延",
        "",
        table(["组", "构建均值ms", "CLI均值s", "CLI中位s", "CLI P95s", "阶段均值s"], latency_rows),
        "",
        f"相同应用输入对照 12 次：CLI 均值 {fnum(control['cliWallMs']['mean']/1000)} 秒，中位 {fnum(control['cliWallMs']['median']/1000)} 秒，"
        f"P95 {fnum(control['cliWallMs']['p95']/1000)} 秒，CV {fnum(control['cliWallMs']['cv'], 3)}。这证明平台抖动存在，因此不做常数扣减，也不把 CLI 墙钟冒充纯推理或首 token 时延。",
        "",
        "## 分层摘要",
        "",
        table(["比较", "分层", "取值", "题数", "输入节省%", "ΔCLI秒"], strata_rows),
        "",
        "阶段、来源、长度并非独立随机因素，分层只能用于定位异质性，不能作单因素因果解释。完整质量分层及 10000 次来源家族聚类重采样范围见 analysis.json。",
        "",
        "## Compiler 实际动作",
        "",
        f"- 固定预算：20000 估算 token（本实验未搜索预算）。",
        f"- 字节完全相同：{compiler['exactApplicationInputSameCases']}/48；token 数相同：{compiler['equalApplicationTokenCountCases']}/48。",
        f"- 被拒条目：{sum(compiler['rejectedReasonCounts'].values())}，原因计数 {json.dumps(compiler['rejectedReasonCounts'], ensure_ascii=False)}。",
        f"- C−B token 差：均值 {fnum(compiler['tokenDeltaCMinusB']['mean'], 3)}，范围 {fnum(compiler['tokenDeltaCMinusB']['min'], 0)} 到 {fnum(compiler['tokenDeltaCMinusB']['max'], 0)}。",
        "",
        "这说明本数据与预算组合多数触发 no-op；它不是 Compiler 永远无效的证明，也不能回答最优上下文窗口。下一轮若研究 Compiler，应先构造超过预算且包含可判定冗余/冲突的长上下文，并单独做预算曲线。",
        "",
        "## 原生 usage（辅助）",
        "",
        table(["组", "input均值", "cached均值", "output均值", "reasoning均值"], native_rows),
        "",
        "原生 input/output usage 覆盖整个 Codex turn 和不可见宿主 framing，不等于上表应用文本 token；底层请求数与重试不可见，因此不直接归因于 Compiler。",
        "",
        "## 盲评开销（不计入被测）",
        "",
        f"96 次盲评调用：CLI 总墙钟 {fnum(review['cliWallMs']['mean']*96/1000, 1)} 秒；每次输入均值 {fnum(review['applicationInputTokens']['mean'], 1)} 应用 token，"
        f"原生输出均值 {fnum(review['native_output_tokens']['mean'], 1)} token。",
        "",
        "## 偏差与外推审计",
        "",
    ]
    for item in a["fallacyScan"]:
        lines.append(f"- {item['name']} — {item['status']}：{item['note']}")
    lines += [
        "",
        "## 最终判定",
        "",
        "- **实验执行与证据链：PASS。** 采样、盲评、账本和匿名映射均完整。",
        "- **生产默认切换：HOLD。** 原因不是运行失败，而是语料来自既有开发/合成材料、评审为同模型家族 AI、完整 provider wire 不可见、时延含平台抖动，且未预注册质量非劣界值。",
        "- **最优窗口：未回答。** 本轮只测试固定 20000 估算-token Compiler 预算，没有预算搜索或完整 Agent 多轮运行。",
        "",
        "## 可复核产物",
        "",
        "- `attempt001/audit001/report.json`：444 次被测账本审计。",
        "- `attempt001/review001/audit001/report.json`：96 次盲评及匿名映射审计。",
        "- `attempt001/analysis001/analysis.json`：全部统计、分层、聚类区间和失败明细。",
        "- `attempt001/analysis001/case_comparisons.csv`：48 题 A/B/C 样本均值。",
        "- `attempt001/analysis001/review_scores.csv`：864 个匿名评分的解盲关联。",
        "",
        "固定输入可逐字复建；外部模型输出与墙钟属于随机/环境观测，本报告不通过重跑把它们伪称为精确可复现。",
    ]
    return "\n".join(lines) + "\n"


def write_csv(path, fieldnames, rows):
    with path.open("x", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def main():
    require(not ANALYSIS_OUT.exists(), "analysis_directory_exists")
    sut_audit, raw_rows = audit()
    rows = [fixed_row(r) for r in raw_rows]
    review_audit, ratings, review_rows = audit_review(rows)
    means = case_arm_means(rows)

    analysis = {
        "materialPassport": {
            "originSkill": "academic-research-suite/experiment-agent",
            "originMode": "run/validate/analyze",
            "originDate": "2026-09-05 Asia/Shanghai",
            "verificationStatus": "ANALYZED",
            "versionLabel": "context_codex_abc_study_v1_analysis001",
        },
        "generatedAt": now(),
        "scope": "48 fixed-stage cases; not a full Agent loop, production benchmark, or optimal-window search",
        "integrity": {"sutAuditStatus": sut_audit["status"], "reviewAuditStatus": review_audit["status"],
                      "sutAuditHash": file_sha(HERE / "attempt001/audit001/report.json"),
                      "reviewManifestHash": review_audit["reviewManifestHash"],
                      "studyManifestHash": file_sha(HERE / "inputs/manifest.json")},
        "executionSummary": arm_execution_summary(rows),
        "comparisons": {
            "AB": pair_block(rows, means, "A_RAW", "B_PACK", seed_offset=1000),
            "BC": pair_block(rows, means, "B_PACK", "C_PACK_COMPILER", seed_offset=2000),
        },
        "strata": {
            "AB": strata_blocks(rows, means, "A_RAW", "B_PACK", 3000),
            "BC": strata_blocks(rows, means, "B_PACK", "C_PACK_COMPILER", 5000),
        },
        "quality": {
            "byArm": {arm: quality_block([r for r in ratings if r["arm"] == arm]) for arm in ARMS},
            "byReviewer": {str(reviewer): quality_block([r for r in ratings if r["reviewer"] == reviewer]) for reviewer in (1, 2)},
            "byStage": {stage: quality_block([r for r in ratings if r["stage"] == stage]) for stage in sorted({r["stage"] for r in ratings})},
            "bySourceKind": {kind: quality_block([r for r in ratings if r["sourceKind"] == kind]) for kind in sorted({r["sourceKind"] for r in ratings})},
            "byLengthBin": {length: quality_block([r for r in ratings if r["lengthBin"] == length]) for length in sorted({r["lengthBin"] for r in ratings})},
            "comparisons": {
                "AB": quality_comparison(ratings, "A_RAW", "B_PACK", 7000),
                "BC": quality_comparison(ratings, "B_PACK", "C_PACK_COMPILER", 8000),
            },
            "strata": {
                "AB": quality_strata(ratings, "A_RAW", "B_PACK", 9000),
                "BC": quality_strata(ratings, "B_PACK", "C_PACK_COMPILER", 11000),
            },
            "interRater": inter_rater(ratings),
            "judgeType": "two independent fresh same-model-family AI judgments; not human gold or cross-model",
        },
        "mechanicalState": mechanical_state(rows),
        "compiler": compiler_behavior(),
        "identicalInputControls": control_summary(rows),
        "reviewCost": review_cost_summary(review_rows),
        "fallacyScan": [],
        "decision": {
            "studyEvidenceChain": "PASS",
            "productionDefaultSwitch": "HOLD",
            "optimalContextWindow": "NOT_ANSWERED",
            "reason": "descriptive fixed-stage reused corpus, same-family AI judges, no preregistered quality non-inferiority margin, provider wire incomplete",
        },
    }
    analysis["fallacyScan"] = fallacy_scan(analysis)

    ANALYSIS_OUT.mkdir()
    review_audit_dir = JROOT / "audit001"
    require(not review_audit_dir.exists(), "review_audit_directory_exists")
    review_audit_dir.mkdir()
    write_new(review_audit_dir / "report.json", review_audit)
    write_new(ANALYSIS_OUT / "analysis.json", analysis)

    case_rows = []
    for case_id in sorted({r["caseId"] for r in rows if r["kind"] == "study"}):
        meta = next(r for r in rows if r["caseId"] == case_id and r["kind"] == "study")
        row = {"caseId": case_id, "stage": meta["stage"], "sourceKind": meta["sourceKind"],
               "family": meta["family"], "cluster": source_cluster(meta), "lengthBin": meta["lengthBin"]}
        for arm in ARMS:
            for metric in BASE_METRICS:
                row[f"{arm}.{metric}"] = means[(case_id, arm)][metric]
        case_rows.append(row)
    case_fields = list(case_rows[0])
    write_csv(ANALYSIS_OUT / "case_comparisons.csv", case_fields, case_rows)
    rating_fields = ["packetId", "reviewer", "caseId", "ordinal", "arm", "repeat", "stage", "sourceKind",
                     "family", "cluster", "lengthBin", "answerId", *SCORE_KEYS, "seriousError",
                     "insufficientEvidence", "issues", "rationale"]
    rating_csv = [{**r, "issues": json.dumps(r["issues"], ensure_ascii=False)} for r in ratings]
    write_csv(ANALYSIS_OUT / "review_scores.csv", rating_fields, rating_csv)
    (ANALYSIS_OUT / "REPORT.zh-CN.md").write_text(render_report(analysis), encoding="utf-8")

    hashes = {str(x.relative_to(ANALYSIS_OUT)): file_sha(x) for x in ANALYSIS_OUT.iterdir() if x.is_file()}
    write_new(ANALYSIS_OUT / "manifest.json", {
        "at": now(), "status": "ANALYSIS_ARTIFACTS_WRITTEN", "analysisCodeHash": file_sha(__file__),
        "reviewAuditHash": file_sha(review_audit_dir / "report.json"), "files": hashes,
        "modelCallsDuringAnalysis": 0,
    })
    print(canonical({"status": "ANALYSIS_COMPLETE", "output": str(ANALYSIS_OUT),
                     "ABApplicationTokenSavingsPct": analysis["comparisons"]["AB"]["metrics"]["applicationInputTokens"]["totalRatioSavingsPct"],
                     "BCApplicationTokenSavingsPct": analysis["comparisons"]["BC"]["metrics"]["applicationInputTokens"]["totalRatioSavingsPct"],
                     "reviewAudit": review_audit["status"]}), flush=True)


if __name__ == "__main__":
    main()
