"""Descriptive trajectory-level analysis, with all costs and reviewer failures."""
import hashlib
import json
from pathlib import Path
import statistics
import sys

from .trajectories import VALIDATION, VALIDATION_ATTEMPT
from .review import DIMENSIONS

OUT = Path("D:/agent-experiments/memory-natural-v1-20260907")


def load(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def quantile(values, fraction):
    values = sorted(values)
    location = (len(values)-1)*fraction
    lower = int(location)
    upper = min(lower+1, len(values)-1)
    return values[lower] + (values[upper]-values[lower])*(location-lower)


def analyze(packet_name):
    output = OUT / "analysis001"
    output.mkdir(exist_ok=False)
    manifest = load(OUT / packet_name / "manifest.json")
    ratings_dir = OUT / (packet_name + "-ratings001")
    groups = {arm: {"trajectories": [], "turns": [], "ratings": []} for arm in ("M0", "M1", "M2")}
    errors, warnings = [], []
    for identity in manifest["mappingNotGivenToReviewers"]:
        source = Path(identity["source"])
        if hashlib.sha256(source.read_bytes()).hexdigest() != identity["sourceSha256"]:
            raise RuntimeError("review source changed")
        row = load(source)
        selected = [load(ratings_dir / f"rating-{identity['sampleId']}-{reviewer}.json") for reviewer in (1, 2)]
        if any("failureType" in review for review in selected):
            errors.append({"sampleId": identity["sampleId"], "reason": "incomplete reviewer pair"})
            score = {key: None for key in DIMENSIONS}
            repeated = None
        else:
            score = {key: statistics.mean(review["rating"]["scores"][key] for review in selected) for key in DIMENSIONS}
            repeated = statistics.mean(review["rating"]["repeatedQuestions"] for review in selected)
        for review in selected:
            if "rating" in review and review["rating"]["severeErrors"]:
                warnings.append({**identity, "reviewer": review["reviewer"], "severeErrors": review["rating"]["severeErrors"]})
        groups[identity["arm"]]["turns"].append({**identity, "foregroundMs": row["foregroundMs"],
            "candidateReadyAfterAnswerMs": row["candidateReadyAfterAnswerMs"], "scores": score,
            "repeatedQuestions": repeated, "retainedCount": row["memoryLoad"]["retainedCount"],
            "runtimeStatus": row["traceSummary"].get("agentStatus"), "candidateCount": row["candidateCount"]})
    for arm, group in groups.items():
        for trajectory in VALIDATION:
            rows = [row for row in group["turns"] if row["trajectoryId"] == trajectory["id"]]
            report = load(OUT / f"flow-{trajectory['id']}-{arm}-{VALIDATION_ATTEMPT}/report.json")
            calls = report["calls"]
            known = [row["usage"] for row in calls if row.get("usage") is not None]
            group["trajectories"].append({"trajectoryId": trajectory["id"], "observedTurns": len(rows),
                "sourceChanged": report["sourceChanged"], "modelCalls": len(calls),
                "inputTokens": sum(usage["input_tokens"] for usage in known),
                "outputTokens": sum(usage["output_tokens"] for usage in known),
                "totalTokensKnown": sum(usage["input_tokens"]+usage["output_tokens"] for usage in known),
                "unknownUsageCalls": len(calls)-len(known),
                "scores": {key: statistics.mean(row["scores"][key] for row in rows)
                    if len(rows) == 3 and all(row["scores"][key] is not None for row in rows) else None for key in DIMENSIONS},
                "foregroundMs": sum(row["foregroundMs"] for row in rows),
                "repeatedQuestions": sum(row["repeatedQuestions"] for row in rows)
                    if all(row["repeatedQuestions"] is not None for row in rows) else None})
    summaries = {}
    for arm, group in groups.items():
        times = [row["foregroundMs"]/1000 for row in group["turns"]]
        summaries[arm] = {"trajectories": len(group["trajectories"]), "ratedTurns": len(times),
            "foregroundSecondsTotal": sum(times), "foregroundP50Seconds": quantile(times, .5) if times else None,
            "foregroundP95Seconds": quantile(times, .95) if times else None,
            "candidateBackgroundSecondsTotal": sum(row["candidateReadyAfterAnswerMs"] for row in group["turns"])/1000,
            "totalTokensKnown": sum(row["totalTokensKnown"] for row in group["trajectories"]),
            "unknownUsageCalls": sum(row["unknownUsageCalls"] for row in group["trajectories"]),
            "modelCalls": sum(row["modelCalls"] for row in group["trajectories"]),
            "quality": {key: statistics.mean(row["scores"][key] for row in group["trajectories"])
                if all(row["scores"][key] is not None for row in group["trajectories"]) else None for key in DIMENSIONS},
            "repeatedQuestions": sum(row["repeatedQuestions"] for row in group["trajectories"])
                if all(row["repeatedQuestions"] is not None for row in group["trajectories"]) else None,
            "observedFinalModelTurns": sum(row["finalModelObserved"] for row in group["turns"])}
    comparisons = {}
    for arm in ("M1", "M2"):
        base, treatment = summaries["M0"], summaries[arm]
        delta = {key: treatment["quality"][key]-base["quality"][key]
                 if treatment["quality"][key] is not None and base["quality"][key] is not None else None for key in DIMENSIONS}
        comparisons[arm + "_vs_M0"] = {
            "qualityDelta": delta, "maxMeanDimensionDecline": max(0, *[-value for value in delta.values()])
                if all(value is not None for value in delta.values()) else None,
            "tokenChangePercent": 100*(treatment["totalTokensKnown"]/base["totalTokensKnown"]-1),
            "foregroundTotalChangePercent": 100*(treatment["foregroundSecondsTotal"]/base["foregroundSecondsTotal"]-1),
            "repeatQuestionReduction": (1-treatment["repeatedQuestions"]/base["repeatedQuestions"])
                                       if base["repeatedQuestions"] and treatment["repeatedQuestions"] is not None else None}
    # Native call directories are unique attempts; never sum STARTED and
    # COMPLETED ledger rows twice. Cached/reasoning counters are subsets.
    campaign_calls = []
    for directory in OUT.glob("*/model_calls/call-*"):
        result = directory / "result.json"
        campaign_calls.append(load(result) if result.exists() else {"status": "NO_TERMINAL_RECEIPT", "usage": None})
    known = [row["usage"] for row in campaign_calls if row.get("usage")]
    result = {"verificationStatus": "ANALYZED", "confidence": "CAUTION_SMALL_SYNTHETIC_SAMPLE",
        "independentUnit": "complete 3-session trajectory", "independentNPerArm": 6,
        "summaries": summaries, "comparisons": comparisons, "groups": groups,
        "reviewFailures": errors, "severeReviewFlagsRequiringEvidenceTriage": warnings,
        "campaign": {"nativeCalls": len(campaign_calls),
            "totalTokensKnown": sum(row["input_tokens"]+row["output_tokens"] for row in known),
            "unknownUsageCalls": len(campaign_calls)-len(known)},
        "productionDecision": "KEEP_GLOBAL_MEMORY_OFF",
        "formalNoninferiorityTest": "NOT_PERFORMED", "humanConfirmation": "SIMULATED_NOT_OBSERVED",
        "materialLimitations": ["authored synthetic trajectories", "same-family model reviewers", "fixed product fixture",
            "native turn usage includes host overhead", "no live web/marketplace rollout",
            "foreground measurement covers projection and run_agent, not login, task-manager classification, or network delivery"]}
    (output / "metrics.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"summaries": summaries, "comparisons": comparisons,
                      "severeFlags": len(warnings), "reviewFailures": len(errors)}, ensure_ascii=True), flush=True)


if __name__ == "__main__":
    analyze(sys.argv[1])
