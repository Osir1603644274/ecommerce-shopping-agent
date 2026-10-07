from app.catalog_requirements import assess,select_groups
from app.catalog_conversation import transition

def req(value,mode='require',facet='属性',terms=None):
    return dict(facet=facet,mode=mode,value=value,terms=terms or [])

def test_exclusion_distinguishes_explicit_conflict_absence_and_negated_evidence():
    r=[req('鸡肉','exclude')]
    assert assess('无谷鸡肉冻干狗粮',[],r)[0]['status']=='conflict'
    assert assess('不含鸡肉的小颗粒狗粮',[],r)[0]['status']=='supported'
    assert assess('泰迪狗粮',[],r)[0]['status']=='unknown'
    assert assess('无鸡肉与鸡肉口味可选',[],r)[0]['status']=='unknown'
    assert assess('不用系鞋带免系带女鞋',[],[req('鞋带','exclude',terms=['系带'])])[0]['status']=='supported'

def test_accessory_device_name_does_not_trigger_body_exclusion():
    r=[req('充电宝主板',facet='商品',terms=['主板','电路板']),req('充电宝','exclude',facet='商品')]
    assert assess('原装品胜219充电宝电路板',[],r)[1]['status']=='unknown'
    assert assess('品胜快充充电宝',[],r)[1]['status']=='conflict'
    r[1]=req('充电宝整机','exclude',facet='商品',terms=['充电宝','移动电源'])
    assert assess('原装品胜219充电宝电路板',[],r)[1]['status']=='unknown'
    assert assess('品胜快充充电宝',[],r)[1]['status']=='conflict'


def test_broad_alias_and_identifier_substrings_do_not_establish_attributes():
    assert assess('大棚固定夹',[],[req('大号',terms=['大号','大'])])[0]['status']=='unknown'
    assert assess('鸡蛋配方狗粮',[],[req('鸡肉','exclude',terms=['鸡'])])[0]['status']=='unknown'
    assert assess('120个装',[],[req('20个装')])[0]['status']=='unknown'
    assert assess('iphone13promax',[],[req('iphone13')])[0]['status']=='unknown'


def test_missing_model_alias_cannot_exclude_the_observed_circuit_board():
    r=[req('充电宝主板',facet='商品',terms=['主板']),req('品胜',facet='品牌'),
       req('充电宝整机','exclude',facet='商品',terms=['充电宝','移动电源'])]
    title='原装品胜219充电宝电路板安卓口苹果口升压板5V2.4a移动电源pcb板'
    e=assess(title,[],r)
    assert e[0]['status']=='unknown' and e[0]['lexicalHint']
    assert e[2]['status']!='conflict'

def test_budget_numbers_never_become_verified_price_and_soft_preferences_do_not_filter():
    r=[req('100元以内',facet='预算'),req('防滑','prefer')]
    e=assess('100g 女鞋',[],r)
    assert all(x['status']=='unknown' for x in e)
    good,bad=select_groups([dict(title='女鞋',members=[])],r)
    assert len(good)==1 and not bad

def test_explicit_material_conflict_and_missing_material_are_different():
    r=[req('木头',facet='材质',terms=['木质','木制'])]
    assert assess('大号不锈钢夹子',[],r)[0]['status']=='conflict'
    assert assess('大号晾衣夹',[],r)[0]['status']=='unknown'
    assert assess('大号木质夹子',[],r)[0]['status']=='supported'

def test_cross_source_selection_prioritizes_required_brand_without_raw_score_comparison():
    r=[req('玫琳凯',facet='品牌'),req('洗面奶',facet='商品')]
    groups=[dict(title='左颜右色洗面奶',members=[]),dict(title='玫琳凯男士洗面奶',members=[])]
    good,bad=select_groups(groups,r)
    assert good[0]['title']=='玫琳凯男士洗面奶'
    assert len(good)==2 and not bad # Missing brand metadata does not prove conflict.

