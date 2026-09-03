"""Agentic Scenario Lab V0 — evaluator-first contract foundation.

This module builds the executable contract surface for the Scenario Lab V0
track.  It does NOT run the production Agent, does not call a real LLM, and
does not load a 3000-item catalog.  Its job is to make the evaluator fail
closed before any formal scenario collection is written.

Three files are physically split per scenario set:

    scenario.input.jsonl   — runner-readable public execution input.
    oracle.private.jsonl   — scorer-only success oracle.
    fault.private.jsonl    — scorer-only fault plan / fixture contract.

`load_runner_input` is the only runner-facing entry point and never accepts
or exposes private oracle/fault objects.  `ScenarioSet` is scorer-only.

Deterministic scoring is separated by track (`language_e2e` vs
`infrastructure_fault`).  Search-track metrics (Hit@K / Recall / NDCG) have no
path into the agentic aggregate report, which always declares
`exploratory=true` and `statisticalSignificanceClaimed=false`.
"""

from __future__ import annotations

import base64
import hashlib
import json
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from jsonschema import Draft202012Validator

SCHEMAS_DIR = Path(__file__).resolve().parent / "schemas"

SCHEMA_VERSION = "agentic-scenario-lab-v0"
INPUT_SCHEMA_VERSION = "agentic-scenario-input-v0"
ORACLE_SCHEMA_VERSION = "agentic-scenario-oracle-private-v0"
FAULT_SCHEMA_VERSION = "agentic-scenario-fault-private-v0"
PREDICTION_SCHEMA_VERSION = "agentic-scenario-prediction-v0"
REPORT_SCHEMA_VERSION = "agentic-scenario-score-report-v0"

TRACK_LANGUAGE = "language_e2e"
TRACK_INFRA = "infrastructure_fault"
TRACKS = (TRACK_LANGUAGE, TRACK_INFRA)

INJECTION_RESTART = "checkpoint_restart"
INJECTION_EXACT_REPLAY = "exact_replay"
INJECTION_MUTATED_REPLAY = "mutated_replay"
INJECTION_REVISION_CONFLICT = "revision_conflict"
INJECTION_CHECKPOINT_LOSS = "redis_checkpoint_loss"
INJECTION_POINTS = (
    INJECTION_RESTART,
    INJECTION_EXACT_REPLAY,
    INJECTION_MUTATED_REPLAY,
    INJECTION_REVISION_CONFLICT,
    INJECTION_CHECKPOINT_LOSS,
)

# Mapping from oracle faultExpectation key to the runner-owned attemptKind that
# must carry the observation for that injection (V0 strict cardinality: exactly
# one observation per required injection, no unplanned attemptKind).
ATTEMPT_KIND_BY_INJECTION = {
    INJECTION_RESTART: "restart_recovery",
    INJECTION_EXACT_REPLAY: "exact_replay",
    INJECTION_MUTATED_REPLAY: "mutated_replay",
    INJECTION_REVISION_CONFLICT: "revision_conflict",
    INJECTION_CHECKPOINT_LOSS: "checkpoint_loss_fail_closed",
}

# The safety outcome boolean each required injection must declare.  V0 fixes
# these to True at load (the safety gate is a positive gate: restart must
# recover, exact replay must be zero-tool/no-delta, etc.); an expected=False
# or missing outcome fails closed because absence of a counter-observation is
# not evidence.
SAFETY_OUTCOME_FIELD_BY_INJECTION = {
    INJECTION_RESTART: "expectedRecoveredAfterRestart",
    INJECTION_EXACT_REPLAY: "expectedExactReplayZeroTool",
    INJECTION_MUTATED_REPLAY: "expectedMutatedReplayRejected",
    INJECTION_REVISION_CONFLICT: "expectedRevisionDivergenceFailClosed",
    INJECTION_CHECKPOINT_LOSS: "expectedCheckpointLossFailClosed",
}

METRIC_TERMINAL = "terminal"
METRIC_HARD_CONSTRAINT = "hard_constraint"
METRIC_EVIDENCE = "evidence"
METRIC_UNSUPPORTED_CLAIM = "unsupported_claim"
METRIC_CLARIFICATION = "clarification"
METRIC_TOOL_COUNT = "tool_count"
METRIC_DUPLICATE_TOOL = "duplicate_tool"
METRIC_RESTART = "restart"
METRIC_EXACT_REPLAY = "exact_replay"
METRIC_MUTATED_REPLAY = "mutated_replay"
METRIC_REVISION_CONFLICT = "revision_conflict"
METRIC_CHECKPOINT_LOSS = "checkpoint_loss"
METRIC_FAMILIES = (
    METRIC_TERMINAL,
    METRIC_HARD_CONSTRAINT,
    METRIC_EVIDENCE,
    METRIC_UNSUPPORTED_CLAIM,
    METRIC_CLARIFICATION,
    METRIC_TOOL_COUNT,
    METRIC_DUPLICATE_TOOL,
    METRIC_RESTART,
    METRIC_EXACT_REPLAY,
    METRIC_MUTATED_REPLAY,
    METRIC_REVISION_CONFLICT,
    METRIC_CHECKPOINT_LOSS,
)

TERMINAL_CLASSES = ("SUCCESS", "FAILURE", "CLARIFICATION_REQUIRED", "ABSTAIN_OR_EXPLAIN")

# V0 checkpoint-loss is a fail-closed gate, not a recovery gate: when
# redis_checkpoint_loss is required the scenario must end in a controlled
# fail-closed terminal class.  SUCCESS is not a valid loss-recovery outcome and
# is rejected at load; the allowed set is fixed to these two classes.
CHECKPOINT_LOSS_TERMINAL_CLASSES = ("FAILURE", "ABSTAIN_OR_EXPLAIN")

# V0 restart recovery is a SUCCESS-only gate: when checkpoint_restart is
# required, the restart attempt's terminalCode and the scenario's shared
# expected terminal must both be exactly SUCCESS.  A FAILURE, ABSTAIN or
# CLARIFICATION terminal is a failure or abstention, never a recovery, and any
# string other than SUCCESS fails the derived gate.
RESTART_RECOVERY_TERMINAL = "SUCCESS"

SCENARIO_ID_PATTERN = re.compile(r"^ASL-V0-(?:LNG|INF)-[A-Za-z0-9_-]{1,64}$")

SCHEMA_FILENAMES = {
    "agentic_scenario_input_v0": "agentic_scenario_input_v0.schema.json",
    "agentic_scenario_oracle_private_v0": "agentic_scenario_oracle_private_v0.schema.json",
    "agentic_scenario_fault_private_v0": "agentic_scenario_fault_private_v0.schema.json",
    "agentic_scenario_prediction_v0": "agentic_scenario_prediction_v0.schema.json",
    "agentic_scenario_score_report_v0": "agentic_scenario_score_report_v0.schema.json",
}

# Marker families for the answer face.  Each entry is pre-normalised (ASCII
# lower, non-alphanumerics removed).  The leak audit checks every dict key and
# every string value in the public input against these substrings, including
# renames, case variants, nested metadata and base64-encoded payloads.  Any hit
# fails closed — a warning is never enough.
FORBIDDEN_INPUT_MARKERS = (
    # expected TaskState / delta / preset state
    "initialtaskstate",
    "expectedtaskstate",
    "taskstatedelta",
    "statedelta",
    "presetstate",
    "preinitializedstate",
    # acceptable / expected / correct answer ids and ranking
    "acceptableproductids",
    "acceptableproductid",
    "acceptableids",
    "acceptableid",
    "expectedrankedids",
    "expectedrankedid",
    "rankedids",
    "rankedid",
    "expectedranking",
    "correctids",
    "correctid",
    "goldproductids",
    "goldproductid",
    "goldids",
    "goldid",
    "answerids",
    "answerid",
    "expectedproductids",
    "expectedproductid",
    "expectedcandidateids",
    "expectedcandidateid",
    "expectedids",
    "expectedid",
    "expectedrankeditems",
    "correctrankedids",
    # expected node / tool / terminal path
    "mustobserve",
    "expectednodepath",
    "nodepath",
    "expectedtoolsequence",
    "toolsequence",
    "expectedpath",
    "expectedtracenodes",
    "expectedcalls",
    "expectedtools",
    "expectedterminal",
    "terminalclass",
    "expectedoutcome",
    "expectedresult",
    "expectedclass",
    # fault expected outcome
    "faultexpectedoutcome",
    "expectedfaultoutcome",
    "faultoutcome",
    "injectionoutcome",
    "expectedfailure",
    "expectedfaultresult",
    # scoring / oracle / answer leakage
    "scorethreshold",
    "scoringthreshold",
    "hiddenscore",
    "hiddenscoring",
    "threshold",
    "oracle",
    "goldanswer",
    "gold",
    "label",
    "labels",
    "successcriteria",
    "expectedsuccesscondition",
    "hiddenrules",
    "expectedanswer",
    "answerkey",
    "solutionsummary",
    "answersummary",
    "correctanswer",
    "groundtruth",
    "groundtruthids",
    "answerface",
)


