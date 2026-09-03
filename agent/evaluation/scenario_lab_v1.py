"""Independent evaluator/loader for Agentic Commerce Benchmark V1.

This module intentionally does not import production Agent code.  It enforces
physical public/private separation, sealed-world alignment, constraint AST
semantics, bundle/cart verification and fail-closed handling of unknown facts.
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

from .commerce_world_v1 import load_world_manifest, load_world_products

SCHEMAS_DIR = Path(__file__).resolve().parent / "schemas"
SCHEMA_FILENAMES = {
    "world": "commerce_world_manifest_v1.schema.json",
    "input": "commerce_scenario_input_v1.schema.json",
    "oracle": "commerce_oracle_private_v1.schema.json",
    "fault": "commerce_fault_private_v1.schema.json",
    "prediction": "commerce_prediction_v1.schema.json",
    "receipt": "commerce_runner_receipt_v1.schema.json",
    "report": "commerce_score_report_v1.schema.json",
}
SCHEMA_VERSIONS = {
    "world": "commerce-world-manifest-v1",
    "input": "commerce-scenario-input-v1",
    "oracle": "commerce-oracle-private-v1",
    "fault": "commerce-fault-private-v1",
    "prediction": "commerce-prediction-v1",
    "receipt": "commerce-runner-receipt-v1",
    "report": "commerce-score-report-v1",
}


class CommerceScenarioError(ValueError):
    """A V1 contract invariant failed; callers must fail closed."""


_validators: dict[str, Draft202012Validator] = {}


def canonical_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def sha256_file(path: Path | str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _digest_json(value: Any) -> str:
    return sha256_bytes(canonical_bytes(value))


_RUNNER_COLLECTOR = "commerce-benchmark-runner"
_RUNNER_VERSION = "v1"
_ANSWER_EXTRACTOR = "commerce-claim-extractor"
_ANSWER_EXTRACTOR_VERSION = "v1"
_ZERO_DIGEST = "0" * 64


def _trace_field_digest(trace: Sequence[Mapping[str, Any]], field: str) -> str:
    return _digest_json([item.get(field) for item in trace])


def make_runner_receipt(
    prediction: Mapping[str, Any],
    oracle: Mapping[str, Any],
    fault: Mapping[str, Any],
    *,
    world_manifest_sha256: str,
    environment_artifact_sha256: str,
    status: str = "ATTESTED",
) -> Mapping[str, Any]:
    """Build a separate receipt for a trusted harness test double.

    The returned object is *not* part of a prediction.  In production the
    harness, rather than the model output channel, writes this JSONL record.
    This helper exists only to make the receipt contract deterministic in
    offline tests; the scorer still recomputes every digest and identity.
    """
    trace = list(prediction.get("toolTrace", []))
    if status == "NOT_COLLECTED":
        trace_digest = fault_digest = before_digest = after_digest = output_digest = answer_digest = claims_digest = _ZERO_DIGEST
    else:
        trace_digest = _digest_json(trace)
        fault_digest = _digest_json(fault)
        before_digest = _trace_field_digest(trace, "beforeObservationDigest")
        after_digest = _trace_field_digest(trace, "afterObservationDigest")
        output_digest = _trace_field_digest(trace, "outputDigest")
        answer_digest = _digest_json(str(prediction.get("answerText", "")))
        claims_digest = _digest_json(list(prediction.get("claims", [])))
    return {
        "receiptId": f"receipt-{prediction.get('predictionId', '')}",
        "schemaVersion": SCHEMA_VERSIONS["receipt"],
        "scenarioId": str(prediction.get("scenarioId", "")),
        "predictionId": str(prediction.get("predictionId", "")),
        "runId": str(prediction.get("runId", "")),
        "worldId": str(prediction.get("worldId", "")),
        "catalogRevision": str(prediction.get("catalogRevision", "")),
        "environmentRevision": str(prediction.get("environmentRevision", "")),
        "attestationStatus": status,
        "collector": _RUNNER_COLLECTOR,
        "collectorVersion": _RUNNER_VERSION,
        "answerExtractor": _ANSWER_EXTRACTOR,
        "answerExtractorVersion": _ANSWER_EXTRACTOR_VERSION,
        "predictionSha256": _digest_json(prediction),
        "oracleSha256": _digest_json(oracle),
        "faultSha256": _digest_json(fault),
        "worldManifestSha256": str(world_manifest_sha256),
        "environmentArtifactSha256": str(environment_artifact_sha256),
        "traceDigestSha256": trace_digest,
        "faultDigestSha256": fault_digest,
        "beforeObservationDigestSha256": before_digest,
        "afterObservationDigestSha256": after_digest,
        "outputDigestSha256": output_digest,
        "answerText": str(prediction.get("answerText", "")),
        "answerDigestSha256": answer_digest,
        "claimsDigestSha256": claims_digest,
    }


def _validator(kind: str) -> Draft202012Validator:
    if kind not in SCHEMA_FILENAMES:
        raise ValueError(f"unknown V1 record kind: {kind}")
    if kind not in _validators:
        with open(SCHEMAS_DIR / SCHEMA_FILENAMES[kind], encoding="utf-8") as fh:
            schema = json.load(fh)
        Draft202012Validator.check_schema(schema)
        _validators[kind] = Draft202012Validator(schema)
    return _validators[kind]


def validate_record(record: Mapping[str, Any], kind: str) -> None:
    if not isinstance(record, Mapping):
        raise CommerceScenarioError(f"{kind}: record must be an object")
    errors = sorted(_validator(kind).iter_errors(record), key=lambda error: list(error.path))
    if errors:
        error = errors[0]
        location = ".".join(str(part) for part in error.path) or "$"
        raise CommerceScenarioError(f"{kind}.{location}: {error.message}")


def load_jsonl(path: Path | str, kind: str) -> list[Mapping[str, Any]]:
    records: list[Mapping[str, Any]] = []
    with open(path, encoding="utf-8") as fh:
        for line_no, line in enumerate(fh, 1):
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError as exc:
                raise CommerceScenarioError(f"{path}:{line_no}: invalid JSON") from exc
            if not isinstance(value, Mapping):
                raise CommerceScenarioError(f"{path}:{line_no}: record is not an object")
            validate_record(value, kind)
            records.append(value)
    if not records:
        raise CommerceScenarioError(f"{kind} file is empty: {path}")
    return records


def write_jsonl(path: Path | str, records: Sequence[Mapping[str, Any]], kind: str | None = None) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        for record in records:
            if kind:
                validate_record(record, kind)
                if kind == "input":
                    audit_public_input_leaks(record)
            fh.write(canonical_bytes(record).decode("utf-8"))


def _normal(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", text.lower())


PUBLIC_FORBIDDEN_MARKERS = (
    "intentfamily", "complexitystratum", "acceptableproduct", "acceptablebundle",
    "expectedproduct", "expectedrank", "expectedids", "correctproduct", "goldproduct",
    "expectedtool", "expectedpath", "expectedoutcome", "expectedterminal", "mustobserve",
    "faultprivate", "faultplan", "oracleprivate", "groundtruth", "goldanswer", "answerkey",
    "scorethreshold", "hiddenoracle", "targetproduct", "targetids", "solutionids",
    "initialtaskstate", "taskstatedelta", "futureenvironment", "futureprice", "futurestock",
    "constraintast", "expectedconstraint", "correctanswer", "privateoracle", "privatefault",
)


def _walk(node: Any, path: str = "$"):
    if isinstance(node, Mapping):
        for key, value in node.items():
            yield from _walk_key(str(key), f"{path}.{key}")
            yield from _walk(value, f"{path}.{key}")
    elif isinstance(node, Sequence) and not isinstance(node, (str, bytes, bytearray)):
        for index, value in enumerate(node):
            yield from _walk(value, f"{path}[{index}]")
    elif isinstance(node, str):
        yield path, node


def _walk_key(key: str, path: str):
    yield path, key


def _decode_base64(text: str) -> str | None:
    compact = re.sub(r"\s+", "", text)
    if len(compact) < 16 or len(compact) % 4:
        return None
    if not re.fullmatch(r"[A-Za-z0-9+/]+={0,2}", compact):
        return None
    try:
        decoded = base64.b64decode(compact, validate=True).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return None
    return decoded


def audit_public_input_leaks(record: Mapping[str, Any]) -> None:
    """Reject renamed/nested/base64 private answer fields before execution."""
    if not isinstance(record, Mapping):
        raise CommerceScenarioError("public input must be an object")
    for path, value in _walk(record):
        normalized = _normal(value)
        for marker in PUBLIC_FORBIDDEN_MARKERS:
            if marker in normalized:
                raise CommerceScenarioError(f"public input leak at {path}: marker {marker!r}")
        decoded = _decode_base64(value)
        if decoded:
            decoded_normalized = _normal(decoded)
            for marker in PUBLIC_FORBIDDEN_MARKERS:
                if marker in decoded_normalized:
                    raise CommerceScenarioError(f"public input base64 leak at {path}: marker {marker!r}")


def _private_values_for_public_audit(oracle: Mapping[str, Any], fault: Mapping[str, Any]) -> tuple[str, ...]:
    """Extract answer-bearing private values without treating all oracle text as secret."""
    values: list[str] = []
    conditions = oracle.get("successConditions", {}) if isinstance(oracle, Mapping) else {}
    for product_id in conditions.get("acceptableProductIds", []) or []:
        values.append(str(product_id))
    for bundle in conditions.get("acceptableBundles", []) or []:
        for product_id in bundle:
            values.append(str(product_id))
    for point in conditions.get("requiredObservationPoints", []) or []:
        values.append(str(point))
    for dependency in conditions.get("documentDependencies", []) or []:
        if isinstance(dependency, Mapping) and dependency.get("documentId") not in (None, ""):
            values.append(str(dependency["documentId"]))
    for injection in fault.get("injections", []) or []:
        if isinstance(injection, Mapping):
            for key in ("injectionId", "point", "trigger", "expectedInvariant"):
                if injection.get(key) not in (None, ""):
                    values.append(str(injection[key]))
    # Very short IDs (for example ``p1``) create false positives in ordinary
    # prose; only detect values with enough entropy to be a meaningful leak.
    return tuple(sorted({value for value in values if len(value) >= 4}, key=lambda item: (-len(item), item)))


def audit_public_private_value_leaks(public: Mapping[str, Any], oracle: Mapping[str, Any], fault: Mapping[str, Any]) -> None:
    """Reject private answer values copied into public text, nesting or base64."""
    private_values = _private_values_for_public_audit(oracle, fault)
    if not private_values:
        return
    for path, value in _walk(public):
        for private_value in private_values:
            if private_value in value:
                raise CommerceScenarioError(f"public input private value leak at {path}: {private_value!r}")
        decoded = _decode_base64(value)
        if decoded:
            for private_value in private_values:
                if private_value in decoded:
                    raise CommerceScenarioError(f"public input private value base64 leak at {path}: {private_value!r}")


@dataclass(frozen=True)
class PublicScenarioView:
    scenario_id: str
    world_id: str
    catalog_revision: str
    environment_revision: str
    turns: tuple[Mapping[str, Any], ...]


@dataclass(frozen=True)
class ScenarioBundle:
    scenario_id: str
    input: Mapping[str, Any]
    oracle: Mapping[str, Any]
    fault: Mapping[str, Any]
    prediction: Mapping[str, Any]
    receipt: Mapping[str, Any]


class ScenarioV1Set:
    """Private scorer-side aligned set; runner receives only PublicScenarioView."""

    def __init__(self, scenarios: Sequence[ScenarioBundle], file_sha256: Mapping[str, str], manifest_path: Path | None = None):
        self.scenarios = tuple(scenarios)
        self.file_sha256 = dict(file_sha256)
        self.manifest_path = manifest_path.resolve() if manifest_path is not None else None

    @classmethod
    def load(cls, input_path: Path | str, oracle_path: Path | str, fault_path: Path | str, prediction_path: Path | str, receipt_path: Path | str | None = None, *, manifest_path: Path | str | None = None) -> "ScenarioV1Set":
        if receipt_path is None:
            raise CommerceScenarioError("separate runner receipt JSONL is required")
        inputs = load_jsonl(input_path, "input")
        oracles = load_jsonl(oracle_path, "oracle")
        faults = load_jsonl(fault_path, "fault")
        predictions = load_jsonl(prediction_path, "prediction")
        receipts = load_jsonl(receipt_path, "receipt")
        records = {"input": inputs, "oracle": oracles, "fault": faults, "prediction": predictions, "receipt": receipts}
        indexes: dict[str, dict[str, Mapping[str, Any]]] = {}
        for kind, rows in records.items():
            ids = [row["scenarioId"] for row in rows]
            if len(ids) != len(set(ids)):
                raise CommerceScenarioError(f"duplicate scenarioId in {kind}")
            if kind == "prediction":
                prediction_ids = [str(row["predictionId"]) for row in rows]
                if len(prediction_ids) != len(set(prediction_ids)):
                    raise CommerceScenarioError("duplicate predictionId in prediction")
            if kind == "receipt":
                receipt_ids = [str(row["receiptId"]) for row in rows]
                if len(receipt_ids) != len(set(receipt_ids)):
                    raise CommerceScenarioError("duplicate receiptId in receipt")
            for row in rows:
                indexes.setdefault(str(row["scenarioId"]), {})[kind] = row
        expected = set(indexes)
        for scenario_id, bundle in indexes.items():
            if set(bundle) != set(records):
                raise CommerceScenarioError(f"scenario {scenario_id}: input/private/prediction set mismatch")
            public = bundle["input"]
            audit_public_input_leaks(public)
            for kind in ("oracle", "fault", "prediction"):
                if bundle[kind]["worldId"] != public["worldId"] or bundle[kind]["catalogRevision"] != public["catalogRevision"] or bundle[kind]["environmentRevision"] != public["environmentRevision"]:
                    raise CommerceScenarioError(f"scenario {scenario_id}: world/catalog/environment drift in {kind}")
            _validate_private_contract(bundle["oracle"], bundle["fault"])
            audit_public_private_value_leaks(public, bundle["oracle"], bundle["fault"])
            receipt = bundle["receipt"]
            if any(str(receipt.get(key, "")) != str(bundle["prediction"].get(key, "")) for key in ("scenarioId", "predictionId", "runId", "worldId", "catalogRevision", "environmentRevision")):
                raise CommerceScenarioError(f"scenario {scenario_id}: runner receipt identity mismatch")
        if manifest_path is not None:
            manifest = load_world_manifest(manifest_path)
            for scenario_id, bundle in indexes.items():
                if bundle["input"]["worldId"] != manifest["worldId"] or bundle["input"]["catalogRevision"] != manifest["catalogRevision"]:
                    raise CommerceScenarioError(f"scenario {scenario_id}: manifest identity mismatch")
                if bundle["input"]["environmentRevision"] not in manifest["environmentRevisions"]:
                    raise CommerceScenarioError(f"scenario {scenario_id}: unknown environment revision")
                sequence, _ = _resolve_environment_sequence(bundle["oracle"])
                if any(revision not in manifest["environmentRevisions"] for revision in sequence):
                    raise CommerceScenarioError(f"scenario {scenario_id}: environmentSequence names an unknown revision")
                if manifest.get("status") != "READY" and bundle["oracle"].get("oracleStatus") == "READY":
                    raise CommerceScenarioError(f"scenario {scenario_id}: READY oracle cannot use a PENDING_DATA_SCALE world")
        aligned = [ScenarioBundle(scenario_id, indexes[scenario_id]["input"], indexes[scenario_id]["oracle"], indexes[scenario_id]["fault"], indexes[scenario_id]["prediction"], indexes[scenario_id]["receipt"]) for scenario_id in sorted(expected)]
        return cls(aligned, {name: sha256_file(path) for name, path in (("input", input_path), ("oracle", oracle_path), ("fault", fault_path), ("prediction", prediction_path), ("receipt", receipt_path))}, Path(manifest_path) if manifest_path is not None else None)


def load_runner_input(path: Path | str) -> tuple[PublicScenarioView, ...]:
    rows = load_jsonl(path, "input")
    views: list[PublicScenarioView] = []
    for row in rows:
        audit_public_input_leaks(row)
        views.append(PublicScenarioView(str(row["scenarioId"]), str(row["worldId"]), str(row["catalogRevision"]), str(row["environmentRevision"]), tuple(row["turns"])))
    return tuple(views)


def _resolve_environment_sequence(oracle: Mapping[str, Any]) -> tuple[tuple[str, ...], str]:
    """Return the private-oracle start-to-terminal environment sequence.

    ``environmentRevision`` remains the public/start identity.  The optional
    sequence is private contract data; it is never inferred from a prediction.
    Keeping this check in one helper makes direct verification, loading, and
    authoritative scoring share the same fail-closed semantics.
    """
    start = str(oracle.get("environmentRevision", "")).strip()
    if not start:
        raise CommerceScenarioError("environment sequence has no starting revision")
    conditions = oracle.get("successConditions")
    if not isinstance(conditions, Mapping):
        raise CommerceScenarioError("environment sequence requires successConditions")
    raw_sequence = conditions.get("environmentSequence")
    if raw_sequence is None:
        return (start,), start
    if not isinstance(raw_sequence, list) or not raw_sequence:
        raise CommerceScenarioError("environmentSequence must be a non-empty array")
    if any(not isinstance(revision, str) or not revision.strip() for revision in raw_sequence):
        raise CommerceScenarioError("environmentSequence revisions must be non-empty strings")
    sequence = tuple(str(revision).strip() for revision in raw_sequence)
    if len(sequence) != len(set(sequence)):
        raise CommerceScenarioError("environmentSequence revisions must be unique")
    if sequence[0] != start:
        raise CommerceScenarioError("environmentSequence must start at oracle environmentRevision")
    return sequence, sequence[-1]


def _validate_private_contract(oracle: Mapping[str, Any], fault: Mapping[str, Any]) -> None:
    """Validate oracle/fault closure before any scoring or runner handoff."""
    conditions = oracle["successConditions"]
    _resolve_environment_sequence(oracle)
    injections = list(fault.get("injections", []))
    points = [str(injection.get("point")) for injection in injections]
    if len(points) != len(set(points)):
        raise CommerceScenarioError("fault injection points must be unique")
    injection_ids = [str(injection.get("injectionId")) for injection in injections]
    if len(injection_ids) != len(set(injection_ids)):
        raise CommerceScenarioError("fault injection IDs must be unique")
    if oracle.get("oracleStatus") in {"PENDING_DATA_SCALE", "PENDING_SCENARIO_AUTHORING"}:
        if fault.get("faultStatus") != oracle.get("oracleStatus"):
            raise CommerceScenarioError("pending oracle/fault status mismatch")
        return
    expected_fault_status = "READY" if injections else "NONE"
    if fault.get("faultStatus") != expected_fault_status:
        raise CommerceScenarioError("ready oracle/fault status mismatch")
    dependencies = conditions.get("documentDependencies", []) or []
    if not isinstance(dependencies, list):
        raise CommerceScenarioError("documentDependencies must be an array")
    dependency_keys: set[tuple[str, str, str, str, str]] = set()
    for dependency in dependencies:
        if not isinstance(dependency, Mapping):
            raise CommerceScenarioError("document dependency must be an object")
        document_type = str(dependency.get("documentType", ""))
        binding = str(dependency.get("binding", ""))
        if (document_type, binding) not in {("knowledge", "product_constraint"), ("policy", "selected_product_merchant")}:
            raise CommerceScenarioError("document dependency type/binding is invalid")
        key = (document_type, str(dependency.get("documentId", "")), str(dependency.get("field", "")), str(dependency.get("operator", "")), json.dumps(dependency.get("value"), ensure_ascii=False, sort_keys=True, separators=(",", ":")))
        if not all(key_part for key_part in key[:4]) or key in dependency_keys:
            raise CommerceScenarioError("document dependency identity is not unique")
        dependency_keys.add(key)
        if document_type == "knowledge" and not _ast_contains_atom(conditions.get("constraintAst", {}), dependency):
            raise CommerceScenarioError("knowledge dependency is absent from constraintAst")
    solution_type = conditions.get("solutionType")
    if solution_type == "single_product" and not conditions.get("acceptableProductIds"):
        if not conditions.get("allowNoAnswer") or "SUCCESS" in conditions.get("terminalClasses", []):
            raise CommerceScenarioError("ready single_product oracle has no acceptable universe")
    if solution_type == "bundle" and not conditions.get("acceptableBundles"):
        raise CommerceScenarioError("ready bundle oracle has no acceptable bundles")
    if solution_type == "cart" and not conditions.get("cartRules"):
        raise CommerceScenarioError("ready cart oracle has no cart rules")
    if oracle.get("complexityStratum") == "S2_OBSERVATION_DEPENDENT":
        required = list(conditions.get("requiredObservationPoints", []))
        if not required or sorted(required) != sorted(points):
            raise CommerceScenarioError("S2 oracle/fault required observation closure mismatch")


def _fact_value(facts: Mapping[str, Any], field: str) -> tuple[bool, Any]:
    value = facts.get(field)
    if isinstance(value, Mapping):
        return bool(value.get("known", False)), value.get("value")
    return (value is not None), value


def _evaluate_constraint_state(node: Mapping[str, Any], facts: Mapping[str, Any]) -> str:
    """Return TRUE/FALSE/UNKNOWN; UNKNOWN is never made true by NOT."""
    kind = node.get("kind")
    if kind == "all":
        children = list(node.get("children", []))
        if not children:
            return "FALSE"
        states = [_evaluate_constraint_state(child, facts) for child in children]
        if "FALSE" in states:
            return "FALSE"
        return "TRUE" if all(state == "TRUE" for state in states) else "FALSE"
    if kind == "any":
        children = list(node.get("children", []))
        if not children:
            return "FALSE"
        states = [_evaluate_constraint_state(child, facts) for child in children]
        return "TRUE" if "TRUE" in states else "FALSE"
    if kind == "not":
        child_state = _evaluate_constraint_state(node["child"], facts)
        return "TRUE" if child_state == "FALSE" else "FALSE"
    if kind != "atom":
        return "FALSE"
    atom = node.get("atom", {})
    known, actual = _fact_value(facts, str(atom.get("field", "")))
    if not known:
        return "UNKNOWN"
    expected = atom.get("value")
    operator = atom.get("operator")
    try:
        if operator == "EQ": return "TRUE" if actual == expected else "FALSE"
        if operator == "NEQ": return "TRUE" if actual != expected else "FALSE"
        if operator == "IN": return "TRUE" if isinstance(expected, (list, tuple, set)) and actual in expected else "FALSE"
        if operator == "NOT_IN": return "TRUE" if isinstance(expected, (list, tuple, set)) and actual not in expected else "FALSE"
        if operator == "CONTAINS": return "TRUE" if str(expected) in str(actual) else "FALSE"
        if operator == "LTE": return "TRUE" if float(actual) <= float(expected) else "FALSE"
        if operator == "GTE": return "TRUE" if float(actual) >= float(expected) else "FALSE"
    except (TypeError, ValueError):
        return "FALSE"
    return "FALSE"


def evaluate_constraint_ast(node: Mapping[str, Any], facts: Mapping[str, Any]) -> bool:
    return _evaluate_constraint_state(node, facts) == "TRUE"


def _ast_contains_atom(node: Mapping[str, Any], dependency: Mapping[str, Any]) -> bool:
    """Return whether an oracle AST contains the exact document predicate atom."""
    if not isinstance(node, Mapping):
        return False
    kind = node.get("kind")
    if kind == "atom":
        atom = node.get("atom")
        return isinstance(atom, Mapping) and all(
            atom.get(key) == dependency.get(key)
            for key in ("field", "operator", "value")
        ) and atom.get("unknownPolicy") == "fail"
    if kind == "not":
        child = node.get("child")
        return isinstance(child, Mapping) and _ast_contains_atom(child, dependency)
    if kind in {"all", "any"}:
        return any(isinstance(child, Mapping) and _ast_contains_atom(child, dependency) for child in node.get("children", []))
    return False


def _product_map(products: Sequence[Mapping[str, Any]]) -> dict[str, Mapping[str, Any]]:
    return {str(product["productId"]): product for product in products}


_ACTIVE_ENVIRONMENT_FIELDS = frozenset(("price", "stock", "promotionEligible", "environmentRevision"))


def _constraint_mentions_active_environment(node: Mapping[str, Any]) -> bool:
    kind = node.get("kind")
    if kind == "atom":
        return str((node.get("atom") or {}).get("field", "")) in _ACTIVE_ENVIRONMENT_FIELDS
    if kind == "not":
        child = node.get("child")
        return isinstance(child, Mapping) and _constraint_mentions_active_environment(child)
    if kind in {"all", "any"}:
        return any(isinstance(child, Mapping) and _constraint_mentions_active_environment(child) for child in node.get("children", []))
    return False


def _product_satisfies(
    product: Mapping[str, Any],
    ast: Mapping[str, Any],
    *,
    active_environment_row: Mapping[str, Any] | None = None,
    require_active_environment: bool = False,
    active_environment_revision: str | None = None,
) -> bool:
    """Evaluate static facts plus the exact active offer when one is bound.

    Catalog ``price`` is only a fallback for legacy/non-bound callers.  A
    verifier with an active environment must supply one unique, validated offer
    row; dynamic fields are then overlaid on the product facts so price/stock/
    promotion constraints cannot accidentally read the catalog snapshot.
    """
    facts = dict(product.get("facts") or {})
    if require_active_environment:
        if active_environment_row is None:
            return False
        if str(active_environment_row.get("productId")) != str(product.get("productId")):
            return False
        if not isinstance(active_environment_row.get("environmentRevision"), str):
            return False
        if active_environment_revision is not None and active_environment_row.get("environmentRevision") != active_environment_revision:
            return False
        for field in ("price", "stock", "promotionEligible"):
            if field not in active_environment_row:
                return False
            facts[field] = {"known": True, "value": active_environment_row[field]}
        facts["environmentRevision"] = {"known": True, "value": active_environment_row["environmentRevision"]}
    else:
        if active_environment_row is None and _constraint_mentions_active_environment(ast):
            return False
        facts.setdefault("price", {"known": True, "value": product.get("price")})
    facts.setdefault("merchantId", {"known": True, "value": product.get("merchantId")})
    facts.setdefault("category", {"known": True, "value": product.get("category")})
    return evaluate_constraint_ast(ast, facts)


def calculate_cart_total(cart_items: Sequence[Mapping[str, Any]], products: Sequence[Mapping[str, Any]], coupons: Sequence[Mapping[str, Any]] = (), *, environment_rows: Sequence[Mapping[str, Any]] = (), environment_revision: str | None = None) -> tuple[float, float, float]:
    by_id = _product_map(products)
    offers = {str(row.get("productId")): row for row in environment_rows}
    if environment_revision is not None and any(row.get("environmentRevision") != environment_revision for row in environment_rows):
        raise CommerceScenarioError("cart offer environment revision mismatch")
    if len({str(item.get("productId")) for item in cart_items}) != len(cart_items):
        raise CommerceScenarioError("cart contains duplicate product item rows")
    coupon_ids = [str(coupon.get("couponId", "")) for coupon in coupons]
    if len(coupon_ids) != len(set(coupon_ids)):
        raise CommerceScenarioError("duplicate coupon ID")
    if len(coupons) > 1 and any(not bool(coupon.get("stackable", False)) for coupon in coupons):
        raise CommerceScenarioError("illegal coupon stacking")
    subtotal = 0.0
    merchant_subtotals: dict[str, float] = {}
    for item in cart_items:
        product = by_id.get(str(item.get("productId")))
        quantity = item.get("quantity")
        if product is None or not isinstance(quantity, int) or quantity < 1:
            raise CommerceScenarioError("cart contains unknown product or invalid quantity")
        offer = offers.get(str(item.get("productId")))
        if environment_rows and offer is None:
            raise CommerceScenarioError("cart product has no current environment offer")
        if offer is not None and int(offer.get("stock", 0)) < quantity:
            raise CommerceScenarioError("cart requests more than current stock")
        line_total = float((offer or product).get("price", 0)) * quantity
        subtotal += line_total
        merchant_id = str(product.get("merchantId", ""))
        if not merchant_id:
            raise CommerceScenarioError("cart product has no merchant identity")
        merchant_subtotals[merchant_id] = merchant_subtotals.get(merchant_id, 0.0) + line_total
    discount = 0.0
    consumed_by_merchant: dict[str, float] = {}
    platform_consumed = 0.0
    # Canonical couponId order makes direct calculation and authoritative
    # verification invariant to the order in which the model supplied IDs.
    ordered_coupons = sorted(coupons, key=lambda coupon: str(coupon.get("couponId", "")))
    for coupon in ordered_coupons:
        scope = str(coupon.get("scope", "merchant"))
        if scope == "platform":
            applicable_subtotal = subtotal
        elif scope == "merchant":
            merchant_id = str(coupon.get("merchantId", ""))
            if not merchant_id or merchant_id not in merchant_subtotals:
                raise CommerceScenarioError("merchant coupon has no applicable cart items")
            applicable_subtotal = merchant_subtotals[merchant_id]
        else:
            raise CommerceScenarioError("unknown coupon scope")
        if environment_revision is not None and coupon.get("environmentRevision") != environment_revision:
            raise CommerceScenarioError("coupon environment revision mismatch")
        threshold = float(coupon.get("threshold", math.inf))
        if not math.isfinite(threshold) or threshold < 0:
            raise CommerceScenarioError("coupon threshold is invalid")
        if applicable_subtotal < threshold:
            continue
        if coupon.get("kind") == "fixed":
            single_discount = float(coupon.get("amount", 0))
        elif coupon.get("kind") == "percent":
            single_discount = applicable_subtotal * float(coupon.get("amount", 0))
        else:
            raise CommerceScenarioError("unknown coupon kind")
        cap = float(coupon.get("cap", single_discount))
        if not math.isfinite(single_discount) or not math.isfinite(cap) or single_discount < 0 or cap < 0:
            raise CommerceScenarioError("coupon discount is invalid")
        # A coupon can never discount beyond its own applicable subtotal.  A
        # second stacked coupon also cannot make the cart discount exceed the
        # cart subtotal, keeping the reported total arithmetic non-negative.
        if scope == "merchant":
            merchant_id = str(coupon.get("merchantId", ""))
            remaining_scope = min(
                max(0.0, applicable_subtotal - consumed_by_merchant.get(merchant_id, 0.0)),
                max(0.0, subtotal - discount),
            )
        else:
            # Platform coupons share the remaining whole-cart capacity with
            # all merchant coupons, so mixed scopes can never go negative.
            remaining_scope = max(0.0, subtotal - (discount - platform_consumed) - platform_consumed)
        single_discount = min(single_discount, cap, remaining_scope)
        if scope == "merchant":
            consumed_by_merchant[merchant_id] = consumed_by_merchant.get(merchant_id, 0.0) + single_discount
        else:
            platform_consumed += single_discount
        discount = min(subtotal, discount + single_discount)
    return round(subtotal, 2), round(discount, 2), round(max(0.0, subtotal - discount), 2)


def _expected_fact(product: Mapping[str, Any], field: str, catalog_revision: str, environment_revision: str, environment_rows: Sequence[Mapping[str, Any]]) -> tuple[bool, Any, str, str, str] | None:
    """Return (known, value, sourceRef, revision, factTier) from frozen world."""
    if field in {"price", "stock", "promotionEligible", "environmentRevision"}:
        for row in environment_rows:
            if str(row.get("productId")) == str(product.get("productId")) and row.get("environmentRevision") == environment_revision and field in row:
                return True, row[field], str(row.get("sourceRef", "")), environment_revision, str(row.get("factTier", "synthetic_fixture"))
        if environment_rows:
            return None
    facts = product.get("facts") or {}
    if field in facts and isinstance(facts[field], Mapping):
        fact = facts[field]
        if not fact.get("known", False):
            return False, None, str(fact.get("sourceRef", "")), catalog_revision, str(fact.get("factTier", "source_claim"))
        return True, fact.get("value"), str(fact.get("sourceRef", "")), catalog_revision, str(fact.get("factTier", "source_claim"))
    if field in {"productId", "category", "merchantId", "title"} and field in product:
        return True, product[field], str(product.get("sourceRef", "")), catalog_revision, "source_claim"
    return None


def _verify_citations(
    oracle: Mapping[str, Any],
    prediction: Mapping[str, Any],
    products: Sequence[Mapping[str, Any]],
    selected: Sequence[str],
    environment_rows: Sequence[Mapping[str, Any]],
    *,
    environment_revision: str | None = None,
) -> tuple[bool, str]:
    citations = prediction.get("evidenceCitations", [])
    by_id = _product_map(products)
    seen_ids: set[str] = set()
    selected_ids = set(selected)
    required_fields = set(oracle["successConditions"].get("evidence", {}).get("requiredFields", []))
    required_fields_by_product = {product_id: set(required_fields) for product_id in selected_ids}
    active_revision = str(environment_revision if environment_revision is not None else oracle["environmentRevision"])
    cited_selected: set[str] = set()
    for citation in citations:
        evidence_id = str(citation.get("evidenceId", ""))
        product_id = str(citation.get("productId", ""))
        if not evidence_id or evidence_id in seen_ids or product_id not in by_id:
            return False, "duplicate/unknown evidence identity"
        seen_ids.add(evidence_id)
        expected = _expected_fact(by_id[product_id], str(citation.get("field", "")), str(oracle["catalogRevision"]), active_revision, environment_rows)
        if expected is None:
            return False, "citation field has no frozen provenance"
        known, value, source_ref, revision, tier = expected
        if not known or citation.get("value") != value or str(citation.get("sourceRef")) != source_ref or str(citation.get("revision")) != revision or str(citation.get("factTier")) != tier:
            return False, "citation does not equal frozen world fact"
        if product_id in selected_ids:
            cited_selected.add(product_id)
            required_fields_by_product[product_id].discard(str(citation.get("field")))
    if not selected_ids.issubset(cited_selected):
        return False, "selected product lacks bound evidence"
    missing_required = {
        product_id: sorted(fields)
        for product_id, fields in required_fields_by_product.items()
        if fields
    }
    if missing_required:
        return False, f"required evidence field missing per selected product: {missing_required}"
    if len(citations) < int(oracle["successConditions"].get("evidence", {}).get("minCitations", 0)):
        return False, "citation count below oracle minimum"
    return True, "citations match frozen world"


def _operator_matches(actual: Any, operator: str, expected: Any) -> bool:
    """Evaluate a document dependency without coercing unknown/malformed values."""
    if actual is None:
        return False
    try:
        if operator == "EQ":
            return actual == expected
        if operator == "NEQ":
            return actual != expected
        if operator == "IN":
            return isinstance(expected, (list, tuple, set)) and actual in expected
        if operator == "NOT_IN":
            return isinstance(expected, (list, tuple, set)) and actual not in expected
        if operator == "CONTAINS":
            return isinstance(actual, str) and str(expected) in actual
        if operator == "LTE":
            return float(actual) <= float(expected)
        if operator == "GTE":
            return float(actual) >= float(expected)
    except (TypeError, ValueError):
        return False
    return False


def _document_row_map(rows: Sequence[Mapping[str, Any]], key: str) -> dict[str, Mapping[str, Any]] | None:
    result: dict[str, Mapping[str, Any]] = {}
    for row in rows:
        if not isinstance(row, Mapping):
            return None
        identity = str(row.get(key, ""))
        if not identity or identity in result:
            return None
        result[identity] = row
    return result


def _document_expected_value(document_type: str, row: Mapping[str, Any], field: str) -> Any:
    if document_type == "knowledge":
        # A knowledge citation names the real product attribute that the
        # document constrains, not an arbitrary document label.
        if str(row.get("attributeField", "")) != field:
            return None
        return row.get("attributeValue")
    terms = row.get("terms")
    if not isinstance(terms, Mapping) or field not in terms:
        return None
    return terms[field]


def _verify_document_citations(
    oracle: Mapping[str, Any],
    prediction: Mapping[str, Any],
    products: Sequence[Mapping[str, Any]],
    selected: Sequence[str],
    knowledge: Sequence[Mapping[str, Any]],
    policies: Sequence[Mapping[str, Any]],
    reserved_evidence_ids: set[str],
) -> tuple[bool, str]:
    """Verify exact frozen knowledge/policy citations and every dependency."""
    conditions = oracle.get("successConditions", {})
    dependencies = conditions.get("documentDependencies", []) or []
    if "documentCitations" not in prediction:
        return False, "prediction is missing required documentCitations"
    citations = prediction.get("documentCitations", [])
    if not isinstance(dependencies, list) or not isinstance(citations, list):
        return False, "document dependencies/citations must be arrays"
    knowledge_by_id = _document_row_map(knowledge, "knowledgeId")
    policy_by_id = _document_row_map(policies, "policyId")
    if knowledge_by_id is None or policy_by_id is None:
        return False, "document artifact identities are not unique"
    seen_ids: set[str] = set()
    for citation in citations:
        if not isinstance(citation, Mapping):
            return False, "document citation is not an object"
        evidence_id = str(citation.get("evidenceId", ""))
        document_type = str(citation.get("documentType", ""))
        document_id = str(citation.get("documentId", ""))
        if not evidence_id or evidence_id in seen_ids or evidence_id in reserved_evidence_ids:
            return False, "product/document evidenceId collision"
        seen_ids.add(evidence_id)
        row_map = knowledge_by_id if document_type == "knowledge" else policy_by_id if document_type == "policy" else None
        if row_map is None or document_id not in row_map:
            return False, "unknown document citation identity"
        row = row_map[document_id]
        field = str(citation.get("field", ""))
        expected_value = _document_expected_value(document_type, row, field)
        if expected_value is None:
            return False, "document citation field is not a frozen document fact"
        if citation.get("value") != expected_value or str(citation.get("sourceRef", "")) != str(row.get("sourceRef", "")) or str(citation.get("revision", "")) != str(oracle.get("catalogRevision", "")) or str(citation.get("factTier", "")) != str(row.get("factTier", "")):
            return False, "document citation does not equal frozen document fact"
        if document_type == "policy":
            if str(citation.get("documentVersion", "")) != str(row.get("version", "")):
                return False, "policy document version drift"
        elif citation.get("documentVersion") not in (None, ""):
            return False, "knowledge document has an unexpected version"

    product_map = _product_map(products)
    for dependency in dependencies:
        if not isinstance(dependency, Mapping):
            return False, "document dependency is not an object"
        document_type = str(dependency.get("documentType", ""))
        binding = str(dependency.get("binding", ""))
        document_id = str(dependency.get("documentId", ""))
        field = str(dependency.get("field", ""))
        operator = str(dependency.get("operator", ""))
        expected = dependency.get("value")
        row_map = knowledge_by_id if document_type == "knowledge" else policy_by_id if document_type == "policy" else None
        if row_map is None or document_id not in row_map:
            return False, "document dependency references unknown frozen document"
        row = row_map[document_id]
        actual = _document_expected_value(document_type, row, field)
        if actual is None or not _operator_matches(actual, operator, expected):
            return False, "document dependency predicate is not supported by frozen document"
        if document_type == "knowledge":
            rule = row.get("selectionRule")
            if binding != "product_constraint" or not isinstance(rule, Mapping) or rule.get("field") != field or rule.get("operator") != operator or rule.get("value") != expected or rule.get("unknownPolicy") != "fail" or not _ast_contains_atom(conditions.get("constraintAst", {}), dependency):
                return False, "knowledge dependency rule/constraintAst closure is incomplete"
        elif binding != "selected_product_merchant":
            return False, "policy dependency binding is invalid"
        if document_type == "policy":
            merchant_id = str(row.get("merchantId", ""))
            if not selected or any(str(product_map.get(product_id, {}).get("merchantId", "")) != merchant_id for product_id in selected):
                return False, "selected product merchant does not match policy dependency"
        matching = [citation for citation in citations if str(citation.get("documentType", "")) == document_type and str(citation.get("documentId", "")) == document_id and str(citation.get("field", "")) == field and citation.get("value") == actual]
        if not matching:
            return False, "required document dependency lacks an exact citation"
    return True, "document dependencies and citations match frozen documents"


def _verify_claims(prediction: Mapping[str, Any], citations: Sequence[Mapping[str, Any]]) -> tuple[bool, str]:
    extraction = prediction.get("claimExtraction")
    if not isinstance(extraction, Mapping) or extraction.get("provenance") != _ANSWER_EXTRACTOR or extraction.get("version") != _ANSWER_EXTRACTOR_VERSION or extraction.get("claimsComplete") is not True:
        return False, "claim extraction is incomplete or unbound"
    by_id = {str(citation.get("evidenceId")): citation for citation in citations}
    for claim in prediction.get("claims", []):
        refs = [str(ref) for ref in claim.get("evidenceRefs", [])]
        if not refs:
            return False, "claim has empty evidenceRefs"
        bound = [by_id.get(ref) for ref in refs]
        if any(citation is None for citation in bound):
            return False, "claim references unknown evidence"
        if not any((str(citation.get("productId")) == str(claim.get("subject")) or str(citation.get("documentId")) == str(claim.get("subject"))) and str(citation.get("field")) == str(claim.get("field")) and citation.get("value") == claim.get("value") for citation in bound if citation is not None):
            return False, "claim subject/field/value disagrees with evidence"
    return True, "claims are fully evidence-bound"


def _valid_digest(value: Any) -> bool:
    return isinstance(value, str) and bool(re.fullmatch(r"[a-f0-9]{64}", value))


def _verify_runner_receipt(
    oracle: Mapping[str, Any],
    fault: Mapping[str, Any] | None,
    prediction: Mapping[str, Any],
    receipt: Mapping[str, Any] | None,
    *,
    world_manifest_sha256: str | None = None,
    environment_artifact_sha256: str | None = None,
    strict_world_binding: bool = True,
) -> tuple[bool, str]:
    """Verify a separate receipt emitted by the trusted benchmark harness."""
    if not isinstance(receipt, Mapping) or receipt.get("attestationStatus") != "ATTESTED":
        return False, "missing separate trusted runner receipt"
    expected_identity = {
        "scenarioId": str(oracle.get("scenarioId")),
        "worldId": str(oracle.get("worldId")),
        "catalogRevision": str(oracle.get("catalogRevision")),
        "environmentRevision": str(oracle.get("environmentRevision")),
        "runId": str(prediction.get("runId", "")),
    }
    for key in ("scenarioId", "worldId", "catalogRevision", "environmentRevision"):
        if str(prediction.get(key, "")) != expected_identity[key]:
            return False, "prediction/world identity drift"
    if not expected_identity["runId"] or expected_identity["runId"].lower() in {"unknown", "placeholder", "not-run", "none", "null", "0"}:
        return False, "completed prediction has placeholder run identity"
    if any(str(receipt.get(key, "")) != value for key, value in expected_identity.items()):
        return False, "runner receipt identity drift"
    if receipt.get("collector") != _RUNNER_COLLECTOR or receipt.get("collectorVersion") != _RUNNER_VERSION:
        return False, "untrusted runner collector/version"
    if receipt.get("answerExtractor") != _ANSWER_EXTRACTOR or receipt.get("answerExtractorVersion") != _ANSWER_EXTRACTOR_VERSION:
        return False, "untrusted answer extractor/version"
    if strict_world_binding:
        if not _valid_digest(world_manifest_sha256) or receipt.get("worldManifestSha256") != world_manifest_sha256:
            return False, "runner receipt world manifest binding drift"
        if not _valid_digest(environment_artifact_sha256) or receipt.get("environmentArtifactSha256") != environment_artifact_sha256:
            return False, "runner receipt environment artifact binding drift"
    trace = list(prediction.get("toolTrace", []))
    fault_record = fault or {}
    if any(not all(_valid_digest(item.get(field)) for field in ("beforeObservationDigest", "afterObservationDigest", "outputDigest")) for item in trace):
        return False, "trace observation/output digests are missing"
    expected = {
        "predictionSha256": _digest_json(prediction),
        "oracleSha256": _digest_json(oracle),
        "faultSha256": _digest_json(fault_record),
        "faultDigestSha256": _digest_json(fault_record),
        "traceDigestSha256": _digest_json(trace),
        "beforeObservationDigestSha256": _trace_field_digest(trace, "beforeObservationDigest"),
        "afterObservationDigestSha256": _trace_field_digest(trace, "afterObservationDigest"),
        "outputDigestSha256": _trace_field_digest(trace, "outputDigest"),
        "answerDigestSha256": _digest_json(str(prediction.get("answerText", ""))),
        "claimsDigestSha256": _digest_json(list(prediction.get("claims", []))),
    }
    if any(receipt.get(key) != value for key, value in expected.items()):
        return False, "runner receipt digest drift"
    if receipt.get("answerText") != str(prediction.get("answerText", "")):
        return False, "runner receipt answer text drift"
    if fault is not None and any(str(fault.get(key, "")) != expected_identity[key] for key in ("scenarioId", "worldId", "catalogRevision", "environmentRevision")):
        return False, "fault identity drift"
    extraction = prediction.get("claimExtraction")
    if not isinstance(extraction, Mapping) or extraction.get("provenance") != _ANSWER_EXTRACTOR or extraction.get("version") != _ANSWER_EXTRACTOR_VERSION or extraction.get("claimsComplete") is not True:
        return False, "claim extraction provenance is not runner-owned"
    answer_text = str(prediction.get("answerText", ""))
    claims = list(prediction.get("claims", []))
    if not answer_text.strip():
        return False, "completed prediction has no structured answer text"
    if not claims:
        if not _is_explicit_no_answer(answer_text):
            return False, "factual answer has empty claims"
        if prediction.get("terminalClass") == "SUCCESS" or not bool(oracle.get("successConditions", {}).get("allowNoAnswer")):
            return False, "empty answer is not allowed by the terminal contract"
    return True, "trusted runner receipt matches prediction, fault, world and answer"


def _is_explicit_no_answer(text: str) -> bool:
    normalized = text.strip().lower()
    if not normalized:
        return False
    return bool(re.search(r"无法(?:核实|确认|判断)|不确定|未知|没有符合|无法找到|cannot|unable|unknown|no suitable|no answer", normalized))


def _verify_trace(oracle: Mapping[str, Any], fault: Mapping[str, Any] | None, prediction: Mapping[str, Any]) -> tuple[bool, str]:
    trace = list(prediction.get("toolTrace", []))
    if [item.get("index") for item in trace] != list(range(len(trace))):
        return False, "tool trace indices are not runner-owned contiguous observations"
    if int(prediction.get("resourceUsage", {}).get("toolCalls", -1)) != len(trace):
        return False, "toolCalls does not equal runner-owned tool trace"
    if fault is None:
        return (False, "S2 requires an explicit private fault contract") if oracle.get("complexityStratum") == "S2_OBSERVATION_DEPENDENT" else (True, "no fault contract required for non-S2 direct verification")
    injections = list(fault.get("injections", []))
    required_points = list(oracle.get("successConditions", {}).get("requiredObservationPoints", []))
    fault_points = [str(injection.get("point")) for injection in injections]
    if oracle.get("complexityStratum") == "S2_OBSERVATION_DEPENDENT":
        if not required_points or sorted(required_points) != sorted(fault_points):
            return False, "S2 oracle/fault observation points are not exact"
        if any(item.get("observationPoint") not in required_points for item in trace if item.get("observationPoint") is not None):
            return False, "tool trace contains an undeclared observation point"
        if any(item.get("observationDependent") is True and not item.get("observationPoint") for item in trace):
            return False, "observation-dependent trace lacks an observation point"
        for point in required_points:
            matches = [item for item in trace if item.get("observationPoint") == point]
            if len(matches) != 1 or matches[0].get("observationDependent") is not True:
                return False, "required observation is missing or self-reported as non-dependent"
            injection = next(injection for injection in injections if injection.get("point") == point)
            if matches[0].get("injectionId") != injection.get("injectionId"):
                return False, "observation injection identity drift"
    elif any(item.get("observationPoint") for item in trace):
        return False, "non-S2 trace contains undeclared observation point"
    expected_status = "READY" if injections else "NONE"
    if fault.get("faultStatus") != expected_status:
        return False, "oracle/fault status mismatch"
    return True, "tool trace satisfies fault/observation contract"


def _validate_environment_rows(products: Sequence[Mapping[str, Any]], rows: Sequence[Mapping[str, Any]], revision: str) -> tuple[bool, str]:
    if not rows:
        return False, f"environment artifact {revision} is missing"
    product_ids = [str(product.get("productId")) for product in products]
    row_ids = [str(row.get("productId")) for row in rows]
    if len(row_ids) != len(set(row_ids)) or set(row_ids) != set(product_ids) or len(row_ids) != len(product_ids):
        return False, f"environment artifact {revision} does not cover products exactly"
    for row in rows:
        if str(row.get("environmentRevision")) != revision:
            return False, f"environment artifact revision drift: {revision}"
        if not isinstance(row.get("price"), (int, float)) or isinstance(row.get("price"), bool) or not math.isfinite(float(row.get("price"))) or float(row.get("price")) < 0:
            return False, f"environment artifact {revision} has invalid price"
        if not isinstance(row.get("stock"), int) or isinstance(row.get("stock"), bool) or row.get("stock") < 0:
            return False, f"environment artifact {revision} has invalid stock"
        if not isinstance(row.get("promotionEligible"), bool):
            return False, f"environment artifact {revision} has invalid promotion eligibility"
    product_map = _product_map(products)
    if any(str(row.get("merchantId")) != str(product_map[str(row.get("productId"))].get("merchantId")) for row in rows):
        return False, f"environment artifact {revision} merchant drift"
    return True, "environment artifact is complete and revision-bound"


def verify_solution(
    oracle: Mapping[str, Any],
    prediction: Mapping[str, Any],
    products: Sequence[Mapping[str, Any]],
    coupons: Sequence[Mapping[str, Any]] = (),
    *,
    fault: Mapping[str, Any] | None = None,
    receipt: Mapping[str, Any] | None = None,
    environment_rows: Sequence[Mapping[str, Any]] = (),
    environment_rows_by_revision: Mapping[str, Sequence[Mapping[str, Any]]] | None = None,
    knowledge: Sequence[Mapping[str, Any]] = (),
    policies: Sequence[Mapping[str, Any]] = (),
    world_manifest_sha256: str | None = None,
    environment_artifact_sha256: str | None = None,
    strict_world_binding: bool = True,
) -> Mapping[str, Any]:
    """Verify a product/bundle/cart against frozen facts, faults and evidence."""
    conditions = oracle["successConditions"]
    checks: list[Mapping[str, Any]] = []
    terminal_ok = prediction.get("terminalClass") in conditions["terminalClasses"]
    checks.append({"name": "terminal_class", "passed": terminal_ok, "details": str(prediction.get("terminalClass"))})
    if oracle.get("oracleStatus") != "READY":
        return {"eligible": False, "passed": False, "checks": checks + [{"name": "data_scale", "passed": False, "details": "BLOCKED_DATA_SCALE"}]}
    if prediction.get("runStatus") != "COMPLETED":
        return {"eligible": False, "passed": False, "checks": checks + [{"name": "run_status", "passed": False, "details": "runStatus must be COMPLETED"}]}
    base_revision = str(oracle.get("environmentRevision"))
    try:
        environment_sequence, terminal_revision = _resolve_environment_sequence(oracle)
    except CommerceScenarioError as exc:
        checks.append({"name": "environment_sequence", "passed": False, "details": str(exc)})
        return {"eligible": True, "passed": False, "checks": checks}
    checks.append({"name": "environment_sequence", "passed": True, "details": f"{base_revision}->{terminal_revision}"})
    active_environment_rows = environment_rows
    if strict_world_binding:
        if environment_rows_by_revision is None:
            environment_rows_by_revision = {base_revision: environment_rows}
        environment_checks = [_validate_environment_rows(products, environment_rows_by_revision.get(revision, ()), revision) for revision in environment_sequence]
        environment_ok = all(item[0] for item in environment_checks)
        environment_detail = "; ".join(item[1] for item in environment_checks if not item[0]) or "all required environment artifacts are bound"
        checks.append({"name": "environment_binding", "passed": environment_ok, "details": environment_detail})
        if not environment_ok:
            return {"eligible": True, "passed": False, "checks": checks}
        active_environment_rows = environment_rows_by_revision[terminal_revision]
    elif len(environment_sequence) > 1:
        checks.append({"name": "environment_binding", "passed": False, "details": "dynamic environment sequence requires audited revision artifacts"})
        return {"eligible": True, "passed": False, "checks": checks}
    by_id = _product_map(products)
    active_offers = {str(row.get("productId")): row for row in active_environment_rows}
    require_active_environment = bool(active_environment_rows)
    selected = [str(product_id) for product_id in prediction.get("selectedProductIds", [])]
    solution_type = conditions["solutionType"]
    allow_no_answer = bool(conditions.get("allowNoAnswer")) and prediction.get("terminalClass") != "SUCCESS"
    selection_ok = (not selected) if allow_no_answer else all(product_id in by_id for product_id in selected)
    if prediction.get("terminalClass") != "SUCCESS" and not allow_no_answer:
        selection_ok = False
    if prediction.get("terminalClass") == "SUCCESS":
        if solution_type == "single_product":
            selection_ok = selection_ok and len(selected) == 1
        elif solution_type == "bundle":
            selection_ok = selection_ok and len(selected) >= 2
        elif solution_type == "cart":
            selection_ok = selection_ok and len(selected) >= 1
    acceptable = conditions.get("acceptableProductIds")
    if selection_ok and selected and acceptable is not None:
        selection_ok = set(selected).issubset(set(acceptable))
    if selection_ok and selected:
        selection_ok = all(
            _product_satisfies(
                by_id[product_id],
                conditions["constraintAst"],
                active_environment_row=active_offers.get(product_id),
                require_active_environment=require_active_environment,
                active_environment_revision=terminal_revision,
            )
            for product_id in selected
        )
    if solution_type == "bundle" and selection_ok:
        bundles = {tuple(sorted(bundle)) for bundle in conditions.get("acceptableBundles", [])}
        selection_ok = tuple(sorted(selected)) in bundles and len({by_id[product_id].get("merchantId") for product_id in selected}) == 1
    if solution_type == "cart" and selection_ok:
        cart = prediction.get("cart")
        selection_ok = isinstance(cart, Mapping)
        if selection_ok:
            items = list(cart.get("items", []))
            rules = conditions.get("cartRules", {})
            item_ids = [str(item.get("productId")) for item in items]
            selection_ok = rules.get("minItems", 1) <= len(items) <= rules.get("maxItems", 1) and set(item_ids) == set(selected) and len(item_ids) == len(set(item_ids))
            if rules.get("requiredMerchantId"):
                selection_ok = selection_ok and all(by_id.get(product_id, {}).get("merchantId") == rules["requiredMerchantId"] for product_id in item_ids)
            try:
                coupon_ids_list = [str(coupon_id) for coupon_id in list(cart.get("couponIds", []))]
                if len(coupon_ids_list) != len(set(coupon_ids_list)):
                    raise CommerceScenarioError("duplicate coupon ID")
                coupon_id_set = set(coupon_ids_list)
                coupon_map = {str(coupon.get("couponId")): coupon for coupon in coupons}
                if not coupon_id_set.issubset(coupon_map):
                    raise CommerceScenarioError("unknown coupon ID")
                allowed_coupon_ids = set(str(coupon_id) for coupon_id in rules.get("allowedCouponIds", []))
                if allowed_coupon_ids and not coupon_id_set.issubset(allowed_coupon_ids):
                    raise CommerceScenarioError("coupon is outside oracle allowance")
                item_merchants = {str(by_id[product_id].get("merchantId")) for product_id in item_ids}
                selected_coupons = [coupon_map[coupon_id] for coupon_id in sorted(coupon_ids_list)]
                if any(coupon.get("scope", "merchant") != "platform" and str(coupon.get("merchantId")) not in item_merchants for coupon in selected_coupons):
                    raise CommerceScenarioError("coupon merchant is outside cart")
                subtotal, discount, total = calculate_cart_total(items, products, selected_coupons, environment_rows=active_environment_rows, environment_revision=terminal_revision)
                selection_ok = selection_ok and abs(float(cart.get("subtotal", -1)) - subtotal) < 0.01 and abs(float(cart.get("discountTotal", -1)) - discount) < 0.01 and abs(float(cart.get("finalTotal", -1)) - total) < 0.01
                if rules.get("maxTotal") is not None:
                    selection_ok = selection_ok and total <= float(rules["maxTotal"])
            except (CommerceScenarioError, TypeError, ValueError):
                selection_ok = False
    checks.append({"name": "product_or_cart_constraints", "passed": selection_ok, "details": solution_type})
    evidence_ok, evidence_detail = _verify_citations(oracle, prediction, products, selected, active_environment_rows, environment_revision=terminal_revision)
    product_evidence_ids = {str(citation.get("evidenceId", "")) for citation in prediction.get("evidenceCitations", []) if isinstance(citation, Mapping)}
    document_evidence_ids = [str(citation.get("evidenceId", "")) for citation in prediction.get("documentCitations", []) if isinstance(citation, Mapping)]
    if any(not evidence_id for evidence_id in document_evidence_ids) or len(document_evidence_ids) != len(set(document_evidence_ids)) or product_evidence_ids.intersection(document_evidence_ids):
        evidence_ok = False
        evidence_detail = "product/document evidenceId collision"
    checks.append({"name": "evidence", "passed": evidence_ok, "details": evidence_detail})
    document_ok, document_detail = _verify_document_citations(oracle, prediction, products, selected, knowledge, policies, product_evidence_ids)
    checks.append({"name": "document_evidence", "passed": document_ok, "details": document_detail})
    claims_ok, claims_detail = _verify_claims(prediction, list(prediction.get("evidenceCitations", [])) + list(prediction.get("documentCitations", [])))
    checks.append({"name": "claim_evidence_binding", "passed": claims_ok, "details": claims_detail})
    receipt_ok, receipt_detail = _verify_runner_receipt(oracle, fault, prediction, receipt, world_manifest_sha256=world_manifest_sha256, environment_artifact_sha256=environment_artifact_sha256, strict_world_binding=strict_world_binding)
    checks.append({"name": "runner_receipt", "passed": receipt_ok, "details": receipt_detail})
    trace_ok, trace_detail = _verify_trace(oracle, fault, prediction)
    checks.append({"name": "observation_trace", "passed": trace_ok, "details": trace_detail})
    passed = terminal_ok and selection_ok and evidence_ok and document_ok and claims_ok and receipt_ok and trace_ok
    return {"eligible": True, "passed": passed, "checks": checks}


def score_set(scenario_set: ScenarioV1Set, products: Sequence[Mapping[str, Any]] | None = None, coupons: Sequence[Mapping[str, Any]] | None = None, *, run_manifest: Mapping[str, Any] | None = None, world_manifest: Path | str | None = None, world_sha256: str | None = None, environment_rows: Sequence[Mapping[str, Any]] | None = None) -> Mapping[str, Any]:
    """Score only an explicitly identified run against an audited world.

    There is intentionally no default strategy, model, prompt or fake world
    SHA.  A pending world can produce a valid non-eligible report, but never a
    semantic success report.
    """
    if run_manifest is None or world_manifest is None:
        raise CommerceScenarioError("score_set requires explicit run_manifest and audited world manifest path")
    if not isinstance(world_manifest, (str, Path)):
        raise CommerceScenarioError("score_set rejects an unaudited manifest Mapping")
    if products is not None or coupons not in (None, ()) or environment_rows not in (None, ()):
        raise CommerceScenarioError("authoritative score_set does not accept caller-supplied products/coupons/environment rows")
    world_manifest_path = Path(world_manifest).resolve()
    if not world_manifest_path.is_file():
        raise CommerceScenarioError("score_set requires an existing audited world manifest path")
    if scenario_set.manifest_path is None or scenario_set.manifest_path != world_manifest_path:
        raise CommerceScenarioError("scenario set must be loaded against the same audited world manifest path")
    audited_manifest = load_world_manifest(world_manifest_path)
    actual_world_sha = sha256_file(world_manifest_path)
    if world_sha256 is not None and str(world_sha256) != actual_world_sha:
        raise CommerceScenarioError("caller world SHA disagrees with audited manifest bytes")
    if not actual_world_sha or not re.fullmatch(r"[a-f0-9]{64}", str(actual_world_sha)):
        raise CommerceScenarioError("score_set requires the real audited world manifest SHA")
    if str(actual_world_sha) == "0" * 64:
        raise CommerceScenarioError("score_set rejects a placeholder world SHA")
    validate_record(audited_manifest, "world")
    if audited_manifest.get("manifestAudit") != "verified":
        raise CommerceScenarioError("world manifest has not passed its integrity audit")
    required_run_keys = ("runId", "strategy", "modelRevision", "promptRevision", "worldRevision", "sealed")
    if any(key not in run_manifest for key in required_run_keys):
        raise CommerceScenarioError("score_set requires explicit run manifest")
    strategy = str(run_manifest["strategy"])
    if strategy not in {"FAST", "PAE", "BOUNDED_REACT"} or not isinstance(run_manifest["sealed"], bool):
        raise CommerceScenarioError("invalid run manifest strategy/sealed value")
    for identity_key in ("runId", "modelRevision", "promptRevision", "worldRevision"):
        identity = str(run_manifest.get(identity_key, "")).strip()
        if not identity or identity.lower() in {"unknown", "placeholder", "not-run", "none", "null", "0", "todo"}:
            raise CommerceScenarioError(f"run manifest has empty/placeholder {identity_key}")
    if audited_manifest.get("schemaVersion") != "commerce-world-manifest-v1":
        raise CommerceScenarioError("world manifest is not V1")
    if audited_manifest.get("status") != "READY" and any(scenario.oracle.get("oracleStatus") == "READY" for scenario in scenario_set.scenarios):
        raise CommerceScenarioError("READY oracle cannot be scored against a PENDING_DATA_SCALE world")
    artifact_paths = {str(row["path"]): world_manifest_path.parent / str(row["path"]) for row in audited_manifest["artifacts"]}
    products = tuple(load_world_products(artifact_paths["products.jsonl"]))
    coupons = tuple(load_world_products(artifact_paths["coupons.jsonl"]))
    # Knowledge and policy artifacts are part of the audited world, not
    # caller-provided context. Loading them here prevents a prediction from
    # scoring by relying on an unsealed document projection.
    knowledge = tuple(load_world_products(artifact_paths["knowledge.jsonl"]))
    policies = tuple(load_world_products(artifact_paths["policies.jsonl"]))
    environment_cache: dict[str, tuple[Sequence[Mapping[str, Any]], str]] = {}

    def environment_for(revision: str) -> tuple[Sequence[Mapping[str, Any]], str]:
        if revision not in environment_cache:
            path = artifact_paths.get(f"environments/{revision}.jsonl")
            if path is None or not path.is_file():
                raise CommerceScenarioError(f"audited environment artifact missing: {revision}")
            environment_cache[revision] = (tuple(load_world_products(path)), sha256_file(path))
        return environment_cache[revision]

    per_scenario: list[Mapping[str, Any]] = []
    for scenario in scenario_set.scenarios:
        if scenario.input["worldId"] != audited_manifest.get("worldId") or scenario.input["catalogRevision"] != audited_manifest.get("catalogRevision"):
            raise CommerceScenarioError(f"scenario {scenario.scenario_id}: world manifest mismatch")
        if str(run_manifest["worldRevision"]) != str(scenario.input["catalogRevision"]):
            raise CommerceScenarioError("run manifest worldRevision mismatch")
        environment_sequence, terminal_revision = _resolve_environment_sequence(scenario.oracle)
        environment_by_revision: dict[str, Sequence[Mapping[str, Any]]] = {}
        for revision in environment_sequence:
            environment_by_revision[revision] = environment_for(revision)[0]
        _, terminal_environment_sha = environment_for(terminal_revision)
        result = verify_solution(
            scenario.oracle,
            scenario.prediction,
            products,
            coupons,
            fault=scenario.fault,
            receipt=scenario.receipt,
            environment_rows=environment_by_revision[terminal_revision],
            environment_rows_by_revision=environment_by_revision,
            knowledge=knowledge,
            policies=policies,
            world_manifest_sha256=actual_world_sha,
            environment_artifact_sha256=terminal_environment_sha,
            strict_world_binding=True,
        )
        per_scenario.append({"scenarioId": scenario.scenario_id, "intentFamily": scenario.oracle["intentFamily"], "complexityStratum": scenario.oracle["complexityStratum"], "passed": bool(result["passed"]), "eligible": bool(result["eligible"]), "checks": list(result["checks"]), "resourceUsage": dict(scenario.prediction["resourceUsage"])})
    eligible = [row for row in per_scenario if row["eligible"]]
    passed = [row for row in eligible if row["passed"]]
    usage = [row["resourceUsage"] for row in eligible]
    hard_failures = sum(1 for row in eligible if any(check["name"] == "product_or_cart_constraints" and not check["passed"] for check in row["checks"]))
    unsupported_failures = sum(1 for row in eligible if any(check["name"] == "claim_evidence_binding" and not check["passed"] for check in row["checks"]))
    mean_tools = sum(float(row["toolCalls"]) for row in usage) / len(usage) if usage else 0.0
    mean_latency = sum(float(row["latencyMs"]) for row in usage) / len(usage) if usage else 0.0
    groups: dict[tuple[str, str], list[Mapping[str, Any]]] = {}
    for row in per_scenario:
        groups.setdefault((str(row["intentFamily"]), str(row["complexityStratum"])), []).append(row)
    by_intent_stratum = []
    for (intent, stratum), rows in sorted(groups.items()):
        group_eligible = [row for row in rows if row["eligible"]]
        group_passed = [row for row in group_eligible if row["passed"]]
        by_intent_stratum.append({"intentFamily": intent, "complexityStratum": stratum, "scenarioCount": len(rows), "eligible": len(group_eligible), "passed": len(group_passed), "taskSuccessRate": len(group_passed) / len(group_eligible) if group_eligible else 0.0})
    run_projection = {"runId": str(run_manifest["runId"]), "strategy": strategy, "modelRevision": str(run_manifest["modelRevision"]), "promptRevision": str(run_manifest["promptRevision"]), "worldRevision": str(run_manifest["worldRevision"]), "sealed": bool(run_manifest["sealed"])}
    source_sha = {**scenario_set.file_sha256, "world": str(actual_world_sha)}
    report_id = "report-" + hashlib.sha256(canonical_bytes({"runManifest": run_projection, "sourceSha256": source_sha})).hexdigest()[:16]
    unsupported = ["Pilot output is not a statistical-significance claim."]
    if audited_manifest.get("status") != "READY":
        unsupported.append("World is PENDING_DATA_SCALE; no scenario is eligible for quality scoring.")
    report = {"reportId": report_id, "schemaVersion": SCHEMA_VERSIONS["report"], "worldId": audited_manifest["worldId"], "runManifest": run_projection, "sourceSha256": source_sha, "perScenario": per_scenario, "aggregate": {"scenarioCount": len(per_scenario), "eligible": len(eligible), "passed": len(passed), "taskSuccessRate": len(passed) / len(eligible) if eligible else 0.0, "hardConstraintViolationRate": hard_failures / len(eligible) if eligible else 0.0, "unsupportedClaimRate": unsupported_failures / len(eligible) if eligible else 0.0, "meanToolCalls": mean_tools, "meanLatencyMs": mean_latency, "confidenceInterval": None, "byIntentStratum": by_intent_stratum}, "unsupportedClaims": unsupported}
    validate_record(report, "report")
    return report
