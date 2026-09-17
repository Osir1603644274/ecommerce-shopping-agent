"""One serialized GPU worker; immutable evidence, no per-request settings edits."""
import asyncio
from collections import OrderedDict
import hashlib
import json
from pathlib import Path
import time

from .catalog_data import EvidenceStore
from .catalog_evidence import CatalogBinding, CatalogSearchRequest, CatalogSearchResult
from .catalog_worker import LiveBridge, ROOT
from .settings import settings


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def verify_scope(scope):
    if scope is not None:
        body = {k: v for k, v in scope.items() if k!='scopeId'}
        if scope.get('scopeId')!=fingerprint(body) or scope.get('commerceAuthority') is not False:
            raise ValueError('document_scope_integrity_failed')


def workflow_code_binding():
    from .catalog_fast_selection import selection_file
    directory = Path(__file__).parent
    paths = ['catalog_service.py', 'catalog_requirements.py', 'catalog_worker.py', 'catalog_conversation.py', 'catalog_model_client.py', 'api/catalog_workspace.py', 'api/workspace_answer_stream.py', 'catalog_fast_retrieval.py', 'catalog_fast_retrieval_v2.py', 'catalog_fast_retrieval_v3.py', 'catalog_fast_selection.py']
    configuration = selection_file(settings.catalog_workspace_fast_index_dir,settings.catalog_workspace_fast_index_version)
    paths += ['product_followup.py', 'api/commerce_controls.py', 'catalog_execution_view.py']
    paths += ['catalog_commerce.py', 'catalog_remote.py', 'catalog_search_server.py']
    paths += ['catalog_react.py', 'control/react_context.py', 'control/react_decision.py']
    return fingerprint({'code': {p: hashlib.sha256((directory/p).read_bytes()).hexdigest() for p in paths},
        'fastEnabled': settings.catalog_workspace_fast_enabled,
        'reuseModelClient': settings.catalog_workspace_reuse_model_client,
        'externalCommerceEnabled': settings.commerce_workspace_external_catalog_enabled,
        'searchServiceUrl': settings.catalog_search_service_url,
        'fastConfiguration': hashlib.sha256(configuration.read_bytes()).hexdigest() if settings.catalog_workspace_fast_enabled else None,
        'modelPath': settings.catalog_workspace_model_path})


def document_scope(query, sources, binding_sha, requirements=None):
    """Alternate source-local ranks. This is a presentation policy, not LTR."""
    by_source = {s['source']: s for s in sources}
    if set(by_source) != {'kuaisearch', 'multicpr'}:
        raise ValueError('both_sources_required')
    groups = OrderedDict()
    for rank in range(10):
        for source in ('kuaisearch', 'multicpr'):
            row = by_source[source]
            if len(row['hits']) != len(row['metadata']):
                raise ValueError('metadata_alignment')
            if rank >= len(row['hits']):
                continue
            hit, meta = row['hits'][rank], row['metadata'][rank]
            if hit['docid'] != meta['docid'] or hit['source'] != source or hit['rank'] != rank+1:
                raise ValueError('source_rank_identity')
            key = (source, hit['docid'])
            member = {'docid': hit['docid'], 'source': source, 'originalRank': hit['rank'],
                'originalScore': hit['score'], 'brand': meta['fields']['brand']['value'],
                'seller': meta['fields']['seller']['value'], 'recordSha256': meta['provenance']['recordSha256']}
            if key not in groups or member['originalScore'] > groups[key]['members'][0]['originalScore']:
                groups[key] = {'title': meta['fields']['title']['value'], 'members': [member]}
    requirements=requirements or []
    from .catalog_requirements import select_groups
    eligible,excluded=select_groups(list(groups.values()),requirements)
    # Review the already retrieved source Top10 pools before presentation is
    # truncated; a valid item must not disappear behind six false positives.
    displayed = [dict(g, number=i) for i, g in enumerate(eligible, 1)]
    value = {'query': query, 'groups': displayed, 'sources': sources, 'bindingSha256': binding_sha,
             'presentationPolicy': 'record_identity_constraint_evidence_then_source_rank_v3', 'commerceAuthority': False,
             'requirements':requirements,'excludedGroups':excluded,
             'presentationLimit':6,
             'constraintPolicy':'explicit_conflict_excluded_missing_evidence_unknown_v1'}
    return dict(value, scopeId=fingerprint(value))


class CatalogService:
    def __init__(self):
        self.bridge = None
        self.store = None
        self.binding = None
        self.queue = asyncio.Lock()

    async def start(self):
        if self.bridge is not None and not self.bridge.closed:
            return
        self.close()
        registry = json.loads((ROOT/'datasets/registry.json').read_text(encoding='utf-8'))
        expected = registry['externalSearchCorpora']['metadataManifestSha256']
        self.store = EvidenceStore(Path(settings.catalog_workspace_metadata_path), expected_manifest_sha256=expected)
        self.bridge = LiveBridge(settings.catalog_workspace_model_path)
        try:
            strategy = await self.bridge.start()
            self.binding = CatalogBinding(dataRoot=str(strategy['dataRoot']), runId='catalog-workspace-v1',
                manifestSha256=fingerprint({'strategy': strategy, 'metadata': expected,
                    'workerSha256': hashlib.sha256(Path(__file__).with_name('catalog_worker.py').read_bytes()).hexdigest()}))
        except BaseException:
            self.close()
            raise

    async def search(self, query, *, requirements=None, retrieval_query=None):
        # A waiting request may time out without cancelling the active owner's
        # worker. Cancellation inside provider terminates its active worker.
        async with asyncio.timeout(settings.catalog_workspace_timeout_seconds):
            async with self.queue:
                await self.start()
                retrieval_query=retrieval_query or query
                results = []
                for source in ('kuaisearch', 'multicpr'):
                    timings = []
                    started = time.perf_counter()
                    provider = self.bridge.provider(self.binding, timeout=settings.catalog_workspace_timeout_seconds,
                        on_result=lambda request, result, seconds: timings.append(result['timings']))
                    raw = await provider(CatalogSearchRequest(query=retrieval_query, source=source, limit=10, binding=self.binding))
                    result = CatalogSearchResult.model_validate(raw)
                    if result.binding != self.binding or result.source != source or result.query != retrieval_query:
                        raise ValueError('retrieval_response_mismatch')
                    hits = [h.model_dump() for h in result.hits]
                    if len(hits)>10 or len({h['docid'] for h in hits}) != len(hits):
                        raise ValueError('retrieval_count_or_duplicate')
                    metadata = [self.store.describe_hit(h) for h in hits]
                    results.append({'source': source, 'hits': hits, 'metadata': metadata,
                                    'timings': timings, 'seconds': time.perf_counter()-started})
                return document_scope(query, results, self.binding.manifest_sha256,requirements)

    def close(self):
        if self.bridge:
            self.bridge.close()
        if self.store:
            self.store.close()
        self.bridge = self.store = None
        self.binding = None


_service = None


def get_catalog_service():
    global _service
    if _service is None:
        if settings.catalog_search_service_url:
            from .catalog_remote import RemoteCatalogService
            _service = RemoteCatalogService()
        else:
            _service = CatalogService()
    return _service
