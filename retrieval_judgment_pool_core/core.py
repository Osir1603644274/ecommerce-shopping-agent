"""Immutable, label-isolated retrieval judgment-pool pipeline.

The core stops before human labeling.  It accepts only public, label-free query
and document snapshots and produces UNJUDGED audit/blind packages.  It never
reads qrels, judgments, sealed/hidden inputs, or production Agent state.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import random
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from jsonschema import Draft202012Validator


CORE_VERSION = "1.0.0"
CONFIG_SCHEMA_VERSION = "retrieval-judgment-pool-config-v1"
RUN_SCHEMA_VERSION = "retrieval-judgment-pool-run-v1"
STATE_SCHEMA_VERSION = "retrieval-judgment-pool-state-v1"
RECEIPT_SCHEMA_VERSION = "retrieval-judgment-pool-receipt-v1"
STAGES = (
    "NORMALIZE",
    "RETRIEVE",
    "ANALYZE",
    "CE_SCORE",
    "SELECT",
    "PACKAGE",
    "VERIFY",
    "READY",
)
RUN_ID_RE = re.compile(r"^rjp-[0-9a-f]{20}$")
TOKEN_RE = re.compile(r"[a-z0-9]+|[\u3400-\u9fff]", re.IGNORECASE)
FORBIDDEN_REF_MARKERS = ("qrel", "sealed", "hidden", "gold", "judgment", "answer-key")
FORBIDDEN_DATA_KEYS = ("qrel", "relevance", "judgment", "human_label", "gold_label", "sealed")
SAFE_POLICY_KEYS = {
    "contains_labels",
    "output_label",
    "read_qrels",
    "read_sealed_or_hidden",
    "outside_pool_is_negative",
    "production_release_allowed",
}
CONCEPTS: dict[str, tuple[str, ...]] = {
    "concept:apple": ("苹果", "苹菓", "iphone", "apple"),
    "concept:camera": ("摄影", "拍照", "相机", "摄像", "影像", "夜景"),
    "concept:gaming": ("游戏", "手游", "电竞", "玩家"),
    "concept:repair": ("维修", "修手机", "售后", "服务站", "维修点"),
    "concept:merchant": ("商家", "中心", "商店", "服务站", "维修点"),
    "concept:phone": ("手机", "phone", "iphone"),
    "concept:budget2000": ("两千", "2000", "2k"),
}


class PoolError(RuntimeError):
    """Stable, non-sensitive error returned by CLI and MCP adapters."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message

    def as_dict(self) -> dict[str, str]:
        return {"code": self.code, "message": self.message}


@dataclass(frozen=True)
class RunPaths:
    root: Path

    @property
    def manifest(self) -> Path:
        return self.root / "manifest.json"

    @property
    def state(self) -> Path:
        return self.root / "state.json"


