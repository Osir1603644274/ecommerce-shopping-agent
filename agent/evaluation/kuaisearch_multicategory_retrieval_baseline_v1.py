"""SUT-only retrieval ranking for the frozen KuaiSearch corpus.

This module deliberately has no judgment, taxonomy, or review
input.  It accepts only retrieval documents and a three-field query projection
(``queryId``, ``query``, ``split``), then emits deterministic top-k rankings.
The scorer is a separate concern and must consume the ranking output later.
"""

from __future__ import annotations

import hashlib
import importlib
import importlib.metadata
import json
import math
import os
import platform
import re
import sys
import time
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from statistics import mean, median
from typing import Any, Callable, Iterable, Mapping, Sequence

import numpy as np


SCHEMA_VERSION = "kuaisearch-multicategory-retrieval-baseline-v1"
DATASET_REVISION = "09807c773ce67360ed8df30842e372182fcf7ad9"
EXPECTED_DOCUMENTS_SHA256 = "6b8f55f94fb292e9ff946014221244e07fa338e22cfa29de39c42f8c2d1e245f"
EXPECTED_BREADTH_MANIFEST_SHA256 = "a3662a065043202e09fc604abf351a679b56fe151232b771de81dfd3158e2eba"
EXPECTED_DOCUMENT_COUNT = 46_079
EXPECTED_QUERY_COUNT = 507
TOP_K = 100
EXPECTED_STRATEGIES = ("bm25_fields", "bm25_title", "dense_title", "rrf_bm25_dense", "rrf_bm25_dense_ce")
RRF_K = 60
DENSE_MODEL = "BAAI/bge-small-zh-v1.5"
CROSS_ENCODER_MODEL = "BAAI/bge-reranker-v2-m3"
CROSS_ENCODER_REVISION = "953dc6f6f85a1b2dbfca4c34a2796e7dde08d41e"
BM25_FIELD_WEIGHTS = {"title": 1.0, "attributeText": 0.45, "brand": 0.25}

_DENSE_FILES = {
    "config.json": (739, "9088751d39abbf86ec3d19ffca92ad62ad19075f7e59712e6c71217fa125d1d3"),
    "model_optimized.onnx": (94781076, "1294ea4b6331115a353d81f96b85e8c8d7fdcc284453d5b2fab5b016230aad38"),
    "ort_config.json": (1234, "97e78d1d21c2eb719e865b018f17915df6a12ed987446eb7f3f3a783a5afb1e1"),
    "special_tokens_map.json": (125, "b6d346be366a7d1d48332dbc9fdf3bf8960b5d879522b7799ddba59e76237ee3"),
    "tokenizer_config.json": (367, "e6f3b96db926a37d4039995fbf5ad17de158dfb8f6343d607e4dbaad18d75f5a"),
    "tokenizer.json": (439125, "48cea5d44424912a6fd1ea647bf4fe50b55ab8b1e5879c3275f80e339e8fae26"),
    "vocab.txt": (109540, "45bbac6b341c319adc98a532532882e91a9cefc0329aa57bac9ae761c27b291c"),
}
_CROSS_FILES = {
    "config.json": (795, "13dcd6c31d9fec9d1d8e158702072f62d7fa7d312a64b9fe057bec9a08cfe41a"),
    "model.safetensors": (2271071852, "d9e3e081faff1eefb84019509b2f5558fd74c1a05a2c7db22f74174fcedb5286"),
    "sentencepiece.bpe.model": (5069051, "cfc8146abe2a0488e9e2a0c56de7952f7c11ab059eca145a0a727afce0db2865"),
    "special_tokens_map.json": (964, "8c785abebea9ae3257b61681b4e6fd8365ceafde980c21970d001e834cf10835"),
    "tokenizer_config.json": (1173, "7e4c1cc848840aeccdd763458c18dd525eb0f795c992e00ebe9c28554e7db2d4"),
    "tokenizer.json": (17098273, "69564b696052886ed0ac63fa393e928384e0f8caada38c1f4864a9bfbf379c15"),
}


class RankingInputError(ValueError):
    """Raised when an input is not an SUT-safe retrieval projection."""


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RankingInputError(f"invalid or missing JSON: {path}") from exc
    if not isinstance(value, dict):
        raise RankingInputError(f"JSON artifact must be an object: {path}")
    return value


@dataclass(frozen=True)
class RetrievalDocument:
    doc_id: str
    title: str
    attribute_text: str
    brand: str

    def field(self, name: str) -> str:
        return {
            "title": self.title,
            "attributeText": self.attribute_text,
            "brand": self.brand,
        }[name]


