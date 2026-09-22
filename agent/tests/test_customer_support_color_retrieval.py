import pytest
from app.customer_support.knowledge import retrieve_policy

@pytest.mark.parametrize('query',['商品1坏了，给我换1件白色，原来买的是黑色。','我要把黑色商品1换成红色2件。','我想把蓝色换成银色可以吗？'])
def test_color_exchange_retrieves_applicable_scope(query):
    result=retrieve_policy(query)
    assert any(c['id'].endswith(':exchange-specification') for c in result['citations'])

def test_version_boundary_still_holds_for_color_exchange():
    result=retrieve_policy('黑色换成白色',policy_version='unknown-version')
    assert result['status']=='POLICY_VERSION_UNAVAILABLE' and not result['citations']
