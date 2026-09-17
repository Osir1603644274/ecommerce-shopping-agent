"""Evidence-bound complete-cost ratios; never converts missing usage to zero."""
import argparse
import json
from pathlib import Path

from agent.evaluation.context_history_strategies_v1_20260905.artifacts import file_sha, now, write_new
from .close_interrupted_cohort import digest
from .native_cost_audit import audit as native_cost_audit


def compare(base, other):
    out = {"qualityAcceptance": False, "overallDecision": "HOLD_QUALITY_REVIEW_PENDING"}
    if not base.get("dataCollectionComplete") or not other.get("dataCollectionComplete"):
        return {**out, "resourceDecision": "UNAVAILABLE_INCOMPLETE_EPISODE", "tokenSavingPercent": None, "timeGrowthPercent": None}
    bt, ot = base.get("completeNativeTokenTotal"), other.get("completeNativeTokenTotal")
    bm, om = base.get("completeConversationMs"), other.get("completeConversationMs")
    token_known = type(bt) is int and bt > 0 and type(ot) is int and ot >= 0
    time_known = isinstance(bm, (int, float)) and bm > 0 and isinstance(om, (int, float)) and om >= 0
    token_pass = ot * 100 <= bt * 90 if token_known else None
    time_pass = om * 100 <= bm * 115 + 1e-6 if time_known else None
    return {**out, "tokenSavingPercent": 100 * (1 - ot / bt) if token_known else None,
        "timeGrowthPercent": 100 * (om / bm - 1) if time_known else None,
        "tokenAtLeast10Percent": token_pass, "timeIncreaseAtMost15Percent": time_pass,
        "resourceDecision": "UNAVAILABLE_UNKNOWN_USAGE_OR_TIME" if not token_known or not time_known else
            "OBSERVED_RESOURCE_THRESHOLDS_MET_QUALITY_PENDING" if token_pass and time_pass else "OBSERVED_RESOURCE_THRESHOLDS_NOT_MET",
        "unsafeTurnsA": base.get("unsafeTerminalTurns", []), "unsafeTurnsOther": other.get("unsafeTerminalTurns", []),
        "ratioScope": "ALL_ATTEMPTED_TURNS_IN_COMPLETE_EPISODES_NOT_SUCCESSFUL_TASK_BENEFIT"}


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def load_environment_notes(cohort):
    path = cohort / "environment_notes.json"
    if not path.exists():
        return None, {}
    if path.is_symlink():
        raise ValueError("linked_environment_notes")
    notes, bindings = read(path), {str(path): file_sha(path)}
    for event in notes.get("events", []):
        for field in ("sourceJournal", "sourceResult"):
            if field in event:
                reference = Path(event[field])
                if reference.is_symlink() or not reference.resolve().is_relative_to(cohort.parent.resolve()):
                    raise ValueError("environment_reference_outside_study")
                if digest(reference) != event[field + "Sha256"]:
                    raise ValueError("environment_observation_source_changed")
                bindings[str(reference)] = file_sha(reference)
    return notes, bindings


