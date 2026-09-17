from copy import deepcopy
from unittest.mock import AsyncMock
import pytest
from .test_commerce_workspace import async_test
from .test_catalog_workspace import scope
from app.api import commerce_workspace as ws
from app.catalog_commerce import resolve_cards
from app.catalog_commerce import verify_card_reference


@async_test
async def test_disabled_does_not_resolve_or_grant_eligibility(monkeypatch):
    monkeypatch.setattr(ws.auth.settings,'commerce_workspace_external_catalog_enabled',False)
    java=AsyncMock();monkeypatch.setattr(ws.auth,'_java',java)
    assert await resolve_cards(scope())==[]
    java.assert_not_awaited()


@async_test
async def test_exact_source_and_hash_binding_without_changing_evidence(monkeypatch):
    monkeypatch.setattr(ws.auth.settings,'commerce_workspace_external_catalog_enabled',True)
    evidence=scope();original=deepcopy(evidence)
    java=AsyncMock(return_value=[dict(source='multicpr',sourceItemId='1',rawSha256='a'*64,productId='4001000000000001')])
    card=AsyncMock(return_value=dict(id=4001000000000001,title='source item',purchasable=True,catalogEligible=True))
    monkeypatch.setattr(ws.auth,'_java',java);monkeypatch.setattr(ws,'_card',card)
    result=await resolve_cards(evidence)
    assert evidence==original and evidence['commerceAuthority'] is False
    assert len(result)==1 and result[0]['sourceDocid']=='multicpr:1'
    card.assert_awaited_once_with(4001000000000001)
    assert java.call_args.kwargs['body']['identities'][0]['rawSha256']=='a'*64


@async_test
async def test_no_import_identity_keeps_document_only(monkeypatch):
    monkeypatch.setattr(ws.auth.settings,'commerce_workspace_external_catalog_enabled',True)
    monkeypatch.setattr(ws.auth,'_java',AsyncMock(return_value=[]))
    card=AsyncMock();monkeypatch.setattr(ws,'_card',card)
    assert await resolve_cards(scope())==[]
    card.assert_not_awaited()


@pytest.mark.parametrize('tamper', ['none','id','hash','docid'])
@async_test
async def test_followup_is_bound_to_source_scope_and_current_java_identity(monkeypatch,tamper):
    monkeypatch.setattr(ws.auth.settings,'commerce_workspace_external_catalog_enabled',True)
    evidence=scope()
    card=dict(id='4001000000000001',sourceDocid='multicpr:1',sourceRecordSha256='a'*64)
    if tamper=='id':card['id']='4001000000000002'
    if tamper=='hash':card['sourceRecordSha256']='b'*64
    if tamper=='docid':card['sourceDocid']='multicpr:999'
    java=AsyncMock(return_value=[dict(source='multicpr',sourceItemId='1',rawSha256='a'*64,productId='4001000000000001')])
    monkeypatch.setattr(ws.auth,'_java',java)
    assert await verify_card_reference({'catalogSearch':{'scope':evidence}},card) is (tamper=='none')
    if tamper in {'hash','docid'}:java.assert_not_awaited()


@pytest.mark.parametrize('source,ident,raw',[('other','1','a'*64),('kuaisearch','999','a'*64),('kuaisearch','1','b'*64)])
@async_test
async def test_response_cannot_substitute_identity_or_source_hash(monkeypatch,source,ident,raw):
    monkeypatch.setattr(ws.auth.settings,'commerce_workspace_external_catalog_enabled',True)
    monkeypatch.setattr(ws.auth,'_java',AsyncMock(return_value=[dict(source=source,sourceItemId=ident,rawSha256=raw,productId='1')]))
    card=AsyncMock();monkeypatch.setattr(ws,'_card',card)
    with pytest.raises(ValueError,match='identity_mismatch'):await resolve_cards(scope())
    card.assert_not_awaited()


@pytest.mark.parametrize('enabled,imported,can_purchase,expected',[
    (False,True,True,False),(True,False,True,False),(True,True,True,True),(True,True,False,False)])
@async_test
async def test_non_phone_requires_enabled_import_authority_and_current_offer(monkeypatch,enabled,imported,can_purchase,expected):
    monkeypatch.setattr(ws.auth.settings,'commerce_workspace_external_catalog_enabled',enabled)
    monkeypatch.setattr(ws.auth.settings,'commerce_workspace_local_offers_enabled',True)
    monkeypatch.setattr(ws.auth,'_java',AsyncMock(return_value=dict(
        product=dict(title='奶糖',categoryL3='奶糖',brand='unknown'),
        offer=dict(priceMinor=1200,currency='CNY',available=10,kind='local_simulated',
                   externalCatalogEligible=imported,canPurchase=can_purchase))))
    assert (await ws._card(4000000000010144))['purchasable'] is expected
