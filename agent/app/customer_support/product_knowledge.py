"""Retrieve only the selected SKU's source attributes; never reinterpret them as sale promises."""
import hashlib
import json
import re

from ..bm25 import BM25Document, BM25Index


def retrieve_product(query, product, *, expected_item_id, limit=3):
    if str(product.get('id')) != str(expected_item_id):
        raise ValueError('product evidence belongs to another item')
    fragments = []
    raw = product.get('attributeText')
    if isinstance(raw, str):
        # Keep source text, not extracted/inferred attributes with uncertain provenance.
        fragments = [piece.strip() for piece in re.split(r'[\n；;。]+', raw) if piece.strip()]
    documents = [BM25Document(str(i), text[:1200], {'text': text[:1200]}) for i, text in enumerate(fragments[:100])]
    if not documents:
        return {'status': 'NO_EVIDENCE', 'citations': []}
    index = BM25Index(documents)
    ranked = sorted(((index.score(query, i), doc) for i, doc in enumerate(documents)), key=lambda row: (-row[0], row[1].doc_id))
    snapshot = {key: product.get(key) for key in ('id', 'entityVersion', 'attributeText', 'datasetRevision', 'source', 'dataNature')}
    sha = hashlib.sha256(json.dumps(snapshot, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    citations = [{'id': f"product:{expected_item_id}:{sha[:16]}:{doc.doc_id}", 'kind': 'product', 'title': '当前目录商品资料原文', 'text': doc.text,
                  'itemId': str(expected_item_id), 'entityVersion': product.get('entityVersion'), 'datasetRevision': product.get('datasetRevision'),
                  'source': product.get('source'), 'dataNature': product.get('dataNature'), 'snapshotSha256': sha,
                  'contentSha256': hashlib.sha256(doc.text.encode()).hexdigest(), 'score': score}
                 for score, doc in ranked[:limit] if score > 0]
    return {'status': 'FOUND' if citations else 'NO_EVIDENCE', 'citations': citations}
