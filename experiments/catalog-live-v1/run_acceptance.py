"""New live smoke evidence; never reload saved rankings or claim qrel metrics."""
import asyncio
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import re
import sys
import time

from live_bridge import LiveBridge, experiment_settings, ROOT
from app.catalog_evidence import CatalogBinding, use_catalog_evidence_provider
from app.catalog_data import EvidenceStore, use_catalog_metadata
from app.settings import settings

OUT = Path('D:/agent-datasets/catalog-live-acceptance-20260913-v1')
MODEL = 'D:/agent-datasets/search-closure-v1/training-preparation/runs/pairwise-lora-v1/checkpoints/epoch-2'
METADATA = Path('D:/agent-datasets/integration-repair-20260913-v1/metadata')
CASES = [
    {'id': 'K1', 'source': 'kuaisearch', 'query': '抽屉式透明桌面收纳盒', 'check': '商品主体与收纳需求，不虚构材质尺寸'},
    {'id': 'M1', 'source': 'multicpr', 'query': '抽屉式透明桌面收纳盒', 'check': '同查询另一个完整来源，不假定跨源分数可比'},
    {'id': 'K2', 'source': 'kuaisearch', 'query': '200元以内的可折叠笔记本电脑支架', 'check': '价格未知，不承诺满足预算'},
    {'id': 'M2', 'source': 'multicpr', 'query': '200元以内的可折叠笔记本电脑支架', 'check': '缺失结构化属性披露，价格文本不等于已核实价格'},
]


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def write(name, value):
    path = OUT / name
    with path.open('x', encoding='utf-8') as f:
        json.dump(value, f, ensure_ascii=False, indent=2, allow_nan=False)
        f.write('\n')


def checks(answer, traces, runtime_results):
    controller = [t for t in traces if t['tool'] == 'catalog_evidence_controller']
    tools = [t for t in traces if t['tool'] == 'search_catalog_evidence']
    detail = tools[0]['detail'] if tools else {}
    hits = detail.get('hits', [])
    ids = {h['docid'] for h in hits}
    citations = set(re.findall(r'(?:kuaisearch|multicpr):[0-9]+', answer))
    raw_hits = runtime_results[0]['result']['hits'] if len(runtime_results) == 1 else []
    return {
        'controller_complete': bool(controller and controller[-1]['ok']),
        'two_model_calls': bool(controller and controller[-1]['detail']['modelCallCount'] == 2),
        'one_real_retrieval': len(runtime_results) == 1,
        'raw_hits_preserved': bool(hits) and [(h['docid'], h['rank'], h['score'], h['text']) for h in hits] ==
            [(h['docid'], h['rank'], h['score'], h['text']) for h in raw_hits],
        'metadata_aligned': bool(hits) and [m['docid'] for m in detail.get('metadata', [])] == [h['docid'] for h in hits],
        'citations_present_and_valid': bool(citations) and citations <= ids,
        'no_commerce_authority': detail.get('commerceAuthority') is False,
        'unknown_price_retained': bool(hits) and all('verified_price' in h['unknown'] for h in hits),
        'semantic_review_required': True,
    }


async def main():
    from app.llm import run_agent
    OUT.mkdir(parents=True, exist_ok=False)
    before = deepcopy(settings.model_dump())
    registry = json.loads((ROOT/'datasets/registry.json').read_text(encoding='utf-8'))
    metadata_sha = registry['externalSearchCorpora']['metadataManifestSha256']
    contract = {'status': 'FROZEN_BEFORE_LIVE_RUN', 'cases': CASES, 'profile': 'w211', 'model': MODEL,
        'metadata_manifest_sha256': metadata_sha, 'max_tokens': 2048, 'retrieval_timeout_seconds': 240,
        'agent_timeout_seconds': 330, 'llm_model': settings.deepseek_model, 'llm_endpoint': settings.deepseek_base_url,
        'new_training': False, 'rank_replay': False, 'max_primary_model_calls': 8,
        'limits': '4 authored integration smoke cases, no qrels, no unseen-test or generalization claim',
        'code': {str(p): digest(p) for p in [Path(__file__), Path(__file__).with_name('live_bridge.py'),
            ROOT/'agent/app/catalog_data.py', ROOT/'agent/app/catalog_evidence.py',
            ROOT/'agent/app/catalog_evidence_agent.py', ROOT/'agent/app/llm.py',
            ROOT/'experiments/search-closure-v1/retrieval_runtime.py']}}
    write('CONTRACT.json', contract)
    if not settings.deepseek_api_key:
        write('BLOCKED.json', {'reason': 'credential_not_configured'})
        return
    bridge = None
    store = None
    results = []
    try:
        store = EvidenceStore(METADATA, expected_manifest_sha256=metadata_sha)
        bridge = LiveBridge(MODEL)
        print('Verifying corpus, indexes and models in worker', flush=True)
        strategy = await bridge.start()
        write('STRATEGY.json', strategy)
        write('BINDING.json', {'strategy_sha256': digest(OUT/'STRATEGY.json'), 'contract_sha256': digest(OUT/'CONTRACT.json')})
        binding = CatalogBinding(dataRoot=str(strategy['dataRoot']), runId=OUT.name,
                                 manifestSha256=digest(OUT/'STRATEGY.json'))
        print('Verified live runtime ready', flush=True)
        for case in CASES:
            raw = []
            def observe(request, result, seconds):
                record = {'request': request.model_dump(by_alias=True), 'result': result,
                          'seconds': seconds, 'modelCalled': True, 'rankReplay': False}
                raw.append(record)
                write(case['id']+'-retrieval.json', record)
            started = time.perf_counter()
            print('Starting '+case['id'], flush=True)
            try:
                with experiment_settings(binding, case['source']), use_catalog_metadata(store), \
                        use_catalog_evidence_provider(binding, bridge.provider(binding, on_result=observe)):
                    answer, traces, turns, _, _ = await run_agent(case['query'], domain_hint='catalog_evidence')
                trace_rows = [t.model_dump() for t in traces]
                record = {'case': case, 'answer': answer, 'traces': trace_rows, 'turns': turns,
                          'seconds': time.perf_counter()-started, 'checks': checks(answer, trace_rows, raw)}
                write(case['id']+'-agent.json', record)
                results.append({'id': case['id'], 'seconds': record['seconds'], 'checks': record['checks']})
                print(case['id']+' '+json.dumps(record['checks']), flush=True)
            except Exception as exc:
                record = {'id': case['id'], 'errorType': type(exc).__name__, 'seconds': time.perf_counter()-started}
                write(case['id']+'-failure.json', record)
                results.append(record)
            if bridge.closed:
                break
    except Exception as exc:
        write('BLOCKED.json', {'errorType': type(exc).__name__, 'completed_cases': len(results)})
        raise
    finally:
        if bridge:
            bridge.close()
        if store:
            store.close()
        assert settings.model_dump() == before, 'settings leaked'
        write('RUN-RESULTS.json', {'results': results, 'settings_restored': True,
            'worker_stopped': bridge is None or not bridge.process.is_alive(), 'production_activation': False,
            'status': 'LIVE_SMOKE_REQUIRES_SEMANTIC_REVIEW' if len(results)==len(CASES) else 'INCOMPLETE'})


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, default=OUT)
    args = parser.parse_args()
    OUT = args.output.resolve()
    asyncio.run(main())