def run(cohort, output):
    cohort = cohort.resolve()
    manifest = read(cohort / "started.json")
    terminal = read(cohort / "result.json")  # No partial-cohort headline ratios.
    if manifest.get("kind") != "SERIAL_DEVELOPMENT_COHORT_NOT_FORMAL" or len(manifest.get("jobs", [])) != 3:
        raise ValueError("unexpected_cohort")
    script = Path(manifest["script"])
    if file_sha(script) != manifest["scriptSha256"]:
        raise ValueError("script_changed")
    queries = [row["userText"] for row in read(script)["turns"]]
    records, sources, bindings = {}, [], {}
    for job in manifest["jobs"]:
        label = job["label"]
        if label not in ("A", "B", "C") or label in records:
            raise ValueError("duplicate_or_invalid_arm_label")
        directory = Path(job["output"]).resolve()
        if directory.parent != cohort:
            raise ValueError("attempt_not_direct_cohort_child")
        start = read(directory / "started.json")
        hashes = read(directory / "source_snapshot/manifest.json")["sources"]
        sources.append(hashes)
        if start["arm"] != job["arm"] or start["scriptSha256"] != manifest["scriptSha256"]:
            raise ValueError("arm_or_script_binding_mismatch")
        if any(manifest["sources"].get(name) != value for name, value in hashes.items()):
            raise ValueError("child_sources_differ_from_frozen_cohort")
        if any(digest(directory / "source_snapshot" / name) != value for name, value in hashes.items()):
            raise ValueError("frozen_child_source_content_changed")
        census_path = cohort / (label + "001_census.json")
        census = read(census_path)
        if census.get("arm") != start["arm"] or census.get("plannedTurns") != len(queries):
            raise ValueError("census_arm_or_planned_length_mismatch")
        native = native_cost_audit(directory)
        for key in ("completeNativeTokenTotal", "knownNativeTokenSubtotal", "unknownUsageCalls"):
            if census.get(key) != native[key]:
                raise ValueError("census_native_receipt_recomputation_mismatch:" + key)
        for name, value in census["sourceSha256"].items():
            if digest(directory / name) != value:
                raise ValueError("census_source_changed:" + name)
        observed_ms = 0
        for turn in range(1, census["observedTurns"] + 1):
            row = read(directory / f"turn-{turn:02d}.json")
            if row["turn"] != turn or row["query"] != queries[turn - 1]:
                raise ValueError("actual_user_script_mismatch")
            observed_ms += row["durationMs"]
        if observed_ms != census["observedTurnMsSubtotal"] or census["dataCollectionComplete"] and observed_ms != census["completeConversationMs"]:
            raise ValueError("census_time_recomputation_mismatch")
        supervisor = Path(job["supervisor"]).resolve()
        if supervisor.parent != cohort:
            raise ValueError("supervisor_not_direct_cohort_child")
        child_terminal = read(supervisor / "result.json")
        records[label] = {"arm": start["arm"], "historyPolicy": start["historyPolicy"],
            "census": census, "supervisedExitCode": child_terminal.get("childExitCode"),
            "supervisorReason": child_terminal.get("reason"), "nativeCostAudit": native}
        audit_path = cohort / (label + "001_audit.json")
        records[label]["executionAuditStatus"] = read(audit_path)["status"] if audit_path.exists() else "NO_COMPLETE_EXECUTION_AUDIT"
        for path in (directory / "started.json", directory / "source_snapshot/manifest.json", census_path,
                     Path(job["supervisor"]) / "result.json", audit_path):
            if path.exists():
                bindings[str(path)] = file_sha(path)
    if any(source != sources[0] for source in sources[1:]):
        raise ValueError("different_sut_source_cohort")
    comparisons = {label + "_vs_A": compare(records["A"]["census"], records[label]["census"]) for label in ("B", "C")}
    for path in (cohort / "started.json", cohort / "result.json", script):
        bindings[str(path)] = file_sha(path)
    environment_notes, environment_bindings = load_environment_notes(cohort)
    bindings.update(environment_bindings)
    value = {"kind": "SINGLE_FAMILY_DEVELOPMENT_COMPLETE_FLOW_REPORT", "at": now(),
        "cohort": str(cohort), "familyId": read(script).get("familyId"), "sourceFamilyCount": 1,
        "plannedTurnsPerArm": len(queries), "sameFrozenSutSources": True, "sameActualUserScript": True,
        "serialOrder": manifest["order"], "storagePolicy": manifest.get("storagePolicy", "not_declared_in_cohort_manifest"),
        "postStartEnvironmentObservations": environment_notes,
        "arms": records, "comparisons": comparisons, "cohortTerminalStatus": terminal["status"],
        "artifactHashes": bindings, "reportSourceSha256": file_sha(__file__),
        "qualityAcceptance": False, "formalAcceptance": False,
        "notes": ["Native input+output includes summary, lookups, repair and failures; unknown stays unavailable.",
            "One serial order and one synthetic developer family; no significance, causal superiority or production claim.",
            "P95 is empirical nearest-rank across dependent turns, not an independent-sample confidence interval.",
            "Judge costs are not in SUT census. Independent every-turn quality/serious-error audit still required."]}
    write_new(output, value)
    print(json.dumps({"sameSources": True, "sameUserScript": True, "comparisons": comparisons}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("cohort", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    run(args.cohort, args.output)
