"""Prepare authoritative facts for the parent model; never score title slogans."""
from copy import deepcopy
import hashlib
import time

from ..schemas import ToolTrace
from ..settings import settings
from .client import search_evidence

CONTRACT = 'product-evidence-comparison-v1'


async def knowledge_for_products(query: str, product_ids: list[int], context_query: str | None = None) -> dict:
    if not settings.product_knowledge_enabled:
        return dict(status='DISABLED',evidence=[],gaps=[dict(reason='FEATURE_DISABLED')])
    try:
        return await search_evidence(settings.product_knowledge_mcp_url,query,
            [str(i) for i in product_ids],timeout=settings.product_knowledge_timeout_seconds,context_query=context_query)
    except Exception as exc:
        # No outbound web fallback, no fabricated "zero" specifications.
        return dict(status='UNAVAILABLE',evidence=[],gaps=[dict(reason='KNOWLEDGE_UNAVAILABLE',errorType=type(exc).__name__)],
            notice='知识服务暂不可用，仅能依据商品事实比较；未知性能不能据此下结论。')


def check_candidates(products: list[dict], category: str, requirements: list[dict]) -> list[dict]:
    from ..domains.ecommerce.models import ShoppingRequirement, compare_product_details, validate_requirements
    parsed=[ShoppingRequirement.model_validate(r) for r in requirements]
    validate_requirements(category,parsed)
    # Check every candidate separately. There is no top-3 truncation before LLM deliberation.
    checked=[]
    for product in products:
        result=compare_product_details(category,[product],parsed)
        rows=[{k:r[k] for k in ('fullyMatched','hardFailures','hardUnknowns','checks') if k in r} for r in result['products']]
        checked.append(dict(productId=product['id'],decision=dict(products=rows,
            eliminated=[{k:r[k] for k in ('productId','reason','checks') if k in r} for r in result.get('eliminated',[])])))
    return checked


def product_check_projection(product: dict, requirements: list[dict]) -> dict:
    # Preserve every matching token (including conflicts) for the exact-token
    # observer. Full source bodies remain in the catalog/detail service.
    from ..domains.ecommerce.used_phone_attributes import observe_used_phone_attributes
    keys={r['key'] for r in requirements}
    result={k:deepcopy(product[k]) for k in ('id','title','brand','categoryL1','categoryL2','categoryL3','source','provenanceUrl','dataNature',
        'priceStatus','snapshotPriceMinor','syntheticReferencePrice') if k in product}
    result['attributes']=[deepcopy(a) for a in product.get('attributes',[]) if a.get('key') in keys]
    for attribute in result['attributes']:
        raw=attribute.get('rawValue')
        if not isinstance(raw,str): continue
        observation=observe_used_phone_attributes(raw).get(attribute['key'])
        if observation is None: continue
        excerpt=','.join(observation.matched_raw_tokens)
        if observe_used_phone_attributes(excerpt)[attribute['key']] != observation:
            raise ValueError('attribute_projection_changed_observation')
        attribute['rawValue']=excerpt
        attribute['rawProjection']='all_exact_matching_tokens'
        attribute['fullRawSha256']=hashlib.sha256(raw.encode('utf8')).hexdigest()
    return result


def knowledge_projection(value: dict) -> dict:
    value=deepcopy(value)
    value['bindings']=[{k:b[k] for k in ('itemId','status','modelKeys','region','variantStatus') if k in b} for b in value.get('bindings',[])]
    for e in value.get('evidence',[]):
        e['source']={k:e['source'][k] for k in ('url','section','checkedOn','region','authority','softwareVersion','testConditions','limits') if k in e['source']}
        for k in ('value','unit','label','applicability','listingIdentityVerified'):e.pop(k,None)
    grouped={}; other=[]
    for gap in value.get('gaps',[]):
        if set(gap)=={'modelKey','field','reason'}:
            grouped.setdefault((gap['modelKey'],gap['reason']),[]).append(gap['field'])
        else: other.append(gap)
    value['gaps']=other+[dict(modelKey=model,reason=reason,fields=list(dict.fromkeys(fields)))
        for (model,reason),fields in grouped.items()]
    grouped_items={};grouped_models={};other=[]
    for gap in value['gaps']:
        if set(gap)=={'itemId','reason'}:
            grouped_items.setdefault(gap['reason'],[]).append(gap['itemId'])
        elif set(gap)=={'modelKey','reason','fields'}:
            grouped_models.setdefault((gap['reason'],tuple(gap['fields'])),[]).append(gap['modelKey'])
        else:other.append(gap)
    value['gaps']=other+[dict(reason=reason,itemIds=ids) for reason,ids in grouped_items.items()]+[
        dict(reason=reason,fields=list(fields),modelKeys=models) for (reason,fields),models in grouped_models.items()]
    return value


