"""Agent knowledge retrieval primitives."""

from .merchant_docs import (
    DEFAULT_MERCHANT_DOCS_PATH,
    MERCHANT_DOC_SOURCE_NAME,
    MERCHANT_DOC_SOURCE_TYPE,
    load_merchant_doc_chunks,
    load_merchant_docs,
    merchant_doc_to_chunk,
)
from .models import (
    Citation,
    KnowledgeChunk,
    RetrievalStep,
    RetrievalTrace,
    SearchKnowledgeResult,
)
from .policy_docs import (
    POLICY_DOC_SOURCE_NAME,
    POLICY_DOC_SOURCE_TYPE,
    load_policy_markdown_chunks,
    parse_markdown_front_matter,
    policy_markdown_to_chunks,
)
from .publisher import (
    load_all_knowledge_chunks,
    rebuild_knowledge_index,
    validate_unique_chunk_ids,
)
from .qdrant_index import (
    KNOWLEDGE_PAYLOAD_INDEXES,
    ensure_knowledge_collection,
    knowledge_chunk_payload,
    knowledge_chunk_to_point,
    knowledge_point_id,
)
from .reviews import (
    REVIEW_SOURCE_NAME,
    REVIEW_SOURCE_TYPE,
    build_review_retrieval_trace,
    review_to_chunk,
    review_to_citation,
    reviews_to_citations,
)
from .review_hybrid_search import search_review_hybrid
from .router import route_knowledge_sources
from .search import DEFAULT_POLICY_DOCS_DIR, search_knowledge as search_knowledge_legacy
from .source_aware_search import (
    search_knowledge_source_aware,
    search_merchant_docs_lexical_index,
)
from .runtime_search import search_knowledge
from .unified_search import SOURCE_NAME_TO_TYPE, search_knowledge_index

__all__ = [
    "Citation",
    "DEFAULT_MERCHANT_DOCS_PATH",
    "DEFAULT_POLICY_DOCS_DIR",
    "KnowledgeChunk",
    "KNOWLEDGE_PAYLOAD_INDEXES",
    "MERCHANT_DOC_SOURCE_NAME",
    "MERCHANT_DOC_SOURCE_TYPE",
    "POLICY_DOC_SOURCE_NAME",
    "POLICY_DOC_SOURCE_TYPE",
    "REVIEW_SOURCE_NAME",
    "REVIEW_SOURCE_TYPE",
    "RetrievalStep",
    "RetrievalTrace",
    "SearchKnowledgeResult",
    "SOURCE_NAME_TO_TYPE",
    "build_review_retrieval_trace",
    "ensure_knowledge_collection",
    "knowledge_chunk_payload",
    "knowledge_chunk_to_point",
    "knowledge_point_id",
    "load_policy_markdown_chunks",
    "load_all_knowledge_chunks",
    "load_merchant_doc_chunks",
    "load_merchant_docs",
    "merchant_doc_to_chunk",
    "parse_markdown_front_matter",
    "policy_markdown_to_chunks",
    "review_to_chunk",
    "review_to_citation",
    "reviews_to_citations",
    "rebuild_knowledge_index",
    "route_knowledge_sources",
    "search_knowledge",
    "search_knowledge_legacy",
    "search_knowledge_index",
    "search_knowledge_source_aware",
    "search_merchant_docs_lexical_index",
    "search_review_hybrid",
    "validate_unique_chunk_ids",
]
