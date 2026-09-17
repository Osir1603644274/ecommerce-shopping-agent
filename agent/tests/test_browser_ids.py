from app.api.browser_ids import browser_ids

def test_large_identity_is_exact_and_does_not_convert_money_or_mutate_source():
    ident = 6806929769710081737
    source = {'cards': [{'id': ident}], 'items': [{'itemId': ident, 'quantity': 2}], 'priceMinor': 200000}
    result = browser_ids(source)
    assert result['cards'][0]['id'] == str(ident)
    assert result['items'][0]['itemId'] == str(ident)
    assert result['priceMinor'] == 200000
    assert source['cards'][0]['id'] == ident

def test_uuid_and_small_identifiers_remain_backward_compatible():
    value = {'id':'order-uuid','itemId':123,'items':[{'id':4}]}
    assert browser_ids(value) == value