def canonical_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _atomic_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    with tmp.open("wb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


def write_json(path: Path, value: Any) -> None:
    _atomic_write(path, json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8") + b"\n")


def write_jsonl(path: Path, rows: Iterable[Mapping[str, Any]]) -> None:
    payload = b"".join(canonical_bytes(dict(row)) for row in rows)
    _atomic_write(path, payload)


def read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PoolError("INVALID_JSON", "required JSON input is missing or malformed") from exc
    if not isinstance(value, dict):
        raise PoolError("INVALID_JSON", "JSON input must be an object")
    return value


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    try:
        with path.open("r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, 1):
                if not line.strip():
                    continue
                value = json.loads(line)
                if not isinstance(value, dict):
                    raise ValueError("row is not an object")
                rows.append(value)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        raise PoolError("INVALID_JSONL", "required JSONL input is missing or malformed") from exc
    return rows


def _flatten(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float, bool)):
        return str(value)
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _normalized_text(value: str) -> str:
    return re.sub(r"\s+", "", value.casefold())


def _tokens(value: str) -> list[str]:
    return TOKEN_RE.findall(value.casefold())


def _features(value: str) -> list[str]:
    compact = _normalized_text(value)
    features = [f"tok:{token}" for token in _tokens(value)]
    features.extend(f"c2:{compact[index:index + 2]}" for index in range(max(0, len(compact) - 1)))
    for concept, aliases in CONCEPTS.items():
        if any(alias.casefold() in compact for alias in aliases):
            features.append(concept)
    return features


def _walk_keys(node: Any) -> Iterable[str]:
    if isinstance(node, Mapping):
        for key, value in node.items():
            yield str(key)
            yield from _walk_keys(value)
    elif isinstance(node, list):
        for item in node:
            yield from _walk_keys(item)


def _assert_label_free(node: Any, *, config: bool = False) -> None:
    for key in _walk_keys(node):
        lowered = key.casefold()
        if config and lowered in SAFE_POLICY_KEYS:
            continue
        if any(marker in lowered for marker in FORBIDDEN_DATA_KEYS):
            raise PoolError("LABEL_INPUT_FORBIDDEN", "label, qrel, judgment, or sealed fields are forbidden")


def _safe_relative(root: Path, ref: str, *, kind: str) -> Path:
    if not isinstance(ref, str) or not ref.strip():
        raise PoolError("INVALID_REFERENCE", f"{kind} reference is empty")
    candidate_ref = Path(ref)
    if candidate_ref.is_absolute() or ".." in candidate_ref.parts or "\x00" in ref:
        raise PoolError("PATH_OUT_OF_SCOPE", f"{kind} reference is outside its allowed root")
    if any(marker in ref.casefold() for marker in FORBIDDEN_REF_MARKERS):
        raise PoolError("SENSITIVE_INPUT_FORBIDDEN", f"{kind} reference names a forbidden data class")
    resolved_root = root.resolve()
    resolved = (resolved_root / candidate_ref).resolve()
    if resolved != resolved_root and resolved_root not in resolved.parents:
        raise PoolError("PATH_OUT_OF_SCOPE", f"{kind} reference is outside its allowed root")
    return resolved


def _validate_config(config: dict[str, Any], schema_path: Path) -> None:
    schema = read_json(schema_path)
    errors = sorted(Draft202012Validator(schema).iter_errors(config), key=lambda error: list(error.path))
    if errors:
        raise PoolError("CONFIG_SCHEMA_INVALID", "config violates retrieval-judgment-pool-config-v1")
    _assert_label_free(config, config=True)
    safety = config["safety"]
    if safety != {
        "output_label": "UNJUDGED",
        "outside_pool_is_negative": False,
        "read_qrels": False,
        "read_sealed_or_hidden": False,
        "production_release_allowed": False,
    }:
        raise PoolError("SAFETY_POLICY_INVALID", "config must preserve the pre-label safety boundary")
    ids = [item["retriever_id"] for item in config["retrievers"]]
    if len(ids) != len(set(ids)):
        raise PoolError("DUPLICATE_RETRIEVER_ID", "retriever_id values must be unique")
    kinds = {item["kind"] for item in config["retrievers"]}
    if not {"lexical_bm25f", "char_ngram", "semantic_hash"}.issubset(kinds):
        raise PoolError("RETRIEVER_FAMILY_INCOMPLETE", "lexical, character/fuzzy, and semantic-hash retrievers are required")


def _normalize_inputs(
    queries: Sequence[dict[str, Any]],
    documents: Sequence[dict[str, Any]],
    config: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    _assert_label_free(queries)
    _assert_label_free(documents)
    mapping = config["field_mapping"]
    normalized_queries: list[dict[str, Any]] = []
    seen_queries: set[str] = set()
    for row in queries:
        query_id = str(row.get(mapping["query_id"], "")).strip()
        text = str(row.get(mapping["query_text"], "")).strip()
        variants = row.get(mapping["query_variants"], [])
        structured = row.get(mapping["query_structured"], {})
        if not query_id or query_id in seen_queries or not text:
            raise PoolError("QUERY_IDENTITY_INVALID", "query ids must be non-empty and unique")
        if not isinstance(variants, list) or any(not isinstance(item, str) or not item.strip() for item in variants):
            raise PoolError("QUERY_VARIANTS_INVALID", "query variants must be non-empty strings")
        if not isinstance(structured, dict):
            raise PoolError("QUERY_STRUCTURED_INVALID", "query structured constraints must be an object")
        seen_queries.add(query_id)
        normalized_queries.append({
            "query_id": query_id,
            "text": text,
            "variants": list(dict.fromkeys(item.strip() for item in variants)),
            "structured": structured,
        })
    normalized_documents: list[dict[str, Any]] = []
    seen_documents: set[str] = set()
    text_fields = mapping["document_text_fields"]
    blind_fields = mapping["blind_display_fields"]
    for row in documents:
        document_id = str(row.get(mapping["document_id"], "")).strip()
        entity_type = str(row.get(mapping["document_entity_type"], "")).strip()
        source = str(row.get(mapping["document_source"], "")).strip()
        if not document_id or document_id in seen_documents or not entity_type or not source:
            raise PoolError("DOCUMENT_IDENTITY_INVALID", "document ids, entity types, and sources must be non-empty; ids must be unique")
        seen_documents.add(document_id)
        normalized_documents.append({
            "document_id": document_id,
            "entity_type": entity_type,
            "source": source,
            "fields": {field: row.get(field) for field in text_fields},
            "display": {field: row.get(field) for field in blind_fields},
        })
    if not normalized_queries or not normalized_documents:
        raise PoolError("EMPTY_DATASET", "query and document snapshots must both be non-empty")
    return sorted(normalized_queries, key=lambda item: item["query_id"]), sorted(normalized_documents, key=lambda item: item["document_id"])


def _query_texts(query: Mapping[str, Any]) -> list[str]:
    return [str(query["text"]), *(str(item) for item in query.get("variants", []))]


def _document_text(document: Mapping[str, Any], fields: Sequence[str] | None = None) -> str:
    selected = fields or list(document["fields"])
    return " ".join(_flatten(document["fields"].get(field)) for field in selected)


def _bm25f_scores(query: Mapping[str, Any], documents: Sequence[Mapping[str, Any]], retriever: Mapping[str, Any]) -> dict[str, float]:
    weights = retriever["field_weights"]
    per_field_tokens: dict[str, dict[str, list[str]]] = {}
    averages: dict[str, float] = {}
    for field in weights:
        per_field_tokens[field] = {
            doc["document_id"]: _tokens(_flatten(doc["fields"].get(field))) for doc in documents
        }
        averages[field] = sum(len(tokens) for tokens in per_field_tokens[field].values()) / len(documents) or 1.0
    query_variants = [_tokens(text) for text in _query_texts(query)]
    scores = {doc["document_id"]: 0.0 for doc in documents}
    for query_tokens in query_variants:
        variant_scores = {doc["document_id"]: 0.0 for doc in documents}
        for term in set(query_tokens):
            matching_docs = sum(
                1 for doc in documents if any(term in per_field_tokens[field][doc["document_id"]] for field in weights)
            )
            idf = math.log(1.0 + (len(documents) - matching_docs + 0.5) / (matching_docs + 0.5))
            for doc in documents:
                doc_id = doc["document_id"]
                term_score = 0.0
                for field, weight in weights.items():
                    tokens = per_field_tokens[field][doc_id]
                    tf = tokens.count(term)
                    if tf:
                        norm = tf + 1.2 * (0.25 + 0.75 * len(tokens) / averages[field])
                        term_score += float(weight) * (tf * 2.2 / norm)
                variant_scores[doc_id] += idf * term_score
        for doc_id, score in variant_scores.items():
            scores[doc_id] = max(scores[doc_id], score)
    return scores


def _char_scores(query: Mapping[str, Any], documents: Sequence[Mapping[str, Any]], retriever: Mapping[str, Any]) -> dict[str, float]:
    minimum = int(retriever.get("ngram_min", 2))
    maximum = int(retriever.get("ngram_max", 3))

    def grams(text: str) -> set[str]:
        compact = _normalized_text(text)
        return {
            compact[index:index + size]
            for size in range(minimum, maximum + 1)
            for index in range(max(0, len(compact) - size + 1))
        }

    scores: dict[str, float] = {}
    for document in documents:
        doc_text = _document_text(document)
        doc_grams = grams(doc_text)
        best = 0.0
        for text in _query_texts(query):
            query_grams = grams(text)
            dice = (2.0 * len(query_grams & doc_grams) / (len(query_grams) + len(doc_grams))) if query_grams and doc_grams else 0.0
            ratio = SequenceMatcher(None, _normalized_text(text), _normalized_text(doc_text)).ratio()
            best = max(best, 0.75 * dice + 0.25 * ratio)
        scores[document["document_id"]] = best
    return scores


def _hash_vector(features: Sequence[str], dimensions: int) -> list[float]:
    vector = [0.0] * dimensions
    for feature in features:
        digest = hashlib.sha256(feature.encode("utf-8")).digest()
        index = int.from_bytes(digest[:4], "big") % dimensions
        sign = 1.0 if digest[4] & 1 else -1.0
        vector[index] += sign
    norm = math.sqrt(sum(value * value for value in vector)) or 1.0
    return [value / norm for value in vector]


def _semantic_scores(query: Mapping[str, Any], documents: Sequence[Mapping[str, Any]], retriever: Mapping[str, Any]) -> dict[str, float]:
    dimensions = int(retriever.get("dimensions", 256))
    query_vectors = [_hash_vector(_features(text), dimensions) for text in _query_texts(query)]
    scores: dict[str, float] = {}
    for document in documents:
        doc_vector = _hash_vector(_features(_document_text(document)), dimensions)
        scores[document["document_id"]] = max(sum(left * right for left, right in zip(query_vector, doc_vector, strict=True)) for query_vector in query_vectors)
    return scores


def _structured_value(document: Mapping[str, Any], key: str) -> Any:
    if key == "entity_type":
        return document["entity_type"]
    value = document["fields"].get(key)
    if value is not None:
        return value
    attributes = document["fields"].get("attributes")
    return attributes.get(key) if isinstance(attributes, dict) else None


def _structured_status(query: Mapping[str, Any], document: Mapping[str, Any]) -> tuple[int, int, int]:
    structured = query.get("structured", {})
    matched = conflicts = unknown = 0
    for key, expected in structured.items():
        if key == "brand_not":
            actual = _structured_value(document, "brand")
            if actual in (None, ""):
                unknown += 1
            elif str(actual).casefold() == str(expected).casefold():
                conflicts += 1
            else:
                matched += 1
        elif key == "max_price_minor":
            actual = _structured_value(document, "price_minor")
            if type(actual) not in (int, float):
                unknown += 1
            elif actual <= expected:
                matched += 1
            else:
                conflicts += 1
        else:
            actual = _structured_value(document, key)
            if actual in (None, ""):
                unknown += 1
            elif str(actual).casefold() == str(expected).casefold():
                matched += 1
            else:
                conflicts += 1
    return matched, conflicts, unknown


def _structured_scores(query: Mapping[str, Any], documents: Sequence[Mapping[str, Any]]) -> dict[str, float]:
    scores = {}
    for document in documents:
        matched, conflicts, unknown = _structured_status(query, document)
        scores[document["document_id"]] = 2.0 * matched - 1.5 * conflicts - 0.1 * unknown
    return scores


def _rank_scores(scores: Mapping[str, float], depth: int) -> list[dict[str, Any]]:
    ordered = sorted(scores.items(), key=lambda item: (-item[1], item[0]))[:depth]
    return [
        {"document_id": document_id, "rank": rank, "score": round(float(score), 10)}
        for rank, (document_id, score) in enumerate(ordered, 1)
    ]


def _pair_overlap_score(query: Mapping[str, Any], document: Mapping[str, Any]) -> float:
    query_features = set(_features(" ".join(_query_texts(query))))
    document_features = set(_features(_document_text(document)))
    overlap = len(query_features & document_features) / max(1, len(query_features))
    matched, conflicts, unknown = _structured_status(query, document)
    return overlap + 0.15 * matched - 0.2 * conflicts - 0.02 * unknown


class _ExclusiveLock:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.fd: int | None = None

    def __enter__(self) -> "_ExclusiveLock":
        try:
            self.fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(self.fd, str(os.getpid()).encode("ascii"))
        except FileExistsError as exc:
            raise PoolError("RUN_BUSY", "the run is already being changed by another caller") from exc
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if self.fd is not None:
            os.close(self.fd)
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass


class PoolService:
    """Shared deterministic service used by CLI, Skill scripts, and MCP."""

    def __init__(self, data_root: Path | str, run_root: Path | str) -> None:
        self.data_root = Path(data_root).resolve()
        self.run_root = Path(run_root).resolve()
        self.schema_path = Path(__file__).resolve().parent / "schemas" / "config.schema.json"
        self.data_root.mkdir(parents=True, exist_ok=True)
        self.run_root.mkdir(parents=True, exist_ok=True)

    def _dataset_paths(self, dataset_ref: str) -> tuple[Path, Path, Path, Path]:
        dataset = _safe_relative(self.data_root, dataset_ref, kind="dataset")
        if not dataset.is_dir():
            raise PoolError("DATASET_NOT_FOUND", "dataset reference does not exist")
        return dataset, dataset / "queries.jsonl", dataset / "documents.jsonl", dataset / "config.json"

    def _run_paths(self, run_id: str) -> RunPaths:
        if not RUN_ID_RE.fullmatch(run_id):
            raise PoolError("RUN_ID_INVALID", "run_id has an invalid shape")
        root = _safe_relative(self.run_root, run_id, kind="run")
        if not root.is_dir():
            raise PoolError("RUN_NOT_FOUND", "run_id does not exist")
        return RunPaths(root)

    def _identity(self, dataset_ref: str) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]], dict[str, str]]:
        _dataset, query_path, document_path, config_path = self._dataset_paths(dataset_ref)
        config = read_json(config_path)
        _validate_config(config, self.schema_path)
        query_rows = read_jsonl(query_path)
        document_rows = read_jsonl(document_path)
        normalized_queries, normalized_documents = _normalize_inputs(query_rows, document_rows, config)
        source_hashes = {
            "queries": sha256_file(query_path),
            "documents": sha256_file(document_path),
            "config": sha256_file(config_path),
        }
        identity_payload = {
            "core_version": CORE_VERSION,
            "dataset_ref": dataset_ref.replace("\\", "/"),
            "dataset": config["dataset"],
            "source_hashes": source_hashes,
            "field_mapping_hash": sha256_bytes(canonical_bytes(config["field_mapping"])),
            "retriever_binding_hash": sha256_bytes(canonical_bytes(config["retrievers"])),
            "reranker_binding_hash": sha256_bytes(canonical_bytes(config["reranker"])),
        }
        identity_hash = sha256_bytes(canonical_bytes(identity_payload))
        return config, normalized_queries, normalized_documents, {**source_hashes, "identity": identity_hash}

    def create_run(self, dataset_ref: str, *, inject_failure_after: str | None = None) -> dict[str, Any]:
        if inject_failure_after not in (None, "NORMALIZE"):
            raise PoolError("INVALID_INJECTION_STAGE", "create failure injection may only target NORMALIZE")
        config, queries, documents, hashes = self._identity(dataset_ref)
        run_id = f"rjp-{hashes['identity'][:20]}"
        run_dir = self.run_root / run_id
        lock_path = self.run_root / f".{run_id}.create.lock"
        with _ExclusiveLock(lock_path):
            if run_dir.exists():
                manifest = read_json(run_dir / "manifest.json")
                if manifest.get("identity_hash") != hashes["identity"]:
                    raise PoolError("RUN_ID_COLLISION", "existing run identity does not match")
                return {"run_id": run_id, "status": self.get_run_status(run_id)["status"], "idempotent_reuse": True}
            run_dir.mkdir(parents=False, exist_ok=False)
            paths = RunPaths(run_dir)
            manifest = {
                "schema_version": RUN_SCHEMA_VERSION,
                "core_version": CORE_VERSION,
                "run_id": run_id,
                "identity_hash": hashes["identity"],
                "dataset_ref": dataset_ref.replace("\\", "/"),
                "dataset": config["dataset"],
                "source_hashes": {key: hashes[key] for key in ("queries", "documents", "config")},
                "field_mapping_hash": sha256_bytes(canonical_bytes(config["field_mapping"])),
                "retriever_binding_hash": sha256_bytes(canonical_bytes(config["retrievers"])),
                "reranker_binding_hash": sha256_bytes(canonical_bytes(config["reranker"])),
                "safety": config["safety"],
            }
            write_json(paths.manifest, manifest)
            query_output = run_dir / "normalized" / "queries.jsonl"
            document_output = run_dir / "normalized" / "documents.jsonl"
            config_output = run_dir / "normalized" / "config.json"
            write_jsonl(query_output, queries)
            write_jsonl(document_output, documents)
            write_json(config_output, config)
            normalize_outputs = self._hash_paths(run_dir, (query_output, document_output, config_output, paths.manifest))
            state = {
                "schema_version": STATE_SCHEMA_VERSION,
                "run_id": run_id,
                "identity_hash": hashes["identity"],
                "status": "IN_PROGRESS",
                "phase": "NORMALIZE",
                "stages": {
                    "NORMALIZE": {
                        "status": "COMPLETE",
                        "inputs": {key: hashes[key] for key in ("queries", "documents", "config")},
                        "outputs": normalize_outputs,
                    }
                },
                "external_submissions": {},
            }
            write_json(paths.state, state)
            self._maybe_inject(inject_failure_after, "NORMALIZE")
        return {"run_id": run_id, "status": "IN_PROGRESS", "phase": "NORMALIZE", "idempotent_reuse": False}

    def submit_retrieval_run(self, run_id: str, retriever_id: str, submission_ref: str) -> dict[str, Any]:
        paths = self._run_paths(run_id)
        with _ExclusiveLock(paths.root / ".mutate.lock"):
            state, manifest, config, queries, documents = self._load_run(paths)
            retriever = next((item for item in config["retrievers"] if item["retriever_id"] == retriever_id), None)
            if retriever is None or retriever["kind"] != "external":
                raise PoolError("EXTERNAL_RETRIEVER_NOT_DECLARED", "retriever_id is not a declared external retriever")
            if state["phase"] not in {"NORMALIZE", "RETRIEVE"}:
                raise PoolError("RUN_ALREADY_BUILT", "external runs cannot be submitted after retrieval closes")
            dataset_dir, *_ = self._dataset_paths(manifest["dataset_ref"])
            submission_path = _safe_relative(dataset_dir, submission_ref, kind="submission")
            rows = read_jsonl(submission_path)
            self._validate_external_rows(rows, queries, documents, int(retriever["depth"]))
            submission_hash = sha256_file(submission_path)
            previous = state["external_submissions"].get(retriever_id)
            if previous:
                if previous["sha256"] != submission_hash:
                    raise PoolError("EXTERNAL_RUN_DRIFT", "an immutable external submission already exists with different bytes")
                return {"run_id": run_id, "retriever_id": retriever_id, "status": "ACCEPTED", "idempotent_reuse": True}
            output = paths.root / "external_runs" / f"{retriever_id}.jsonl"
            write_jsonl(output, rows)
            state["external_submissions"][retriever_id] = {
                "sha256": sha256_file(output),
                "row_count": len(rows),
                "model": retriever["model"],
                "revision": retriever["revision"],
            }
            write_json(paths.state, state)
            return {"run_id": run_id, "retriever_id": retriever_id, "status": "ACCEPTED", "idempotent_reuse": False}

    def build_pool(self, run_id: str, *, inject_failure_after: str | None = None) -> dict[str, Any]:
        paths = self._run_paths(run_id)
        if inject_failure_after is not None and inject_failure_after not in STAGES[:-1]:
            raise PoolError("INVALID_INJECTION_STAGE", "failure injection stage is invalid")
        with _ExclusiveLock(paths.root / ".mutate.lock"):
            state, manifest, config, queries, documents = self._load_run(paths)
            if state["status"] == "READY":
                verification = self._verify_loaded(paths, state, manifest, config, queries, documents)
                return {"run_id": run_id, "status": "READY", "idempotent_reuse": True, "verification": verification}
            self._ensure_external_ready(paths, state, config)
            retriever_runs = self._stage_retrieve(paths, state, config, queries, documents)
            self._maybe_inject(inject_failure_after, "RETRIEVE")
            analysis = self._stage_analyze(paths, state, config, queries, documents, retriever_runs)
            self._maybe_inject(inject_failure_after, "ANALYZE")
            reranker_rows = self._stage_rerank(paths, state, config, queries, documents, analysis)
            self._maybe_inject(inject_failure_after, "CE_SCORE")
            pool_rows = self._stage_select(paths, state, config, queries, documents, retriever_runs, analysis, reranker_rows)
            self._maybe_inject(inject_failure_after, "SELECT")
            self._stage_package(paths, state, config, queries, pool_rows)
            self._maybe_inject(inject_failure_after, "PACKAGE")
            self._stage_verify(paths, state, manifest, config, queries, documents)
            self._maybe_inject(inject_failure_after, "VERIFY")
            state["status"] = "READY"
            state["phase"] = "READY"
            state["stages"]["READY"] = {"status": "COMPLETE", "inputs": {}, "outputs": {}}
            write_json(paths.state, state)
            verification = self._verify_loaded(paths, state, manifest, config, queries, documents)
            return {"run_id": run_id, "status": "READY", "idempotent_reuse": False, "verification": verification}

    def get_run_status(self, run_id: str) -> dict[str, Any]:
        paths = self._run_paths(run_id)
        state = read_json(paths.state)
        config = read_json(paths.root / "normalized" / "config.json")
        pending_external = [
            item["retriever_id"] for item in config["retrievers"]
            if item["kind"] == "external" and item["retriever_id"] not in state.get("external_submissions", {})
        ]
        return {
            "run_id": run_id,
            "status": state["status"],
            "phase": state["phase"],
            "completed_stages": [stage for stage in STAGES if state.get("stages", {}).get(stage, {}).get("status") == "COMPLETE"],
            "pending_external_retrievers": pending_external,
        }

    def export_blind_packet(self, run_id: str) -> dict[str, Any]:
        paths = self._run_paths(run_id)
        state = read_json(paths.state)
        if state.get("status") != "READY":
            raise PoolError("RUN_NOT_READY", "blind packet is unavailable until the run is READY")
        blind = paths.root / "blind_packet.jsonl"
        rows = read_jsonl(blind)
        return {
            "run_id": run_id,
            "status": "READY",
            "artifact": "blind_packet.jsonl",
            "sha256": sha256_file(blind),
            "query_count": len(rows),
            "candidate_pair_count": sum(len(row["candidates"]) for row in rows),
            "label_status": "UNJUDGED",
        }

    def verify_run(self, run_id: str) -> dict[str, Any]:
        paths = self._run_paths(run_id)
        state, manifest, config, queries, documents = self._load_run(paths)
        return self._verify_loaded(paths, state, manifest, config, queries, documents)

    def _load_run(self, paths: RunPaths) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
        state = read_json(paths.state)
        manifest = read_json(paths.manifest)
        config = read_json(paths.root / "normalized" / "config.json")
        queries = read_jsonl(paths.root / "normalized" / "queries.jsonl")
        documents = read_jsonl(paths.root / "normalized" / "documents.jsonl")
        if state.get("run_id") != manifest.get("run_id") or state.get("identity_hash") != manifest.get("identity_hash"):
            raise PoolError("RUN_IDENTITY_DRIFT", "run state and manifest identity do not match")
        current_config, current_queries, current_documents, hashes = self._identity(manifest["dataset_ref"])
        if hashes["identity"] != manifest["identity_hash"]:
            raise PoolError("INPUT_DRIFT", "dataset, config, model, or adapter binding changed after run creation")
        if canonical_bytes(current_config) != canonical_bytes(config) or canonical_bytes(current_queries) != canonical_bytes(queries) or canonical_bytes(current_documents) != canonical_bytes(documents):
            raise PoolError("NORMALIZED_INPUT_DRIFT", "normalized inputs no longer match their source snapshot")
        for stage in state.get("stages", {}).values():
            for relative, expected in stage.get("outputs", {}).items():
                artifact = paths.root / relative
                if not artifact.is_file() or sha256_file(artifact) != expected:
                    raise PoolError("ARTIFACT_TAMPERED", "a completed stage artifact is missing or has changed")
        return state, manifest, config, queries, documents

    @staticmethod
    def _hash_paths(root: Path, paths: Sequence[Path]) -> dict[str, str]:
        return {path.relative_to(root).as_posix(): sha256_file(path) for path in paths}

    def _record_stage(self, paths: RunPaths, state: dict[str, Any], stage: str, inputs: Mapping[str, str], outputs: Sequence[Path]) -> None:
        state["phase"] = stage
        state["stages"][stage] = {
            "status": "COMPLETE",
            "inputs": dict(sorted(inputs.items())),
            "outputs": self._hash_paths(paths.root, outputs),
        }
        write_json(paths.state, state)

    @staticmethod
    def _maybe_inject(requested: str | None, stage: str) -> None:
        if requested == stage:
            raise PoolError("INJECTED_FAILURE", f"test-only failure injected after {stage}")

    @staticmethod
    def _validate_external_rows(rows: Sequence[dict[str, Any]], queries: Sequence[dict[str, Any]], documents: Sequence[dict[str, Any]], depth: int) -> None:
        _assert_label_free(rows)
        query_ids = {row["query_id"] for row in queries}
        document_ids = {row["document_id"] for row in documents}
        grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in rows:
            if set(row) != {"query_id", "document_id", "rank", "score"}:
                raise PoolError("EXTERNAL_RUN_SCHEMA_INVALID", "external run rows must contain exactly query_id, document_id, rank, score")
            if row["query_id"] not in query_ids or row["document_id"] not in document_ids:
                raise PoolError("EXTERNAL_RUN_IDENTITY_INVALID", "external run references an unknown query or document")
            if type(row["rank"]) is not int or not 1 <= row["rank"] <= depth or type(row["score"]) not in (int, float) or not math.isfinite(row["score"]):
                raise PoolError("EXTERNAL_RUN_VALUE_INVALID", "external ranks/scores are invalid")
            grouped[row["query_id"]].append(row)
        if set(grouped) != query_ids:
            raise PoolError("EXTERNAL_RUN_INCOMPLETE", "external run must cover every query")
        for query_id, query_rows in grouped.items():
            ranks = sorted(row["rank"] for row in query_rows)
            if ranks != list(range(1, len(ranks) + 1)) or len({row["document_id"] for row in query_rows}) != len(query_rows):
                raise PoolError("EXTERNAL_RUN_CARDINALITY_INVALID", "external ranks must be contiguous and document ids unique per query")

    def _ensure_external_ready(self, paths: RunPaths, state: dict[str, Any], config: dict[str, Any]) -> None:
        pending = [
            item["retriever_id"] for item in config["retrievers"]
            if item["kind"] == "external" and item["retriever_id"] not in state.get("external_submissions", {})
        ]
        if pending:
            raise PoolError("EXTERNAL_RUN_REQUIRED", "declared external retrieval runs are still missing")

    def _stage_retrieve(self, paths: RunPaths, state: dict[str, Any], config: dict[str, Any], queries: list[dict[str, Any]], documents: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
        stage = state["stages"].get("RETRIEVE")
        if stage:
            return {item["retriever_id"]: read_jsonl(paths.root / "runs" / f"{item['retriever_id']}.jsonl") for item in config["retrievers"]}
        outputs: list[Path] = []
        result: dict[str, list[dict[str, Any]]] = {}
        for retriever in config["retrievers"]:
            retriever_id = retriever["retriever_id"]
            if retriever["kind"] == "external":
                rows = read_jsonl(paths.root / "external_runs" / f"{retriever_id}.jsonl")
            else:
                rows = []
                for query in queries:
                    if retriever["kind"] == "lexical_bm25f":
                        scores = _bm25f_scores(query, documents, retriever)
                    elif retriever["kind"] == "char_ngram":
                        scores = _char_scores(query, documents, retriever)
                    elif retriever["kind"] == "semantic_hash":
                        scores = _semantic_scores(query, documents, retriever)
                    elif retriever["kind"] == "structured":
                        scores = _structured_scores(query, documents)
                    else:
                        raise PoolError("RETRIEVER_KIND_UNSUPPORTED", "configured retriever kind is unsupported")
                    rows.extend({"query_id": query["query_id"], **row} for row in _rank_scores(scores, int(retriever["depth"])))
            output = paths.root / "runs" / f"{retriever_id}.jsonl"
            write_jsonl(output, rows)
            outputs.append(output)
            result[retriever_id] = rows
        self._record_stage(paths, state, "RETRIEVE", state["stages"]["NORMALIZE"]["outputs"], outputs)
        return result

    def _stage_analyze(self, paths: RunPaths, state: dict[str, Any], config: dict[str, Any], queries: list[dict[str, Any]], documents: list[dict[str, Any]], runs: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
        output = paths.root / "analysis.json"
        if state["stages"].get("ANALYZE"):
            return read_json(output)
        by_retriever_query: dict[str, dict[str, list[dict[str, Any]]]] = {}
        for retriever_id, rows in runs.items():
            grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
            for row in rows:
                grouped[row["query_id"]].append(row)
            by_retriever_query[retriever_id] = grouped
        query_analyses = []
        rrf_k = int(config["fusion"]["k"])
        for query in queries:
            query_id = query["query_id"]
            ranks: dict[str, dict[str, int]] = defaultdict(dict)
            scores: dict[str, dict[str, float]] = defaultdict(dict)
            sets: dict[str, set[str]] = {}
            for retriever_id, grouped in by_retriever_query.items():
                query_rows = grouped[query_id]
                sets[retriever_id] = {row["document_id"] for row in query_rows}
                for row in query_rows:
                    ranks[row["document_id"]][retriever_id] = int(row["rank"])
                    scores[row["document_id"]][retriever_id] = float(row["score"])
            union = sorted(set().union(*sets.values()))
            fusion = {
                document_id: sum(1.0 / (rrf_k + rank) for rank in ranks[document_id].values())
                for document_id in union
            }
            fused = [document_id for document_id, _score in sorted(fusion.items(), key=lambda item: (-item[1], item[0]))]
            unique = {retriever_id: sorted(document_id for document_id in values if sum(document_id in other for other in sets.values()) == 1) for retriever_id, values in sets.items()}
            disagreement = []
            depth_by_id = {item["retriever_id"]: int(item["depth"]) for item in config["retrievers"]}
            for document_id in union:
                percentiles = [ranks[document_id].get(retriever_id, depth_by_id[retriever_id] + 1) / (depth_by_id[retriever_id] + 1) for retriever_id in sets]
                if min(percentiles) <= 0.2 and max(percentiles) > 0.5:
                    disagreement.append({"document_id": document_id, "spread": round(max(percentiles) - min(percentiles), 10)})
            disagreement.sort(key=lambda item: (-item["spread"], item["document_id"]))
            overlaps = {}
            retriever_ids = sorted(sets)
            for index, left in enumerate(retriever_ids):
                for right in retriever_ids[index + 1:]:
                    union_size = len(sets[left] | sets[right])
                    overlaps[f"{left}|{right}"] = round(len(sets[left] & sets[right]) / union_size, 10) if union_size else 1.0
            query_analyses.append({
                "query_id": query_id,
                "union_document_ids": union,
                "union_count": len(union),
                "rrf_ranking": [{"document_id": document_id, "rank": rank, "score": round(fusion[document_id], 12)} for rank, document_id in enumerate(fused, 1)],
                "ranks": {document_id: dict(sorted(values.items())) for document_id, values in sorted(ranks.items())},
                "scores": {document_id: dict(sorted(values.items())) for document_id, values in sorted(scores.items())},
                "unique_by_retriever": unique,
                "disagreement": disagreement,
                "pairwise_jaccard": overlaps,
            })
        analysis = {
            "schema_version": "retrieval-judgment-pool-analysis-v1",
            "retriever_count": len(config["retrievers"]),
            "retriever_ids": sorted(runs),
            "query_count": len(queries),
            "queries": query_analyses,
        }
        write_json(output, analysis)
        self._record_stage(paths, state, "ANALYZE", state["stages"]["RETRIEVE"]["outputs"], (output,))
        return analysis

    def _stage_rerank(self, paths: RunPaths, state: dict[str, Any], config: dict[str, Any], queries: list[dict[str, Any]], documents: list[dict[str, Any]], analysis: dict[str, Any]) -> list[dict[str, Any]]:
        output = paths.root / "reranker_scores.jsonl"
        if state["stages"].get("CE_SCORE"):
            return read_jsonl(output)
        documents_by_id = {row["document_id"]: row for row in documents}
        queries_by_id = {row["query_id"]: row for row in queries}
        budget = int(config["reranker"]["pair_budget_per_query"])
        rows = []
        for item in analysis["queries"]:
            ranking = [row["document_id"] for row in item["rrf_ranking"]]
            for index, document_id in enumerate(ranking):
                scored = index < budget
                rows.append({
                    "query_id": item["query_id"],
                    "document_id": document_id,
                    "status": "SCORED" if scored else "NOT_SCORED",
                    "score": round(_pair_overlap_score(queries_by_id[item["query_id"]], documents_by_id[document_id]), 10) if scored else None,
                    "model": config["reranker"]["model"],
                    "revision": config["reranker"]["revision"],
                })
        write_jsonl(output, rows)
        self._record_stage(paths, state, "CE_SCORE", state["stages"]["ANALYZE"]["outputs"], (output,))
        return rows

    def _stage_select(self, paths: RunPaths, state: dict[str, Any], config: dict[str, Any], queries: list[dict[str, Any]], documents: list[dict[str, Any]], runs: dict[str, list[dict[str, Any]]], analysis: dict[str, Any], reranker_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        output = paths.root / "candidate_pool.audit.jsonl"
        if state["stages"].get("SELECT"):
            return read_jsonl(output)
        selection = config["selection"]
        budget = int(selection["final_pool_budget_per_query"])
        query_by_id = {row["query_id"]: row for row in queries}
        document_by_id = {row["document_id"]: row for row in documents}
        run_by_query: dict[str, dict[str, list[dict[str, Any]]]] = defaultdict(lambda: defaultdict(list))
        for retriever_id, rows in runs.items():
            for row in rows:
                run_by_query[row["query_id"]][retriever_id].append(row)
        reranker_by_query: dict[str, dict[str, dict[str, Any]]] = defaultdict(dict)
        for row in reranker_rows:
            reranker_by_query[row["query_id"]][row["document_id"]] = row
        output_rows = []
        for item in analysis["queries"]:
            query_id = item["query_id"]
            selected: list[str] = []
            reasons: dict[str, list[str]] = defaultdict(list)

            def add(document_id: str, reason: str) -> None:
                if document_id not in selected and len(selected) < budget:
                    selected.append(document_id)
                if document_id in selected and reason not in reasons[document_id]:
                    reasons[document_id].append(reason)

            core_ids: list[str] = []
            for retriever_id in sorted(run_by_query[query_id]):
                for row in run_by_query[query_id][retriever_id][: int(selection["top_k_per_retriever"])]:
                    if row["document_id"] not in core_ids:
                        core_ids.append(row["document_id"])
            if len(core_ids) > budget:
                raise PoolError("CORE_BUDGET_EXCEEDED", "per-retriever guaranteed top-k union exceeds the final pool budget")
            for document_id in core_ids:
                add(document_id, "top_k_guarantee")
            unique_candidates = []
            for retriever_id, values in sorted(item["unique_by_retriever"].items()):
                unique_candidates.extend((document_id, retriever_id) for document_id in values)
            for document_id, retriever_id in unique_candidates[: int(selection["unique_quota"])]:
                add(document_id, f"retriever_unique:{retriever_id}")
            for row in item["disagreement"][: int(selection["disagreement_quota"])]:
                add(row["document_id"], "rank_disagreement")
            hard_negatives = []
            for document_id in item["union_document_ids"]:
                matched, conflicts, _unknown = _structured_status(query_by_id[query_id], document_by_id[document_id])
                ranks = item["ranks"][document_id]
                best_rank = min(ranks.values())
                if conflicts and best_rank <= max(3, int(selection["raw_depth_per_retriever"]) // 2):
                    hard_negatives.append((best_rank, -matched, document_id))
            for _rank, _matched, document_id in sorted(hard_negatives)[: int(selection["hard_negative_quota"])]:
                add(document_id, "structured_conflict_hard_negative")
            scored = [row for row in reranker_by_query[query_id].values() if row["status"] == "SCORED"]
            if scored:
                scored_order = sorted(scored, key=lambda row: (-row["score"], row["document_id"]))
                median = sorted(row["score"] for row in scored)[len(scored) // 2]
                uncertain_order = sorted(scored, key=lambda row: (abs(row["score"] - median), row["document_id"]))
                reranker_candidates = []
                for row in (*scored_order, *uncertain_order):
                    if row["document_id"] not in reranker_candidates:
                        reranker_candidates.append(row["document_id"])
                for document_id in reranker_candidates[: int(selection["reranker_quota"])]:
                    add(document_id, "reranker_high_or_uncertain")
            remaining = [document_id for document_id in item["union_document_ids"] if document_id not in selected]
            rng = random.Random(f"{state['identity_hash']}:{query_id}:tail")
            rng.shuffle(remaining)
            for document_id in remaining[: int(selection["random_tail_quota"])]:
                add(document_id, "deterministic_random_tail")
            for row in item["rrf_ranking"]:
                add(row["document_id"], "rrf_fill")
            candidates = []
            for pool_rank, document_id in enumerate(selected, 1):
                document = document_by_id[document_id]
                candidates.append({
                    "document_id": document_id,
                    "entity_type": document["entity_type"],
                    "source": document["source"],
                    "pool_rank": pool_rank,
                    "label": "UNJUDGED",
                    "selection_reasons": reasons[document_id],
                    "retrieval_ranks": item["ranks"].get(document_id, {}),
                    "retrieval_scores": item["scores"].get(document_id, {}),
                    "reranker": reranker_by_query[query_id].get(document_id, {"status": "NOT_SCORED", "score": None}),
                    "display": document["display"],
                })
            output_rows.append({
                "schema_version": "retrieval-judgment-pool-audit-row-v1",
                "query_id": query_id,
                "query_text": query_by_id[query_id]["text"],
                "pool_budget": budget,
                "candidate_count": len(candidates),
                "outside_pool_is_negative": False,
                "human_judgment_performed": False,
                "candidates": candidates,
            })
        write_jsonl(output, output_rows)
        self._record_stage(paths, state, "SELECT", {**state["stages"]["ANALYZE"]["outputs"], **state["stages"]["CE_SCORE"]["outputs"]}, (output,))
        return output_rows

    def _stage_package(self, paths: RunPaths, state: dict[str, Any], config: dict[str, Any], queries: list[dict[str, Any]], pool_rows: list[dict[str, Any]]) -> None:
        output = paths.root / "blind_packet.jsonl"
        if state["stages"].get("PACKAGE"):
            return
        blind_rows = []
        for row in pool_rows:
            blind_rows.append({
                "schema_version": "retrieval-judgment-pool-blind-row-v1",
                "query_id": row["query_id"],
                "query_text": row["query_text"],
                "review_status": "PENDING_HUMAN_REVIEW",
                "candidates": [
                    {
                        "document_id": candidate["document_id"],
                        "entity_type": candidate["entity_type"],
                        "display": candidate["display"],
                        "label": "UNJUDGED",
                    }
                    for candidate in row["candidates"]
                ],
            })
        write_jsonl(output, blind_rows)
        self._record_stage(paths, state, "PACKAGE", state["stages"]["SELECT"]["outputs"], (output,))

    def _stage_verify(self, paths: RunPaths, state: dict[str, Any], manifest: dict[str, Any], config: dict[str, Any], queries: list[dict[str, Any]], documents: list[dict[str, Any]]) -> None:
        if state["stages"].get("VERIFY"):
            return
        pool = paths.root / "candidate_pool.audit.jsonl"
        blind = paths.root / "blind_packet.jsonl"
        receipt = paths.root / "receipt.json"
        receipt_value = {
            "schema_version": RECEIPT_SCHEMA_VERSION,
            "run_id": manifest["run_id"],
            "status": "READY",
            "identity_hash": manifest["identity_hash"],
            "manifest_sha256": sha256_file(paths.manifest),
            "candidate_pool_sha256": sha256_file(pool),
            "blind_packet_sha256": sha256_file(blind),
            "query_count": len(queries),
            "document_count": len(documents),
            "human_judgment_performed": False,
            "output_label": "UNJUDGED",
            "outside_pool_is_negative": False,
            "sealed_or_hidden_data_read": False,
            "qrels_read": False,
            "production_release_allowed": False,
        }
        write_json(receipt, receipt_value)
        authoritative = [
            paths.manifest,
            paths.root / "normalized" / "queries.jsonl",
            paths.root / "normalized" / "documents.jsonl",
            paths.root / "normalized" / "config.json",
            *(paths.root / "runs" / f"{item['retriever_id']}.jsonl" for item in config["retrievers"]),
            paths.root / "analysis.json",
            paths.root / "reranker_scores.jsonl",
            pool,
            blind,
            receipt,
        ]
        lines = [f"{sha256_file(path)}  {path.relative_to(paths.root).as_posix()}\n" for path in sorted(authoritative, key=lambda item: item.relative_to(paths.root).as_posix())]
        sums = paths.root / "SHA256SUMS.txt"
        _atomic_write(sums, "".join(lines).encode("utf-8"))
        self._record_stage(paths, state, "VERIFY", state["stages"]["PACKAGE"]["outputs"], (receipt, sums))

    def _verify_loaded(self, paths: RunPaths, state: dict[str, Any], manifest: dict[str, Any], config: dict[str, Any], queries: list[dict[str, Any]], documents: list[dict[str, Any]]) -> dict[str, Any]:
        if state.get("status") != "READY" or state.get("phase") != "READY":
            raise PoolError("RUN_NOT_READY", "run has not completed VERIFY and READY")
        sums_path = paths.root / "SHA256SUMS.txt"
        try:
            lines = sums_path.read_text(encoding="utf-8").splitlines()
        except OSError as exc:
            raise PoolError("CHECKSUMS_MISSING", "SHA256SUMS.txt is missing") from exc
        for line in lines:
            try:
                expected, relative = line.split("  ", 1)
            except ValueError as exc:
                raise PoolError("CHECKSUMS_INVALID", "SHA256SUMS.txt is malformed") from exc
            artifact = _safe_relative(paths.root, relative, kind="artifact")
            if not artifact.is_file() or sha256_file(artifact) != expected:
                raise PoolError("CHECKSUM_MISMATCH", "an authoritative run artifact failed checksum verification")
        pool_rows = read_jsonl(paths.root / "candidate_pool.audit.jsonl")
        blind_rows = read_jsonl(paths.root / "blind_packet.jsonl")
        if any(candidate.get("label") != "UNJUDGED" for row in pool_rows for candidate in row.get("candidates", [])):
            raise PoolError("LABEL_LEAK_DETECTED", "candidate pool contains a non-UNJUDGED label")
        forbidden_blind = {"source", "pool_rank", "selection_reasons", "retrieval_ranks", "retrieval_scores", "reranker"}
        for row in blind_rows:
            for candidate in row.get("candidates", []):
                if candidate.get("label") != "UNJUDGED" or forbidden_blind & set(candidate):
                    raise PoolError("BLIND_PACKET_LEAK_DETECTED", "blind packet exposes labels or ranking provenance")
        receipt = read_json(paths.root / "receipt.json")
        if receipt.get("run_id") != manifest["run_id"] or receipt.get("identity_hash") != manifest["identity_hash"]:
            raise PoolError("RECEIPT_IDENTITY_DRIFT", "receipt identity does not match the run")
        return {
            "run_id": manifest["run_id"],
            "status": "READY",
            "checksum_entry_count": len(lines),
            "query_count": len(queries),
            "document_count": len(documents),
            "candidate_pair_count": sum(len(row["candidates"]) for row in pool_rows),
            "all_candidates_unjudged": True,
            "blind_provenance_hidden": True,
            "qrels_read": False,
            "sealed_or_hidden_data_read": False,
            "production_release_allowed": False,
        }
