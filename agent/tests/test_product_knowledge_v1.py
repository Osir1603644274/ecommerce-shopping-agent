from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.product_knowledge.store import KnowledgeStore
from app.product_knowledge.comparison import prepare_comparison, validate_knowledge
from app.product_knowledge.answer import render
from app.settings import settings

ROOT=Path(__file__).resolve().parents[2]
DATA=ROOT/'datasets/knowledge/phone-v1'

@pytest.fixture
def anyio_backend(): return 'asyncio'

def test_snapshot_counts_and_no_false_binding():
    store=KnowledgeStore(DATA)
    assert len(store.listings)==439
    assert set(store.listings)=={r['itemId'] for r in store.listings.values()}
    result=store.search('拍照',item_ids=['8542161000270987328'])
    assert not result['evidence']
    assert any(g['reason']=='MODEL_CONFLICT' for g in result['gaps'])
    assert not store.search('续航',item_ids=['9999999999999999999'])['evidence']

def test_scoped_structured_query_and_missing_are_explicit():
    store=KnowledgeStore(DATA)
    result=store.search('iPhone13芯片',model_keys=['apple:13'],fields=['chip'])
    assert len(result['evidence'])==1 and result['evidence'][0]['value']=='A15，6核CPU、4核GPU'
    assert store.search('iPhone13芯片')['modelKeys']==['apple:13']
    assert not store.search('游戏',model_keys=['oppo:a11'])['evidence']
    assert any(g['reason']=='NO_VERIFIED_FACTS' for g in store.search('游戏',model_keys=['oppo:a11'])['gaps'])

def test_snapshot_corruption_rejected(tmp_path):
    import shutil
    shutil.copytree(DATA,tmp_path/'snapshot')
    with (tmp_path/'snapshot/facts.jsonl').open('ab') as f: f.write(b'\n')
    with pytest.raises(ValueError,match='hash_mismatch'): KnowledgeStore(tmp_path/'snapshot')

@pytest.mark.parametrize('raw',['未知宣传词,Android/安卓,无划痕','iOS,Android/安卓,原装屏,非原装内屏/外屏','没有安卓,没有原装屏','Android/安卓,安卓,原装屏'])
def test_attribute_projection_preserves_known_conflict_unknown_and_negation(raw):
    from app.product_knowledge.comparison import product_check_projection
    from app.domains.ecommerce.models import _attribute_value
    from app.domains.ecommerce.used_phone_attributes import materialize_used_phone_product_attributes
    product=dict(id=9007199254740993123,title='test',attributes=list(materialize_used_phone_product_attributes(raw)))
    projected=product_check_projection(product,[dict(key='os'),dict(key='screen_originality')])
    for key in ('os','screen_originality'):
        assert _attribute_value(product,key)[0]==_attribute_value(projected,key)[0]
    assert product['attributes']==list(materialize_used_phone_product_attributes(raw))

def test_shared_proof_roundtrip_and_reject_forward_reference():
    from app.product_knowledge.projection import pack_proof,unpack_proof
    value={'products':[{'id':i,'title':'重复但不是可以丢弃的证据'*10,'source':{'url':'https://example.com/source','section':'参数','date':'2026-09-09'}} for i in range(20)]}
    packed=pack_proof(value)
    assert unpack_proof(packed)==value
    with pytest.raises(ValueError,match='invalid_shared_reference'):
        unpack_proof({'proofFormat':packed['proofFormat'],'root':{'$shared':0},'shared':[{'$shared':0}]})


def test_durable_runtime_publishes_same_request_policy_for_both_views(monkeypatch):
    from app.graph.runtime import GraphV2Runtime
    monkeypatch.setattr(settings,'product_knowledge_enabled',True)
    policies={'existing':42}
    runtime=GraphV2Runtime(user_message='不打游戏，只看拍照',client=None,model='test',
        resolve_tool_schemas=None,tool_caller=None,trace_builder=None,projector=None,max_transitions=8,
        system_policies=policies)
    assert runtime.system_policies=={'existing':42,'productKnowledgeUserQuery':'不打游戏，只看拍照'}
    assert policies=={'existing':42}