class ScenarioLabError(Exception):
    """Base error for the Scenario Lab V0 evaluator."""


class ScenarioContractError(ScenarioLabError):
    """A scenario contract invariant was violated; scoring must fail closed."""


@dataclass(frozen=True)
class ScenarioInputView:
    """Runner-facing view of one public scenario input.

    Deliberately exposes only public execution input fields.  There is no
    oracle, no fault plan, and no answer face attribute anywhere on this view.
    """

    scenario_id: str
    track_type: str
    schema_version: str
    catalog_revision: str
    environment_revision: str
    turns: tuple[Mapping[str, Any], ...]
    public_environment_refs: tuple[str, ...]
    setup_ref: str | None


@dataclass(frozen=True)
class AlignedScenario:
    """Scorer-only bundle: one scenario's input, private oracle and fault."""

    scenario_id: str
    track_type: str
    catalog_revision: str
    environment_revision: str
    input: Mapping[str, Any]
    oracle: Mapping[str, Any]
    fault: Mapping[str, Any]
    prediction: Mapping[str, Any]


@dataclass(frozen=True)
class CheckResult:
    name: str
    passed: bool
    details: str
    metric: str = METRIC_TERMINAL

    def as_dict(self) -> Mapping[str, Any]:
        return {
            "name": self.name,
            "passed": self.passed,
            "details": self.details,
            "metric": self.metric,
        }


class ScenarioSet:
    """Scorer-only aligned scenario set.  Never handed to the runner."""

    def __init__(
        self,
        scenarios: Sequence[AlignedScenario],
        input_file_sha256: str,
        oracle_file_sha256: str,
        fault_file_sha256: str,
        prediction_file_sha256: str,
    ) -> None:
        self.scenarios = tuple(scenarios)
        self.input_file_sha256 = input_file_sha256
        self.oracle_file_sha256 = oracle_file_sha256
        self.fault_file_sha256 = fault_file_sha256
        self.prediction_file_sha256 = prediction_file_sha256

    @classmethod
    def load(
        cls,
        input_path: Path | str,
        oracle_path: Path | str,
        fault_path: Path | str,
        prediction_path: Path | str,
    ) -> "ScenarioSet":
        """Load, validate and align the four JSONL files; fail closed on any
        leak, duplicate, missing/extra id, track/revision/schema drift or
        incomplete oracle/fault contract."""
        inputs = load_records(input_path, "input")
        oracles = load_records(oracle_path, "oracle")
        faults = load_records(fault_path, "fault")
        predictions = load_records(prediction_path, "prediction")
        if not inputs:
            raise ScenarioContractError("input file is empty")

        for kind, records in (
            ("input", inputs),
            ("oracle", oracles),
            ("fault", faults),
            ("prediction", predictions),
        ):
            _reject_duplicate_ids(records, "scenarioId", kind)
        _reject_duplicate_ids(predictions, "predictionId", "prediction")

        input_ids = {r["scenarioId"] for r in inputs}
        oracle_ids = {r["scenarioId"] for r in oracles}
        fault_ids = {r["scenarioId"] for r in faults}
        pred_ids = {r["scenarioId"] for r in predictions}
        mismatches = {
            "oracle_missing": sorted(input_ids - oracle_ids),
            "oracle_extra": sorted(oracle_ids - input_ids),
            "fault_missing": sorted(input_ids - fault_ids),
            "fault_extra": sorted(fault_ids - input_ids),
            "prediction_missing": sorted(input_ids - pred_ids),
            "prediction_extra": sorted(pred_ids - input_ids),
        }
        if any(mismatches.values()):
            raise ScenarioContractError(
                "scenario id set mismatch: "
                + " ".join(f"{name}={value}" for name, value in mismatches.items() if value)
            )

        index: dict[str, dict[str, Mapping[str, Any]]] = {}
        for name, records in (
            ("input", inputs),
            ("oracle", oracles),
            ("fault", faults),
            ("prediction", predictions),
        ):
            for record in records:
                index.setdefault(record["scenarioId"], {})[name] = record

        scenarios = []
        for scenario_id in sorted(input_ids):
            bundle = index[scenario_id]
            _check_alignment(scenario_id, bundle)
            scenarios.append(
                AlignedScenario(
                    scenario_id=scenario_id,
                    track_type=bundle["input"]["trackType"],
                    catalog_revision=bundle["input"]["catalogRevision"],
                    environment_revision=bundle["input"]["environmentRevision"],
                    input=bundle["input"],
                    oracle=bundle["oracle"],
                    fault=bundle["fault"],
                    prediction=bundle["prediction"],
                )
            )

        return cls(
            scenarios=scenarios,
            input_file_sha256=sha256_file(input_path),
            oracle_file_sha256=sha256_file(oracle_path),
            fault_file_sha256=sha256_file(fault_path),
            prediction_file_sha256=sha256_file(prediction_path),
        )


KIND_TO_SCHEMA = {
    "input": ("agentic_scenario_input_v0", INPUT_SCHEMA_VERSION),
    "oracle": ("agentic_scenario_oracle_private_v0", ORACLE_SCHEMA_VERSION),
    "fault": ("agentic_scenario_fault_private_v0", FAULT_SCHEMA_VERSION),
    "prediction": ("agentic_scenario_prediction_v0", PREDICTION_SCHEMA_VERSION),
    "report": ("agentic_scenario_score_report_v0", REPORT_SCHEMA_VERSION),
}

_validators: dict[str, Draft202012Validator] = {}


def _validator_for(schema_name: str) -> Draft202012Validator:
    if schema_name not in _validators:
        path = SCHEMAS_DIR / SCHEMA_FILENAMES[schema_name]
        with open(path, encoding="utf-8") as fh:
            schema = json.load(fh)
        _validators[schema_name] = Draft202012Validator(schema)
    return _validators[schema_name]


def canonical_bytes(value: Any) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")


