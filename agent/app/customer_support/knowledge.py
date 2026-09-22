"""Version-scoped policy retrieval with immutable citations and extractive answers."""
from __future__ import annotations

from functools import lru_cache
import hashlib
import json
import re
from pathlib import Path

from ..bm25 import BM25Document, BM25Index

POLICY_PATH = Path(__file__).with_name("policy_v1.json")


@lru_cache(maxsize=1)
def policy_snapshot():
    raw = POLICY_PATH.read_bytes()
    data = json.loads(raw)
    documents = [BM25Document(doc_id=row["id"], text=row["title"] + " " + row["terms"] + " " + row["text"], payload=dict(row)) for row in data["chunks"]]
    return data, hashlib.sha256(raw).hexdigest(), BM25Index(documents)


def retrieve_policy(query: str, *, policy_version: str = "support-simulator-v1", limit: int = 4):
    """No fallback from an unknown version to the latest policy."""
    if not isinstance(query, str) or not query.strip() or len(query) > 2000:
        raise ValueError("policy query must contain 1..2000 characters")
    if not 1 <= limit <= 8:
        raise ValueError("policy result limit must be 1..8")
    data, digest, index = policy_snapshot()
    if policy_version != data["policyVersion"]:
        return {"status": "POLICY_VERSION_UNAVAILABLE", "policyVersion": policy_version, "citations": []}
    # Capacity notation is common in questions but absent from generic policy prose.
    # Expand the domain concept only when an explicit change/exchange verb is present.
    retrieval_query = query
    if re.search(r'\d+\s*(?:GB|TB)(?![A-Za-z])', query, re.IGNORECASE) and re.search(r'换|改|升级', query):
        retrieval_query += ' 换货 容量 规格'
    if re.search(r'(?:黑|白|红|蓝|绿|金|银|紫|粉|灰|棕|橙|黄)色', query) and re.search(r'换|改|升级', query):
        retrieval_query += ' 换货 颜色 规格'
    scored = [(index.score(retrieval_query, position), document) for position, document in enumerate(index.documents)]
    scored.sort(key=lambda row: (-row[0], row[1].doc_id))
    citations = [{"id": f"policy:{policy_version}:{document.doc_id}", "kind": "policy", "title": document.payload["title"],
                  "text": document.payload["text"], "policyVersion": policy_version, "source": data["source"],
                  "snapshotSha256": digest, "contentSha256": hashlib.sha256(document.payload["text"].encode()).hexdigest(),
                  "score": score}
                 for score, document in scored[:limit] if score > 0]
    return {"status": "FOUND" if citations else "NO_EVIDENCE", "policyVersion": policy_version, "scope": data["scope"], "citations": citations}


def render_selected_policy(retrieval: dict, citation_ids: list[str]):
    """Model can select retrieved paragraphs, never author policy promises or substitute IDs."""
    if retrieval.get("status") != "FOUND":
        return {"answer": "当前缺少适用政策证据，需要核实后再答复。", "citations": [], "status": "NEEDS_VERIFICATION"}
    rows = {row["id"]: row for row in retrieval["citations"]}
    if not citation_ids or len(citation_ids) > 4 or len(set(citation_ids)) != len(citation_ids) or any(value not in rows for value in citation_ids):
        raise ValueError("policy citations must select unique retrieved evidence")
    selected = [rows[value] for value in citation_ids]
    for row in selected:
        if hashlib.sha256(row["text"].encode()).hexdigest() != row["contentSha256"]:
            raise ValueError("policy evidence changed after retrieval")
    return {"answer": "\n\n".join(f"{row['text']} [{index + 1}]" for index, row in enumerate(selected)),
            "citations": selected, "status": "SUPPORTED_EXTRACT"}