def test_v2_full_audit_suffix_corrections_and_snapshot_integrity():
    store=KnowledgeStore(ROOT/'datasets/knowledge/phone-v2')
    assert len(store.listings)==439 and len(store.models)==171
    assert store.listings['291462']['modelKeys']==['vivo:y35plus']
    assert store.listings['4175990']['modelKeys']==['honor:9x']
    assert not store.search('芯片',item_ids=['7441112016001245431'])['evidence']
    assert len(store.facts)==store.manifest['factCount']
    assert len(read:= [json.loads(x) for x in (store.directory/'investigations.jsonl').read_text(encoding='utf8').splitlines()])==171
    assert all(r['attempts'] for r in read)

def test_query_focus_keeps_both_requested_fields_and_never_swaps_model():
    store=KnowledgeStore(ROOT/'datasets/knowledge/phone-v2')
    value=store.search('iPhone13芯片和相机参数',model_keys=['apple:13','apple:16pro'])
    assert {e['modelKey'] for e in value['evidence']}=={'apple:13'}
    assert {'chip','camera'} <= {e['field'] for e in value['evidence']}
    assert not store.search('iPhone13芯片',model_keys=['apple:16pro'])['evidence']
    assert not store.search('预算1300元')['modelKeys']

def test_followup_keeps_requested_model_and_current_explicit_model_wins():
    store=KnowledgeStore(ROOT/'datasets/knowledge/phone-v2')
    result=store.search('没有实测也不要猜，告诉我缺少什么',item_ids=['1275270'],context_query='OPPO A11原神60帧能稳定吗')
    assert result['requestedModelKeys']==['oppo:a11'] and not result['evidence']
    assert any(g.get('field')=='gaming_test' for g in result['gaps'])
    result=store.search('iPhone13芯片',item_ids=['1275270'],context_query='OPPO A11')
    assert result['requestedModelKeys']==['apple:13'] and result['evidence']


def test_endurance_not_battery_health_and_unmeasured_parameter_is_readable(monkeypatch):
    from app.llm import _explicit_used_phone_requirements,_deterministic_used_phone_task_state_decision,_require_clarification_for_empty_phone_recommendation
    from app.domains.ecommerce.models import ShoppingGuideState
    from tests.test_planner import _ecom_state
    monkeypatch.setattr(settings,'product_knowledge_enabled',True)
    assert 'battery_health' not in _explicit_used_phone_requirements('续航好的优先')
    assert 'battery_health' in _explicit_used_phone_requirements('电池健康80%-90%')
    state=_ecom_state(goal='OPPO A11二手手机，原神60帧稳定吗？')
    state.domain_state.pop('candidateScope',None)
    result,_=_deterministic_used_phone_task_state_decision(state,state.goal)
    assert result['status']=='ready'
    _require_clarification_for_empty_phone_recommendation(state,result,ShoppingGuideState(category='phone',mode='recommend'),message=state.goal)
    assert result['status']=='ready'

def test_no_citation_cannot_publish_model_invented_performance():
    payload=dict(products=[dict(id=1,title='待核验手机')],candidates=[dict(productId=1,decision={'products':[dict(fullyMatched=True,hardFailures=0)]})],knowledge={'status':'INSUFFICIENT_EVIDENCE','evidence':[],'bindings':[]})
    text=render(json.dumps({'recommendations':[{'productId':'1','reason':'按常理旗舰定位拍照更强','evidenceIds':[]}],'unknowns':[]}),payload)
    assert '按常理' not in text and '拍照更强' not in text and '性能待核验' in text
    payload['knowledge']['status']='UNAVAILABLE'
    text=render(json.dumps({'recommendations':[{'productId':'1','reason':'备选','evidenceIds':[]}],'unknowns':[]}),payload)
    assert '型号知识暂不可用' in text
    payload['knowledge']['status']='INSUFFICIENT_EVIDENCE'
    text=render(json.dumps({'recommendations':[{'productId':'1','reason':'备选','evidenceIds':[]}],
        'unknowns':['其余19件均无资料，这是模型编造的覆盖结论']}),payload)
    assert '其余19件' not in text


