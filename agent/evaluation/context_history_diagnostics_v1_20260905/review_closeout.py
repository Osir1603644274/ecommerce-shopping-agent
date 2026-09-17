"""Read-only, source-bound closure of complete independent developer reviews."""
import argparse
import json
from pathlib import Path

from agent.evaluation.context_history_review_v2_20260905.run import load_samples, prepare, RUBRIC
from agent.evaluation.context_history_strategies_v1_20260905.artifacts import HERE, file_sha, sha, write_new
from agent.evaluation.context_history_strategies_v1_20260905.review_conversation import DIMENSIONS, disagreements, validate_judgment
from .native_cost_audit import audit as native_audit


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def lineage(directory):
    chain = []
    while True:
        if directory.is_symlink() or directory.resolve().parent != HERE.resolve() or directory in chain:
            raise ValueError("invalid_review_lineage")
        directory = directory.resolve()
        chain.append(directory)
        started = read(directory / "started.json")
        if started.get("kind") != "APPEND_ONLY_EXACT_REVIEW_CONTINUATION":
            if started.get("kind") != "DEVELOPMENT_LOSSLESS_SINGLE_TRAJECTORY_REVIEW_V2":
                raise ValueError("unsupported_review_origin")
            break
        parent = Path(started["parent"])
        if file_sha(parent / "failure.json") != started["parentFailureHash"]:
            raise ValueError("parent_failure_binding_mismatch")
        directory = parent
    return chain


def native_origin(directory, stem, number, chain):
    judgment = directory / f"{stem}-judgment-{number}.json"
    receipt_path = directory / f"{stem}-receipt-{number}.json"
    if not receipt_path.exists():
        return directory / f"{stem}-judge-{number}", judgment
    receipt = read(receipt_path)
    if receipt["kind"] == "NEW_ISOLATED_JUDGE":
        return directory / f"{stem}-judge-{number}", judgment
    if receipt["kind"] != "EXACT_REUSE":
        raise ValueError("unknown_judgment_receipt")
    source = Path(receipt["source"])
    if directory not in chain or source.parent not in chain[chain.index(directory) + 1:] or source.name != judgment.name:
        raise ValueError("reuse_source_not_ancestor")
    request = Path(receipt["requestSource"])
    if request != source.parent / f"{stem}-judge-{number}/call-001/request.json":
        raise ValueError("reuse_request_source_binding_mismatch")
    if file_sha(source) != receipt["sourceHash"] or file_sha(request) != receipt["requestSourceHash"]:
        raise ValueError("reuse_artifact_hash_mismatch")
    if read(judgment) != read(source):
        raise ValueError("reuse_judgment_content_mismatch")
    return native_origin(source.parent, stem, number, chain)


def summarize(scores, identifiers, turns):
    """Primary is the mean of the two mandatory judges, not selected third scores."""
    result = {}
    for identifier in identifiers:
        keys = [(identifier, turn) for turn in range(1, turns + 1)]
        if any(key not in scores[number] for number in (1, 2) for key in keys):
            raise ValueError("incomplete_double_judge_coverage")
        per_judge = {str(number): {dim: sum(scores[number][key][dim] for key in keys) / turns
            for dim in DIMENSIONS} for number in (1, 2)}
        result[identifier] = {"perJudge": per_judge,
            "twoJudgeMean": {dim: (per_judge["1"][dim] + per_judge["2"][dim]) / 2 for dim in DIMENSIONS},
            "seriousClaimTurns": sorted({key[1] for number in scores for key in keys
                if key in scores[number] and scores[number][key]["seriousErrors"]})}
    return result


