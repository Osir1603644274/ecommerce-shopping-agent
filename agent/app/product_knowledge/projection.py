"""Lossless repeated-value interning for deterministic Validator views.

No compression or opaque model context: shared values remain ordinary JSON.
The parent receives a separate checked, readable fact projection.
"""
from collections import Counter
from copy import deepcopy
import json

FORMAT='product-knowledge-shared-proof-v2'

def pack_proof(payload):
    # The deterministic Validator recomputes candidate checks from product
    # facts anyway. Bind the complete claimed result by digest, without
    # transmitting a second copy of every requirement/check.
    import hashlib
    payload=deepcopy(payload)
    if payload.get('contractVersion')=='product-evidence-comparison-v1':
        candidates=payload.pop('candidates')
        payload['candidateChecksSha256']=hashlib.sha256(json.dumps(candidates,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()).hexdigest()
        # Provenance/excerpt receipts stay in the persisted raw ToolTrace.
        # These display-only attribute metadata fields are not read by the
        # hard-condition verifier or parent projection.
        for product in payload['products']:
            for attribute in product.get('attributes',[]):
                for key in ('rawProjection','fullRawSha256','valueType','unit'):
                    attribute.pop(key,None)
                for key in ('normalizedBoolean','normalizedNumber'):
                    if attribute.get(key) is None:attribute.pop(key,None)
    counts=Counter()
    def identify(x):
        text=json.dumps(x,ensure_ascii=False,sort_keys=True,separators=(',',':'))
        return text if isinstance(x,(dict,list,str)) and len(text)>24 else None
    def count(x):
        key=identify(x)
        if key: counts[key]+=1
        if isinstance(x,dict):
            for v in x.values():count(v)
        elif isinstance(x,list):
            for v in x:count(v)
    count(payload);shared=[];ids={};schemas=[];schema_ids={}
    def encode(x):
        key=identify(x)
        if key and counts[key]>1:
            if key in ids:return ['$s',ids[key]]
            value=children(x);idx=len(shared);shared.append(value);ids[key]=idx
            return ['$s',idx]
        return children(x)
    def children(x):
        if isinstance(x,dict):
            if '$shared' in x or '$row' in x or '$literalList' in x:raise ValueError('reserved_proof_key')
            if len(x)>=3:
                keys=tuple(x)
                if keys not in schema_ids:
                    schema_ids[keys]=len(schemas);schemas.append(list(keys))
                return ['$r',schema_ids[keys],[encode(v) for v in x.values()]]
            return {k:encode(v) for k,v in x.items()}
        if isinstance(x,list):
            result=[encode(v) for v in x]
            return {'$literalList':result} if x and x[0] in ('$s','$r') else result
        return x
    root=encode(payload)
    return dict(proofFormat=FORMAT,root=root,shared=shared,schemas=schemas)

def unpack_proof(payload):
    if payload.get('proofFormat') not in {FORMAT,'product-knowledge-shared-proof-v1'}: return payload
    shared=payload['shared'];schemas=payload.get('schemas',[]);visits=0
    if not isinstance(shared,list) or len(shared)>10000:raise ValueError('invalid_shared_proof')
    def decode(x,limit):
        nonlocal visits
        visits+=1
        if visits>500000:raise ValueError('proof_expansion_limit')
        if isinstance(x,dict):
            if set(x)=={'$literalList'}:return [decode(v,limit) for v in x['$literalList']]
            if set(x)=={'$row'}:
                idx,values=x['$row']
                if type(idx) is not int or not 0<=idx<len(schemas) or len(values)!=len(schemas[idx]):raise ValueError('invalid_proof_row')
                return {k:decode(v,limit) for k,v in zip(schemas[idx],values)}
            if set(x)=={'$shared'}:
                idx=x['$shared']
                if type(idx) is not int or not 0<=idx<limit:raise ValueError('invalid_shared_reference')
                return decode(shared[idx],idx)
            return {k:decode(v,limit) for k,v in x.items()}
        if isinstance(x,list):
            if len(x)==2 and x[0]=='$s':return decode({'$shared':x[1]},limit)
            if len(x)==3 and x[0]=='$r':return decode({'$row':x[1:]},limit)
            return [decode(v,limit) for v in x]
        return x
    value=decode(payload['root'],len(shared))
    if value.get('contractVersion')=='product-evidence-comparison-v1':
        import hashlib
        from .comparison import check_candidates
        candidates=check_candidates(value['products'],value['category'],value['requirements'])
        digest=hashlib.sha256(json.dumps(candidates,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()).hexdigest()
        if value.pop('candidateChecksSha256',None)!=digest:raise ValueError('candidate_check_digest_mismatch')
        value['candidates']=candidates
    return value

def answer_projection(payload):
    """Only called after recomputing hard conditions and checking MCP provenance."""
    value=deepcopy(payload)
    from ..domains.ecommerce.models import synthetic_price_value
    value['products']=[]
    for product in payload['products']:
        row={k:product[k] for k in ('id','title','brand','priceStatus','snapshotPriceMinor') if k in product}
        amount, receipt=synthetic_price_value(product,allow_budget=True)
        if amount is not None and receipt is not None:
            row.update(syntheticReferencePriceMinor=amount,priceDisclosure=receipt['disclosureZh'])
        value['products'].append(row)
    value['candidates']=[]
    for c in payload['candidates']:
        decision=c['decision']
        value['candidates'].append(dict(productId=c['productId'],decision=dict(products=[
            {k:r[k] for k in ('fullyMatched','hardFailures','hardUnknowns','checks') if k in r} for r in decision['products']],
            eliminated=[{k:r[k] for k in ('productId','reason','checks') if k in r} for r in decision.get('eliminated',[])])))
    for candidate in value['candidates']:
        for row in [*candidate['decision']['products'],*candidate['decision']['eliminated']]:
            row['checks']=[{k:check[k] for k in ('key','actual','status') if k in check}
                           for check in row.get('checks',[])]
    knowledge=value['knowledge']
    knowledge['bindings']=[{k:b[k] for k in ('itemId','status','modelKeys','region','variantStatus') if k in b} for b in knowledge.get('bindings',[])]
    for e in knowledge.get('evidence',[]):
        e['source']={k:e['source'][k] for k in ('url','section','checkedOn','region','authority','softwareVersion','testConditions','limits') if k in e['source']}
        for k in ('value','unit','label','applicability','listingIdentityVerified'):e.pop(k,None)
    return value