def test_capacity_ranking_and_cross_listing_region_citation_are_rejected():
    knowledge=KnowledgeStore(DATA).search('续航',item_ids=['1275270'])
    evidence=knowledge['evidence'][0]
    payload=dict(products=[dict(id=1275270,title='苹果13')],candidates=[dict(productId=1275270,decision={'products':[dict(fullyMatched=True,hardFailures=[])]})],knowledge=knowledge)
    draft=dict(recommendations=[dict(productId='1275270',reason='容量最大，续航潜力最大',evidenceIds=[evidence['evidenceId']])],unknowns=[])
    assert '续航潜力最大' not in render(json.dumps(draft),payload)
    knowledge['bindings'][0]['region']='HK'
    evidence['source']['region']='CN'
    draft['recommendations'][0]['reason']='此版本官方参数供参考'
    reasons=[]
    render(json.dumps(draft),payload,on_validation=reasons.append)
    assert 'listing_region_mismatch' in reasons[0]


def test_parent_input_hides_ads_preserves_exact_ids_and_proof_reserved_lists():
    from app.product_knowledge.answer import model_input
    from app.product_knowledge.projection import pack_proof,unpack_proof
    iid='9007199254740993123'
    raw=dict(products=[dict(id=int(iid),title='史上最强游戏机',syntheticReferencePriceMinor=50000)],knowledge=dict(bindings=[dict(itemId=iid,status='CLEAR',modelKeys=['apple:13'])]))
    value=model_input(raw)
    assert value['products'][0]['title']=='apple:13' and value['products'][0]['id']==iid
    assert value['products'][0]['syntheticReferencePriceYuan'].startswith('500.00')
    assert raw['products'][0]['title']=='史上最强游戏机'
    literal=dict(a=['$s',0],b=['$r',0,['literal']])
    assert unpack_proof(pack_proof(literal))==literal

@pytest.mark.anyio
async def test_timeout_keeps_facts_and_no_title_ranking(monkeypatch):
    from app.product_knowledge import comparison
    from app.domains.ecommerce import tools
    from app.schemas import ToolTrace
    products=[dict(id=i,title='游戏拍照宣传词'*i,brand='apple',priceStatus='unknown',attributes=[]) for i in range(1,7)]
    monkeypatch.setattr(tools,'get_product_details_tool',AsyncMock(return_value=ToolTrace(tool='get_product_details',ok=True,detail={'products':products})))
    monkeypatch.setattr(settings,'product_knowledge_enabled',True)
    monkeypatch.setattr(settings,'used_phone_synthetic_price_policy','disabled')
    monkeypatch.setattr(comparison,'search_evidence',AsyncMock(side_effect=TimeoutError))
    trace=await prepare_comparison(list(range(1,7)),'phone',[],'不打游戏，主要拍照','scope-test')
    assert trace.ok,trace.detail
    assert trace.detail['knowledge']['status']=='UNAVAILABLE'
    assert [c['productId'] for c in trace.detail['candidates']]==list(range(1,7))
    assert len(trace.detail['products'])==6

@pytest.mark.anyio
async def test_planner_selects_new_contract_and_preserves_raw_goal(monkeypatch):
    from tests.test_planner import _ecom_state, _fake_client
    from app.planner import build_planner_context,create_plan
    from app.llm import shopping_tool_schemas
    monkeypatch.setattr(settings,'product_knowledge_enabled',True)
    monkeypatch.setattr(settings,'shopping_state_authority','legacy')
    state=_ecom_state(goal='不要苹果；只比较这两款的续航')
    state.domain_state['shoppingGuide'].update(mode='compare',comparedIds=[6055970412849301893,1710694])
    schemas=shopping_tool_schemas()
    assert {s['function']['name'] for s in schemas}=={'search_products','get_product_details','search_product_evidence','compare_products'}
    result=await create_plan(build_planner_context(state,state.goal,schemas),client=_fake_client(AsyncMock()),model='unused')
    assert result.outcome=='planned',result
    step=result.plan.steps[0]
    assert step.expected_output=={'requiresEvidenceComparison':True}
    assert step.arguments['userQuery']==state.goal
    assert step.arguments['productIds']==[6055970412849301893,1710694]