@dataclass(frozen=True)
class RetrievalQuery:
    query_id: str
    query: str
    split: str


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def canonical_json(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def _g0_root(g0_dir: Path, raw: Any) -> Path:
    if not isinstance(raw, str) or not raw:
        raise RankingInputError("G0 model snapshot has no root")
    candidate = Path(raw)
    # Contract snapshots are workspace-relative or absolute.  Do not accept
    # arbitrary CLI cache overrides in formal mode.
    return candidate if candidate.is_absolute() else (Path.cwd() / candidate).resolve()


def validate_g0_directory(g0_dir: Path) -> dict[str, Any]:
    """Run the shared, read-only G0 contract validator and return pinned paths.

    This function intentionally accepts no documents/query/cache overrides.
    The runner must derive every ranker input and model root from the frozen G0
    bundle, so a formal invocation cannot silently switch to another corpus.
    """
    root = Path(g0_dir).resolve()
    if not root.is_dir():
        raise RankingInputError(f"G0 directory missing: {root}")
    models = _read_json(root / "models.manifest.json")
    for key in ("dense", "crossEncoder", "crossEncoderDependencies"):
        if not isinstance(models.get(key), Mapping):
            raise RankingInputError(f"G0 models manifest missing {key}")
    dense_root = _g0_root(root, models["dense"].get("root"))
    cross_root = _g0_root(root, models["crossEncoder"].get("root"))
    deps_root = _g0_root(root, models["crossEncoderDependencies"].get("root"))
    workspace = Path(__file__).resolve().parents[2]
    evaluation = workspace / "agent" / "evaluation"
    scripts = workspace / "agent" / "scripts"
    code_by_name = {
        "kuaisearch_multicategory_retrieval_contract_v1.py": evaluation / "kuaisearch_multicategory_retrieval_contract_v1.py",
        "prepare_kuaisearch_multicategory_retrieval_g0_v1.py": scripts / "prepare_kuaisearch_multicategory_retrieval_g0_v1.py",
        "kuaisearch_multicategory_retrieval_baseline_v1.py": evaluation / "kuaisearch_multicategory_retrieval_baseline_v1.py",
        "run_kuaisearch_multicategory_retrieval_baseline_v1.py": scripts / "run_kuaisearch_multicategory_retrieval_baseline_v1.py",
        "kuaisearch_multicategory_retrieval_scorer_v1.py": evaluation / "kuaisearch_multicategory_retrieval_scorer_v1.py",
        "score_kuaisearch_multicategory_retrieval_g1_v1.py": scripts / "score_kuaisearch_multicategory_retrieval_g1_v1.py",
    }
    try:
        contract = importlib.import_module("evaluation.kuaisearch_multicategory_retrieval_contract_v1")
        os.environ["HF_HUB_OFFLINE"] = "1"
        os.environ["TRANSFORMERS_OFFLINE"] = "1"
        contract.verify_g0_bundle(
            g0_dir=root,
            runtime_code_paths=[code_by_name[name] for name in contract.FINAL_CODE_PIN_BASENAMES],
            dense_cache=dense_root,
            cross_cache=cross_root,
            cross_deps=deps_root,
        )
        # This is an offline/environment gate only.  Runtime package evidence
        # is collected later, after the validated dependency bundle is
        # prepended and the model stack is instantiated.
        contract.environment_pin(deps_root)
    except Exception as exc:
        if isinstance(exc, RankingInputError):
            raise
        raise RankingInputError(f"G0 validation failed closed: {exc}") from exc
    inputs = _read_json(root / "inputs.manifest.json")
    queries_rel = inputs.get("queries", {}).get("path")
    documents_rel = inputs.get("documents", {}).get("path")
    if queries_rel != "queries.jsonl" or documents_rel != "documents.jsonl":
        raise RankingInputError("G0 inputs must be the frozen local documents/queries artifacts")
    preregistration = _read_json(root / "preregistration.json")
    code_pins: dict[str, dict[str, Any]] = {}
    for row in preregistration.get("code", []):
        if isinstance(row, Mapping) and isinstance(row.get("path"), str):
            code_pins[Path(row["path"]).name] = {"sha256": row.get("sha256"), "bytes": row.get("bytes")}
    g0_binding = {
        "g0Dir": str(root),
        "manifestSha256": sha256_file(root / "manifest.json"),
        "inputsManifestSha256": sha256_file(root / "inputs.manifest.json"),
        "modelsManifestSha256": sha256_file(root / "models.manifest.json"),
        "preregistrationSha256": sha256_file(root / "preregistration.json"),
        "codePins": code_pins,
    }
    return {
        "g0Dir": root,
        "documents": root / "documents.jsonl",
        "queries": root / "queries.jsonl",
        "denseCache": dense_root,
        "crossEncoderCache": cross_root,
        "crossEncoderDeps": deps_root,
        "g0Binding": g0_binding,
    }


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line_number, line in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise RankingInputError(f"invalid JSONL at {path}:{line_number}") from exc
        if not isinstance(value, dict):
            raise RankingInputError(f"JSONL row is not an object at {path}:{line_number}")
        rows.append(value)
    return rows


def load_documents(path: Path) -> list[RetrievalDocument]:
    rows = _read_jsonl(Path(path))
    documents: list[RetrievalDocument] = []
    seen: set[str] = set()
    for row in rows:
        allowed = {"doc_id", "title", "attr_value", "brand", "seller_name"}
        if set(row) - allowed:
            raise RankingInputError("documents contain fields outside the retrieval projection")
        doc_id = str(row.get("doc_id") or "")
        if not doc_id or doc_id in seen:
            raise RankingInputError("document IDs must be non-empty and unique")
        seen.add(doc_id)
        documents.append(RetrievalDocument(
            doc_id=doc_id,
            title=str(row.get("title") or ""),
            attribute_text=str(row.get("attr_value") or ""),
            brand=str(row.get("brand") or ""),
        ))
    if not documents:
        raise RankingInputError("documents must not be empty")
    return sorted(documents, key=lambda item: item.doc_id)


def load_queries(path: Path) -> list[RetrievalQuery]:
    rows = _read_jsonl(Path(path))
    queries: list[RetrievalQuery] = []
    seen: set[str] = set()
    for row in rows:
        if set(row) != {"queryId", "query", "split"}:
            raise RankingInputError("queries must contain exactly queryId/query/split")
        query_id = str(row.get("queryId") or "")
        if not query_id or query_id in seen:
            raise RankingInputError("query IDs must be non-empty and unique")
        seen.add(query_id)
        queries.append(RetrievalQuery(query_id, str(row.get("query") or ""), str(row.get("split") or "")))
    if not queries:
        raise RankingInputError("queries must not be empty")
    return queries


def tokenize_product_text(text: str) -> list[str]:
    lowered = text.lower()
    latin = re.findall(r"[a-z0-9]+", lowered)
    chinese = re.findall(r"[\u4e00-\u9fff]", lowered)
    return latin + chinese + [chinese[index] + chinese[index + 1] for index in range(len(chinese) - 1)]


class IndexedBM25:
    """Field-aware BM25 with a deterministic string-ID tie-break."""

    def __init__(self, documents: Sequence[RetrievalDocument], *, k1: float = 1.2, b: float = 0.75) -> None:
        started = time.perf_counter()
        self.documents = list(documents)
        self.k1 = float(k1)
        self.b = float(b)
        self._tokens: dict[str, dict[str, list[str]]] = {}
        self._term_frequencies: dict[str, dict[str, Counter[str]]] = {}
        self._postings: dict[str, dict[str, set[str]]] = {}
        self._average_length: dict[str, float] = {}
        for field in ("title", "attributeText", "brand"):
            field_tokens: dict[str, list[str]] = {}
            frequencies: dict[str, Counter[str]] = {}
            postings: dict[str, set[str]] = defaultdict(set)
            for document in self.documents:
                tokens = tokenize_product_text(document.field(field))
                field_tokens[document.doc_id] = tokens
                frequencies[document.doc_id] = Counter(tokens)
                for token in frequencies[document.doc_id]:
                    postings[token].add(document.doc_id)
            self._tokens[field] = field_tokens
            self._term_frequencies[field] = frequencies
            self._postings[field] = dict(postings)
            self._average_length[field] = sum(map(len, field_tokens.values())) / max(len(self.documents), 1)
        self.build_ms = (time.perf_counter() - started) * 1000

    def rank(self, query: str, *, field_weights: Mapping[str, float], top_k: int = TOP_K) -> list[str]:
        if top_k <= 0:
            raise ValueError("top_k must be positive")
        query_tokens = tokenize_product_text(query)
        if not query_tokens:
            return [document.doc_id for document in self.documents[:top_k]]
        scores: dict[str, float] = defaultdict(float)
        candidate_ids: set[str] = set()
        for field, weight in field_weights.items():
            if field not in self._postings:
                raise ValueError(f"unsupported BM25 field: {field}")
            if weight <= 0:
                continue
            postings = self._postings[field]
            candidate_ids.update(doc_id for token in query_tokens for doc_id in postings.get(token, ()))
            doc_count = len(self.documents)
            average_length = max(self._average_length[field], 1.0)
            for doc_id in candidate_ids:
                frequencies = self._term_frequencies[field][doc_id]
                length = len(self._tokens[field][doc_id])
                score = 0.0
                for token in query_tokens:
                    frequency = frequencies.get(token, 0)
                    if not frequency:
                        continue
                    document_frequency = len(postings.get(token, ()))
                    inverse = math.log(1 + (doc_count - document_frequency + 0.5) / (document_frequency + 0.5))
                    denominator = frequency + self.k1 * (1 - self.b + self.b * length / average_length)
                    score += inverse * frequency * (self.k1 + 1) / denominator
                scores[doc_id] += float(weight) * score
        ranked = sorted(scores, key=lambda doc_id: (-scores[doc_id], doc_id))
        if len(ranked) < top_k:
            ranked.extend(document.doc_id for document in self.documents if document.doc_id not in scores)
        return ranked[:top_k]


class DenseIndex:
    def __init__(self, documents: Sequence[RetrievalDocument], *, cache_dir: Path, model: Any | None = None, batch_size: int = 256) -> None:
        started = time.perf_counter()
        self.documents = list(documents)
        self.cache_dir = Path(cache_dir)
        self.model_name = DENSE_MODEL
        if model is None:
            model_dir = resolve_dense_model_dir(self.cache_dir)
            self.cache_receipt = verify_cache(model_dir, _DENSE_FILES, "dense")
            from fastembed import TextEmbedding
            os.environ.setdefault("HF_HUB_OFFLINE", "1")
            os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
            cache_root = self.cache_dir if model_dir != self.cache_dir else model_dir.parent
            model = TextEmbedding(model_name=DENSE_MODEL, cache_dir=str(cache_root))
        else:
            self.cache_receipt = {"path": str(self.cache_dir.resolve()), "injectedModel": True}
        self.model = model
        vectors = np.asarray(list(model.embed([document.title for document in self.documents], batch_size=batch_size)), dtype=np.float32)
        if vectors.ndim != 2 or vectors.shape[0] != len(self.documents):
            raise RankingInputError("dense model returned an invalid document matrix")
        norms = np.linalg.norm(vectors, axis=1, keepdims=True)
        self.vectors = vectors / np.maximum(norms, 1e-12)
        self.build_ms = (time.perf_counter() - started) * 1000

    def rank(self, query: str, *, top_k: int = TOP_K) -> list[str]:
        query_vector = np.asarray(list(self.model.embed([query])), dtype=np.float32)
        if query_vector.shape != (1, self.vectors.shape[1]):
            raise RankingInputError("dense model returned an invalid query vector")
        query_vector = query_vector / np.maximum(np.linalg.norm(query_vector, axis=1, keepdims=True), 1e-12)
        scores = self.vectors @ query_vector[0]
        order = np.lexsort((np.asarray([document.doc_id for document in self.documents]), -scores))
        return [self.documents[index].doc_id for index in order[:top_k]]


def resolve_dense_model_dir(cache_dir: Path) -> Path:
    path = Path(cache_dir)
    return path if (path / "model_optimized.onnx").exists() else path / "fast-bge-small-zh-v1.5"


def resolve_cross_model_dir(cache_dir: Path, revision: str = CROSS_ENCODER_REVISION) -> Path:
    path = Path(cache_dir)
    if (path / "model.safetensors").exists():
        return path
    return path / "models--BAAI--bge-reranker-v2-m3" / "snapshots" / revision


def verify_cache(path: Path, pins: Mapping[str, tuple[int, str]], cache_kind: str) -> dict[str, Any]:
    files: dict[str, dict[str, Any]] = {}
    for name, (expected_bytes, expected_sha) in pins.items():
        file_path = Path(path) / name
        if not file_path.is_file():
            raise RankingInputError(f"{cache_kind} cache file missing: {file_path}")
        actual_bytes = file_path.stat().st_size
        actual_sha = sha256_file(file_path)
        if actual_bytes != expected_bytes or actual_sha != expected_sha:
            raise RankingInputError(f"{cache_kind} cache pin mismatch: {file_path}")
        files[name] = {"bytes": actual_bytes, "sha256": actual_sha}
    return {"path": str(Path(path).resolve()), "files": files}


def freeze_dependency_snapshot(path: Path) -> dict[str, Any]:
    """Match the G0 contract's recursive canonical file snapshot."""

    root = Path(path).resolve()
    if not root.is_dir():
        raise RankingInputError(f"dependency directory missing: {root}")
    records = []
    files = sorted(
        (
            item for item in root.rglob("*")
            if item.is_file()
            and "__pycache__" not in item.relative_to(root).parts
            and item.suffix.lower() not in {".pyc", ".pyo", ".tmp", ".part", ".lock", ".swp"}
            and not item.name.endswith("~")
        ),
        key=lambda item: item.relative_to(root).as_posix(),
    )
    for item in files:
        records.append({
            "path": item.relative_to(root).as_posix(),
            "sha256": sha256_file(item),
            "bytes": item.stat().st_size,
        })
    if not records:
        raise RankingInputError(f"dependency directory is empty: {root}")
    return {
        "root": str(root),
        "fileCount": len(records),
        "snapshotSha256": hashlib.sha256(canonical_json(records)).hexdigest(),
    }


def rrf_fuse(rankings: Mapping[str, Sequence[str]], *, top_k: int = TOP_K, k: int = RRF_K) -> list[str]:
    if set(rankings) != {"bm25", "dense"}:
        raise ValueError("RRF requires exactly one BM25 and one Dense ranking")
    scores: dict[str, float] = defaultdict(float)
    best_rank: dict[str, int] = {}
    for ranking in rankings.values():
        seen: set[str] = set()
        for rank, doc_id in enumerate(ranking, 1):
            if doc_id in seen:
                continue
            seen.add(doc_id)
            scores[doc_id] += 1.0 / (k + rank)
            best_rank[doc_id] = min(rank, best_rank.get(doc_id, rank))
    ordered = sorted(scores, key=lambda doc_id: (-scores[doc_id], best_rank[doc_id], doc_id))
    return ordered[:top_k]


class CrossEncoderReranker:
    def __init__(self, documents: Mapping[str, RetrievalDocument], *, cache_dir: Path, revision: str = CROSS_ENCODER_REVISION, deps_dir: Path | None = None, model: Any | None = None, tokenizer: Any | None = None, device: Any | None = None) -> None:
        started = time.perf_counter()
        model_dir = resolve_cross_model_dir(Path(cache_dir), revision)
        self.cache_receipt = verify_cache(model_dir, _CROSS_FILES, "cross_encoder")
        self.model_name = CROSS_ENCODER_MODEL
        self.revision = revision
        self.deps_dir = prepend_local_deps(deps_dir)
        self.dependencies_snapshot = (
            freeze_dependency_snapshot(Path(self.deps_dir))
            if self.deps_dir else None
        )
        if model is None or tokenizer is None or device is None:
            os.environ["HF_HUB_OFFLINE"] = "1"
            os.environ["TRANSFORMERS_OFFLINE"] = "1"
            import torch
            from transformers import AutoModelForSequenceClassification, AutoTokenizer
            tokenizer = AutoTokenizer.from_pretrained(str(model_dir), local_files_only=True)
            model = AutoModelForSequenceClassification.from_pretrained(str(model_dir), local_files_only=True)
            device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
            model.to(device)
            model.eval()
        self.documents = dict(documents)
        self.tokenizer, self.model, self.device = tokenizer, model, device
        self.load_ms = (time.perf_counter() - started) * 1000

    @staticmethod
    def passage(document: RetrievalDocument) -> str:
        return f"标题：{document.title}；品牌：{document.brand}；属性：{document.attribute_text}"

    def rerank(self, query: str, ranking: Sequence[str], *, candidate_limit: int = 20, batch_size: int = 16) -> tuple[list[str], float]:
        import torch
        candidates = list(ranking[:candidate_limit])
        passages = [self.passage(self.documents[doc_id]) for doc_id in candidates]
        started = time.perf_counter()
        scores: list[float] = []
        with torch.inference_mode():
            for start in range(0, len(candidates), batch_size):
                batch = passages[start:start + batch_size]
                encoded = self.tokenizer([query] * len(batch), batch, padding=True, truncation=True, max_length=512, return_tensors="pt").to(self.device)
                logits = self.model(**encoded).logits.reshape(-1).float().cpu().tolist()
                scores.extend(float(score) for score in logits)
        ordered = [doc_id for doc_id, _ in sorted(zip(candidates, scores), key=lambda pair: (-pair[1], pair[0]))]
        return ordered + [doc_id for doc_id in ranking[candidate_limit:] if doc_id not in set(ordered)], (time.perf_counter() - started) * 1000


def prepend_local_deps(deps_dir: Path | None) -> str | None:
    """Put the pinned dependency bundle first before any transformers import."""

    if deps_dir is None:
        return None
    resolved = str(Path(deps_dir).resolve())
    if not Path(resolved).is_dir():
        raise RankingInputError(f"CrossEncoder dependency directory missing: {resolved}")
    if resolved in sys.path:
        sys.path.remove(resolved)
    sys.path.insert(0, resolved)
    return resolved


def _rss_bytes() -> int | None:
    try:
        import psutil
        return int(psutil.Process().memory_info().rss)
    except Exception:
        return None


def _latency_summary(values: Sequence[float]) -> dict[str, float]:
    if not values:
        return {"count": 0, "meanMs": 0.0, "medianMs": 0.0, "p95Ms": 0.0}
    ordered = sorted(float(value) for value in values)
    return {"count": len(ordered), "meanMs": round(mean(ordered), 4), "medianMs": round(median(ordered), 4), "p95Ms": round(ordered[max(0, math.ceil(len(ordered) * 0.95) - 1)], 4)}


def package_versions(*, runtime_model_stack: bool = False) -> dict[str, str | None]:
    versions: dict[str, str | None] = {}
    modules = {
        "fastembed": "fastembed",
        "onnxruntime": "onnxruntime",
        "torch": "torch",
        "transformers": "transformers",
        "huggingface_hub": "huggingface_hub",
    }
    distributions = {"huggingface_hub": "huggingface-hub"}
    for name, module_name in modules.items():
        try:
            if runtime_model_stack:
                module = sys.modules.get(module_name) or importlib.import_module(module_name)
                versions[name] = str(getattr(module, "__version__"))
            else:
                versions[name] = importlib.metadata.version(distributions.get(name, name))
        except (importlib.metadata.PackageNotFoundError, ImportError, AttributeError):
            versions[name] = None
    return versions


def hardware_receipt() -> dict[str, Any]:
    gpu_name: str | None = None
    gpu_vram: int | None = None
    try:
        torch = sys.modules.get("torch") or importlib.import_module("torch")
        if torch.cuda.is_available():
            gpu_name = str(torch.cuda.get_device_name(0))
            gpu_vram = int(torch.cuda.get_device_properties(0).total_memory)
    except (ImportError, AttributeError, RuntimeError):
        pass
    total_ram: int | None = None
    try:
        import psutil
        total_ram = int(psutil.virtual_memory().total)
    except (ImportError, AttributeError, OSError):
        pass
    return {
        "gpu": {"name": gpu_name, "totalVramBytes": gpu_vram},
        "cpu": platform.processor(),
        "platform": platform.platform(),
        "totalRamBytes": total_ram,
        "python": sys.version.split()[0],
    }


def rank_queries(documents: Sequence[RetrievalDocument], queries: Sequence[RetrievalQuery], *, dense: DenseIndex, bm25: IndexedBM25 | None = None, cross_encoder: CrossEncoderReranker | None = None, top_k: int = TOP_K, verify_cross_encoder_repeat: bool = True) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if top_k != TOP_K:
        raise ValueError("this preregistered runner fixes top_k=100")
    bm25 = bm25 or IndexedBM25(documents)
    rows: list[dict[str, Any]] = []
    component_timings: dict[str, list[float]] = defaultdict(list)
    strategy_timings: dict[str, list[float]] = defaultdict(list)
    repeat_verified = 0
    repeat_mismatches = 0
    repeat_extra_ms: list[float] = []
    for query in queries:
        t = time.perf_counter(); fields = bm25.rank(query.query, field_weights=BM25_FIELD_WEIGHTS, top_k=top_k); fields_ms = (time.perf_counter() - t) * 1000; component_timings["bm25Fields"].append(fields_ms)
        t = time.perf_counter(); title = bm25.rank(query.query, field_weights={"title": 1.0}, top_k=top_k); title_ms = (time.perf_counter() - t) * 1000; component_timings["bm25Title"].append(title_ms)
        t = time.perf_counter(); dense_rank = dense.rank(query.query, top_k=top_k); dense_ms = (time.perf_counter() - t) * 1000; component_timings["dense"].append(dense_ms)
        t = time.perf_counter(); fused = rrf_fuse({"bm25": fields, "dense": dense_rank}, top_k=top_k); fusion_ms = (time.perf_counter() - t) * 1000; component_timings["rrfFusion"].append(fusion_ms)
        rrf_e2e_ms = fields_ms + dense_ms + fusion_ms
        strategy_timings["bm25_fields"].append(fields_ms)
        strategy_timings["bm25_title"].append(title_ms)
        strategy_timings["dense_title"].append(dense_ms)
        strategy_timings["rrf_bm25_dense"].append(rrf_e2e_ms)
        ce_rank = None
        if cross_encoder is not None:
            ce_rank, ce_ms = cross_encoder.rerank(query.query, fused, candidate_limit=20)
            component_timings["crossEncoder"].append(ce_ms)
            strategy_timings["rrf_bm25_dense_ce"].append(rrf_e2e_ms + ce_ms)
            if verify_cross_encoder_repeat:
                repeat_started = time.perf_counter()
                repeat_rank, _repeat_component_ms = cross_encoder.rerank(query.query, fused, candidate_limit=20)
                repeat_extra_ms.append((time.perf_counter() - repeat_started) * 1000)
                if repeat_rank != ce_rank:
                    repeat_mismatches += 1
                    raise RankingInputError(f"CrossEncoder repeat ranking mismatch for query {query.query_id}")
                repeat_verified += 1
        strategy_rows = [
            ("bm25_fields", fields, fields_ms),
            ("bm25_title", title, title_ms),
            ("dense_title", dense_rank, dense_ms),
            ("rrf_bm25_dense", fused, rrf_e2e_ms),
        ]
        if ce_rank is not None:
            strategy_rows.append(("rrf_bm25_dense_ce", ce_rank, rrf_e2e_ms + ce_ms))
        for strategy, ranked_doc_ids, latency_ms in strategy_rows:
            rows.append({
                "schemaVersion": SCHEMA_VERSION,
                "queryId": query.query_id,
                "strategy": strategy,
                "rankedDocIds": list(ranked_doc_ids[:TOP_K]),
                "queryLatencyMs": round(float(latency_ms), 4),
            })
    return rows, {"components": {name: _latency_summary(values) for name, values in sorted(component_timings.items())}, "strategies": {name: _latency_summary(values) for name, values in sorted(strategy_timings.items())}, "repeatVerifiedQueries": repeat_verified, "repeatMismatchCount": repeat_mismatches, "repeatExtraLatencyMs": _latency_summary(repeat_extra_ms)}


def validate_rank_rows(rows: Sequence[Mapping[str, Any]], *, expected_queries: int = EXPECTED_QUERY_COUNT,
                       require_cross_encoder: bool = True) -> None:
    """Strict final-output gate used by formal runs before any bytes are written."""
    expected_strategies = EXPECTED_STRATEGIES if require_cross_encoder else EXPECTED_STRATEGIES[:-1]
    expected_count = expected_queries * len(expected_strategies)
    if len(rows) != expected_count:
        raise RankingInputError(f"expected {expected_count} ranking rows, got {len(rows)}")
    counts: Counter[str] = Counter()
    query_ids_by_strategy: dict[str, set[str]] = defaultdict(set)
    expected_query_ids: set[str] | None = None
    required_keys = {"schemaVersion", "queryId", "strategy", "rankedDocIds", "queryLatencyMs"}
    for row in rows:
        if set(row) != required_keys:
            raise RankingInputError("ranking row schema drift")
        if row["schemaVersion"] != SCHEMA_VERSION:
            raise RankingInputError("ranking row schemaVersion drift")
        query_id = row["queryId"]
        strategy = row["strategy"]
        if not isinstance(query_id, str) or not query_id or strategy not in expected_strategies:
            raise RankingInputError("ranking row queryId/strategy drift")
        ranked = row["rankedDocIds"]
        if not isinstance(ranked, list) or len(ranked) != TOP_K or len(set(ranked)) != TOP_K or any(not isinstance(doc_id, str) or not doc_id for doc_id in ranked):
            raise RankingInputError("each formal ranking must contain exactly 100 unique string doc IDs")
        latency = row["queryLatencyMs"]
        if isinstance(latency, bool) or not isinstance(latency, (int, float)) or not math.isfinite(float(latency)) or float(latency) < 0:
            raise RankingInputError("queryLatencyMs must be a finite non-negative number")
        counts[strategy] += 1
        query_ids_by_strategy[strategy].add(query_id)
        if expected_query_ids is None:
            expected_query_ids = {query_id}
        else:
            expected_query_ids.add(query_id)
    if set(counts) != set(expected_strategies) or any(counts[name] != expected_queries for name in expected_strategies):
        raise RankingInputError("formal output must contain exactly 507 rows per strategy")
    if expected_query_ids is None or len(expected_query_ids) != expected_queries or any(ids != expected_query_ids for ids in query_ids_by_strategy.values()):
        raise RankingInputError("formal strategies must cover the same 507 query IDs")


def build_receipt(*, documents_path: Path, queries_path: Path, documents: Sequence[RetrievalDocument], queries: Sequence[RetrievalQuery], bm25: IndexedBM25, dense: DenseIndex, cross_encoder: CrossEncoderReranker | None, query_latency: Mapping[str, Any], rss_before: int | None, rss_after: int | None, code_paths: Sequence[Path], rss_samples: Sequence[int | None] | None = None) -> dict[str, Any]:
    index_identity = hashlib.sha256(canonical_json({"documentsSha256": sha256_file(Path(documents_path)), "fields": BM25_FIELD_WEIGHTS, "tokenizer": "mixed-latin-single-chinese-plus-bigram-v1", "denseTextField": "title"})).hexdigest()
    receipt = {
        "schemaVersion": SCHEMA_VERSION,
        "datasetRevision": DATASET_REVISION,
        "contract": {"topK": TOP_K, "rrfK": RRF_K, "bm25Fields": BM25_FIELD_WEIGHTS, "denseTextField": "title", "crossEncoderCandidateLimit": 20, "crossEncoderDoesNotExpandCandidates": True, "randomSeed": 0},
        "datasetPins": {"documentsSha256": EXPECTED_DOCUMENTS_SHA256, "breadthManifestSha256": EXPECTED_BREADTH_MANIFEST_SHA256},
        "indexIdentity": index_identity,
        "inputs": {"documents": {"path": str(Path(documents_path).resolve()), "rows": len(documents), "sha256": sha256_file(Path(documents_path))}, "queries": {"path": str(Path(queries_path).resolve()), "rows": len(queries), "sha256": sha256_file(Path(queries_path))}},
        "models": {"dense": {"name": DENSE_MODEL, "cache": dense.cache_receipt}, "crossEncoder": ({"name": cross_encoder.model_name, "revision": cross_encoder.revision, "device": str(cross_encoder.device), "dependenciesPath": cross_encoder.deps_dir, "dependenciesSnapshot": cross_encoder.dependencies_snapshot, "cache": cross_encoder.cache_receipt} if cross_encoder else None)},
        "codeSha256": {str(Path(path).resolve()): sha256_file(Path(path)) for path in code_paths},
        "packageVersions": package_versions(runtime_model_stack=cross_encoder is not None),
        "hardware": hardware_receipt(),
        "memory": {"unit": "process RSS sampled", "isTruePeak": False, "rssBeforeBytes": rss_before, "rssAfterBytes": rss_after, "sampledPeakRssBytes": max((value for value in (rss_samples or (rss_before, rss_after)) if value is not None), default=None), "samplingPoints": ["before_build", "after_bm25", "after_dense", "after_ce_load", "after_query_batch"]},
        "buildMs": {"bm25": round(bm25.build_ms, 4), "dense": round(dense.build_ms, 4), "crossEncoderLoad": round(cross_encoder.load_ms, 4) if cross_encoder else None},
        "latency": {"coldBuildMs": round(bm25.build_ms + dense.build_ms + (cross_encoder.load_ms if cross_encoder else 0.0), 4), "components": dict(query_latency.get("components", {})), "strategies": dict(query_latency.get("strategies", {})), "crossEncoderRepeatVerification": {"repeatVerifiedQueries": int(query_latency.get("repeatVerifiedQueries", 0)), "repeatMismatchCount": int(query_latency.get("repeatMismatchCount", 0)), "repeatExtraLatencyMs": dict(query_latency.get("repeatExtraLatencyMs", {}))}},
    }
    return receipt


def write_run(*, output_dir: Path, rows: Sequence[Mapping[str, Any]], receipt: Mapping[str, Any], strict: bool = True) -> None:
    output_dir = Path(output_dir)
    if output_dir.exists() and any(output_dir.iterdir()):
        raise FileExistsError(f"refusing to overwrite non-empty output: {output_dir}")
    if strict:
        validate_rank_rows(rows)
    output_dir.mkdir(parents=True, exist_ok=True)
    rankings_path = output_dir / "rankings_top100.jsonl"
    rankings_path.write_bytes(b"".join(canonical_json(dict(row)) for row in rows))
    manifest = dict(receipt)
    manifest["outputs"] = {"rankings_top100.jsonl": {"rows": len(rows), "sha256": sha256_file(rankings_path), "bytes": rankings_path.stat().st_size}}
    (output_dir / "manifest.json").write_bytes(canonical_json(manifest))


__all__ = [
    "BM25_FIELD_WEIGHTS", "CROSS_ENCODER_MODEL", "CROSS_ENCODER_REVISION", "DATASET_REVISION", "DenseIndex", "IndexedBM25", "RankingInputError", "RetrievalDocument", "RetrievalQuery", "CrossEncoderReranker", "build_receipt", "freeze_dependency_snapshot", "hardware_receipt", "load_documents", "load_queries", "package_versions", "prepend_local_deps", "rank_queries", "resolve_cross_model_dir", "resolve_dense_model_dir", "rrf_fuse", "sha256_file", "validate_g0_directory", "validate_rank_rows", "verify_cache", "write_run",
]
