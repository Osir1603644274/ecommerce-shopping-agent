import asyncio
import pytest
from unittest.mock import AsyncMock, patch
from app.domains.ecommerce import tools
from .test_ecommerce_two_stage_search import _Client, _Response


def test_phone_search_passes_scope_to_both_backend_channels():
    calls=[]
    class Client(_Client):
        async def get(self,url,params=None):
            calls.append((url,params))
            return await super().get(url,params)
    with (patch.object(tools.settings,'ecommerce_guide_enabled',True),
          patch.object(tools.settings,'product_legacy_catalog_version','frozen-phone'),
          patch.object(tools.settings,'product_retrieval_mode','bm25'),
          patch.object(tools.settings,'used_phone_synthetic_price_policy','disabled'),
          patch.object(tools,'_product_http_client',return_value=Client()),
          patch.object(tools,'get_product_details_tool',new=AsyncMock(return_value=tools.ToolTrace(tool='get_product_details',ok=True,detail={'products':[]})))):
        asyncio.run(tools.search_products_tool('二手手机','手机',requirements=[]))
    assert len(calls)==2
    assert all(params['catalogVersion']=='frozen-phone' for _,params in calls)


def test_phone_vector_warmup_uses_same_published_scope():
    client=AsyncMock()
    client.get.return_value=_Response([{'id':1}])
    with (patch.object(tools.settings,'product_legacy_catalog_version','frozen-phone'),
          patch.object(tools,'_product_http_client',return_value=client),
          patch.object(tools,'_local_product_vector_rank',return_value=[])):
        assert asyncio.run(tools.warm_local_product_vector_cache())==1
    assert client.get.call_args.kwargs['params']['catalogVersion']=='frozen-phone'


def test_unified_catalog_does_not_warm_legacy_phone_vectors():
    from app import main
    with (patch.object(main.settings,'catalog_workspace_enabled',True),
          patch.object(main.settings,'product_retrieval_mode','hybrid'),
          patch.object(main.settings,'product_vector_backend','local')):
        assert not main._needs_legacy_product_vector_warmup()
    with (patch.object(main.settings,'catalog_workspace_enabled',False),
          patch.object(main.settings,'product_retrieval_mode','hybrid'),
          patch.object(main.settings,'product_vector_backend','local')):
        assert main._needs_legacy_product_vector_warmup()


def test_all_product_searches_share_catalog_route_contract():
    from app import catalog_conversation as conversation
    call=AsyncMock(side_effect=RuntimeError('captured'))
    with patch.object(conversation,'model_call',call),pytest.raises(RuntimeError,match='captured'):
        asyncio.run(conversation.plan_turn('找一台3000元左右的苹果手机',{}))
    prompt=call.call_args.args[0][0]['content']
    schema=call.call_args.kwargs['tools'][0]['function']
    assert schema['name']=='interpret_shopping_intent'
    properties=schema['parameters']['properties']
    assert 'route' not in properties and 'action' not in properties
    assert schema['parameters']['additionalProperties'] is False
    assert set(properties['intent']['enum'])=={
        'search','modify','new_search','undo','compare','cancel',
        'product_question','clarify','business_request',
    }
    for intent in ('search','modify','new_search','undo','compare','cancel'):
        assert conversation.expand_intent({'intent':intent})['route']=='catalog'
    assert conversation.expand_intent({'intent':'product_question'})['route']=='product'
    assert conversation.expand_intent({'intent':'business_request'})['route']=='business'
    assert '商品检索和需求修改对所有品类使用同一规则' in prompt and 'phone用于' not in prompt