def test_foreign_evidence_and_unavailable_facts_rejected():
    value=KnowledgeStore(DATA).search('芯片',item_ids=['1275270'])
    value['transport']='MCP_STREAMABLE_HTTP'
    validate_knowledge(value,[1275270])
    poisoned=deepcopy(value);poisoned['evidence'][0]['modelKey']='oppo:a96'
    with pytest.raises(ValueError):validate_knowledge(poisoned,[1275270])
    poisoned=deepcopy(value);poisoned['status']='UNAVAILABLE'
    with pytest.raises(ValueError):validate_knowledge(poisoned,[1275270])

def test_render_rejects_fabricated_id_and_citation():
    store=KnowledgeStore(DATA);knowledge=store.search('续航',item_ids=['1275270'])
    payload=dict(products=[dict(id=1275270,title='苹果13')],candidates=[dict(productId=1275270,decision={'products':[dict(fullyMatched=True,hardFailures=[])]})],knowledge=knowledge)
    for iid,refs in [('9007199254740993123',[]),('1275270',['pk:invented'])]:
        answer=render(json.dumps(dict(recommendations=[dict(productId=iid,reason='更好',evidenceIds=refs)],unknowns=[])),payload)
        assert '证据不足' in answer


def test_answer_and_cards_use_same_displayed_products():
    from app.product_knowledge.answer import model_input, fallback
    payload=dict(products=[dict(id=i,title='phone-'+str(i)) for i in (1,2)], sourceDisplayIds=[2],
        candidates=[dict(productId=i,decision={'products':[dict(fullyMatched=True,hardFailures=[])]}) for i in (1,2)],
        knowledge=dict(bindings=[],evidence=[]))
    assert [p['id'] for p in model_input(payload)['products']] == ['2']
    text=fallback(payload)
    assert 'phone-2' in text and 'phone-1' not in text
    assert '尚未登记具体筛选条件' in text
    payload['userQuery']='推荐一部 2000 元左右的二手手机'
    payload['requirements']=[dict(key='price_minor',operator='lte',value=200000.0,priority='hard')]
    text=fallback(payload)
    assert text.startswith('本轮先按不超过 ¥2,000.00、优先接近预算筛选')
    reasons=[]
    text=render(json.dumps(dict(recommendations=[dict(productId='1',reason='备选',evidenceIds=[])],unknowns=[])),payload,on_validation=reasons.append)
    assert 'outside_displayed_products' in reasons[0]
    assert 'phone-1' not in text

def test_scope_menu_and_executor_reject_scope_tampering(monkeypatch):
    from tests.test_candidate_scope import _state, _planned_step
    from app.executor import _validate_scope_rerank_resolved,ExecutorArgumentResolutionError
    from app.llm import _explicit_harness_tool_schemas,_scope_rerank_title_order_intent
    monkeypatch.setattr(settings,'product_knowledge_enabled',True)
    monkeypatch.setattr(settings,'shopping_state_authority','legacy')
    state=_state(request_extra={'intent':'evidence_comparison'})
    step=_planned_step(state,'这里面续航好一点的，不打游戏')
    assert step.tool_name=='compare_products'
    assert step.arguments['productIds']==[101,102,103]
    assert 'rankingIntent' not in step.arguments
    assert _scope_rerank_title_order_intent('这里面续航好一点的，不打游戏')=='evidence_comparison'
    assert 'rerank_products_in_scope' not in {s['function']['name'] for s in _explicit_harness_tool_schemas('这里面续航好一点的',state)}
    _validate_scope_rerank_resolved(state,step.arguments)
    for key,value in [('productIds',[101,102,999]),('scopeId','foreign'),('category','laptop')]:
        bad={**step.arguments,key:value}
        with pytest.raises(ExecutorArgumentResolutionError):_validate_scope_rerank_resolved(state,bad)

