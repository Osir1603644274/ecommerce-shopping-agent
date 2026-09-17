"""Versioned public-corpus evidence. Never grants commerce or attribute authority.

The sidecar points into preserved source bytes; retrieval text and scoring stay
unchanged. Register a store in an isolated experiment using use_catalog_metadata.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections import OrderedDict
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Any, Iterator

SOURCES = ('kuaisearch', 'multicpr')
PLACEHOLDERS = {'', 'UNKNOWN', 'NULL', 'NONE', 'N/A', '无品牌', '其他', '其它', '其他/OTHER', '其它/OTHER', '缺失'}
COMMERCE_UNKNOWN = ('verified_price', 'currency_unit', 'inventory', 'purchase_availability', 'sku')


def digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open('rb') as f:
        for block in iter(lambda: f.read(8 * 1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def clean(value: Any) -> str:
    text = ' '.join(str(value or '').split())
    return '' if text.upper() in {'UNKNOWN', 'NULL', 'NONE', 'N/A'} else text


def available(value: Any) -> bool:
    return clean(value).upper() not in PLACEHOLDERS


def decode_record(source: str, line: bytes) -> dict:
    text = line.decode('utf-8-sig').rstrip('\r\n')
    if source == 'kuaisearch':
        raw = json.loads(text)
        docid = 'kuaisearch:' + str(raw['item_id'])
        title, brand = clean(raw['item_title']), clean(raw.get('brand_name'))
        categories = [clean(raw.get(f'category_level{i}_name')) for i in (1, 2, 3)]
        seller = clean(raw.get('seller_name'))
    elif source == 'multicpr':
        native, title = text.split('\t', 1)
        raw = {'id': native, 'title': title}
        docid, title, brand, categories, seller = 'multicpr:' + native, clean(title), '', [], ''
    else:
        raise ValueError('unsupported source')
    retrieval_text = ' '.join(dict.fromkeys(v for v in (title, brand, *categories) if v))
    return dict(docid=docid, source=source, title=title, brand=brand, seller=seller,
                categories=categories, retrieval_text=retrieval_text, raw=raw)


def quality_flags(record: dict) -> list[str]:
    title = record['title']
    flags = []
    if len(title) <= 4:
        flags.append('short_title_requires_context')
    if '\ufffd' in title:
        flags.append('source_replacement_character')
    if re.search(r'https?://|www\.', title, re.I):
        flags.append('url_fragment_present')
    if re.search(r'多少钱拍多少|补差价|运费补|专用链接|测试链接', title):
        flags.append('nonstandard_listing_candidate')
    if not available(record['brand']):
        flags.append('brand_unavailable')
    # A basketball "7号" or a battery "5号" is not an address defect.
    return flags


def title_key(title: str) -> str:
    # Same whitespace cleanup as existing retrieval, no semantic/SKU merging.
    return hashlib.sha256(clean(title).encode()).hexdigest()


def field_evidence(value: Any, *, present: bool = True) -> dict:
    known = any(available(x) for x in value) if isinstance(value, list) else available(value)
    return {'value': value if known and present else None,
            'state': 'source_claim' if known and present else 'unknown',
            'basis': 'source_record' if present else 'field_absent'}


class EvidenceStore:
    """Read-only sidecar plus record-level source byte verification.

    Manifest and sidecar hashes are checked once on open; each record read checks
    its original bytes. No import-time scan, global cache or implicit rebuild.
    """
    def __init__(self, directory: str | Path, *, expected_manifest_sha256: str):
        self.root = Path(directory).resolve()
        manifest = self.root / 'MANIFEST.json'
        if digest(manifest) != expected_manifest_sha256:
            raise ValueError('metadata manifest binding mismatch')
        self.manifest = json.loads(manifest.read_text(encoding='utf-8'))
        if self.manifest.get('status') != 'COMPLETE' or self.manifest.get('version') != 'catalog-data-repair-v1':
            raise ValueError('metadata store incomplete or unsupported')
        sidecar = self.root / 'metadata.sqlite'
        if digest(sidecar) != self.manifest['artifacts']['metadata.sqlite']['sha256']:
            raise ValueError('metadata sidecar checksum mismatch')
        self.db = sqlite3.connect(sidecar.as_uri() + '?mode=ro', uri=True)
        self.db.execute('PRAGMA query_only=ON')
        self.manifest_sha256 = expected_manifest_sha256

    def close(self) -> None:
        self.db.close()

    def record(self, docid: str) -> dict:
        row = self.db.execute('SELECT source,source_line,byte_offset,byte_length,record_sha256 FROM records WHERE docid=?', (docid,)).fetchone()
        if row is None:
            raise ValueError('document not in bound metadata store')
        source, line, offset, length, expected = row
        path = Path(self.manifest['sources'][source]['path'])
        with path.open('rb') as f:
            f.seek(offset)
            raw = f.read(length)
        if hashlib.sha256(raw).hexdigest() != expected:
            raise ValueError('source record bytes changed')
        record = decode_record(source, raw)
        if record['docid'] != docid:
            raise ValueError('source record identity mismatch')
        record['provenance'] = {'file': str(path), 'sourceLine': line, 'byteOffset': offset,
                                'byteLength': length, 'recordSha256': expected,
                                'metadataManifestSha256': self.manifest_sha256}
        return record

    def describe_hit(self, hit: dict) -> dict:
        record = self.record(hit['docid'])
        if record['source'] != hit['source'] or record['retrieval_text'] != hit['text']:
            raise ValueError('retriever text/source differs from metadata evidence')
        fields = {name: field_evidence(record[name], present=record['source'] == 'kuaisearch')
                  for name in ('brand', 'seller', 'categories')}
        fields['title'] = field_evidence(record['title'])
        for name in COMMERCE_UNKNOWN:
            fields[name] = {'value': None, 'state': 'unknown', 'basis': 'no_verified_commerce_record'}
        return {'docid': record['docid'], 'source': record['source'], 'raw': record['raw'],
                'fields': fields, 'qualityFlags': quality_flags(record),
                'titleGroupKey': title_key(record['title']), 'provenance': record['provenance'],
                'commerceAuthority': False}

    def legacy_link(self, docid: str) -> dict:
        row = self.db.execute('SELECT status,candidates FROM legacy_links WHERE legacy_docid=?', (docid,)).fetchone()
        if row is None:
            raise ValueError('unknown legacy document')
        return {'legacyDocid': docid, 'state': row[0], 'candidateDocids': json.loads(row[1]),
                'attributeJoinAllowed': False}


def presentation_groups(hits: list[dict], metadata: list[dict]) -> list[dict]:
    if len(hits) != len(metadata) or any(h['docid'] != m['docid'] for h, m in zip(hits, metadata)):
        raise ValueError('metadata/hit alignment mismatch')
    groups: OrderedDict[str, dict] = OrderedDict()
    for hit, info in zip(hits, metadata):
        key = info['titleGroupKey']
        group = groups.setdefault(key, {'title': info['fields']['title']['value'],
            'representativeDocid': hit['docid'], 'members': [],
            'identityMeaning': 'same_title_not_same_sku', 'commerceAuthority': False})
        group['members'].append({'docid': hit['docid'], 'source': hit['source'],
            'originalRank': hit['rank'], 'originalScore': hit['score']})
    return list(groups.values())


def plan_search(domain: str, constraints: list[dict]) -> dict:
    """Policy input is an explicit upstream intent, not guessed dataset routing.

    Both general sources cover many categories. A missing public field is never
    proof of matching or failing a user constraint. Phone checks stay delegated
    to the existing commerce fact/constraint mechanism.
    """
    if domain not in {'used_phone', 'general', 'unknown'}:
        raise ValueError('unsupported domain')
    required = {'field', 'operator', 'value'}
    if any(not isinstance(c, dict) or not required <= c.keys() for c in constraints):
        raise ValueError('invalid explicit constraint')
    if domain == 'unknown':
        return {'route': 'clarify_domain', 'sources': [], 'constraints': constraints, 'autoExecute': False}
    if domain == 'used_phone':
        return {'route': 'existing_phone_search', 'sources': ['commerce_catalog'],
                'constraints': constraints, 'constraintPolicy': 'existing_authoritative_three_state_checks',
                'rerankerOverride': None, 'autoExecute': False}
    return {'route': 'public_catalog_search', 'sources': list(SOURCES), 'constraints': constraints,
            'constraintPolicy': 'source_claims_and_unknowns_not_commerce_filters',
            'retrieval': 'source_local_w211_then_selected_ce',
            'crossSourceFusion': 'requires_separate_evaluation', 'autoExecute': False}


_store: ContextVar[EvidenceStore | None] = ContextVar('catalog_metadata_store', default=None)


@contextmanager
def use_catalog_metadata(store: EvidenceStore) -> Iterator[None]:
    token = _store.set(store)
    try:
        yield
    finally:
        _store.reset(token)


def enrich_catalog_detail(detail: dict) -> dict:
    store = _store.get()
    if store is None:
        return detail
    # A broken metadata binding must fail, not silently produce false authority.
    metadata = [store.describe_hit(hit) for hit in detail['hits']]
    return {**detail, 'metadataVersion': 'catalog-data-repair-v1', 'metadata': metadata,
            'presentationGroups': presentation_groups(detail['hits'], metadata),
            'presentationPolicy': 'group_same_title_keep_all_ids_ranks_and_scores',
            'metadataManifestSha256': store.manifest_sha256}