def test_selective_refine_then_undo_restores_full_requirements_and_softness():
    old=dict(query='大号木夹20个装；优先防风',retrievalQuery='大号木夹20个装',scope=None,revision=2,history=[],
             requirements=[req('大号'),req('20个装',facet='数量'),req('防风','prefer')])
    p=dict(action='refine',query='大号木夹；优先防风',retrievalQuery='大号木夹',requirements=[req('大号'),req('防风','prefer')])
    changed,_=transition(old,p)
    assert changed['requirements']==p['requirements']
    restored,_=transition(changed,dict(action='undo'))
    assert restored['requirements']==old['requirements']
    assert restored['retrievalQuery']==old['retrievalQuery']
    new,_=transition(restored,dict(action='new',query='手机壳',requirements=[req('手机壳',facet='商品')]))
    assert not new['history'] and len(new['requirements'])==1


def test_literal_product_query_preserves_packaging_even_if_model_drops_it(monkeypatch):
    import asyncio,json
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from app import catalog_conversation as c
    fake=dict(intent='search',query='芝士奶酪',retrievalQuery='芝士奶酪',requirements=[],numbers=[],question='')
    reply=SimpleNamespace(tool_calls=[SimpleNamespace(function=SimpleNamespace(name='interpret_shopping_intent',arguments=json.dumps(fake)))])
    monkeypatch.setattr(c,'model_call',AsyncMock(return_value=(reply,{})))
    value,receipt=asyncio.run(c.plan_turn('一杯芝士奶酪',{'messages':[]}))
    assert value['query']==value['retrievalQuery']=='一杯芝士奶酪'
    assert receipt['literalQueryPreserved']


def test_literal_guard_does_not_override_business_routing_or_clarification(monkeypatch):
    import asyncio,json
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from app import catalog_conversation as c
    for intent,route,action in [('business_request','business','inspect'),('clarify','catalog','clarify')]:
        fake=dict(intent=intent,query='',numbers=[],question='请补充商品')
        reply=SimpleNamespace(tool_calls=[SimpleNamespace(function=SimpleNamespace(name='interpret_shopping_intent',arguments=json.dumps(fake)))])
        monkeypatch.setattr(c,'model_call',AsyncMock(return_value=(reply,{})))
        value,_=asyncio.run(c.plan_turn('手机',{'messages':[]}))
        assert value['route']==route and value['action']==action and value['query']==''


def test_exclusion_only_change_reuses_positive_query_despite_model_expansion(monkeypatch):
    import asyncio,json
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from app import catalog_conversation as c
    current=dict(query='品胜充电宝主板',retrievalQuery='品胜充电宝主板',scope=None,
                 requirements=[req('充电宝主板',facet='商品'),req('品胜',facet='品牌')])
    fake=dict(intent='modify',query='品胜充电宝主板，不要整机',
              retrievalQuery='品胜 充电宝主板 移动电源主板',numbers=[],question='',
              requirements=current['requirements']+[req('充电宝整机','exclude',facet='商品')])
    reply=SimpleNamespace(tool_calls=[SimpleNamespace(function=SimpleNamespace(name='interpret_shopping_intent',arguments=json.dumps(fake)))])
    monkeypatch.setattr(c,'model_call',AsyncMock(return_value=(reply,{})))
    value,receipt=asyncio.run(c.plan_turn('不要整个充电宝',{'messages':[],'catalogSearch':current}))
    assert value['retrievalQuery']=='品胜充电宝主板'
    assert receipt['positiveQueryReused'] and receipt['modelPlan']['retrievalQuery']==fake['retrievalQuery']


def test_conversational_first_search_uses_clean_model_query(monkeypatch):
    import asyncio,json
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from app import catalog_conversation as c
    fake=dict(intent='search',query='苹果手机',retrievalQuery='苹果手机',numbers=[],question='',
              requirements=[req('手机',facet='商品'),req('苹果',facet='品牌')])
    reply=SimpleNamespace(tool_calls=[SimpleNamespace(function=SimpleNamespace(
        name='interpret_shopping_intent',arguments=json.dumps(fake)))])
    monkeypatch.setattr(c,'model_call',AsyncMock(return_value=(reply,{})))
    value,receipt=asyncio.run(c.plan_turn('我喜欢苹果手机',{'messages':[]}))
    assert value['retrievalQuery']=='苹果手机'
    assert receipt['queryDecision']=={
        'reason':'model_suggestion','modelSuggestion':'苹果手机','plannedRetrievalQuery':'苹果手机'}
    assert not receipt.get('literalQueryPreserved')
    for message in ('求推荐苹果手机', '我要苹果手机', '请推荐苹果手机'):
        value,receipt=asyncio.run(c.plan_turn(message,{'messages':[]}))
        assert value['retrievalQuery']=='苹果手机'
        assert not receipt.get('literalQueryPreserved')


