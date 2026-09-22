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
    routes=call.call_args.kwargs['tools'][0]['function']['parameters']['properties']['route']['enum']
    assert routes==['business','catalog','product']
    assert 'catalog用于所有商品检索' in prompt and 'phone用于' not in prompt
