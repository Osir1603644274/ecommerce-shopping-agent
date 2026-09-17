import math
import re
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from threading import RLock
from typing import Any


DEFAULT_BM25_K1 = 1.5
DEFAULT_BM25_B = 0.75


def tokenize_for_bm25(text: str) -> list[str]:
    """Tokenize mixed Chinese/English text without external dependencies."""

    lowered = text.lower()
    tokens = [token for token in re.findall(r"[a-z0-9]+", lowered) if len(token) >= 2]
    chinese_chars = re.findall(r"[\u4e00-\u9fff]", lowered)
    tokens.extend(
        "".join(chinese_chars[index : index + 2])
        for index in range(max(len(chinese_chars) - 1, 0))
    )
    return [token for token in tokens if len(token) >= 2]


@dataclass(frozen=True)
class BM25Document:
    doc_id: str
    text: str
    payload: dict[str, Any]


class BM25Index:
    def __init__(
        self,
        documents: list[BM25Document],
        *,
        k1: float = DEFAULT_BM25_K1,
        b: float = DEFAULT_BM25_B,
    ) -> None:
        if k1 <= 0:
            raise ValueError("k1 must be positive")
        if not 0 <= b <= 1:
            raise ValueError("b must be between 0 and 1")

        self.k1 = k1
        self.b = b
        self._lock = RLock()
        self.documents: list[BM25Document] = []
        self._document_by_id: dict[str, BM25Document] = {}
        self._document_positions: dict[str, int] = {}
        self._term_frequencies_by_id: dict[str, Counter[str]] = {}
        self._document_lengths_by_id: dict[str, int] = {}
        self._postings: dict[str, dict[str, int]] = {}
        self._total_document_length = 0
        self.term_frequencies: list[Counter[str]] = []
        self.document_lengths: list[int] = []
        self.average_document_length = 0.0
        self.document_frequencies: dict[str, int] = {}
        for document in documents:
            if document.doc_id in self._document_by_id:
                raise ValueError(f"duplicate BM25 document ID: {document.doc_id}")
            self._insert_new_document(document)
        self._refresh_compatibility_views()

    def _insert_new_document(self, document: BM25Document) -> None:
        term_frequency = Counter(tokenize_for_bm25(document.text))
        document_length = sum(term_frequency.values())
        self._document_positions[document.doc_id] = len(self.documents)
        self.documents.append(document)
        self._document_by_id[document.doc_id] = document
        self._term_frequencies_by_id[document.doc_id] = term_frequency
        self._document_lengths_by_id[document.doc_id] = document_length
        self._total_document_length += document_length
        for term, frequency in term_frequency.items():
            self._postings.setdefault(term, {})[document.doc_id] = frequency

    def _remove_document_terms(self, doc_id: str) -> None:
        term_frequency = self._term_frequencies_by_id.pop(doc_id)
        self._total_document_length -= self._document_lengths_by_id.pop(doc_id)
        for term in term_frequency:
            posting = self._postings[term]
            posting.pop(doc_id, None)
            if not posting:
                self._postings.pop(term, None)

    def _refresh_compatibility_views(self) -> None:
        self.term_frequencies = [
            self._term_frequencies_by_id[document.doc_id]
            for document in self.documents
        ]
        self.document_lengths = [
            self._document_lengths_by_id[document.doc_id]
            for document in self.documents
        ]
        self.average_document_length = (
            self._total_document_length / len(self.documents)
            if self.documents
            else 0.0
        )
        self.document_frequencies = {
            term: len(posting)
            for term, posting in self._postings.items()
        }

    @property
    def document_count(self) -> int:
        with self._lock:
            return len(self.documents)

    def idf(self, term: str) -> float:
        with self._lock:
            document_frequency = self.document_frequencies.get(term, 0)
            return math.log(
                1
                + (
                    len(self.documents) - document_frequency + 0.5
                )
                / (document_frequency + 0.5)
            )

    def _score_terms(
        self,
        query_terms: Counter[str],
        doc_id: str,
    ) -> float:
        term_frequency = self._term_frequencies_by_id[doc_id]
        document_length = self._document_lengths_by_id[doc_id]
        score = 0.0
        for term, query_count in query_terms.items():
            frequency = term_frequency.get(term, 0)
            if frequency <= 0:
                continue
            document_frequency = len(self._postings.get(term, {}))
            idf = math.log(
                1
                + (
                    len(self.documents) - document_frequency + 0.5
                )
                / (document_frequency + 0.5)
            )
            length_norm = 1 - self.b
            if self.average_document_length:
                length_norm += (
                    self.b * document_length / self.average_document_length
                )
            denominator = frequency + self.k1 * length_norm
            term_score = idf * (frequency * (self.k1 + 1)) / denominator
            score += math.log1p(query_count) * term_score
        return score

    def score(self, query: str, document_index: int) -> float:
        with self._lock:
            if not self.documents:
                return 0.0
            document = self.documents[document_index]
            return self._score_terms(
                Counter(tokenize_for_bm25(query)),
                document.doc_id,
            )

    def upsert(self, document: BM25Document) -> None:
        with self._lock:
            existing_position = self._document_positions.get(document.doc_id)
            if existing_position is None:
                self._insert_new_document(document)
            else:
                self._remove_document_terms(document.doc_id)
                self.documents[existing_position] = document
                self._document_by_id[document.doc_id] = document
                term_frequency = Counter(tokenize_for_bm25(document.text))
                document_length = sum(term_frequency.values())
                self._term_frequencies_by_id[document.doc_id] = term_frequency
                self._document_lengths_by_id[document.doc_id] = document_length
                self._total_document_length += document_length
                for term, frequency in term_frequency.items():
                    self._postings.setdefault(term, {})[
                        document.doc_id
                    ] = frequency
            self._refresh_compatibility_views()

    def remove(self, doc_id: str) -> bool:
        with self._lock:
            position = self._document_positions.pop(doc_id, None)
            if position is None:
                return False
            self._remove_document_terms(doc_id)
            self._document_by_id.pop(doc_id, None)
            self.documents.pop(position)
            for index in range(position, len(self.documents)):
                self._document_positions[self.documents[index].doc_id] = index
            self._refresh_compatibility_views()
            return True

    def search(
        self,
        query: str,
        *,
        limit: int = 10,
        doc_ids: set[str] | None = None,
    ) -> list[dict[str, Any]]:
        if limit <= 0:
            raise ValueError("limit must be positive")
        with self._lock:
            query_terms = Counter(tokenize_for_bm25(query))
            candidate_ids: set[str] = set()
            for term in query_terms:
                candidate_ids.update(self._postings.get(term, {}))
            if doc_ids is not None:
                candidate_ids.intersection_update(doc_ids)
            scored = [
                (doc_id, self._score_terms(query_terms, doc_id))
                for doc_id in candidate_ids
            ]
            scored = [item for item in scored if item[1] > 0]
            scored.sort(key=lambda item: (-item[1], item[0]))
            return [
                {
                    **self._document_by_id[doc_id].payload,
                    "score": score,
                }
                for doc_id, score in scored[:limit]
            ]

    def score_by_doc_id(self, query: str, doc_ids: set[str]) -> dict[str, float]:
        with self._lock:
            query_terms = Counter(tokenize_for_bm25(query))
            return {
                doc_id: self._score_terms(query_terms, doc_id)
                for doc_id in doc_ids
                if doc_id in self._document_by_id
            }

    def matching_doc_ids(
        self,
        predicate: Callable[[BM25Document], bool],
    ) -> set[str]:
        with self._lock:
            return {
                document.doc_id
                for document in self.documents
                if predicate(document)
            }
