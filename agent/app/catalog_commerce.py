"""Attach server-verified trading cards without changing immutable source evidence."""
import re


async def resolve_optional_cards(scope):
    """Source search remains readable during authority outages; never grant commerce access."""
    from fastapi import HTTPException
    try:
        return await resolve_cards(scope), None
    except HTTPException as exc:
        if exc.status_code not in {502, 503, 504}:
            raise
        return [], '交易资料服务暂不可用，本轮不提供报价、库存及购买权限。'


def source_only_answer(scope, notice):
    """Use verified source records when no trading cards can be presented."""
    from .catalog_service import verify_scope
    if scope is None:
        return notice
    verify_scope(scope)
    lines = ['本轮检索到以下来源记录：', '']
    for index, group in enumerate(scope.get('groups', [])[:scope.get('presentationLimit', 6)], 1):
        member = group['members'][0]
        lines.append(f"{index}. {group.get('title') or member.get('title') or '商品资料'}（来源：{member['docid']}）")
    return '\n'.join(lines + ['', notice])


async def resolve_cards(scope):
    from .api import commerce_workspace as ws
    from .catalog_service import verify_scope
    if not ws.auth.settings.commerce_workspace_external_catalog_enabled or scope is None:
        return []
    verify_scope(scope)
    members=[]
    for group in scope.get('groups',[])[:scope.get('presentationLimit',6)]:
        for member in group['members']:
            source,ident=member['docid'].split(':',1)
            raw_sha=member.get('recordSha256','')
            if source!=member['source'] or source not in {'kuaisearch','multicpr'}:
                raise ValueError('catalog_commerce_identity_invalid')
            if not re.fullmatch(r'[1-9][0-9]{0,11}',ident) or not re.fullmatch(r'[a-f0-9]{64}',raw_sha):
                raise ValueError('catalog_commerce_provenance_invalid')
            members.append(dict(source=source,sourceItemId=ident,rawSha256=raw_sha))
    members=list({(m['source'],m['sourceItemId']):m for m in members}.values())
    if not members:return []
    if len(members)>20:raise ValueError('catalog_commerce_too_many_candidates')
    resolved=await ws.auth._java('POST','/api/products/resolve-sources',body={'identities':members})
    expected={(m['source'],m['sourceItemId']):m['rawSha256'] for m in members}
    by_identity={}
    for row in resolved:
        identity=(row['source'],row['sourceItemId'])
        if expected.get(identity)!=row['rawSha256'] or identity in by_identity:
            raise ValueError('catalog_commerce_response_identity_mismatch')
        if not re.fullmatch(r'[1-9][0-9]*',str(row['productId'])):
            raise ValueError('catalog_commerce_product_id_invalid')
        by_identity[identity]=row
    cards=[]
    for member in members:
        row=by_identity.get((member['source'],member['sourceItemId']))
        if row is None:continue  # Unimported, changed-source or archived records remain evidence only.
        card=await ws._card(int(row['productId']))
        card['sourceDocid']=row['source']+':'+row['sourceItemId']
        card['sourceRecordSha256']=row['rawSha256']
        cards.append(card)
    return cards


async def verify_card_reference(state,card):
    """Recheck a current source-bound card for read-only follow-up, never accept model IDs."""
    from .api import commerce_workspace as ws
    from .catalog_service import verify_scope
    if not ws.auth.settings.commerce_workspace_external_catalog_enabled:return False
    scope=(state.get('catalogSearch') or {}).get('scope')
    if not scope:return False
    verify_scope(scope)
    docid=card.get('sourceDocid');raw=card.get('sourceRecordSha256')
    member=next((m for g in scope['groups'] for m in g['members'] if m['docid']==docid and m['recordSha256']==raw),None)
    if not member:return False
    source,ident=docid.split(':',1)
    rows=await ws.auth._java('POST','/api/products/resolve-sources',body={'identities':[dict(source=source,sourceItemId=ident,rawSha256=raw)]})
    return len(rows)==1 and rows[0]==dict(source=source,sourceItemId=ident,rawSha256=raw,productId=str(card['id']))