def test_budget_only_reuses_clean_query_but_replaces_stale_conversation(monkeypatch):
    import asyncio,json
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from app import catalog_conversation as c
    base=[req('手机',facet='商品'),req('苹果',facet='品牌')]
    fake=dict(intent='modify',query='苹果手机，预算3000元以内',retrievalQuery='苹果手机',
              numbers=[],question='',requirements=base+[dict(req('3000',facet='预算'),operator='lte',unit='元')])
    reply=SimpleNamespace(tool_calls=[SimpleNamespace(function=SimpleNamespace(
        name='interpret_shopping_intent',arguments=json.dumps(fake)))])
    monkeypatch.setattr(c,'model_call',AsyncMock(return_value=(reply,{})))
    for old,reason in [('苹果手机','unchanged_positive_requirements'),
                       ('我喜欢苹果手机','stale_conversational_query_replaced')]:
        current=dict(query=old,retrievalQuery=old,requirements=base)
        value,receipt=asyncio.run(c.plan_turn('预算3000元以内',{'catalogSearch':current,'messages':[]}))
        assert value['retrievalQuery']=='苹果手机'
        assert receipt['queryDecision']['reason']==reason
        assert value['requirements'][-1]['operator']=='lte'


def test_removing_brand_does_not_turn_android_allowed_into_query_preference(monkeypatch):
    import asyncio,json
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from app import catalog_conversation as c
    current=dict(query='手机；苹果；3000元以内',retrievalQuery='苹果手机',
                 requirements=[req('手机',facet='商品'),req('苹果',facet='品牌'),
                               dict(req('3000',facet='预算'),operator='lte',unit='元')])
    fake=dict(intent='modify',query='手机，不限苹果，安卓也行，3000元以内',retrievalQuery='手机 安卓',
              numbers=[],question='',requirements=[current['requirements'][0],current['requirements'][2]])
    reply=SimpleNamespace(tool_calls=[SimpleNamespace(function=SimpleNamespace(
        name='interpret_shopping_intent',arguments=json.dumps(fake)))])
    monkeypatch.setattr(c,'model_call',AsyncMock(return_value=(reply,{})))
    value,receipt=asyncio.run(c.plan_turn('不限苹果，安卓也行',{'catalogSearch':current,'messages':[]}))
    assert value['retrievalQuery']=='手机'
    assert receipt['queryDecision']['reason']=='positive_requirement_removed'
    assert receipt['queryDecision']['modelSuggestion']=='手机 安卓'


def test_provider_query_selection_records_bounded_rewrite_and_fallback():
    from app.guide_execution import select_provider_query
    state={'query':'手机；苹果','retrievalQuery':'苹果手机'}
    assert select_provider_query(state)==('苹果手机','shopping_state')
    assert select_provider_query(state,{'query':'iPhone 13'})==('iPhone 13','bounded_rewrite')
    assert select_provider_query({'query':'玻璃杯','retrievalQuery':''})==(
        '玻璃杯','shopping_query_fallback')


def test_unit_change_is_not_treated_as_unchanged_positive_requirement(monkeypatch):
    import asyncio,json
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from app import catalog_conversation as c
    product=req('玻璃杯',facet='商品')
    old_size=dict(req('888',facet='容量'),operator='eq',unit='ml')
    new_size=dict(req('888',facet='容量'),operator='eq',unit='g')
    current=dict(query='玻璃杯888ml',retrievalQuery='玻璃杯888ml',
                 requirements=[product,old_size])
    fake=dict(intent='modify',query='玻璃杯888g',retrievalQuery='玻璃杯888g',
              numbers=[],question='',requirements=[product,new_size])
    reply=SimpleNamespace(tool_calls=[SimpleNamespace(function=SimpleNamespace(
        name='interpret_shopping_intent',arguments=json.dumps(fake)))])
    monkeypatch.setattr(c,'model_call',AsyncMock(return_value=(reply,{})))
    value,receipt=asyncio.run(c.plan_turn('改成888g',{'catalogSearch':current,'messages':[]}))
    assert value['retrievalQuery']=='玻璃杯888g'
    assert not receipt.get('positiveQueryReused')