def run(directory, output):
    directory = directory.resolve()
    terminal = read(directory / "result.json")  # Never unblind a running review.
    if terminal.get("status") != "DEVELOPMENT_EVERY_TURN_REVIEW_REQUIRES_CLAIM_AUDIT":
        raise ValueError("complete_review_required")
    chain = lineage(directory)
    started = read(directory / "started.json")
    if started.get("prepareOnly") or started["rubricHash"] != sha(RUBRIC):
        raise ValueError("rubric_or_execution_mismatch")
    for path, expected in started["sources"].items():
        if file_sha(path) != expected:
            raise ValueError("review_source_drift")
    mapping = read(directory / "mapping_PRIVATE_NOT_IN_JUDGE_INPUT.json")
    attempts = [Path(row["directory"]).resolve().relative_to(HERE.resolve()).as_posix() for row in mapping]
    samples, current = load_samples(attempts, started["turns"])
    if mapping != current:
        raise ValueError("complete_dialogue_evidence_binding_mismatch")
    for ancestor in chain:
        old = read(ancestor / "started.json")
        if old["sources"] != started["sources"] or old["rubricHash"] != started["rubricHash"]:
            raise ValueError("ancestor_source_or_rubric_mismatch")
        if read(ancestor / "mapping_PRIVATE_NOT_IN_JUDGE_INPUT.json") != mapping:
            raise ValueError("ancestor_dialogue_mapping_mismatch")
    origin = chain[-1]
    packet_audits = {(row["sampleId"], tuple(row["targetTurns"])): row
        for row in read(origin / "packet_audit.json")}
    costs, native_threads, unknown = {}, set(), []
    native_total = 0
    for ancestor in chain:
        for call_dir in sorted(ancestor.glob("*-judge-*")):
            if not call_dir.is_dir():
                continue
            audited = native_audit(call_dir, flat=True)
            costs[str(call_dir)] = audited
            native_total += audited["knownNativeTokenSubtotal"]
            unknown.extend([{"directory": str(call_dir), "ordinal": value} for value in audited["unknownUsageCalls"]])
            for call in audited["calls"]:
                thread = call["threadId"]
                if thread is not None:
                    if thread in native_threads:
                        raise ValueError("judge_session_reused_between_calls")
                    native_threads.add(thread)
    scores, decisions, bindings, claims = {1: {}, 2: {}, 3: {}}, [], {}, []
    turns = started["turns"]
    for start in range(1, turns + 1, started["chunkSize"]):
        targets = list(range(start, min(start + started["chunkSize"], turns + 1)))
        for sample in samples:
            identifier = sample["sampleId"]
            visible, request, audit = prepare(sample, targets)
            if audit != packet_audits[(identifier, tuple(targets))]:
                raise ValueError("packet_not_exactly_reproduced")
            stem = f"{identifier}-chunk-{start:02d}"
            judged = {}
            for number in (1, 2, 3):
                path = directory / f"{stem}-judgment-{number}.json"
                if not path.exists():
                    if number <= 2:
                        raise ValueError("mandatory_judge_missing")
                    continue
                call_dir, original = native_origin(directory, stem, number, chain)
                native = costs.get(str(call_dir))
                if native is None or native["nativeCalls"] != 1 or native["failedOrUnclosedCalls"]:
                    raise ValueError("accepted_judgment_without_completed_native_call")
                if read(call_dir / "call-001/request.json") != request:
                    raise ValueError("actual_judge_request_mismatch")
                native_result = read(call_dir / "call-001/result.json")
                value = read(path)
                if json.loads(native_result["answer"]["content"]) != value:
                    raise ValueError("judgment_not_native_answer")
                judged[number] = validate_judgment(value, [visible], targets)
                scores[number].update(judged[number])
                bindings[str(path)] = file_sha(path)
                for key, score in judged[number].items():
                    for error in score["seriousErrors"]:
                        claims.append({"sampleId": identifier, "turn": key[1], "judge": number, **error,
                            "sourceJudgment": str(path), "semanticVerdict": "PENDING"})
            disputed = disagreements(judged[1], judged[2])
            if bool(disputed) != (3 in judged):
                raise ValueError("third_judge_policy_mismatch")
            decisions.append({"sampleId": identifier, "turns": targets, "disputed": [list(key) for key in disputed]})
    if decisions != sorted(terminal["decisions"], key=lambda row: (row["turns"][0],
            [sample["sampleId"] for sample in samples].index(row["sampleId"]))):
        raise ValueError("terminal_disputes_do_not_recompute")
    summaries = summarize(scores, [sample["sampleId"] for sample in samples], turns)
    # This is post-completion unblinding, never a label in a judge request.
    arms = {}
    for row in mapping:
        arm = read(Path(row["directory"]) / "started.json")["arm"]
        if arm in arms:
            raise ValueError("duplicate_unblinded_arm")
        arms[arm] = {"sampleId": row["sampleId"], **summaries[row["sampleId"]]}
    base = arms["A_FULL_HISTORY"]["twoJudgeMean"]
    comparisons = {arm: {"meanLossVsA": {dim: base[dim] - value["twoJudgeMean"][dim] for dim in DIMENSIONS},
        "eachDimensionLossAtMostPoint5": all(base[dim] - value["twoJudgeMean"][dim] <= .5 for dim in DIMENSIONS)}
        for arm, value in arms.items() if arm != "A_FULL_HISTORY"}
    for ancestor in chain:
        for name in ("started.json", "result.json", "failure.json", "mapping_PRIVATE_NOT_IN_JUDGE_INPUT.json", "packet_audit.json"):
            path = ancestor / name
            if path.exists():
                bindings[str(path)] = file_sha(path)
    value = {"status": "MECHANICAL_REVIEW_CLOSURE_SEMANTIC_AUDIT_PENDING", "review": str(directory),
        "turnsPerArm": turns, "arms": arms, "comparisons": comparisons, "decisions": decisions,
        "seriousClaims": claims, "nativeJudgeCosts": costs, "knownJudgeTokenSubtotal": native_total,
        "completeJudgeTokenTotal": None if unknown else native_total, "unknownJudgeUsage": unknown,
        "allCallsIncludingFailedAncestorsChargedOnce": True, "sources": bindings,
        "closeoutSourceHash": file_sha(__file__), "costAuditSourceHash": file_sha(Path(__file__).with_name("native_cost_audit.py")),
        "semanticAuditRequired": True, "qualityAcceptance": False, "formalAcceptance": False,
        "aggregationRule": "Primary=mean of mandatory judges 1 and 2 across every paired turn; third judgments and every serious claim retained for dispute/semantic audit, never cherry-picked into primary scores."}
    write_new(output, value)
    print(json.dumps({"status": value["status"], "comparisons": comparisons,
        "seriousClaims": len(claims), "completeJudgeTokenTotal": value["completeJudgeTokenTotal"]}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("review", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    run(args.review, args.output)
