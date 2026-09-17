"""Source-byte, identity, grouping and incomplete-answer boundaries; no paid calls."""
import asyncio
import hashlib
import json
import sqlite3
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.catalog_data import (EvidenceStore, available, decode_record, digest,
    enrich_catalog_detail, plan_search, presentation_groups, quality_flags, use_catalog_metadata)
from app.catalog_evidence import CatalogBinding, search_catalog_evidence_tool, use_catalog_evidence_provider
from app.catalog_evidence_agent import run_catalog_evidence_agent
from app.settings import settings


@pytest.fixture
def store(tmp_path):
    source = tmp_path / 'items.jsonl'
    originals = [dict(item_id=i, item_title='同标题商品', brand_name='无品牌', seller_name=seller,
                      category_level1_name='女装', category_level2_name='连衣裙', category_level3_name='UNKNOWN')
                 for i, seller in [(1, '甲店'), (2, '乙店')]]
    blobs = [(json.dumps(x, ensure_ascii=False)+'\n').encode() for x in originals]
    source.write_bytes(b''.join(blobs))
    db = sqlite3.connect(tmp_path/'metadata.sqlite')
    db.execute('CREATE TABLE records(docid TEXT,source TEXT,source_line INT,byte_offset INT,byte_length INT,record_sha256 TEXT)')
    db.execute('CREATE TABLE legacy_links(legacy_docid TEXT,status TEXT,candidates TEXT)')
    offset = 0
    for i, blob in enumerate(blobs, 1):
        db.execute('INSERT INTO records VALUES(?,?,?,?,?,?)', (f'kuaisearch:{i}', 'kuaisearch', i, offset, len(blob), hashlib.sha256(blob).hexdigest()))
        offset += len(blob)
    db.execute('INSERT INTO legacy_links VALUES(?,?,?)', ('ksd-legacy', 'ambiguous_tuple_candidates', '["kuaisearch:1","kuaisearch:2"]'))
    db.commit(); db.close()
    manifest = {'version':'catalog-data-repair-v1','status':'COMPLETE',
        'sources':{'kuaisearch':{'path':str(source)}},
        'artifacts':{'metadata.sqlite':{'sha256':digest(tmp_path/'metadata.sqlite')}}}
    (tmp_path/'MANIFEST.json').write_text(json.dumps(manifest), encoding='utf-8')
    s = EvidenceStore(tmp_path, expected_manifest_sha256=digest(tmp_path/'MANIFEST.json'))
    yield s
    s.close()


def hits(store):
    return [dict(docid=f'kuaisearch:{i}', source='kuaisearch', rank=i, score=-float(i),
        text=store.record(f'kuaisearch:{i}')['retrieval_text'], provenance={'test':True}, unknown=[]) for i in (1,2)]


def test_raw_seller_recovered_without_retrieval_text_change(store):
    values = hits(store); detail = store.describe_hit(values[0])
    assert detail['fields']['seller']['value'] == '甲店'
    assert '甲店' not in values[0]['text']
    assert detail['fields']['brand']['state'] == 'unknown'
    assert detail['fields']['inventory']['value'] is None
    assert detail['commerceAuthority'] is False
    assert detail['raw']['category_level3_name']=='UNKNOWN'


def test_changed_raw_bytes_fail_even_if_json_still_parses(store):
    source=Path(store.manifest['sources']['kuaisearch']['path'])
    source.write_bytes(source.read_bytes().replace('甲店'.encode(),'丙店'.encode()))
    with pytest.raises(ValueError,match='bytes changed'):store.record('kuaisearch:1')


@pytest.mark.parametrize('field,value',[('text','另一个商品'),('source','multicpr'),('docid','multicpr:1')])
def test_foreign_hit_cannot_receive_metadata(store,field,value):
    hit=hits(store)[0];hit[field]=value
    with pytest.raises(ValueError):store.describe_hit(hit)


def test_groups_preserve_every_identity_rank_score_and_seller(store):
    original=hits(store);before=deepcopy(original)
    metadata=[store.describe_hit(x) for x in original]
    groups=presentation_groups(original,metadata)
    assert len(groups)==1 and original==before
    assert [m['docid'] for m in groups[0]['members']]==['kuaisearch:1','kuaisearch:2']
    assert [m['originalScore'] for m in groups[0]['members']]==[-1.,-2.]
    assert groups[0]['identityMeaning']=='same_title_not_same_sku'
    assert [m['fields']['seller']['value'] for m in metadata]==['甲店','乙店']


