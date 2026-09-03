"""Public-only offline runtime for the used-phone complex-intent pilot.

This module is intentionally label-blind.  It only opens the two pinned public
JSONL inputs passed to :func:`load_public_bundle`; hidden judgments are neither
named nor imported here.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
from pathlib import Path
import re
from typing import Any, Iterable, Mapping, Sequence


CASE_FILE = "blind_cases_pending.jsonl"
CANDIDATE_FILE = "blind_candidates_pending.jsonl"
EXPECTED_PUBLIC_SHA256 = {
    CASE_FILE: "ac4bc82b636c069b866675dcea6e6af1f5755828346872379f292395c3942aea",
    CANDIDATE_FILE: "b00ba159b5d8d53111f2d1dacd1fa958a600ef30e203bf5992c9086a23639018",
}
EXPECTED_CASE_COUNTS = {
    "BLIND-CASE-001": 16,
    "BLIND-CASE-002": 16,
    "BLIND-CASE-003": 16,
    "BLIND-CASE-004": 0,
    "BLIND-CASE-005": 2,
    "BLIND-CASE-006": 17,
    "BLIND-CASE-007": 0,
    "BLIND-CASE-008": 16,
}
PUBLIC_FACT_GROUPS = (
    "battery_health",
    "motherboard_repair",
    "screen_originality",
    "battery_originality",
    "scratch_level",
    "os",
)
# Ordered longest/negative-first so substring aliases such as 非原装屏 are not
# accidentally normalized as 原装屏.
PUBLIC_FACT_ALIASES: Mapping[str, tuple[tuple[str, str], ...]] = {
    "os": (("android", "android/安卓"), ("android", "安卓"), ("ios", "ios")),
    "battery_health": (("90_plus", "90%+"), ("80_to_90", "80%-90%"), ("below_80", "80%以下")),
    "motherboard_repair": (("repaired", "主板有过维修"), ("not_repaired", "主板未维修"), ("repaired", "主板维修")),
    "screen_originality": (("non_original", "非原装内屏/外屏"), ("non_original", "非原装屏"), ("original", "原装屏")),
    "battery_originality": (("non_original", "非原装电池"), ("original", "原装电池")),
    "scratch_level": (("none", "无划痕"), ("light", "轻微划痕"), ("present", "划痕")),
}
CASE_ID_RE = re.compile(r"^BLIND-CASE-00[1-8]$")
CANDIDATE_ID_RE = re.compile(r"^(BLIND-CASE-00[1-8])-CAND-[0-9a-f]{12}$")


class PublicBundleError(ValueError):
    """Raised when the public envelope does not match the frozen contract."""


@dataclass(frozen=True)
class PublicCase:
    review_case_id: str
    query_messages: tuple[Mapping[str, Any], ...]
    user_visible_candidate_context: tuple[Mapping[str, Any], ...]
    candidates: tuple[Mapping[str, Any], ...]
    raw: Mapping[str, Any]

    @property
    def query_text(self) -> str:
        return "\n".join(str(message.get("text", "")) for message in self.query_messages)


@dataclass(frozen=True)
class PublicBundle:
    directory: Path
    cases: tuple[PublicCase, ...]
    input_sha256: Mapping[str, str]

    @property
    def bundle_contract_sha256(self) -> str:
        """Composite digest of the two pinned public artifacts."""

        return hashlib.sha256(canonical_json_bytes(dict(self.input_sha256))).hexdigest()

    def by_id(self) -> dict[str, PublicCase]:
        return {case.review_case_id: case for case in self.cases}


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def assert_public_input_path(path: Path, bundle_dir: Path) -> None:
    """Reject every public-runtime read outside the two explicit artifacts."""

    resolved = path.resolve()
    directory = bundle_dir.resolve()
    if resolved.parent != directory or resolved.name not in EXPECTED_PUBLIC_SHA256:
        raise PublicBundleError(f"public runtime denied input path: {resolved.name}")


def canonical_json_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def _read_jsonl(path: Path, *, public_bundle_dir: Path) -> list[dict[str, Any]]:
    assert_public_input_path(path, public_bundle_dir)
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8", newline="") as stream:
        for line_number, line in enumerate(stream, 1):
            if not line.strip():
                raise PublicBundleError(f"blank JSONL row: {path.name}:{line_number}")
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise PublicBundleError(f"invalid JSON: {path.name}:{line_number}: {exc}") from exc
            if not isinstance(row, dict):
                raise PublicBundleError(f"row must be an object: {path.name}:{line_number}")
            rows.append(row)
    return rows


def load_public_bundle(bundle_dir: str | Path) -> PublicBundle:
    """Load and verify the frozen public envelope without reading a manifest.

    The two expected hashes are compiled into this public module so validation
    does not require opening any adjacent construction or sealed files.
    """

    directory = Path(bundle_dir).resolve()
    paths = {name: directory / name for name in EXPECTED_PUBLIC_SHA256}
    for name, path in paths.items():
        assert_public_input_path(path, directory)
        if not path.is_file():
            raise PublicBundleError(f"missing public input: {name}")
        actual = sha256_file(path)
        if actual != EXPECTED_PUBLIC_SHA256[name]:
            raise PublicBundleError(f"public input hash mismatch for {name}: {actual}")

    case_rows = _read_jsonl(paths[CASE_FILE], public_bundle_dir=directory)
    candidate_rows = _read_jsonl(paths[CANDIDATE_FILE], public_bundle_dir=directory)
    if len(case_rows) != 8 or len(candidate_rows) != 83:
        raise PublicBundleError(f"expected 8 cases/83 candidates, got {len(case_rows)}/{len(candidate_rows)}")

    case_by_id: dict[str, dict[str, Any]] = {}
    for row in case_rows:
        case_id = row.get("reviewCaseId")
        if not isinstance(case_id, str) or not CASE_ID_RE.fullmatch(case_id):
            raise PublicBundleError(f"non-opaque or invalid case id: {case_id!r}")
        if case_id in case_by_id:
            raise PublicBundleError(f"duplicate case id: {case_id}")
        messages = row.get("userContext", {}).get("messages")
        visible = row.get("userVisibleCandidateContext")
        if not isinstance(messages, list) or not messages or not isinstance(visible, list):
            raise PublicBundleError(f"invalid public case context: {case_id}")
        case_by_id[case_id] = row

    candidates_by_case: dict[str, list[dict[str, Any]]] = {case_id: [] for case_id in case_by_id}
    seen_candidate_ids: set[str] = set()
    for row in candidate_rows:
        candidate_id = row.get("candidateDisplayId")
        case_id = row.get("reviewCaseId")
        match = CANDIDATE_ID_RE.fullmatch(candidate_id) if isinstance(candidate_id, str) else None
        if not match or match.group(1) != case_id or case_id not in case_by_id:
            raise PublicBundleError(f"invalid opaque candidate id or case link: {candidate_id!r}")
        if candidate_id in seen_candidate_ids:
            raise PublicBundleError(f"duplicate candidate id: {candidate_id}")
        if not isinstance(row.get("candidate"), dict) or not isinstance(row.get("evidenceRefs"), list):
            raise PublicBundleError(f"missing public raw evidence: {candidate_id}")
        seen_candidate_ids.add(candidate_id)
        candidates_by_case[case_id].append(row)

    actual_counts = {case_id: len(candidates_by_case[case_id]) for case_id in sorted(case_by_id)}
    if actual_counts != EXPECTED_CASE_COUNTS:
        raise PublicBundleError(f"candidate distribution mismatch: {actual_counts}")

    case006 = case_by_id["BLIND-CASE-006"]
    visible006 = case006["userVisibleCandidateContext"]
    if len(visible006) != 1 or visible006[0].get("label") != "先前商品":
        raise PublicBundleError("case 006 must identify its anchor through public visible context")
    anchor_id = visible006[0].get("candidateDisplayId")
    if anchor_id not in {row["candidateDisplayId"] for row in candidates_by_case["BLIND-CASE-006"]}:
        raise PublicBundleError("case 006 visible anchor is absent from its public candidate rows")

    cases = tuple(
        PublicCase(
            review_case_id=case_id,
            query_messages=tuple(case_by_id[case_id]["userContext"]["messages"]),
            user_visible_candidate_context=tuple(case_by_id[case_id]["userVisibleCandidateContext"]),
            candidates=tuple(sorted(candidates_by_case[case_id], key=lambda row: row["candidateDisplayId"])),
            raw=case_by_id[case_id],
        )
        for case_id in sorted(case_by_id)
    )
    return PublicBundle(
        directory=directory,
        cases=cases,
        input_sha256={name: EXPECTED_PUBLIC_SHA256[name] for name in sorted(EXPECTED_PUBLIC_SHA256)},
    )


def flatten_raw_value(value: Any) -> str:
    if isinstance(value, list):
        return " ".join(flatten_raw_value(item) for item in value)
    if isinstance(value, dict):
        return " ".join(flatten_raw_value(value[key]) for key in sorted(value))
    return str(value)


def normalize_public_fact(group: str, raw_value: Any) -> str | None:
    """Normalize one fact from public raw evidence under the visible alias map."""

    raw_text = flatten_raw_value(raw_value).casefold()
    for normalized, alias in PUBLIC_FACT_ALIASES.get(group, ()):
        if alias.casefold() in raw_text:
            return normalized
    return None


def candidate_public_text(candidate: Mapping[str, Any]) -> str:
    metadata = candidate.get("candidate", {})
    # The text floor is intentionally narrower than the full public envelope:
    # only the public title and raw attribute evidence participate in BM25.
    parts = [str(metadata.get("title", ""))]
    parts.extend(flatten_raw_value(ref.get("rawValue", "")) for ref in candidate.get("evidenceRefs", []))
    return " ".join(parts)


_ASCII_TOKEN_RE = re.compile(r"[a-z0-9]+(?:[.+%-][a-z0-9]+)*", re.IGNORECASE)
_CJK_RE = re.compile(r"[\u3400-\u9fff]")


def tokenize(text: str) -> list[str]:
    lowered = text.casefold()
    tokens = _ASCII_TOKEN_RE.findall(lowered)
    tokens.extend(character for character in lowered if _CJK_RE.fullmatch(character))
    return tokens


def bm25_rank(query: str, candidates: Sequence[Mapping[str, Any]], *, k1: float = 1.2, b: float = 0.75) -> list[str]:
    """Deterministic BM25 over public title, metadata, and raw attributes only."""

    documents = [tokenize(candidate_public_text(candidate)) for candidate in candidates]
    if not documents:
        return []
    query_terms = sorted(set(tokenize(query)))
    average_length = sum(map(len, documents)) / len(documents) or 1.0
    document_frequency = {term: sum(term in document for document in documents) for term in query_terms}
    scores: list[tuple[float, str]] = []
    for candidate, document in zip(candidates, documents):
        frequencies: dict[str, int] = {}
        for token in document:
            frequencies[token] = frequencies.get(token, 0) + 1
        score = 0.0
        for term in query_terms:
            frequency = frequencies.get(term, 0)
            if not frequency:
                continue
            inverse_document_frequency = math.log(1.0 + (len(documents) - document_frequency[term] + 0.5) / (document_frequency[term] + 0.5))
            denominator = frequency + k1 * (1.0 - b + b * len(document) / average_length)
            score += inverse_document_frequency * frequency * (k1 + 1.0) / denominator
        scores.append((score, str(candidate["candidateDisplayId"])))
    return [candidate_id for _, candidate_id in sorted(scores, key=lambda item: (-item[0], item[1]))]


def write_jsonl(path: str | Path, rows: Iterable[Mapping[str, Any]]) -> str:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = b"".join(canonical_json_bytes(row) for row in rows)
    target.write_bytes(payload)
    return hashlib.sha256(payload).hexdigest()


def write_json(path: str | Path, value: Mapping[str, Any]) -> str:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = canonical_json_bytes(value)
    target.write_bytes(payload)
    return hashlib.sha256(payload).hexdigest()
