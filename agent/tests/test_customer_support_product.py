import pytest
from app.customer_support.product_knowledge import retrieve_product
from app.customer_support.planner import SupportPlan
from app.customer_support.runtime import execute_plan
from .test_commerce_workspace import async_test


def test_product_retrieval_keeps_sku_and_version_and_never_uses_extracted_claims():
    product = {'id': 123, 'entityVersion': 4, 'datasetRevision': 'r1', 'attributeText': '存储容量256GB；接口USB-C', 'attributes': [{'rawValue': '赠送充电器'}]}
    result = retrieve_product('存储容量', product, expected_item_id=123)
    assert result['citations'][0]['text'] == '存储容量256GB'
    assert result['citations'][0]['entityVersion'] == 4
    assert retrieve_product('赠送充电器', product, expected_item_id=123)['citations'] == []
    with pytest.raises(ValueError): retrieve_product('容量', product, expected_item_id=124)


@async_test
async def test_missing_original_spec_is_not_filled_from_current_catalog():
    async def forbidden(*args, **kwargs): raise AssertionError('current catalog must not fill original sale specification')
    order = {'id': 'o1', 'currency': 'CNY', 'items': [{'itemId': 123, 'itemType': 'PRODUCT', 'evidenceJson': '{}'}]}
    result = await execute_plan(SupportPlan(intent='product', subject='current_order', item_number=1, product_topic='original_specification'),
                                order=order, retrieval={}, java=forbidden, run_id='r1', tool_receipts=[])
    assert '没有可靠的规格快照' in result['answer']


@async_test
async def test_original_unit_price_is_not_presented_as_refund_allocation():
    order = {'id': 'o1', 'currency': 'CNY', 'items': [{'itemId': 123, 'itemType': 'PRODUCT', 'unitPriceMinor': 101}]}
    result = await execute_plan(SupportPlan(intent='product', subject='current_order', item_number=1, product_topic='original_price'),
                                order=order, retrieval={}, java=None, run_id='r1', tool_receipts=[])
    assert '1.01 CNY' in result['answer'] and '不是扣除订单优惠后可退的金额' in result['answer']