def test_ambiguous_legacy_link_is_never_attribute_authority(store):
    result=store.legacy_link('ksd-legacy')
    assert len(result['candidateDocids'])==2 and not result['attributeJoinAllowed']


def test_context_restores_after_exception(store):
    detail={'hits':hits(store)}
    with pytest.raises(RuntimeError):
        with use_catalog_metadata(store):
            assert 'presentationGroups' in enrich_catalog_detail(detail)
            raise RuntimeError()
    assert enrich_catalog_detail(detail) is detail


@pytest.mark.parametrize('value',['无品牌','其他/other','UNKNOWN','',None])
def test_placeholder_not_a_real_brand(value):
    assert not available(value)


def test_multicpr_and_quality_flags_do_not_invent_or_delete():
    record=decode_record('multicpr','434\t5号充电电池\n'.encode())
    assert record['brand']=='' and record['seller']=='' and record['categories']==[]
    assert quality_flags(record)==['brand_unavailable']
    record=decode_record('multicpr','2209\t补：邮费 运费,补差价专用链接。\n'.encode())
    assert 'nonstandard_listing_candidate' in quality_flags(record)
    assert record['docid']=='multicpr:2209' and record['retrieval_text']==record['title']


def test_general_sources_not_selected_by_category_and_phone_keeps_checks():
    constraints=[dict(field='price',operator='lte',value=2000)]
    plan=plan_search('general',constraints)
    assert plan['sources']==['kuaisearch','multicpr'] and not plan['autoExecute']
    plan=plan_search('used_phone',constraints)
    assert plan['route']=='existing_phone_search' and plan['rerankerOverride'] is None
    assert plan_search('unknown',[])['route']=='clarify_domain'


def response(stage,reason,content='部分回答'):
    call=SimpleNamespace(id='call1',function=SimpleNamespace(name='search_catalog_evidence',arguments='{"query":"测试","source":"kuaisearch","limit":10}'))
    return SimpleNamespace(id=stage,usage=None,choices=[SimpleNamespace(finish_reason=reason,
        message=SimpleNamespace(content=content if stage=='final' else None,tool_calls=None if stage=='final' else [call]))])


@pytest.fixture
def enabled(monkeypatch):
    binding=CatalogBinding(dataRoot='F:/agent',runId='repair-test',manifestSha256='a'*64)
    for k,val in dict(catalog_evidence_enabled=True,catalog_evidence_data_root=binding.data_root,
        catalog_evidence_run_id=binding.run_id,catalog_evidence_manifest_sha256=binding.manifest_sha256,
        catalog_evidence_source='kuaisearch').items():monkeypatch.setattr(settings,k,val)
    return binding


def test_metadata_is_wired_through_actual_tool_path(store,enabled):
    original=hits(store)
    async def provider(request):return {'binding':enabled.model_dump(by_alias=True),'query':request.query,'source':request.source,'hits':original}
    with use_catalog_metadata(store),use_catalog_evidence_provider(enabled,provider):
        trace=asyncio.run(search_catalog_evidence_tool('测试','kuaisearch'))
    assert trace.ok and len(trace.detail['presentationGroups'])==1
    assert [(x['docid'],x['rank'],x['score'],x['text']) for x in trace.detail['hits']]==[(x['docid'],x['rank'],x['score'],x['text']) for x in original]


@pytest.mark.parametrize('reason',['length','content_filter',None,'tool_calls'])
def test_nonempty_unfinished_answer_is_not_success(enabled,reason):
    create=AsyncMock(side_effect=[response('selection','tool_calls'),response('final',reason)])
    client=SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    from app.schemas import ToolTrace
    caller=AsyncMock(return_value=ToolTrace(tool='search_catalog_evidence',ok=True,detail={'hits':[]}))
    answer,traces,*_=asyncio.run(run_catalog_evidence_agent('测试',client=client,tool_caller=caller))
    assert not traces[-1].ok and traces[-1].detail['code']=='catalog_answer_incomplete'
    assert traces[-1].detail['modelCalls'][-1]['finishReason']==reason
    assert '部分回答' in answer and '未正常完成' in answer
    assert create.await_count==2


def test_incomplete_selection_does_not_call_search(enabled):
    create=AsyncMock(return_value=response('selection','length'));caller=AsyncMock()
    client=SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    _,traces,*_=asyncio.run(run_catalog_evidence_agent('测试',client=client,tool_caller=caller))
    assert not traces[-1].ok and caller.await_count==0