@pytest.mark.anyio
async def test_new_comparison_crosses_planner_executor_validator_and_view(monkeypatch):
    from app import task_state
    from tests.fake_redis import FakeRedis
    from tests.test_candidate_scope import _state
    from tests.test_planner import _fake_client
    from app.harness import run_harness_step,_build_validated_results
    from app.llm import shopping_tool_schemas
    from app.domains.ecommerce import tools
    from app.product_knowledge import comparison
    from app.schemas import ToolTrace
    monkeypatch.setattr(settings,'product_knowledge_enabled',True)
    monkeypatch.setattr(settings,'shopping_state_authority','legacy')
    monkeypatch.setattr(settings,'used_phone_synthetic_price_policy','disabled')
    state=_state(requirements=[],request_extra={'intent':'evidence_comparison'})
    state.domain_state['candidateScope']['sourceQuery']='想看OPPO A11原神实测'
    state.domain_state['shoppingGuide']['candidateIds']=[101,102,103]
    fake=FakeRedis();monkeypatch.setattr(task_state,'_client',fake)
    task_state._task_locks.clear()
    await fake.set(task_state._state_key(state.task_id),state.model_dump_json(by_alias=True))
    products=[dict(id=i,title='商品'+str(i),brand='apple',priceStatus='unverified',attributes=[]) for i in (101,102,103)]
    monkeypatch.setattr(tools,'get_product_details_tool',AsyncMock(return_value=ToolTrace(tool='get_product_details',ok=True,detail={'products':products})))
    monkeypatch.setattr(comparison,'search_evidence',AsyncMock(side_effect=TimeoutError))
    result=await run_harness_step(state,'这里面续航好的，不打游戏',shopping_tool_schemas(),client=_fake_client(AsyncMock()),model='unused')
    assert result.action=='task_completed',result
    assert result.validator_result.outcome=='passed'
    views=_build_validated_results(result.task_state,[])
    assert views[0]['evidence']['userQuery']=='这里面续航好的，不打游戏'
    assert views[0]['evidence']['contextQuery']=='想看OPPO A11原神实测'
    assert views[0]['evidence']['knowledge']['status']=='UNAVAILABLE'
    assert result.task_state.domain_state.get('scopeRerankRequest') is None

@pytest.mark.anyio
async def test_fresh_search_then_evidence_uses_persisted_ids_and_preserves_scope(monkeypatch):
    from app import task_state
    from tests.fake_redis import FakeRedis
    from tests.test_candidate_scope import _state
    from tests.test_planner import _fake_client
    from tests.two_stage_ranking_fixtures import two_stage_search_detail
    from app.harness import run_harness_step,_build_validated_results
    from app.llm import shopping_tool_schemas
    from app.schemas import ToolTrace
    from app.tools import call_tool
    from app.domains.ecommerce import tools
    from app.product_knowledge import comparison
    monkeypatch.setattr(settings,'product_knowledge_enabled',True)
    monkeypatch.setattr(settings,'shopping_state_authority','legacy')
    monkeypatch.setattr(settings,'used_phone_synthetic_price_policy','disabled')
    state=_state(with_scope=False,with_request=False)
    fake=FakeRedis();monkeypatch.setattr(task_state,'_client',fake)
    task_state._task_locks.clear()
    await fake.set(task_state._state_key(state.task_id),state.model_dump_json(by_alias=True))
    ids=[1275270,9007199254740993123]
    search=two_stage_search_detail(ids)
    products=[{**p,'brand':'apple'} for p in search['candidates']]
    monkeypatch.setattr(tools,'get_product_details_tool',AsyncMock(return_value=ToolTrace(tool='get_product_details',ok=True,detail={'products':products})))
    monkeypatch.setattr(comparison,'search_evidence',AsyncMock(side_effect=TimeoutError))
    calls=[]
    async def caller(name,arguments):
        calls.append((name,deepcopy(arguments)))
        if name=='search_products': return ToolTrace(tool=name,ok=True,detail=deepcopy(search))
        return await call_tool(name,arguments)
    result=await run_harness_step(state,'找iOS手机，拍照优先，不打游戏',shopping_tool_schemas(),client=_fake_client(AsyncMock()),model='unused',tool_caller=caller)
    assert result.action=='continue_to_executor',result
    result=await run_harness_step(result.task_state,'找iOS手机，拍照优先，不打游戏',shopping_tool_schemas(),client=_fake_client(AsyncMock()),model='unused',tool_caller=caller)
    assert result.action=='task_completed',result
    assert [name for name,_ in calls]==['search_products','compare_products']
    assert calls[1][1]['productIds']==ids
    assert calls[1][1]['requirements']==calls[0][1]['requirements']
    assert result.task_state.domain_state['candidateScope']['rankedItemIds']==ids
    assert _build_validated_results(result.task_state,[])[-1]['evidence']['userQuery']=='找iOS手机，拍照优先，不打游戏'
