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


@pytest.mark.parametrize('enabled',[False,True])
def test_full_catalog_phone_search_is_explicit_and_opt_in(enabled):
    from app import catalog_conversation as conversation
    call=AsyncMock(side_effect=RuntimeError('captured'))
    with (patch.object(conversation.settings,'commerce_workspace_external_catalog_enabled',enabled),
          patch.object(conversation,'model_call',call),pytest.raises(RuntimeError,match='captured')):
        asyncio.run(conversation.plan_turn('在全量目录中搜索手机',{}))
    prompt=call.call_args.args[0][0]['content']
    assert ('用户明确要求在全量目录' in prompt)==enabled
    assert ('手机本体不可误送catalog' in prompt)==(not enabled)
