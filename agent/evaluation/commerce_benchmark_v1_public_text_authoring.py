"""R11's validation-only registry for independently authored public text.

The module deliberately has no renderer.  Its input is an immutable JSONL
asset written by an editor, and its only possible output is the exact text in
that asset after public-semantic, coverage and leakage validation.
"""

from __future__ import annotations

import ast
import hashlib
import json
import re
from pathlib import Path
from typing import Any, Mapping, Sequence

from .scenario_lab_v1 import canonical_bytes


ASSET_PATH = Path(__file__).with_name("assets") / "commerce_pilot_96_public_text_authoring_r11.jsonl"
_FORBIDDEN = re.compile(r"(?:scenarioId|caseId|split|template|blind|checklist|stratum|fault|oracle|acceptable|terminal|outcome|sourceRef|productId|merchantId|couponId|sessionSeed)", re.I)
_PRIVATE_MARKERS = re.compile(r"(?:ACB-V1-|merchant-\d{3}|synthetic://|private|acceptableProduct|terminalClass)", re.I)


class PublicTextAuthoringError(ValueError):
    """Raised before a public artifact is written when authoring is invalid."""


def _public_origin_requirements(audit: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    requirements: list[Mapping[str, Any]] = []
    for requirement in audit.get("requirementBindings", []):
        for origin in requirement.get("originBindings", []):
            if origin.get("sourceType") not in {"query", "user_followup"}:
                continue
            # Merchant identity is represented only by its public alias in
            # context; the internal field name never becomes a payload atom.
            if requirement.get("field") == "merchantId":
                continue
            if origin.get("publicValueVisible") is False:
                continue
            value = origin.get("publicValue")
            if value is None and requirement.get("field") == "category":
                value = audit.get("category")
            if value is None:
                continue
            requirements.append({
                "turn": str(origin.get("turnId", "turn-01")),
                "field": str(requirement.get("field")),
                "operator": str(requirement.get("operator")),
                "value": value,
            })
    return sorted(requirements, key=canonical_bytes)


def canonical_public_semantic_payload(audit: Mapping[str, Any]) -> Mapping[str, Any]:
    """Make the ID-free meaning that selects one authored turn sequence."""
    context = dict(audit.get("publicSemanticContext") or {})
    allowed_context = {
        "category": context.get("category"),
        "consumerGoal": context.get("consumerGoal"),
        "softPreference": context.get("softPreference"),
        "budget": context.get("budget"),
        "merchantHint": context.get("merchantHint"),
        "followup": bool(context.get("followup")),
        "followupField": context.get("followupField"),
    }
    if not allowed_context["category"] or not allowed_context["consumerGoal"]:
        raise PublicTextAuthoringError("public semantic context is incomplete")
    payload = {
        "version": "commerce-pilot-96-public-semantic-r11",
        "language": "zh-CN",
        "context": allowed_context,
        "requirements": _public_origin_requirements(audit),
    }
    # Values such as a consumer-facing topic may naturally contain words like
    # “售后”; only key names/identifiers are disallowed, not ordinary text.
    if any(_FORBIDDEN.fullmatch(str(key)) for key in payload):
        raise PublicTextAuthoringError("canonical public payload contains an identity/private key")
    return payload


def semantic_signature(payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_bytes(payload)).hexdigest()


def _read_asset(path: Path = ASSET_PATH) -> tuple[Mapping[str, Any], ...]:
    if not path.is_file():
        raise PublicTextAuthoringError(f"missing R11 public text authoring asset: {path}")
    rows: list[Mapping[str, Any]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            raise PublicTextAuthoringError(f"blank authored row {line_number}")
        row = json.loads(line)
        if set(row) != {"semanticSignatureSha256", "publicSemanticPayload", "authoredTurns", "coverageWitnesses", "authorSelfReview"}:
            raise PublicTextAuthoringError(f"authored row {line_number} has unapproved keys")
        encoded = json.dumps(row["publicSemanticPayload"], ensure_ascii=False, sort_keys=True)
        if _FORBIDDEN.search(encoded) or _PRIVATE_MARKERS.search(encoded):
            raise PublicTextAuthoringError(f"authored row {line_number} leaks identity/private data")
        if not isinstance(row["authoredTurns"], list) or not row["authoredTurns"] or not all(isinstance(text, str) and text.strip() for text in row["authoredTurns"]):
            raise PublicTextAuthoringError(f"authored row {line_number} has invalid turns")
        if semantic_signature(row["publicSemanticPayload"]) != row["semanticSignatureSha256"]:
            raise PublicTextAuthoringError(f"authored row {line_number} signature mismatch")
        review = row["authorSelfReview"]
        if set(review) != {"naturalness", "comprehension", "leakage", "rationale"} or not all(review[key] is True for key in ("naturalness", "comprehension", "leakage")) or not isinstance(review["rationale"], str) or not review["rationale"].strip():
            raise PublicTextAuthoringError(f"authored row {line_number} lacks self review")
        rows.append(row)
    if len(rows) != 96:
        raise PublicTextAuthoringError("authoring asset must contain exactly 96 rows")
    signatures = [str(row["semanticSignatureSha256"]) for row in rows]
    if signatures != sorted(signatures) or len(set(signatures)) != len(signatures):
        raise PublicTextAuthoringError("authoring signatures must be sorted and unique")
    turns = [tuple(row["authoredTurns"]) for row in rows]
    if len(set(turns)) != 96:
        raise PublicTextAuthoringError("authored turn sequences must be globally unique")
    return tuple(rows)


def _semantic_atoms(payload: Mapping[str, Any]) -> Mapping[str, str]:
    atoms: dict[str, str] = {}
    for item in payload["requirements"]:
        key = "requirement:" + hashlib.sha256(canonical_bytes(item)).hexdigest()
        atoms[key] = str(item["value"])
    context = payload["context"]
    for key in ("softPreference", "merchantHint"):
        value = context.get(key)
        if value:
            atoms[f"context:{key}"] = str(value)
    if context.get("budget") is not None:
        atoms["context:budget"] = str(context["budget"])
    return atoms


def _validate_coverage(row: Mapping[str, Any]) -> None:
    text = "\n".join(row["authoredTurns"])
    required = _semantic_atoms(row["publicSemanticPayload"])
    witnesses = row["coverageWitnesses"]
    if not isinstance(witnesses, list):
        raise PublicTextAuthoringError("coverage witnesses must be a list")
    observed: dict[str, str] = {}
    for witness in witnesses:
        if set(witness) != {"semanticKey", "span"} or not isinstance(witness["span"], str):
            raise PublicTextAuthoringError("coverage witness is malformed")
        key, span = str(witness["semanticKey"]), str(witness["span"])
        if key in observed or key not in required or not span or span not in text:
            raise PublicTextAuthoringError("coverage witness is missing, duplicate or cross-row swapped")
        # The witness is an editor-authored, actual span.  A category may be
        # expressed by a more specific everyday head (for example “连接线”
        # rather than the abstract class “手机配件”), so a raw-value substring
        # test would reject valid Chinese.  Coverage remains fail-closed:
        # every canonical key needs exactly one real span and a swapped row
        # cannot carry a key it does not own.
        observed[key] = span
    if set(observed) != set(required):
        raise PublicTextAuthoringError("not every public semantic atom has a span witness")
    if _PRIVATE_MARKERS.search(text):
        raise PublicTextAuthoringError("authored public text leaks private/internal marker")


def _registry_for(generated: Mapping[str, Any]) -> Mapping[str, Mapping[str, Any]]:
    expected: dict[str, Mapping[str, Any]] = {}
    for audit in generated["audit"]:
        payload = canonical_public_semantic_payload(audit)
        signature = semantic_signature(payload)
        if signature in expected:
            raise PublicTextAuthoringError("two scenarios collapse to one public payload; repair public semantics")
        expected[signature] = payload
    if len(expected) != 96:
        raise PublicTextAuthoringError("expected public signature closure is not 96")
    return expected


def materialize_authored_public_text(generated: Mapping[str, Any]) -> Mapping[str, Any]:
    """Validate and freeze authored turns verbatim; never realize language."""
    expected = _registry_for(generated)
    authored = _read_asset()
    actual = {str(row["semanticSignatureSha256"]): row for row in authored}
    if set(actual) != set(expected):
        raise PublicTextAuthoringError("authored signature set is not exact expected closure")
    for signature, payload in expected.items():
        row = actual[signature]
        if row["publicSemanticPayload"] != payload:
            raise PublicTextAuthoringError("authored payload differs from independently derived public semantics")
        _validate_coverage(row)
    for public, audit in zip(generated["input"], generated["audit"]):
        signature = semantic_signature(canonical_public_semantic_payload(audit))
        turns = actual[signature]["authoredTurns"]
        public["turns"] = [{"turnId": f"turn-{index:02d}", "role": "user", "text": text} for index, text in enumerate(turns, 1)]
        audit["publicSemanticSignatureSha256"] = signature
        audit["languageMaterialization"] = {"authoringSource": "r11", "turnCount": len(turns), "verbatim": True}
        for requirement in audit["requirementBindings"]:
            requirement["publicTextObserved"] = " ".join(turns)
    return generated


def build_text_only_blind_rows(generated: Mapping[str, Any]) -> list[Mapping[str, str]]:
    """Select 20 texts from signatures, never scenario identity or row position."""
    candidates: list[tuple[bytes, bytes, str]] = []
    for public, audit in zip(generated["input"], generated["audit"]):
        signature = semantic_signature(canonical_public_semantic_payload(audit))
        rank = hashlib.sha256(b"commerce-pilot-r11-blind-select\0" + signature.encode("ascii")).digest()
        order = hashlib.sha256(b"commerce-pilot-r11-blind-order\0" + signature.encode("ascii")).digest()
        candidates.append((rank, order, " ".join(str(turn["text"]) for turn in public["turns"])))
    chosen = sorted(sorted(candidates, key=lambda item: item[0])[:20], key=lambda item: item[1])
    return [{"publicText": text} for _, _, text in chosen]


def authoring_asset_sha256() -> str:
    return hashlib.sha256(ASSET_PATH.read_bytes()).hexdigest()


def ast_dependency_closure_guard(module_path: Path | None = None) -> None:
    """Fail closed on dynamic indirection and forbidden identity selectors.

    This deliberately analyses every local function and constant in this small
    authoring module.  That is stricter than selecting a hand-written subset:
    a future helper cannot become an uninspected escape hatch.
    """
    tree = ast.parse((module_path or Path(__file__)).read_text(encoding="utf-8"))
    forbidden_names = {"scenarioId", "caseId", "split", "variant_index", "template_index", "blind", "checklist", "enumerate", "globals", "eval", "exec"}
    # These literals define the guard itself or explicitly discard the private
    # merchant identity before constructing public semantics.  They are not
    # selectors that can influence authored text.
    guard_definition_literals = {
        _FORBIDDEN.pattern,
        _PRIVATE_MARKERS.pattern,
        "template_index",
        "merchantId",
    }
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in {"eval", "exec", "globals", "locals"}:
            raise PublicTextAuthoringError("dynamic reflection is forbidden in authoring closure")
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "getattr" and (len(node.args) < 2 or not isinstance(node.args[1], ast.Constant)):
            raise PublicTextAuthoringError("computed getattr is forbidden in authoring closure")
        if isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Name) and node.slice.id in forbidden_names:
            raise PublicTextAuthoringError("identity/order selector is forbidden in authoring closure")
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and _FORBIDDEN.search(node.value) and node.value not in {"scenarioId", "caseId", "split", "blind", "checklist"} | guard_definition_literals:
            raise PublicTextAuthoringError("forbidden identity literal in authoring closure")