async def prepare_comparison(product_ids: list[int], category: str, requirements: list[dict],
                             user_query: str, scope_id: str | None = None, context_query: str | None = None) -> ToolTrace:
    from ..domains.ecommerce.tools import get_product_details_tool, apply_synthetic_prices
    started=time.perf_counter()
    try:
        products=[]
        for offset in range(0,len(product_ids),10):
            result=await get_product_details_tool(product_ids[offset:offset+10])
            if not result.ok or not isinstance(result.detail,dict): raise ValueError('authoritative_details_unavailable')
            products.extend(result.detail.get('products',[]))
        by_id={p['id']:p for p in products}
        if len(products)!=len(product_ids) or set(by_id)!=set(product_ids): raise ValueError('product_details_scope_mismatch')
        products=[by_id[i] for i in product_ids]
        if category=='phone' and settings.used_phone_synthetic_price_policy!='disabled':
            products=apply_synthetic_prices(products,directory=settings.used_phone_synthetic_price_dir,
                policy=settings.used_phone_synthetic_price_policy)
        products=[product_check_projection(p,requirements) for p in products]
        candidates=check_candidates(products,category,requirements)
        knowledge=knowledge_projection(await knowledge_for_products(user_query,product_ids,context_query))
        detail=dict(contractVersion=CONTRACT,comparisonMode='scope' if scope_id else 'specified',scopeId=scope_id,
            userQuery=user_query,contextQuery=context_query,category=category,requirements=deepcopy(requirements),productIds=product_ids,
            products=products,candidates=candidates,knowledge=knowledge,
            evidenceRefs=[e['evidenceId'] for e in knowledge.get('evidence',[])],
            answerConstraint='由父Agent依据原始需求权衡。硬条件不通过不得推荐为满足；未知不当满足。型号证据不是实物保证；实测须保留协议。事实标注证据ID，推断说明依据。')
        return ToolTrace(tool='compare_products',ok=True,detail=detail,duration_ms=round((time.perf_counter()-started)*1000,2))
    except Exception as exc:
        return ToolTrace(tool='compare_products',ok=False,detail=dict(code='invalid_evidence_comparison',message=str(exc)),
            duration_ms=round((time.perf_counter()-started)*1000,2))


async def evidence_tool(product_ids: list[int], query: str) -> ToolTrace:
    value=await knowledge_for_products(query,product_ids)
    return ToolTrace(tool='search_product_evidence',ok=value['status'] not in {'DISABLED'},
        detail=dict(productIds=product_ids,query=query,knowledge=value))


def validate_knowledge(knowledge: dict, product_ids: list[int]) -> None:
    if not isinstance(knowledge,dict) or not isinstance(knowledge.get('evidence'),list): raise ValueError('invalid_knowledge')
    if knowledge.get('status') in {'UNAVAILABLE','DISABLED'}:
        if knowledge['evidence']: raise ValueError('unavailable_has_evidence')
        return
    if not knowledge.get('knowledgeVersion') or knowledge.get('transport')!='MCP_STREAMABLE_HTTP': raise ValueError('missing_mcp_provenance')
    ids={str(i) for i in product_ids}
    bindings=knowledge.get('bindings',[])
    if any(b.get('itemId') not in ids for b in bindings): raise ValueError('foreign_binding')
    models={m for b in bindings if b.get('status')=='CLEAR' for m in b.get('modelKeys',[])}
    seen=set()
    for e in knowledge['evidence']:
        if e.get('modelKey') not in models or not e.get('evidenceId') or e['evidenceId'] in seen: raise ValueError('foreign_or_duplicate_evidence')
        if not isinstance(e.get('source'),dict) or not e['source'].get('url'): raise ValueError('missing_evidence_source')
        seen.add(e['evidenceId'])
