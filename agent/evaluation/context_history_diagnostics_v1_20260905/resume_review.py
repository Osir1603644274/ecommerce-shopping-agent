"""Append-only continuation of an interrupted review, with exact-input reuse."""
import argparse
import asyncio
import json
from pathlib import Path

from agent.evaluation.context_history_review_v2_20260905.run import load_samples, prepare, RUBRIC
from agent.evaluation.context_history_strategies_v1_20260905.artifacts import HERE, file_sha, sha, write_new
from agent.evaluation.context_history_strategies_v1_20260905.review_conversation import DIMENSIONS, disagreements, validate_judgment
from agent.evaluation.context_history_strategies_v1_20260905.subscription import SubscriptionClient


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


async def run(parent, output):
    parent, output = parent.resolve(), output.resolve()
    if parent.parent != HERE.resolve() or output.parent != HERE.resolve() or output.exists():
        raise ValueError("invalid_append_only_review_path")
    old = read(parent / "started.json")
    sources = old["sources"]
    def check_sources():
        if any(file_sha(Path(path)) != digest for path, digest in sources.items()):
            raise ValueError("parent_review_source_drift")
    check_sources()
    if old["rubricHash"] != sha(RUBRIC) or old.get("prepareOnly"):
        raise ValueError("incompatible_parent_review")
    mapping = read(parent / "mapping_PRIVATE_NOT_IN_JUDGE_INPUT.json")
    attempts = [Path(row["directory"]).resolve().relative_to(HERE.resolve()).as_posix() for row in mapping]
    samples, current_mapping = load_samples(attempts, old["turns"])
    if current_mapping != mapping:
        raise ValueError("parent_complete_dialogue_or_evidence_hash_mismatch")
    old_audits = {(row["sampleId"], tuple(row["targetTurns"])): row
                  for row in read(parent / "packet_audit.json")}
    jobs = []
    for start in range(1, old["turns"] + 1, old["chunkSize"]):
        targets = list(range(start, min(start + old["chunkSize"], old["turns"] + 1)))
        for sample in samples:
            visible, request, audit = prepare(sample, targets)
            if audit != old_audits[(sample["sampleId"], tuple(targets))]:
                raise ValueError("parent_packet_not_exactly_reproduced")
            jobs.append((sample["sampleId"], targets, visible, request))
    # Preflight every reusable judgment before starting even one new call.
    reusable = {}
    for identifier, targets, visible, request in jobs:
        for number in (1, 2, 3):
            stem = f"{identifier}-chunk-{targets[0]:02d}"
            path = parent / f"{stem}-judgment-{number}.json"
            if not path.exists():
                continue
            request_path = parent / f"{stem}-judge-{number}/call-001/request.json"
            if read(request_path) != request:
                raise ValueError("old_judge_request_not_exact")
            value = read(path)
            scores = validate_judgment(value, [visible], targets)
            reusable[(identifier, targets[0], number)] = (value, scores, path, request_path)
    check_sources()
    output.mkdir()
    write_new(output / "started.json", {**old, "kind": "APPEND_ONLY_EXACT_REVIEW_CONTINUATION",
        "parent": str(parent), "parentFailureHash": file_sha(parent / "failure.json"),
        "resumeSourceHash": file_sha(Path(__file__)), "reusableJudgments": len(reusable),
        "judgeConcurrency": 1, "policy": "Reuse only exact source, complete dialogue, evidence, rubric and request. One new call per missing judge; original failure retained and judge costs separate."})
    write_new(output / "mapping_PRIVATE_NOT_IN_JUDGE_INPUT.json", mapping)
    scores_by_judge = {1: {}, 2: {}, 3: {}}
    receipts, decisions = [], []
    async def judge(job, number):
        identifier, targets, visible, request = job
        check_sources()
        key = (identifier, targets[0], number)
        stem = f"{identifier}-chunk-{targets[0]:02d}"
        if key in reusable:
            value, scores, source, request_source = reusable[key]
            receipt = {"kind": "EXACT_REUSE", "source": str(source), "sourceHash": file_sha(source),
                       "requestSource": str(request_source), "requestSourceHash": file_sha(request_source)}
        else:
            client = SubscriptionClient(output / f"{stem}-judge-{number}", max_calls=1,
                application_input_budget=old["packetBudget"])
            response = await client.chat.completions.create(**request)
            value = json.loads(response.choices[0].message.content)
            scores = validate_judgment(value, [visible], targets)
            receipt = {"kind": "NEW_ISOLATED_JUDGE", "requestHash": sha(request)}
        write_new(output / f"{stem}-judgment-{number}.json", value)
        receipt.update({"sampleId": identifier, "targets": targets, "judge": number})
        receipts.append(receipt)
        write_new(output / f"{stem}-receipt-{number}.json", receipt)
        scores_by_judge[number].update(scores)
        print(json.dumps({"sample": identifier, "targets": targets, "judge": number, "kind": receipt["kind"]}), flush=True)
        return scores
    for job in jobs:
        first, second = await judge(job, 1), await judge(job, 2)
        disputed = disagreements(first, second)
        decisions.append({"sampleId": job[0], "turns": job[1], "disputed": [list(key) for key in disputed]})
        if disputed:
            await judge(job, 3)
    check_sources()
    aggregates = []
    for sample in samples:
        keys = [(sample["sampleId"], turn) for turn in range(1, old["turns"] + 1)]
        aggregates.append({"sampleId": sample["sampleId"], "perJudge": [{"judge": number,
            "meanAllTurns": {dim: sum(scores_by_judge[number][key][dim] for key in keys) / old["turns"] for dim in DIMENSIONS},
            "seriousClaimTurns": [key[1] for key in keys if scores_by_judge[number][key]["seriousErrors"]]} for number in (1, 2)]})
    write_new(output / "result.json", {"status": "DEVELOPMENT_EVERY_TURN_REVIEW_REQUIRES_CLAIM_AUDIT",
        "turnsPerSample": old["turns"], "aggregates": aggregates, "decisions": decisions,
        "reusedJudgments": sum(r["kind"] == "EXACT_REUSE" for r in receipts),
        "newJudgments": sum(r["kind"] == "NEW_ISOLATED_JUDGE" for r in receipts),
        "allIndependentAndAnonymous": True, "humanGold": False, "formalAcceptance": False,
        "semanticClaimAuditRequired": True, "sourceDrift": []})


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("parent", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    try:
        asyncio.run(run(args.parent, args.output))
    except Exception as exc:
        if args.output.exists() and not (args.output / "failure.json").exists():
            write_new(args.output / "failure.json", {"type": type(exc).__name__, "error": str(exc), "formalAcceptance": False})
        raise