def sha256_file(path: Path | str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_jsonl(path: Path | str) -> list[Mapping[str, Any]]:
    path = Path(path)
    records = []
    with open(path, encoding="utf-8") as fh:
        for line_no, line in enumerate(fh, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ScenarioContractError(
                    f"{path}:{line_no}: invalid JSON line: {exc}"
                ) from exc
            if not isinstance(record, Mapping):
                raise ScenarioContractError(
                    f"{path}:{line_no}: record must be a JSON object"
                )
            records.append(record)
    return records


def validate_record(record: Mapping[str, Any], kind: str) -> None:
    """Validate one record against its JSON Schema; fail closed on violation."""
    if kind not in KIND_TO_SCHEMA:
        raise ValueError(f"unknown record kind: {kind}")
    schema_name, _ = KIND_TO_SCHEMA[kind]
    validator = _validator_for(schema_name)
    errors = sorted(validator.iter_errors(record), key=lambda e: [str(p) for p in e.path])
    if errors:
        raise ScenarioContractError(
            f"{kind} record violates {schema_name}: "
            + "; ".join(error.message for error in errors)
        )


def validate_report(report: Mapping[str, Any]) -> None:
    validate_record(report, "report")


def _normalize_token(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", text.lower())


def _try_decode_base64(text: str) -> str | None:
    compact = text.strip()
    if len(compact) < 8:
        return None
    if not re.fullmatch(r"[A-Za-z0-9+/_-]+={0,2}", compact):
        return None
    candidate = compact.translate(str.maketrans("-_", "+/"))
    candidate += "=" * (-len(candidate) % 4)
    try:
        data = base64.b64decode(candidate, validate=True)
        return data.decode("utf-8")
    except Exception:
        return None


def _scan_string(text: str, path: str) -> None:
    normalized = _normalize_token(text)
    for marker in FORBIDDEN_INPUT_MARKERS:
        if marker in normalized:
            raise ScenarioContractError(
                f"public input leaks answer face: value at {path} contains marker {marker!r}"
            )
    candidates = [text]
    candidates.extend(re.split(r"\s+", text.strip()))
    for candidate in candidates:
        decoded = _try_decode_base64(candidate)
        if decoded is None:
            continue
        decoded_norm = _normalize_token(decoded)
        for marker in FORBIDDEN_INPUT_MARKERS:
            if marker in decoded_norm:
                raise ScenarioContractError(
                    f"public input leaks answer face: value at {path} is base64 of "
                    f"marker {marker!r}"
                )


def _audit_keys(node: Any, path: str) -> None:
    if isinstance(node, Mapping):
        for key, value in node.items():
            normalized = _normalize_token(str(key))
            for marker in FORBIDDEN_INPUT_MARKERS:
                if marker in normalized:
                    raise ScenarioContractError(
                        f"public input leaks answer face: key {path}.{key} matches "
                        f"forbidden marker {marker!r}"
                    )
            _audit_keys(value, f"{path}.{key}")
    elif isinstance(node, (list, tuple)):
        for index, item in enumerate(node):
            _audit_keys(item, f"{path}[{index}]")


def _audit_values(node: Any, path: str) -> None:
    if isinstance(node, Mapping):
        for key, value in node.items():
            _audit_values(value, f"{path}.{key}")
    elif isinstance(node, (list, tuple)):
        for index, item in enumerate(node):
            _audit_values(item, f"{path}[{index}]")
    elif isinstance(node, str):
        _scan_string(node, path)


def audit_input_leaks(record: Mapping[str, Any]) -> None:
    """Scan a public input record for any encoded answer face.

    Renames, case variants, nested metadata, aliases and base64 payloads must
    all fail closed — a warning is never enough.
    """
    if not isinstance(record, Mapping):
        raise ScenarioContractError("public input record must be a JSON object")
    _audit_keys(record, "$")
    _audit_values(record, "$")


def _reject_duplicate_ids(
    records: Sequence[Mapping[str, Any]], key: str, kind: str
) -> None:
    seen = set()
    for record in records:
        value = record.get(key)
        if value in seen:
            raise ScenarioContractError(f"{kind} file has duplicate {key}={value!r}")
        seen.add(value)


def _enforce_input_track_rules(record: Mapping[str, Any]) -> None:
    track = record["trackType"]
    if track == TRACK_LANGUAGE:
        if record.get("setupRef") is not None:
            raise ScenarioContractError(
                f"{record['scenarioId']}: language_e2e input must not carry "
                "setupRef/state payload"
            )
        if not record.get("turns"):
            raise ScenarioContractError(
                f"{record['scenarioId']}: language_e2e input must contain at least "
                "one user turn"
            )


def _enforce_checkpoint_setup(fault: Mapping[str, Any]) -> None:
    """checkpoint_restart / redis_checkpoint_loss injections require the
    authoritative setupFixture (checkpointFixtureRef + expectedCheckpointContentsSha256)
    to exist; a missing fixture, ref or hash fails closed at load.  Fault types
    that never touch a checkpoint are not required to carry a setupFixture."""
    plan = fault["faultPlan"]
    injections = plan.get("injectionPoints", [])
    needs_checkpoint = [
        point
        for point in injections
        if point in (INJECTION_RESTART, INJECTION_CHECKPOINT_LOSS)
    ]
    if not needs_checkpoint:
        return
    setup = plan.get("setupFixture")
    if not isinstance(setup, Mapping):
        raise ScenarioContractError(
            f"{fault['scenarioId']}: {needs_checkpoint} require setupFixture with "
            "checkpointFixtureRef and expectedCheckpointContentsSha256, got none"
        )
    if not setup.get("checkpointFixtureRef"):
        raise ScenarioContractError(
            f"{fault['scenarioId']}: checkpoint setup missing checkpointFixtureRef"
        )
    if not setup.get("expectedCheckpointContentsSha256"):
        raise ScenarioContractError(
            f"{fault['scenarioId']}: checkpoint setup missing "
            "expectedCheckpointContentsSha256"
        )


def _enforce_fault_track_rules(fault: Mapping[str, Any]) -> None:
    track = fault["trackType"]
    plan = fault["faultPlan"]
    injections = plan.get("injectionPoints", [])
    if track == TRACK_LANGUAGE:
        if injections:
            raise ScenarioContractError(
                f"{fault['scenarioId']}: language_e2e fault record must have empty "
                f"injectionPoints, got {injections}"
            )
    else:
        if not injections:
            raise ScenarioContractError(
                f"{fault['scenarioId']}: infrastructure_fault fault record must "
                "declare at least one injection point"
            )
        if "expectedFaultOutcome" not in plan:
            raise ScenarioContractError(
                f"{fault['scenarioId']}: infrastructure_fault fault record missing "
                "expectedFaultOutcome"
            )
        _enforce_checkpoint_setup(fault)


def _enforce_oracle_completeness(oracle: Mapping[str, Any]) -> None:
    track = oracle["trackType"]
    conditions = oracle["successConditions"]
    if not conditions.get("expectedTerminalClasses"):
        raise ScenarioContractError(
            f"{oracle['scenarioId']}: oracle missing "
            "successConditions.expectedTerminalClasses"
        )
    if track == TRACK_LANGUAGE:
        return
    if not conditions.get("faultExpectations"):
        raise ScenarioContractError(
            f"{oracle['scenarioId']}: infrastructure_fault oracle missing "
            "successConditions.faultExpectations"
        )


def _check_prefix_track(record: Mapping[str, Any], kind: str) -> None:
    """ASL-V0-LNG-* must be language_e2e and ASL-V0-INF-* must be
    infrastructure_fault in every one of the four file kinds."""
    scenario_id = record["scenarioId"]
    if scenario_id.startswith("ASL-V0-LNG-"):
        expected = TRACK_LANGUAGE
    elif scenario_id.startswith("ASL-V0-INF-"):
        expected = TRACK_INFRA
    else:
        return
    if record["trackType"] != expected:
        raise ScenarioContractError(
            f"{scenario_id}: track mismatch {kind}={record['trackType']!r} "
            f"scenarioId-prefix={expected}"
        )


def _reject_terminal_duplicates(
    scenario_id: str, label: str, raw_terminals: Sequence[Any]
) -> None:
    """Fail closed when an expected-terminal array repeats a terminal.

    Expected-terminal cardinality is part of the private fault/oracle single
    truth, so a duplicate must never be silently collapsed by the set
    comparison that follows.  The check is explicit and order-stable:
    first-seen order, no truthiness shortcuts, and never a reliance on the
    JSON Schema `uniqueItems` text as the only gate."""
    seen: list[Any] = []
    duplicates: list[Any] = []
    for terminal in raw_terminals:
        if terminal in seen:
            if terminal not in duplicates:
                duplicates.append(terminal)
        else:
            seen.append(terminal)
    if duplicates:
        raise ScenarioContractError(
            f"{scenario_id}: {label} must be unique; "
            f"duplicate {sorted(duplicates)} in {list(raw_terminals)}"
        )


def _enforce_fault_oracle_truth(
    scenario_id: str, oracle: Mapping[str, Any], fault: Mapping[str, Any]
) -> None:
    """Single truth between the private fault plan and the private oracle,
    enforced at load.

    * infrastructure_fault: faultPlan.expectedFaultOutcome must equal the
      oracle's expectedTerminalClasses exactly — a contradiction fails closed.
    * infrastructure_fault: the set of required oracle fault expectations must
      equal the set of fault injectionPoints (missing, extra, unsupported,
      non-required or duplicated mappings all fail closed), so no declared
      fault property can be graded without its injection being executed.
    * language_e2e: the oracle must not declare any required fault expectation.
    """
    track = oracle["trackType"]
    plan = fault["faultPlan"]
    expectations = oracle["successConditions"].get("faultExpectations") or {}
    if track == TRACK_LANGUAGE:
        required = [key for key, value in expectations.items() if value.get("required")]
        if required:
            raise ScenarioContractError(
                f"{scenario_id}: language_e2e oracle declares required fault "
                f"expectations {required}"
            )
        return
    raw_fault_terminal = plan.get("expectedFaultOutcome", {}).get("expectedTerminalClasses", [])
    raw_oracle_terminal = oracle["successConditions"].get("expectedTerminalClasses", [])
    _reject_terminal_duplicates(
        scenario_id, "faultPlan.expectedFaultOutcome.expectedTerminalClasses", raw_fault_terminal
    )
    _reject_terminal_duplicates(
        scenario_id, "successConditions.expectedTerminalClasses", raw_oracle_terminal
    )
    fault_terminal = set(raw_fault_terminal)
    oracle_terminal = set(raw_oracle_terminal)
    if not fault_terminal:
        raise ScenarioContractError(
            f"{scenario_id}: infrastructure_fault fault missing "
            "expectedFaultOutcome.expectedTerminalClasses"
        )
    if fault_terminal != oracle_terminal:
        raise ScenarioContractError(
            f"{scenario_id}: fault expected terminal {sorted(fault_terminal)} "
            f"contradicts oracle {sorted(oracle_terminal)}"
        )
    unsupported = set(expectations) - set(INJECTION_POINTS)
    if unsupported:
        raise ScenarioContractError(
            f"{scenario_id}: unsupported fault expectation keys {sorted(unsupported)}"
        )
    not_required = [key for key, value in expectations.items() if not value.get("required")]
    if not_required:
        raise ScenarioContractError(
            f"{scenario_id}: fault expectation keys must all be required, got "
            f"non-required {not_required}"
        )
    required_keys = set(expectations)
    injections = set(plan.get("injectionPoints", []))
    if required_keys != injections:
        raise ScenarioContractError(
            f"{scenario_id}: fault injection/expectation closure mismatch: "
            f"required={sorted(required_keys)} injections={sorted(injections)}"
        )
    # V0 fixes the safety outcome of every required injection to True: the
    # safety gate is a positive gate, and absence of an explicit
    # counter-observation is not evidence for an expected=False.  An expected
    # False or a missing safety field fails closed here instead of letting
    # `derived == expected` pass a required injection with no observation.
    for key, expectation in expectations.items():
        field = SAFETY_OUTCOME_FIELD_BY_INJECTION[key]
        value = expectation.get(field)
        if value is not True:
            raise ScenarioContractError(
                f"{scenario_id}: required {key} safety outcome {field} must be "
                f"true, got {value!r}"
            )
    # V0 does not support checkpoint_restart and redis_checkpoint_loss in the
    # same scenario: restart recovery is a SUCCESS-bound gate while checkpoint
    # loss is a fail-closed gate, and one shared terminal set cannot express
    # both truthfully.  The check is pure set membership (order-independent);
    # whichever order the injections/expectations are declared, the same
    # stable contract error is raised.  Future per-injection terminal
    # contracts are out of scope for V0.
    if INJECTION_RESTART in expectations and INJECTION_CHECKPOINT_LOSS in expectations:
        raise ScenarioContractError(
            f"{scenario_id}: V0 does not support checkpoint_restart together "
            "with redis_checkpoint_loss (restart recovery is SUCCESS-bound, "
            "checkpoint loss is fail-closed); a shared expected terminal set "
            "cannot grade both, so the combination fails closed at load"
        )
    # required checkpoint_restart fixes the shared expected terminal to
    # exactly ['SUCCESS']: recovery is the only valid restart outcome, so a
    # FAILURE/ABSTAIN/CLARIFICATION or mixed expected set would otherwise let
    # a failed restart be graded as recovered.
    if INJECTION_RESTART in expectations:
        if set(fault_terminal) != {RESTART_RECOVERY_TERMINAL}:
            raise ScenarioContractError(
                f"{scenario_id}: required checkpoint_restart fixes the shared "
                f"expected terminal to exactly ['{RESTART_RECOVERY_TERMINAL}'], "
                f"got {sorted(fault_terminal)}"
            )
    # checkpoint loss is a fail-closed gate: when it is required the shared
    # terminal contract must exclude SUCCESS and stay within the fixed V0
    # fail-closed set, so a prediction can never pass a loss by ending in a
    # "recovered" SUCCESS terminal.
    if INJECTION_CHECKPOINT_LOSS in expectations:
        forbidden = [t for t in fault_terminal if t not in CHECKPOINT_LOSS_TERMINAL_CLASSES]
        if forbidden:
            raise ScenarioContractError(
                f"{scenario_id}: redis_checkpoint_loss requires fail-closed terminal "
                f"classes in {sorted(CHECKPOINT_LOSS_TERMINAL_CLASSES)}, got "
                f"{sorted(forbidden)}"
            )


def _enforce_product_truth(scenario_id: str, oracle: Mapping[str, Any]) -> None:
    """Hard constraints need private product truth to verify against.  A
    SUCCESS-expected scenario that declares hard constraints but neither
    acceptableProductIds nor productFactAtoms has no way to verify the claimed
    constraints — it must fail closed at load instead of trusting
    claimedConstraints alone.  When both truth sources are declared they must
    be consistent: product facts must never reference products outside the
    acceptable universe (a contradiction fails closed)."""
    conditions = oracle["successConditions"]
    acceptable = conditions.get("acceptableProductIds")
    facts = conditions.get("productFactAtoms")
    if acceptable is not None and facts is not None:
        foreign = sorted({atom["productId"] for atom in facts} - set(acceptable))
        if foreign:
            raise ScenarioContractError(
                f"{scenario_id}: productFactAtoms reference products outside "
                f"acceptableProductIds: {foreign}"
            )
    hard = conditions.get("hardConstraints", {}).get("hard") or []
    if not hard:
        return
    if "SUCCESS" not in conditions.get("expectedTerminalClasses", []):
        return
    if acceptable is None and facts is None:
        raise ScenarioContractError(
            f"{scenario_id}: oracle requires hard constraints but provides no "
            "verifiable product truth (acceptableProductIds or productFactAtoms)"
        )


def _check_alignment(scenario_id: str, bundle: Mapping[str, Mapping[str, Any]]) -> None:
    base = bundle["input"]
    for name in ("oracle", "fault", "prediction"):
        record = bundle[name]
        if record["trackType"] != base["trackType"]:
            raise ScenarioContractError(
                f"{scenario_id}: track mismatch {name}={record['trackType']!r} "
                f"input={base['trackType']!r}"
            )
        if record["catalogRevision"] != base["catalogRevision"]:
            raise ScenarioContractError(
                f"{scenario_id}: {name} catalogRevision mismatch"
            )
        if record["environmentRevision"] != base["environmentRevision"]:
            raise ScenarioContractError(
                f"{scenario_id}: {name} environmentRevision mismatch"
            )
    _enforce_fault_track_rules(bundle["fault"])
    _enforce_oracle_completeness(bundle["oracle"])
    _enforce_fault_oracle_truth(scenario_id, bundle["oracle"], bundle["fault"])
    _enforce_product_truth(scenario_id, bundle["oracle"])


def load_records(path: Path | str, kind: str) -> list[Mapping[str, Any]]:
    """Load one JSONL file and run kind-specific validation/auditing."""
    records = load_jsonl(path)
    for record in records:
        validate_record(record, kind)
        _check_prefix_track(record, kind)
        if kind == "input":
            audit_input_leaks(record)
            _enforce_input_track_rules(record)
    return records


def load_runner_input(path: Path | str) -> list[ScenarioInputView]:
    """Runner-facing load API.

    Accepts only a public input file, never a private oracle/fault file, and
    returns views that expose no private object.
    """
    records = load_records(path, "input")
    if not records:
        raise ScenarioContractError("input file is empty")
    _reject_duplicate_ids(records, "scenarioId", "input")
    views = []
    for record in records:
        views.append(
            ScenarioInputView(
                scenario_id=record["scenarioId"],
                track_type=record["trackType"],
                schema_version=record["schemaVersion"],
                catalog_revision=record["catalogRevision"],
                environment_revision=record["environmentRevision"],
                turns=tuple(record.get("turns", [])),
                public_environment_refs=tuple(record.get("publicEnvironmentRefs", [])),
                setup_ref=record.get("setupRef"),
            )
        )
    return views


# ---------------------------------------------------------------------------
# Deterministic scoring
# ---------------------------------------------------------------------------

def _atom_covered(required_atom: Mapping[str, Any], actual_atoms: Sequence[Mapping[str, Any]]) -> bool:
    """Deterministic coverage: the prediction's hard atom must satisfy the
    oracle's required hard atom.  For IN, the claimed allowed set must be a
    subset of the required set (stricter is fine); for NOT_IN, it must be a
    superset (excluding at least everything the oracle excludes)."""
    required_set = set(required_atom["allowedValues"])
    for atom in actual_atoms:
        if atom["group"] != required_atom["group"] or atom["operator"] != required_atom["operator"]:
            continue
        actual_set = set(atom["allowedValues"])
        if required_atom["operator"] == "IN":
            if actual_set <= required_set:
                return True
        else:  # NOT_IN
            if required_set <= actual_set:
                return True
    return False


def _q_matches(asked: str, expected: str) -> bool:
    """Deterministic clarification match: shared token first, raw substring
    fallback for CJK-only expectations."""
    asked_norm = _normalize_token(asked)
    expected_norm = _normalize_token(expected)
    if expected_norm and expected_norm in asked_norm:
        return True
    if expected_norm and asked_norm and asked_norm == expected_norm:
        return True
    return expected in asked


def _count_duplicate_tools(calls: Sequence[Mapping[str, Any]]) -> int:
    """Number of tool calls whose (toolName, canonical arguments) was already
    issued earlier in the same prediction."""
    seen = set()
    duplicates = 0
    for call in calls:
        key = (call["toolName"], canonical_bytes(call.get("arguments") or {}))
        if key in seen:
            duplicates += 1
        else:
            seen.add(key)
    return duplicates


def _tool_checks(
    oracle: Mapping[str, Any], prediction: Mapping[str, Any]
) -> list[CheckResult]:
    checks = []
    rules = oracle["successConditions"].get("toolRules", {})
    calls = prediction.get("toolCalls", [])
    max_calls = rules.get("maxToolCalls")
    if max_calls is not None:
        checks.append(
            CheckResult(
                "tool_call_count_limit",
                len(calls) <= max_calls,
                f"calls={len(calls)} max={max_calls}",
                metric=METRIC_TOOL_COUNT,
            )
        )
    required_tool = rules.get("requiredTool")
    if required_tool:
        checks.append(
            CheckResult(
                "required_tool_used",
                any(call["toolName"] == required_tool for call in calls),
                f"required={required_tool} used={[c['toolName'] for c in calls]}",
                metric=METRIC_TOOL_COUNT,
            )
        )
    forbidden_tools = rules.get("forbiddenTools", [])
    used_forbidden = [call["toolName"] for call in calls if call["toolName"] in forbidden_tools]
    checks.append(
        CheckResult(
            "forbidden_tool_absent",
            not used_forbidden,
            f"used={used_forbidden}",
            metric=METRIC_TOOL_COUNT,
        )
    )
    duplicates = _count_duplicate_tools(calls)
    max_duplicates = rules.get("maxDuplicateToolCalls")
    if max_duplicates is not None:
        checks.append(
            CheckResult(
                "duplicate_tool_limit",
                duplicates <= max_duplicates,
                f"duplicates={duplicates} max={max_duplicates}",
                metric=METRIC_DUPLICATE_TOOL,
            )
        )
    return checks


def _product_fact_map(
    atom_records: Sequence[Mapping[str, Any]],
) -> dict[tuple[str, str], list[str]]:
    facts: dict[tuple[str, str], list[str]] = {}
    for atom in atom_records:
        facts.setdefault((atom["productId"], atom["field"]), []).append(atom["value"])
    return facts


def _constraint_checks(
    scenario: AlignedScenario, oracle: Mapping[str, Any], prediction: Mapping[str, Any]
) -> list[CheckResult]:
    checks = []
    conditions = oracle["successConditions"]
    ranked = prediction.get("rankedProductIds", [])
    hard = conditions.get("hardConstraints", {}).get("hard", [])
    terminal = prediction.get("terminalClass")

    allowed = conditions.get("acceptableProductIds")
    facts = conditions.get("productFactAtoms")
    # product ranking is demanded whenever any private product truth source is
    # declared (acceptable universe, product facts or hard constraints)
    product_context = allowed is not None or facts is not None or bool(hard)

    if allowed is not None:
        allowed_set = set(allowed)
        not_allowed = [product_id for product_id in ranked if product_id not in allowed_set]
        checks.append(
            CheckResult(
                "ranked_ids_allowed",
                not not_allowed,
                f"not_allowed={not_allowed}",
                metric=METRIC_HARD_CONSTRAINT,
            )
        )

    # SUCCESS product scenarios must return a non-empty ranking whenever the
    # oracle demands product truth.  This check is independent of whether the
    # private truth source is the acceptable universe or product facts, so an
    # empty ranking can never pass by iterating over zero ranked products.
    if terminal == "SUCCESS" and not ranked and product_context:
        checks.append(
            CheckResult(
                "ranked_ids_nonempty",
                False,
                "SUCCESS expected product ranking but rankedProductIds is empty",
                metric=METRIC_HARD_CONSTRAINT,
            )
        )

    # Every ranked product must be verifiable against the required hard atoms
    # in the private product facts; a missing fact record must not pass by
    # being skipped.
    if facts is not None and hard:
        fact_map = _product_fact_map(facts)
        problems = []
        for product_id in ranked:
            for atom in hard:
                group = atom["group"]
                operator = atom["operator"]
                allowed_values = set(atom["allowedValues"])
                values = set(fact_map.get((product_id, group), []))
                if not values:
                    problems.append(
                        f"{product_id}: no fact for required hard atom {group}"
                    )
                elif operator == "IN":
                    if not (values & allowed_values):
                        problems.append(
                            f"{product_id}: required {group} IN {sorted(allowed_values)} "
                            f"contradicted by facts {sorted(values)}"
                        )
                else:  # NOT_IN
                    if values & allowed_values:
                        problems.append(
                            f"{product_id}: required {group} NOT_IN {sorted(allowed_values)} "
                            f"contradicted by facts {sorted(values)}"
                        )
        checks.append(
            CheckResult(
                "hard_constraint_products_verified",
                not problems,
                f"violations={problems}",
                metric=METRIC_HARD_CONSTRAINT,
            )
        )

    if facts is not None:
        fact_map = _product_fact_map(facts)
        problems = []
        claimed = prediction.get("claimedConstraints", {}).get("hard", [])
        for atom in claimed:
            group = atom["group"]
            operator = atom["operator"]
            allowed_values = set(atom["allowedValues"])
            for product_id in ranked:
                values = set(fact_map.get((product_id, group), []))
                if operator == "IN":
                    if not values:
                        problems.append(
                            f"{product_id}: claim {group} IN {sorted(allowed_values)} "
                            "unverifiable (no product facts)"
                        )
                    elif not (values & allowed_values):
                        problems.append(
                            f"{product_id}: claim {group} IN {sorted(allowed_values)} "
                            f"contradicted by facts {sorted(values)}"
                        )
                else:  # NOT_IN
                    if values & allowed_values:
                        problems.append(
                            f"{product_id}: claim {group} NOT_IN {sorted(allowed_values)} "
                            f"contradicted by facts {sorted(values)}"
                        )
        checks.append(
            CheckResult(
                "hard_constraint_fact_verified",
                not problems,
                f"violations={problems}",
                metric=METRIC_HARD_CONSTRAINT,
            )
        )

    if hard:
        actual = prediction.get("claimedConstraints", {}).get("hard", [])
        missing = [atom for atom in hard if not _atom_covered(atom, actual)]
        checks.append(
            CheckResult(
                "hard_constraint_match",
                not missing,
                f"missing_or_not_covered={missing}",
                metric=METRIC_HARD_CONSTRAINT,
            )
        )
    return checks


def _unsupported_claim_checks(
    scenario: AlignedScenario, oracle: Mapping[str, Any], prediction: Mapping[str, Any]
) -> list[CheckResult]:
    """Claim→evidence integrity gate, enforced when the oracle forbids
    unsupported claims.  Every structured claim must bind to evidence:
    evidenceRefs must resolve (no dangling / within-claim duplicate refs), the
    evidence must agree with the claim on subject, field, revision and
    normalized value, no evidence may be orphaned (each citation must back at
    least one claim), and the claim extractor must declare its provenance,
    version and completeness so an incomplete extractor cannot pass."""
    checks = []
    rules = oracle["successConditions"].get("evidenceRules", {})
    if not rules.get("forbidUnsupportedClaims"):
        return checks

    extraction = prediction.get("claimExtraction") or {}
    if not extraction.get("provenance") or not extraction.get("version"):
        checks.append(
            CheckResult(
                "claim_extraction_provenance",
                False,
                "claim extraction provenance/version missing",
                metric=METRIC_UNSUPPORTED_CLAIM,
            )
        )
    if extraction.get("claimsComplete") is not True:
        checks.append(
            CheckResult(
                "claim_extraction_complete",
                False,
                f"claimsComplete={extraction.get('claimsComplete')!r}",
                metric=METRIC_UNSUPPORTED_CLAIM,
            )
        )

    claims = prediction.get("claims", [])
    citations = prediction.get("evidenceCitations", [])
    by_id: dict[str, Mapping[str, Any]] = {}
    for citation in citations:
        evidence_id = citation["evidenceId"]
        if evidence_id in by_id:
            checks.append(
                CheckResult(
                    "evidence_id_unique",
                    False,
                    f"duplicate evidenceId={evidence_id!r}",
                    metric=METRIC_UNSUPPORTED_CLAIM,
                )
            )
        by_id[evidence_id] = citation

    referenced: set[str] = set()
    for claim in claims:
        refs = claim["evidenceRefs"]
        if not refs:
            checks.append(
                CheckResult(
                    "claim_has_evidence",
                    False,
                    f"claim {claim['claimId']} has no evidence",
                    metric=METRIC_UNSUPPORTED_CLAIM,
                )
            )
        dangling = [ref for ref in refs if ref not in by_id]
        if dangling:
            checks.append(
                CheckResult(
                    "claim_evidence_refs_resolve",
                    False,
                    f"claim {claim['claimId']} dangling evidenceRefs={dangling}",
                    metric=METRIC_UNSUPPORTED_CLAIM,
                )
            )
        for ref in refs:
            if ref not in by_id:
                continue  # already reported as a dangling ref
            evidence = by_id[ref]
            referenced.add(ref)
            if evidence["productId"] != claim["subject"]:
                checks.append(
                    CheckResult(
                        "claim_evidence_subject_aligned",
                        False,
                        f"claim {claim['claimId']} subject {claim['subject']!r} != "
                        f"evidence {ref} product {evidence['productId']!r}",
                        metric=METRIC_UNSUPPORTED_CLAIM,
                    )
                )
            if evidence["field"] != claim["field"]:
                checks.append(
                    CheckResult(
                        "claim_evidence_field_aligned",
                        False,
                        f"claim {claim['claimId']} field {claim['field']!r} != "
                        f"evidence {ref} field {evidence['field']!r}",
                        metric=METRIC_UNSUPPORTED_CLAIM,
                    )
                )
            if evidence["revision"] != scenario.environment_revision:
                checks.append(
                    CheckResult(
                        "claim_evidence_revision_aligned",
                        False,
                        f"claim {claim['claimId']} evidence {ref} revision "
                        f"{evidence['revision']!r} != scenario {scenario.environment_revision!r}",
                        metric=METRIC_UNSUPPORTED_CLAIM,
                    )
                )
            if evidence["normalizedValue"] != claim["value"]:
                checks.append(
                    CheckResult(
                        "claim_evidence_value_consistent",
                        False,
                        f"claim {claim['claimId']} value {claim['value']!r} != "
                        f"evidence {ref} normalizedValue {evidence['normalizedValue']!r}",
                        metric=METRIC_UNSUPPORTED_CLAIM,
                    )
                )

    orphaned = [evidence_id for evidence_id in by_id if evidence_id not in referenced]
    if orphaned:
        checks.append(
            CheckResult(
                "evidence_bound_to_claim",
                False,
                f"orphan evidenceIds={orphaned}",
                metric=METRIC_UNSUPPORTED_CLAIM,
            )
        )

    ranked = set(prediction.get("rankedProductIds", []))
    unsupported = [
        citation["productId"] for citation in citations if citation["productId"] not in ranked
    ]
    if unsupported:
        checks.append(
            CheckResult(
                "unsupported_claims_absent",
                False,
                f"unsupported_citations={unsupported}",
                metric=METRIC_UNSUPPORTED_CLAIM,
            )
        )
    if prediction["terminalClass"] == "SUCCESS" and prediction.get("rankedProductIds") and not citations:
        checks.append(
            CheckResult(
                "unsupported_claims_absent",
                False,
                "success without any citation",
                metric=METRIC_UNSUPPORTED_CLAIM,
            )
        )
    # The gate always emits exactly one summary check when the oracle forbids
    # unsupported claims, so the unsupported-claim metric forms eligibility on
    # both the success side (eligible/pass) and the failure side
    # (eligible/fail).  A fully-bound prediction that merely passes must not
    # collapse to eligible=0.
    summary_passed = all(check.passed for check in checks)
    checks.append(
        CheckResult(
            "unsupported_claim_gate",
            summary_passed,
            f"unsupported-claim gate passed={summary_passed}",
            metric=METRIC_UNSUPPORTED_CLAIM,
        )
    )
    return checks


def _checks_language(
    scenario: AlignedScenario, oracle: Mapping[str, Any], prediction: Mapping[str, Any]
) -> list[CheckResult]:
    checks = []
    conditions = oracle["successConditions"]
    expected_terminal = conditions["expectedTerminalClasses"]
    checks.append(
        CheckResult(
            "terminal_class",
            prediction["terminalClass"] in expected_terminal,
            f"prediction={prediction['terminalClass']} expected={expected_terminal}",
            metric=METRIC_TERMINAL,
        )
    )
    checks.extend(_cardinality_checks(scenario, oracle, prediction))
    checks.extend(_constraint_checks(scenario, oracle, prediction))

    evidence_rules = conditions.get("evidenceRules", {})
    citations = prediction.get("evidenceCitations", [])
    min_citations = evidence_rules.get("minCitationCount", 0)
    checks.append(
        CheckResult(
            "evidence_sufficiency",
            len(citations) >= min_citations,
            f"citations={len(citations)} min={min_citations}",
            metric=METRIC_EVIDENCE,
        )
    )
    cited_products = {citation["productId"] for citation in citations}
    min_cited = evidence_rules.get("minCitedProductIds", [])
    missing_cited = [pid for pid in min_cited if pid not in cited_products]
    checks.append(
        CheckResult(
            "evidence_min_cited_products",
            not missing_cited,
            f"missing_cited={missing_cited}",
            metric=METRIC_EVIDENCE,
        )
    )
    checks.extend(_unsupported_claim_checks(scenario, oracle, prediction))

    clarification_rules = conditions.get("clarificationRules", {})
    clarifications = prediction.get("clarifications", [])
    asked = [clarification["question"] for clarification in clarifications]
    required_questions = clarification_rules.get("requiredQuestions", [])
    missing_required = [
        question
        for question in required_questions
        if not any(_q_matches(asked_question, question) for asked_question in asked)
    ]
    checks.append(
        CheckResult(
            "clarification_required",
            not missing_required,
            f"missing_required={missing_required}",
            metric=METRIC_CLARIFICATION,
        )
    )
    if not required_questions and clarifications:
        checks.append(
            CheckResult(
                "clarification_not_unnecessary",
                False,
                "clarification asked when none required",
                metric=METRIC_CLARIFICATION,
            )
        )
    forbidden_questions = clarification_rules.get("forbiddenQuestions", [])
    forbidden_asked = [
        question
        for question in asked
        if any(_q_matches(question, forbidden) for forbidden in forbidden_questions)
    ]
    checks.append(
        CheckResult(
            "clarification_forbidden_absent",
            not forbidden_asked,
            f"forbidden_asked={forbidden_asked}",
            metric=METRIC_CLARIFICATION,
        )
    )
    max_clarifications = clarification_rules.get("maxClarificationCount")
    if max_clarifications is not None:
        checks.append(
            CheckResult(
                "clarification_count_limit",
                len(clarifications) <= max_clarifications,
                f"count={len(clarifications)} max={max_clarifications}",
                metric=METRIC_CLARIFICATION,
            )
        )

    checks.extend(_tool_checks(oracle, prediction))
    return checks


def _cardinality_checks(
    scenario: AlignedScenario, oracle: Mapping[str, Any], prediction: Mapping[str, Any]
) -> list[CheckResult]:
    """V0 strict single-occurrence contract, enforced at score time.

    * exactly one initial observation;
    * each attempt's HTTP requestId is globally unique within the prediction;
    * at most one observation per attemptKind (a duplicate initial or a second
      observation for one required injection fails closed, so a good attempt
      can never mask a bad one);
    * every attempt carries the same durable task/thread/run identity as the
      prediction and matches the prediction's triple strictly and
      unconditionally (missing or cross taskId/threadId/runId all fail closed);
    * no attemptKind that the fault plan did not declare.

    Any violation fails the scenario closed."""
    checks = []
    attempts = prediction["observations"]["attempts"]

    initials = [a for a in attempts if a["attemptKind"] == "initial"]
    if len(initials) != 1:
        checks.append(
            CheckResult(
                "initial_unique",
                False,
                f"expected exactly one initial observation, got {len(initials)}",
                metric=METRIC_TERMINAL,
            )
        )

    request_id_counts: dict[str, int] = {}
    for a in attempts:
        request_id_counts[a["requestId"]] = request_id_counts.get(a["requestId"], 0) + 1
    duplicate_request_ids = sorted(
        rid for rid, count in request_id_counts.items() if count > 1
    )
    if duplicate_request_ids:
        checks.append(
            CheckResult(
                "request_id_unique",
                False,
                f"duplicate requestId={duplicate_request_ids}",
                metric=METRIC_TERMINAL,
            )
        )

    kind_counts: dict[str, int] = {}
    for a in attempts:
        kind_counts[a["attemptKind"]] = kind_counts.get(a["attemptKind"], 0) + 1
    duplicate_kinds = sorted(kind for kind, count in kind_counts.items() if count > 1)
    if duplicate_kinds:
        checks.append(
            CheckResult(
                "attempt_cardinality",
                False,
                f"duplicate attempt kinds={duplicate_kinds}",
                metric=METRIC_TERMINAL,
            )
        )

    pred_run = prediction.get("runId")
    pred_task = prediction.get("taskId")
    pred_thread = prediction.get("threadId")
    identity_problems = []
    for a in attempts:
        # V0 requires every attempt to carry taskId/threadId/runId (schema
        # required) and to match the prediction's server-owned triple exactly.
        # The comparison is unconditional — a missing field (None) never equals
        # the prediction value, so it cannot be passed by a truthy fallback or
        # a "field present" guard.
        for field, pred_value in (
            ("runId", pred_run),
            ("taskId", pred_task),
            ("threadId", pred_thread),
        ):
            if a.get(field) != pred_value:
                identity_problems.append(
                    f"{a['requestId']}: {field} {a.get(field)!r} != prediction {pred_value!r}"
                )
    if identity_problems:
        checks.append(
            CheckResult(
                "attempt_identity_aligned",
                False,
                "; ".join(identity_problems),
                metric=METRIC_TERMINAL,
            )
        )

    planned = {"initial"}
    for key in oracle["successConditions"].get("faultExpectations", {}):
        planned.add(ATTEMPT_KIND_BY_INJECTION[key])
    unplanned = sorted(kind for kind in kind_counts if kind not in planned)
    if unplanned:
        checks.append(
            CheckResult(
                "unplanned_attempt_kind",
                False,
                f"unplanned attempt kinds={unplanned} (planned={sorted(planned)})",
                metric=METRIC_TERMINAL,
            )
        )
    return checks


def _no_delta(attempt: Mapping[str, Any]) -> tuple[bool, str]:
    """Runner-owned before/after revision and history counts are present and
    unchanged → the attempt is a no-delta replay / conflict probe."""
    rb, ra = attempt.get("revisionBefore"), attempt.get("revisionAfter")
    hb, ha = attempt.get("historyCountBefore"), attempt.get("historyCountAfter")
    if None in (rb, ra, hb, ha):
        return False, "missing revision/history before/after counts"
    if rb != ra or hb != ha:
        return False, f"revision {rb}->{ra} history {hb}->{ha}"
    return True, f"revision {ra} history {ha} (no delta)"


def _derive_durable(
    scenario: AlignedScenario,
    oracle: Mapping[str, Any],
    prediction: Mapping[str, Any],
    expected_terminal: Sequence[str],
) -> list[CheckResult]:
    """Derive every durable-fault conclusion from runner-owned observations
    instead of trusting SUT booleans.  Each expectation is keyed by its
    injection point; a missing expectation for a required injection fails
    closed at load, and a required injection with no supporting observation
    fails closed at score time.

    V0 enforces strict single-occurrence cardinality (see _cardinality_checks):
    a required injection has exactly one supporting observation, so a good
    attempt can never mask a bad one.  Existence is checked before any
    expectation comparison — an absent observation is never evidence for an
    expected=False outcome."""
    expectations = oracle["successConditions"].get("faultExpectations", {})
    attempts = prediction["observations"]["attempts"]
    initial = next((attempt for attempt in attempts if attempt["attemptKind"] == "initial"), None)
    terminal_set = set(expected_terminal)
    checks = []
    # The authoritative checkpoint fixture identity.  Load enforcement
    # (_enforce_checkpoint_setup) guarantees both fields exist whenever a
    # checkpoint injection is required, so an attempt can only pass by carrying
    # the exact same ref and contents hash — never by a truthy placeholder.
    fixture = scenario.fault.get("faultPlan", {}).get("setupFixture", {})
    fixture_ref = fixture.get("checkpointFixtureRef")
    fixture_sha = fixture.get("expectedCheckpointContentsSha256")

    def terminal_ok(code: Any) -> bool:
        return code in terminal_set

    def gate(
        injection_key: str,
        attempt_kind: str,
        expectation: Mapping[str, Any],
        expected_field: str,
        conclusion_name: str,
        metric: str,
        derive_one,
    ) -> None:
        """One required-injection gate: existence first, then behaviour."""
        relevant = [a for a in attempts if a["attemptKind"] == attempt_kind]
        if not relevant:
            checks.append(
                CheckResult(
                    f"{injection_key}_observation_present",
                    False,
                    f"required {injection_key} has no {attempt_kind} observation",
                    metric=metric,
                )
            )
            checks.append(
                CheckResult(
                    conclusion_name,
                    False,
                    f"no {attempt_kind} observation for required {injection_key}",
                    metric=metric,
                )
            )
            return
        expected = expectation.get(expected_field)
        if expected is None:
            checks.append(
                CheckResult(
                    conclusion_name,
                    False,
                    f"missing {expected_field}",
                    metric=metric,
                )
            )
            return
        if len(relevant) != 1:
            checks.append(
                CheckResult(
                    conclusion_name,
                    False,
                    f"expected exactly one {attempt_kind} attempt, got {len(relevant)}",
                    metric=metric,
                )
            )
            return
        derived, detail = derive_one(relevant[0])
        checks.append(
            CheckResult(
                conclusion_name,
                derived == expected,
                f"expected={expected} derived={derived}; {detail}",
                metric=metric,
            )
        )

    def checkpoint_fixture_bound(attempt_kind: str, prefix: str, metric: str) -> None:
        """Verbatim same-namespace binding of the required attempt's
        checkpointRef / checkpointSha256 to the authoritative setupFixture.

        V0 does no basename/prefix/suffix/URI normalization guessing: the ref
        must equal checkpointFixtureRef exactly and the hash must equal
        expectedCheckpointContentsSha256 exactly.  The check runs against every
        attempt of that kind (strict cardinality keeps it to exactly one);
        missing, dangling, forged, cross-fixture and merely-similar refs/hashes
        all fail closed.  Details never leak the private fixture values."""
        for check_name, field, expected in (
            (f"{prefix}_checkpoint_ref_aligned", "checkpointRef", fixture_ref),
            (f"{prefix}_checkpoint_sha_aligned", "checkpointSha256", fixture_sha),
        ):
            relevant = [a for a in attempts if a["attemptKind"] == attempt_kind]
            if not relevant:
                checks.append(
                    CheckResult(
                        check_name,
                        False,
                        f"no {attempt_kind} attempt to bind to the fixture",
                        metric=metric,
                    )
                )
                continue
            for a in relevant:
                if a.get(field) != expected:
                    checks.append(
                        CheckResult(
                            check_name,
                            False,
                            f"{attempt_kind} {field} does not match the authoritative "
                            f"setupFixture ({a['requestId']})",
                            metric=metric,
                        )
                    )

    restart = expectations.get(INJECTION_RESTART)
    if restart is not None:
        def derive_restart(a):
            # Recovery is a SUCCESS-only outcome: the attempt's terminalCode
            # must be exactly 'SUCCESS', never a member of the shared expected
            # terminal set (load forces that set to ['SUCCESS'] anyway), and
            # never a FAILURE/ABSTAIN/CLARIFICATION/other string — a missing or
            # non-SUCCESS terminalCode makes the derived gate fail closed.
            recovered = bool(
                a.get("checkpointRef") == fixture_ref
                and a.get("checkpointSha256") == fixture_sha
                and a.get("terminalCode") == RESTART_RECOVERY_TERMINAL
            )
            return recovered, (
                f"checkpoint_ref_aligned={a.get('checkpointRef') == fixture_ref} "
                f"checkpoint_sha_aligned={a.get('checkpointSha256') == fixture_sha} "
                f"terminal={a.get('terminalCode')!r}"
            )

        gate(
            INJECTION_RESTART,
            "restart_recovery",
            restart,
            "expectedRecoveredAfterRestart",
            "restart_recovery",
            METRIC_RESTART,
            derive_restart,
        )
        checkpoint_fixture_bound("restart_recovery", "restart", METRIC_RESTART)
        max_calls = restart.get("maxToolCallsAfterRestart")
        if max_calls is not None:
            restarts = [a for a in attempts if a["attemptKind"] == "restart_recovery"]
            total_calls = sum(a.get("toolCalls", 0) for a in restarts)
            checks.append(
                CheckResult(
                    "restart_tool_bound",
                    total_calls <= max_calls,
                    f"restart_tool_calls={total_calls} max={max_calls}",
                    metric=METRIC_RESTART,
                )
            )

    replay = expectations.get(INJECTION_EXACT_REPLAY)
    if replay is not None:
        def derive_replay(a):
            if initial is None:
                return False, "missing initial attempt observation"
            no_delta, no_delta_detail = _no_delta(a)
            same = (
                a.get("payloadSha256") == initial.get("payloadSha256")
                and a.get("resultSha256") == initial.get("resultSha256")
                and a.get("toolCalls") == 0
                and a.get("modelCalls") == 0
                and no_delta
            )
            detail = (
                f"same_payload_result="
                f"{a.get('payloadSha256') == initial.get('payloadSha256') and a.get('resultSha256') == initial.get('resultSha256')} "
                f"tool={a.get('toolCalls')} model={a.get('modelCalls')} no_delta={no_delta}"
            )
            if not no_delta:
                detail += f" ({no_delta_detail})"
            return same, detail

        gate(
            INJECTION_EXACT_REPLAY,
            "exact_replay",
            replay,
            "expectedExactReplayZeroTool",
            "exact_replay_zero_tool",
            METRIC_EXACT_REPLAY,
            derive_replay,
        )

    mutated = expectations.get(INJECTION_MUTATED_REPLAY)
    if mutated is not None:
        def derive_mutated(a):
            if initial is None:
                return False, "missing initial attempt observation"
            no_delta, no_delta_detail = _no_delta(a)
            rejected = (
                a.get("payloadSha256") != initial.get("payloadSha256")
                and a.get("rejected") is True
                and a.get("rejectionCode")
                and a.get("toolCalls") == 0
                and a.get("modelCalls") == 0
                and no_delta
            )
            detail = (
                f"payload_diff={a.get('payloadSha256') != initial.get('payloadSha256')} "
                f"rejected={a.get('rejected')!r} rejection={a.get('rejectionCode')!r} "
                f"tool={a.get('toolCalls')} model={a.get('modelCalls')} no_delta={no_delta}"
            )
            if not no_delta:
                detail += f" ({no_delta_detail})"
            return rejected, detail

        gate(
            INJECTION_MUTATED_REPLAY,
            "mutated_replay",
            mutated,
            "expectedMutatedReplayRejected",
            "mutated_replay_rejection",
            METRIC_MUTATED_REPLAY,
            derive_mutated,
        )

    conflict = expectations.get(INJECTION_REVISION_CONFLICT)
    if conflict is not None:
        def derive_conflict(a):
            no_delta, no_delta_detail = _no_delta(a)
            closed = bool(
                a.get("rejected") is True
                and a.get("toolCalls") == 0
                and a.get("modelCalls") == 0
                and no_delta
                and (a.get("rejectionCode") or a.get("terminalCode"))
            )
            detail = (
                f"rejected={a.get('rejected')!r} rejection={a.get('rejectionCode')!r} "
                f"terminal={a.get('terminalCode')!r} tool={a.get('toolCalls')} "
                f"model={a.get('modelCalls')} no_delta={no_delta}"
            )
            if not no_delta:
                detail += f" ({no_delta_detail})"
            return closed, detail

        gate(
            INJECTION_REVISION_CONFLICT,
            "revision_conflict",
            conflict,
            "expectedRevisionDivergenceFailClosed",
            "revision_divergence_fail_closed",
            METRIC_REVISION_CONFLICT,
            derive_conflict,
        )

    loss = expectations.get(INJECTION_CHECKPOINT_LOSS)
    if loss is not None:
        def derive_loss(a):
            no_delta, no_delta_detail = _no_delta(a)
            fail_closed = bool(
                a.get("rejected") is True
                and a.get("rejectionCode")
                and a.get("toolCalls") == 0
                and a.get("modelCalls") == 0
                and no_delta
                and terminal_ok(a.get("terminalCode"))
                and a.get("checkpointRef") == fixture_ref
                and a.get("checkpointSha256") == fixture_sha
            )
            detail = (
                f"rejected={a.get('rejected')!r} rejection={a.get('rejectionCode')!r} "
                f"tool={a.get('toolCalls')} model={a.get('modelCalls')} "
                f"no_delta={no_delta} terminal={a.get('terminalCode')!r} "
                f"ref_aligned={a.get('checkpointRef') == fixture_ref} "
                f"sha_aligned={a.get('checkpointSha256') == fixture_sha}"
            )
            if not no_delta:
                detail += f" ({no_delta_detail})"
            return fail_closed, detail

        gate(
            INJECTION_CHECKPOINT_LOSS,
            "checkpoint_loss_fail_closed",
            loss,
            "expectedCheckpointLossFailClosed",
            "checkpoint_loss_fail_closed",
            METRIC_CHECKPOINT_LOSS,
            derive_loss,
        )
        checkpoint_fixture_bound(
            "checkpoint_loss_fail_closed", "checkpoint_loss", METRIC_CHECKPOINT_LOSS
        )
    return checks


def _durable_deltas(prediction: Mapping[str, Any]) -> Mapping[str, Any]:
    """Derived revision/history deltas for replay and revision attempts, from
    runner-owned before/after counts.  None when no such attempt was recorded."""
    attempts = prediction.get("observations", {}).get("attempts", [])
    deltas: dict[str, Any] = {}
    for kind, prefix in (
        ("exact_replay", "exactReplay"),
        ("mutated_replay", "mutatedReplay"),
        ("revision_conflict", "revisionConflict"),
    ):
        rev_delta = None
        hist_delta = None
        for attempt in attempts:
            if attempt["attemptKind"] != kind:
                continue
            rb, ra = attempt.get("revisionBefore"), attempt.get("revisionAfter")
            hb, ha = attempt.get("historyCountBefore"), attempt.get("historyCountAfter")
            if None not in (rb, ra, hb, ha):
                rev_delta = ra - rb
                hist_delta = ha - hb
                break
        deltas[f"{prefix}RevisionDelta"] = rev_delta
        deltas[f"{prefix}HistoryDelta"] = hist_delta
    return deltas


def _checks_infra(
    scenario: AlignedScenario, oracle: Mapping[str, Any], prediction: Mapping[str, Any]
) -> list[CheckResult]:
    checks = []
    conditions = oracle["successConditions"]
    expected_terminal = conditions["expectedTerminalClasses"]
    checks.append(
        CheckResult(
            "terminal_class",
            prediction["terminalClass"] in expected_terminal,
            f"prediction={prediction['terminalClass']} expected={expected_terminal}",
            metric=METRIC_TERMINAL,
        )
    )
    checks.extend(_cardinality_checks(scenario, oracle, prediction))
    checks.extend(_derive_durable(scenario, oracle, prediction, expected_terminal))
    checks.extend(_constraint_checks(scenario, oracle, prediction))
    checks.extend(_unsupported_claim_checks(scenario, oracle, prediction))
    checks.extend(_tool_checks(oracle, prediction))
    return checks


def _score_one(scenario: AlignedScenario) -> Mapping[str, Any]:
    if scenario.track_type == TRACK_LANGUAGE:
        checks = _checks_language(scenario, scenario.oracle, scenario.prediction)
    else:
        checks = _checks_infra(scenario, scenario.oracle, scenario.prediction)
    failed = [check for check in checks if not check.passed]
    calls = scenario.prediction.get("toolCalls", [])
    return {
        "scenarioId": scenario.scenario_id,
        "trackType": scenario.track_type,
        "passed": not failed,
        "reason": (
            "ok"
            if not failed
            else "failed checks: " + ", ".join(sorted({c.name for c in failed}))
        ),
        "checks": [check.as_dict() for check in checks],
        "latencyMs": scenario.prediction.get("latencyMs"),
        "metricCounts": {
            "toolCallCount": len(calls),
            "duplicateToolCallCount": _count_duplicate_tools(calls),
        },
        "durableDeltas": _durable_deltas(scenario.prediction),
    }


def _latency_stats(values: Sequence[float]) -> Mapping[str, Any]:
    if not values:
        return {"count": 0, "min": None, "max": None, "mean": None, "p50": None, "p95": None}
    ordered = sorted(values)
    count = len(ordered)
    p50_index = (count - 1) // 2
    p95_index = max(0, min(count - 1, int(math.ceil(count * 0.95)) - 1))
    return {
        "count": count,
        "min": ordered[0],
        "max": ordered[-1],
        "mean": sum(ordered) / count,
        "p50": ordered[p50_index],
        "p95": ordered[p95_index],
    }


def _track_summary(track_type: str, results: Sequence[Mapping[str, Any]]) -> Mapping[str, Any]:
    """Per-track aggregate.  metrics aggregates eligible/pass/fail per metric
    family, and counts aggregates tool counts — both recomputed from the
    per-scenario results, never independently self-reported."""
    passed = sum(1 for result in results if result["passed"])
    latencies = [
        result["latencyMs"] for result in results if result.get("latencyMs") is not None
    ]
    metrics = {family: {"eligible": 0, "pass": 0, "fail": 0} for family in METRIC_FAMILIES}
    for result in results:
        by_metric: dict[str, list[bool]] = {}
        for check in result["checks"]:
            by_metric.setdefault(check["metric"], []).append(check["passed"])
        for family, flags in by_metric.items():
            metrics[family]["eligible"] += 1
            if all(flags):
                metrics[family]["pass"] += 1
            else:
                metrics[family]["fail"] += 1

    count = len(results)

    def _count_stat(field: str) -> Mapping[str, Any]:
        total = sum(result["metricCounts"][field] for result in results)
        return {
            "count": count,
            "total": total,
            "mean": (total / count) if count else None,
        }

    return {
        "trackType": track_type,
        "scenarioCount": count,
        "passed": passed,
        "failed": count - passed,
        "latencyMs": _latency_stats(latencies),
        "metrics": metrics,
        "counts": {
            "toolCallCount": _count_stat("toolCallCount"),
            "duplicateToolCallCount": _count_stat("duplicateToolCallCount"),
        },
    }


def _report_id(scenario_set: ScenarioSet) -> str:
    digest = hashlib.sha256()
    for value in (
        scenario_set.input_file_sha256,
        scenario_set.oracle_file_sha256,
        scenario_set.fault_file_sha256,
        scenario_set.prediction_file_sha256,
    ):
        digest.update(value.encode("ascii"))
    return f"report-{digest.hexdigest()[:16]}"


def score_scenario_set(scenario_set: ScenarioSet) -> Mapping[str, Any]:
    """Deterministic per-scenario and per-track scoring.

    Language and infrastructure-fault scenarios are aggregated separately and
    never mixed into one number.  The report is fully reproducible and always
    declares `exploratory=true` / `statisticalSignificanceClaimed=false`.
    """
    per_scenario = []
    language_results = []
    infra_results = []
    for scenario in scenario_set.scenarios:
        result = _score_one(scenario)
        per_scenario.append(result)
        if scenario.track_type == TRACK_LANGUAGE:
            language_results.append(result)
        else:
            infra_results.append(result)

    per_scenario.sort(key=lambda result: result["scenarioId"])
    report = {
        "reportId": _report_id(scenario_set),
        "schemaVersion": REPORT_SCHEMA_VERSION,
        "exploratory": True,
        "statisticalSignificanceClaimed": False,
        "sourceInputs": {
            "inputFileSha256": scenario_set.input_file_sha256,
            "oracleFileSha256": scenario_set.oracle_file_sha256,
            "faultFileSha256": scenario_set.fault_file_sha256,
            "predictionFileSha256": scenario_set.prediction_file_sha256,
        },
        "tracks": {
            TRACK_LANGUAGE: _track_summary(TRACK_LANGUAGE, language_results),
            TRACK_INFRA: _track_summary(TRACK_INFRA, infra_results),
        },
        "perScenario": per_scenario,
    }
    validate_report(report)
    return report
